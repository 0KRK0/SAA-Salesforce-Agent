"""The secret store, tested as a security boundary.

A credential in this product is never a column value — it is a *reference*
issued by the secret store and bound to the company and project that owns it.
These tests defend the two properties that makes worth having:

  * a reference lifted out of one tenant's row cannot be resolved as another's;
  * a backend that is not implemented fails loudly at construction rather than
    silently writing secrets somewhere weaker than the operator chose.
"""

from __future__ import annotations

import pytest

from app.security import secrets as sec


@pytest.fixture(autouse=True)
def _fresh_store():
    sec.reset_store()
    yield
    sec.reset_store()


def _ctx(company: str = "co_1", project: str = "prj_1") -> sec.SecretContext:
    return sec.SecretContext(
        company_id=company, project_id=project, purpose="salesforce_token"
    )


# ---------------------------------------------------------------- round trip
def test_a_stored_secret_round_trips_through_its_reference():
    reference = sec.store_secret("super-secret-token", _ctx())
    assert sec.resolve_secret(reference, _ctx()) == "super-secret-token"


def test_the_reference_does_not_contain_the_secret():
    """The reference lands in a database column and in log lines. If the plain
    secret survived inside it, everything downstream would be a leak."""
    reference = sec.store_secret("super-secret-token", _ctx())
    assert "super-secret-token" not in reference


def test_two_stores_of_the_same_value_produce_different_references():
    """Deterministic references would let anyone confirm a guessed secret by
    storing it and comparing."""
    a = sec.store_secret("same-value", _ctx())
    b = sec.store_secret("same-value", _ctx())
    assert a != b


# ------------------------------------------------------------ tenant binding
def test_a_reference_cannot_be_resolved_from_another_project():
    """The whole point of binding: pasting a rival's token reference into your
    own row must not hand you their credential."""
    reference = sec.store_secret("rival-token", _ctx(project="prj_rival"))
    with pytest.raises(sec.SecretError):
        sec.resolve_secret(reference, _ctx(project="prj_mine"))


def test_a_reference_cannot_be_resolved_from_another_company():
    reference = sec.store_secret("rival-token", _ctx(company="co_rival"))
    with pytest.raises(sec.SecretError):
        sec.resolve_secret(reference, _ctx(company="co_mine"))


def test_a_reference_cannot_be_reused_for_a_different_purpose():
    """A Salesforce token reference must not resolve as an LLM API key: it is
    how one compromised subsystem would reach into another."""
    reference = sec.store_secret("token", sec.SecretContext("co_1", "prj_1", "salesforce_token"))
    with pytest.raises(sec.SecretError):
        sec.resolve_secret(reference, sec.SecretContext("co_1", "prj_1", "llm_key"))


def test_an_empty_reference_is_an_error_not_an_empty_secret():
    """Returning "" would let a NULL column silently become a valid credential."""
    with pytest.raises(sec.SecretError):
        sec.resolve_secret("", _ctx())


def test_an_unrecognized_reference_format_is_refused():
    with pytest.raises(sec.SecretError):
        sec.resolve_secret("not-a-reference", _ctx())


# ------------------------------------------------------- unimplemented backends
@pytest.mark.parametrize(
    "wrapper",
    [sec.VaultKeyWrapper, sec.AzureKeyVaultKeyWrapper, sec.GoogleKmsKeyWrapper],
)
def test_unimplemented_backends_fail_at_construction(wrapper):
    """An operator who selects Vault must get an error, not silent Fernet.

    Failing at startup is the difference between "we do not support that yet"
    and "we quietly stored your production credentials with a weaker key than
    you asked for".
    """
    with pytest.raises(sec.KeyWrapperNotImplemented):
        wrapper("some-key-id")


def test_describe_backend_reports_what_is_actually_in_use():
    described = sec.describe_backend()
    assert described["backend"]
    assert "implemented" in described


# ------------------------------------------------------------------ masking
def test_masking_never_reveals_enough_to_reconstruct():
    masked = sec.masked("sk-live-abcdefghijklmnop")
    assert "abcdefghijkl" not in masked
    assert masked.endswith("mnop")


def test_masking_a_short_value_reveals_nothing():
    """A four-character secret must not be shown in full by the 'keep last 4'
    rule."""
    assert "abc" not in sec.masked("abc")


def test_fingerprints_are_stable_and_non_reversible():
    a = sec.fingerprint("token-value")
    b = sec.fingerprint("token-value")
    assert a == b
    assert "token-value" not in a
    assert a != sec.fingerprint("other-value")
