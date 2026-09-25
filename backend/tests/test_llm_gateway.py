"""The model gateway: whose key runs the work, and where the data goes.

These are commercial and compliance properties as much as technical ones. A
mistake here means a customer's data goes to a vendor they did not choose, or
the operator's key silently pays for a customer's run.
"""

from __future__ import annotations

import httpx
import pytest
import pytest_asyncio

from app.config import settings
from app.llm import gateway
from app.llm.base import LLMError, ProviderConfig
from app.llm.gateway import NoModelAvailable
from app.llm.pricing import estimate
from app.models import LLMCredential, LLMProviderKind, LLMUsage, ProjectPolicy
from app.security.secrets import SecretContext, store_secret
from app.tenancy.policy import snapshot_from

MESSAGES = [{"role": "user", "content": [{"type": "text", "text": "hello"}]}]


def _ok_response(model: str = "claude-sonnet-4-5") -> dict:
    return {
        "model": model,
        "stop_reason": "end_turn",
        "content": [{"type": "text", "text": "ok"}],
        "usage": {"input_tokens": 1000, "output_tokens": 500},
    }


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def _credential(
    db,
    company,
    project,
    *,
    provider: LLMProviderKind = LLMProviderKind.ANTHROPIC,
    name: str = "primary",
    key: str = "sk-project-key",
    is_default: bool = True,
    base_url: str | None = None,
    tier_models: dict | None = None,
) -> LLMCredential:
    row = LLMCredential(
        company_id=company.id,
        project_id=project.id,
        name=name,
        provider=provider,
        secret_ref=store_secret(
            key, SecretContext(company.id, project.id, "llm_key")
        ),
        base_url=base_url,
        tier_models=tier_models,
        is_default=is_default,
    )
    db.add(row)
    await db.commit()
    return row


@pytest_asyncio.fixture(autouse=True)
async def _no_platform_key(monkeypatch):
    """Default posture: the deployment does not lend its key to projects."""
    monkeypatch.setattr(settings, "feature_platform_managed_ai", False)
    yield


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------
async def test_a_project_with_no_credential_gets_a_specific_answer(db, project):
    """"No model available" must say what to do about it, not just fail."""
    with pytest.raises(NoModelAvailable) as exc:
        await gateway.resolve_routes(db, project_id=project.id)
    assert "add your own api key" in str(exc.value).lower()


async def test_the_projects_own_credential_is_used(db, company, project):
    await _credential(db, company, project)
    routes = await gateway.resolve_routes(db, project_id=project.id)
    assert routes[0].provider_kind == "ANTHROPIC"
    assert routes[0].byok is True


async def test_the_deployment_key_is_not_lent_out_unless_the_operator_opts_in(
    db, project, monkeypatch
):
    """Spending the operator's key on a customer's work — and routing their data
    through the operator's vendor account — must be a decision, not a default."""
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-deployment")
    monkeypatch.setattr(settings, "feature_platform_managed_ai", False)
    with pytest.raises(NoModelAvailable):
        await gateway.resolve_routes(db, project_id=project.id)

    monkeypatch.setattr(settings, "feature_platform_managed_ai", True)
    routes = await gateway.resolve_routes(db, project_id=project.id)
    assert routes[0].byok is False


async def test_byok_outranks_the_deployment_key(db, company, project, monkeypatch):
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-deployment")
    monkeypatch.setattr(settings, "feature_platform_managed_ai", True)
    await _credential(db, company, project)
    routes = await gateway.resolve_routes(db, project_id=project.id)
    assert routes[0].byok is True


async def test_policy_can_forbid_a_provider_the_project_has_a_key_for(
    db, company, project
):
    """A project that has committed to one vendor must not reach another, even
    with a working credential for it."""
    await _credential(db, company, project, provider=LLMProviderKind.OPENAI)
    policy = snapshot_from(
        ProjectPolicy(project_id=project.id, allowed_llm_providers=["ANTHROPIC"]),
        project.id,
    )
    with pytest.raises(NoModelAvailable) as exc:
        await gateway.resolve_routes(db, project_id=project.id, policy=policy)
    assert "policy does not permit" in str(exc.value)


