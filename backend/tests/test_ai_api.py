"""AI provider endpoints.

The property under test throughout: **a provider secret goes in and never comes
back out.** There is no endpoint that reveals a stored key, and none of the
responses, audit rows or error messages carry one.
"""

from __future__ import annotations

import httpx
import pytest
import pytest_asyncio

from app.config import settings
from app.db import get_session
from app.main import app
from app.models import AuditEvent, LLMCredential, ProjectMembership, ProjectRole, User
from app.security.auth import create_session_token

API = settings.api_v1
SECRET_KEY = "sk-ant-SUPERSECRETVALUE0001"


@pytest_asyncio.fixture
async def client(db):
    async def _override():
        yield db

    app.dependency_overrides[get_session] = _override
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


def _headers(user_id: str, project_id: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {create_session_token(user_id, project_id)}",
        "X-Project-Id": project_id,
    }


@pytest.fixture
def auth(user, project):
    return _headers(user.id, project.id)


async def _create(client, auth, **overrides) -> dict:
    payload = {
        "name": "primary",
        "provider": "ANTHROPIC",
        "api_key": SECRET_KEY,
        "is_default": True,
    }
    payload.update(overrides)
    resp = await client.post(f"{API}/ai/credentials", json=payload, headers=auth)
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------
async def test_the_provider_list_distinguishes_built_from_declared(client, auth):
    body = (await client.get(f"{API}/ai/providers", headers=auth)).json()
    by_kind = {p["kind"]: p for p in body["providers"]}
    assert by_kind["ANTHROPIC"]["implemented"] is True
    assert by_kind["BEDROCK"]["implemented"] is False
    assert by_kind["BEDROCK"]["notes"]


async def test_the_provider_list_says_whether_byok_is_required(client, auth, monkeypatch):
    monkeypatch.setattr(settings, "feature_platform_managed_ai", False)
    body = (await client.get(f"{API}/ai/providers", headers=auth)).json()
    assert body["byok_required"] is True
    # With no shared key on offer, none is advertised.
    assert body["deployment_providers"] == []


# ---------------------------------------------------------------------------
# The secret never comes back
# ---------------------------------------------------------------------------
async def test_creating_a_credential_never_echoes_the_key(client, auth):
    created = await _create(client, auth)
    assert SECRET_KEY not in str(created)
    assert created["has_key"] is True
    assert created["key_fingerprint"]


async def test_no_endpoint_returns_a_stored_key(client, auth):
    await _create(client, auth)
    listed = await client.get(f"{API}/ai/credentials", headers=auth)
    assert SECRET_KEY not in listed.text

    routing = await client.get(f"{API}/ai/routing", headers=auth)
    assert SECRET_KEY not in routing.text


async def test_the_audit_record_holds_the_provider_not_the_key(client, auth, db):
    from sqlalchemy import select

    await _create(client, auth)
    rows = (
        await db.execute(
            select(AuditEvent).where(AuditEvent.action == "ai.credential_added")
        )
    ).scalars().all()
    assert rows
    serialized = str(rows[0].arguments)
    assert SECRET_KEY not in serialized
    assert "ANTHROPIC" in serialized
    # The last four characters are enough to identify which key was entered.
    assert SECRET_KEY[-4:] in serialized


async def test_the_stored_reference_is_not_the_key(client, auth, db):
    from sqlalchemy import select

    await _create(client, auth)
    credential = (await db.execute(select(LLMCredential))).scalars().one()
    assert SECRET_KEY not in (credential.secret_ref or "")


# ---------------------------------------------------------------------------
# Validation and authority
# ---------------------------------------------------------------------------
async def test_only_a_project_admin_can_add_a_credential(client, db, project):
    developer = User(email="dev-ai@example.com")
    db.add(developer)
    await db.flush()
    db.add(
        ProjectMembership(
            company_id=project.company_id,
            project_id=project.id,
            user_id=developer.id,
            role=ProjectRole.DEVELOPER,
        )
    )
    await db.commit()
    resp = await client.post(
        f"{API}/ai/credentials",
        json={"name": "sneaky", "provider": "ANTHROPIC", "api_key": SECRET_KEY},
        headers=_headers(developer.id, project.id),
    )
    assert resp.status_code == 403


async def test_an_unimplemented_provider_is_refused_with_its_reason(client, auth):
    resp = await client.post(
        f"{API}/ai/credentials",
        json={"name": "bedrock", "provider": "BEDROCK", "api_key": "x"},
        headers=auth,
    )
    assert resp.status_code == 400
    assert "not implemented" in resp.json()["detail"].lower()


async def test_a_provider_needing_an_endpoint_says_so_before_storing_anything(
    client, auth, db
):
    from sqlalchemy import select

    resp = await client.post(
        f"{API}/ai/credentials",
        json={"name": "azure", "provider": "AZURE_OPENAI", "api_key": SECRET_KEY},
        headers=auth,
    )
    assert resp.status_code == 400
    assert (await db.execute(select(LLMCredential))).scalars().all() == []


async def test_policy_can_refuse_a_provider_at_the_point_of_configuration(
    client, auth, db, project
):
    """Better to refuse the key than to store one that can never be used."""
    from app.tenancy.service import policy_row

    row = await policy_row(db, project.id, project.company_id)
    row.allowed_llm_providers = ["ANTHROPIC"]
    await db.commit()

    resp = await client.post(
        f"{API}/ai/credentials",
        json={"name": "openai", "provider": "OPENAI", "api_key": SECRET_KEY},
        headers=auth,
    )
    assert resp.status_code == 403
    assert "policy does not permit" in resp.json()["detail"]


async def test_a_duplicate_name_is_refused(client, auth):
    await _create(client, auth)
    resp = await client.post(
        f"{API}/ai/credentials",
        json={"name": "primary", "provider": "OPENAI", "api_key": SECRET_KEY},
        headers=auth,
    )
    assert resp.status_code == 409


async def test_an_unknown_tier_name_is_dropped_rather_than_silently_stored(client, auth):
    created = await _create(
        client, auth, tier_models={"FAST": "claude-haiku-4-5", "TURBO": "nonsense"}
    )
    assert created["tier_models"] == {"FAST": "claude-haiku-4-5"}


# ---------------------------------------------------------------------------
# Rotation and defaults
# ---------------------------------------------------------------------------
async def test_rotating_a_key_changes_the_reference_and_clears_the_test_result(
    client, auth, db
):
    from sqlalchemy import select

    created = await _create(client, auth)
    before = (await db.execute(select(LLMCredential))).scalars().one().secret_ref

    updated = await client.patch(
        f"{API}/ai/credentials/{created['id']}",
        json={"api_key": "sk-ant-ROTATEDVALUE0002"},
        headers=auth,
    )
    assert updated.status_code == 200
    after = updated.json()
    assert after["key_fingerprint"] != created["key_fingerprint"]
    # A rotated key has not been tested yet; saying otherwise would be a lie.
    assert after["last_test_ok"] is None

    await db.refresh((await db.execute(select(LLMCredential))).scalars().one())
    assert (await db.execute(select(LLMCredential))).scalars().one().secret_ref != before


async def test_only_one_credential_can_be_the_default(client, auth):
    first = await _create(client, auth)
    second = await _create(
        client, auth, name="secondary", provider="OPENAI", is_default=True
    )
    listed = (await client.get(f"{API}/ai/credentials", headers=auth)).json()
    defaults = [c["id"] for c in listed["credentials"] if c["is_default"]]
    assert defaults == [second["id"]]
    assert first["id"] not in defaults


async def test_deleting_a_credential_removes_it(client, auth):
    created = await _create(client, auth)
    assert (
        await client.delete(f"{API}/ai/credentials/{created['id']}", headers=auth)
    ).status_code == 200
    listed = (await client.get(f"{API}/ai/credentials", headers=auth)).json()
    assert listed["count"] == 0


async def test_a_credential_from_another_project_reads_as_not_found(
    client, auth, db, user
):
    from app.models import Company, Project
    from app.security.secrets import SecretContext, store_secret

    other_company = Company(name="Rival AI", slug="rival-ai")
    db.add(other_company)
    await db.flush()
    other_project = Project(
        company_id=other_company.id, name="Theirs", slug="theirs"
    )
    db.add(other_project)
    await db.flush()
    theirs = LLMCredential(
        company_id=other_company.id,
        project_id=other_project.id,
        name="theirs",
        provider="ANTHROPIC",
        secret_ref=store_secret(
            "sk-theirs", SecretContext(other_company.id, other_project.id, "llm_key")
        ),
    )
    db.add(theirs)
    await db.commit()

    resp = await client.patch(
        f"{API}/ai/credentials/{theirs.id}", json={"name": "mine now"}, headers=auth
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Routing and usage
# ---------------------------------------------------------------------------
async def test_routing_answers_where_the_data_would_actually_go(client, auth):
    await _create(client, auth)
    body = (await client.get(f"{API}/ai/routing", headers=auth)).json()
    assert body["available"] is True
    assert body["primary"]["provider"] == "ANTHROPIC"
    assert body["primary"]["byok"] is True
    assert body["fallback_enabled"] is False


async def test_routing_with_nothing_configured_explains_what_to_do(client, auth):
    body = (await client.get(f"{API}/ai/routing", headers=auth)).json()
    assert body["available"] is False
    assert "api key" in body["message"].lower()


async def test_usage_starts_empty_and_states_its_pricing_basis(client, auth):
    body = (await client.get(f"{API}/ai/usage", headers=auth)).json()
    assert body["totals"]["requests"] == 0
    assert body["pricing"]["as_of"]


async def test_pricing_does_not_present_estimates_as_invoices(client, auth):
    body = (await client.get(f"{API}/ai/pricing", headers=auth)).json()
    assert "not invoices" in body["note"]


# ---------------------------------------------------------------------------
# Test connection
# ---------------------------------------------------------------------------
async def test_test_connection_reports_a_real_failure_rather_than_optimism(
    client, auth, db, monkeypatch
):
    """The endpoint makes a live call. With an obviously invalid key and no
    network in the test environment, it must report failure — never success."""
    created = await _create(client, auth)
    resp = await client.post(
        f"{API}/ai/credentials/{created['id']}/test", headers=auth
    )
    assert resp.status_code == 200
    assert resp.json()["success"] is False

    from sqlalchemy import select

    credential = (await db.execute(select(LLMCredential))).scalars().one()
    assert credential.last_test_ok is False
    assert credential.last_tested_at is not None