async def test_an_unreadable_credential_does_not_take_down_the_others(
    db, company, project
):
    """One corrupt reference must not make a project modelless."""
    broken = await _credential(db, company, project, name="broken")
    broken.secret_ref = "local:v1:not-a-real-reference"
    working = await _credential(
        db, company, project, name="working", is_default=False
    )
    await db.commit()

    routes = await gateway.resolve_routes(db, project_id=project.id)
    assert [r.credential_id for r in routes] == [working.id]


async def test_an_unimplemented_provider_is_skipped_rather_than_crashing(
    db, company, project
):
    await _credential(db, company, project, provider=LLMProviderKind.BEDROCK)
    with pytest.raises(NoModelAvailable):
        await gateway.resolve_routes(db, project_id=project.id)


# ---------------------------------------------------------------------------
# Model selection
# ---------------------------------------------------------------------------
async def test_a_credentials_own_tier_model_wins(db, company, project):
    await _credential(
        db, company, project, tier_models={"FAST": "claude-haiku-4-5", "ADVANCED": "x"}
    )
    fast = await gateway.resolve_routes(db, project_id=project.id, tier="FAST")
    assert fast[0].model == "claude-haiku-4-5"


async def test_an_unchosen_tier_falls_back_to_the_catalog_default(db, company, project):
    await _credential(db, company, project, tier_models={"FAST": "claude-haiku-4-5"})
    advanced = await gateway.resolve_routes(db, project_id=project.id, tier="ADVANCED")
    assert advanced[0].model
    assert advanced[0].model != "claude-haiku-4-5"


# ---------------------------------------------------------------------------
# Calling and fallback
# ---------------------------------------------------------------------------
async def test_a_successful_call_records_usage_without_recording_content(
    db, company, project
):
    from sqlalchemy import select

    await _credential(db, company, project)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_ok_response())

    async with _client(handler) as client:
        response, route = await gateway.complete(
            db,
            company_id=company.id,
            project_id=project.id,
            system="rules",
            messages=MESSAGES,
            tools=[],
            client=client,
        )
    await db.commit()

    assert response.text == "ok"
    usage = (await db.execute(select(LLMUsage))).scalars().one()
    assert usage.input_tokens == 1000
    assert usage.byok is True
    assert usage.estimated_cost_usd > 0
    # Nothing on the usage row can hold a prompt or a completion.
    columns = {c.name for c in LLMUsage.__table__.columns}
    assert not columns & {"prompt", "messages", "content", "completion", "text"}


async def test_fallback_is_off_by_default(db, company, project):
    """Moving a customer's data to a second vendor is a decision they make."""
    await _credential(db, company, project, name="a")
    await _credential(
        db, company, project, name="b", provider=LLMProviderKind.OPENAI,
        is_default=False,
    )
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        return httpx.Response(503, json={"error": {"message": "overloaded"}})

    policy = snapshot_from(ProjectPolicy(project_id=project.id), project.id)
    async with _client(handler) as client:
        with pytest.raises(LLMError):
            await gateway.complete(
                db,
                company_id=company.id,
                project_id=project.id,
                system="",
                messages=MESSAGES,
                tools=[],
                policy=policy,
                client=client,
            )
    assert set(calls) == {"api.anthropic.com"}


async def test_fallback_moves_to_the_next_provider_when_enabled(db, company, project):
    await _credential(db, company, project, name="a")
    await _credential(
        db, company, project, name="b", provider=LLMProviderKind.OPENAI,
        is_default=False,
    )
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host)
        if request.url.host == "api.anthropic.com":
            return httpx.Response(503, json={"error": {"message": "overloaded"}})
        return httpx.Response(
            200,
            json={
                "model": "gpt-4o",
                "choices": [{"finish_reason": "stop", "message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2},
            },
        )

    policy = snapshot_from(
        ProjectPolicy(project_id=project.id, allow_llm_fallback=True), project.id
    )
    async with _client(handler) as client:
        response, route = await gateway.complete(
            db,
            company_id=company.id,
            project_id=project.id,
            system="",
            messages=MESSAGES,
            tools=[],
            policy=policy,
            client=client,
        )
    assert route.provider_kind == "OPENAI"
    assert "api.openai.com" in seen


async def test_a_rejected_credential_never_falls_over_to_another_vendor(
    db, company, project
):
    """A 401 is not a capacity problem. Sending the same customer data to a
    different vendor because the first said "unauthorized" is a residency
    decision no retry policy should make."""
    await _credential(db, company, project, name="a")
    await _credential(
        db, company, project, name="b", provider=LLMProviderKind.OPENAI,
        is_default=False,
    )
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host)
        return httpx.Response(401, json={"error": {"message": "bad key"}})

    policy = snapshot_from(
        ProjectPolicy(project_id=project.id, allow_llm_fallback=True), project.id
    )
    async with _client(handler) as client:
        with pytest.raises(LLMError):
            await gateway.complete(
                db,
                company_id=company.id,
                project_id=project.id,
                system="",
                messages=MESSAGES,
                tools=[],
                policy=policy,
                client=client,
            )
    assert set(seen) == {"api.anthropic.com"}


# ---------------------------------------------------------------------------
# Test connection
# ---------------------------------------------------------------------------
async def test_test_connection_makes_a_real_call(db, company, project):
    """A shape-only check would report Connected for a revoked key."""
    credential = await _credential(db, company, project)
    called: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        called.append(str(request.url))
        return httpx.Response(200, json=_ok_response())

    async with _client(handler) as client:
        result = await gateway.test_credential(db, credential, client=client)
    assert result["success"] is True
    assert called


async def test_test_connection_reports_a_bad_key_as_a_failure(db, company, project):
    credential = await _credential(db, company, project)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "invalid key"}})

    async with _client(handler) as client:
        result = await gateway.test_credential(db, credential, client=client)
    assert result["success"] is False
    assert result["error_type"] == "LLM_UNAUTHORIZED"


async def test_test_connection_on_an_unreadable_reference_says_so(db, company, project):
    credential = await _credential(db, company, project)
    credential.secret_ref = "local:v1:garbage"
    await db.commit()
    result = await gateway.test_credential(db, credential)
    assert result["success"] is False
    assert result["error_type"] == "SECRET_STORE_UNAVAILABLE"


# ---------------------------------------------------------------------------
# Cost estimation honesty
# ---------------------------------------------------------------------------
def test_a_known_model_is_priced_from_the_list_table():
    priced = estimate("ANTHROPIC", "claude-sonnet-4-5-20260101", 1_000_000, 0)
    assert priced["priced"] is True
    assert priced["cost_usd"] == pytest.approx(3.0)


def test_an_unknown_model_reports_no_cost_rather_than_a_guess():
    """Someone will budget against this number. A blank prompts a real rate;
    an invented figure does not."""
    priced = estimate("OPENAI", "some-model-shipped-yesterday", 1_000_000, 1_000_000)
    assert priced["priced"] is False
    assert priced["cost_usd"] == 0.0
    assert "not estimated rather than guessed" in priced["basis"]


def test_self_hosted_inference_is_priced_at_zero_and_says_why():
    priced = estimate("OLLAMA", "llama3", 1_000_000, 1_000_000)
    assert priced["priced"] is True
    assert priced["cost_usd"] == 0.0
    assert "self-hosted" in priced["basis"]


def test_cached_input_is_billed_at_the_cached_rate():
    full = estimate("ANTHROPIC", "claude-sonnet-4-5", 1_000_000, 0)
    cached = estimate("ANTHROPIC", "claude-sonnet-4-5", 1_000_000, 0, 1_000_000)
    assert cached["cost_usd"] < full["cost_usd"]


def test_the_price_table_states_when_it_was_checked():
    from app.llm.pricing import describe

    described = describe()
    assert described["as_of"]
    assert "not invoices" in described["note"]


# ---------------------------------------------------------------------------
# Config surface
# ---------------------------------------------------------------------------
def test_a_provider_config_can_be_logged_without_the_key():
    config = ProviderConfig(provider="OPENAI", api_key="sk-secret", base_url="https://x")
    described = config.redacted()
    assert described["has_api_key"] is True
    assert "sk-secret" not in str(described)
