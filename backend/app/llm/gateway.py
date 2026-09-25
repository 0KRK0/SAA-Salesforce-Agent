"""The model gateway: which model runs this project's work, and on whose key.

Everything about *choosing* a model lives here, so the agent runtime never
knows what vendor it is talking to. The order of resolution is deliberate and
is the commercial contract of the product:

  1. **The project's own credential (BYOK).** A customer's key, stored as a
     secret-store reference bound to their company and project. Their data goes
     to their vendor account under their own terms.
  2. **The deployment's key**, and only if `FEATURE_PLATFORM_MANAGED_AI` is on.
     Off by default: silently spending the operator's key on a customer's work
     is a billing surprise, and silently sending a customer's data through the
     operator's vendor account is a compliance one.

Two guard rails sit above both:

  * `allowed_llm_providers` in project policy. If a project says its data may
    only go to Azure OpenAI, nothing else is reachable — not even as a fallback.
  * `allow_llm_fallback`. Off by default. A fallback silently moves customer
    data to a different vendor, so it happens only when someone chose it.

Usage is recorded per run: counts, model, provider and an estimated cost.
Never prompts, never completions, never tool arguments.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.llm.base import (
    LLMError,
    LLMProvider,
    LLMResponse,
    ProviderConfig,
)
from app.llm.catalog import build_provider, default_model, spec_for
from app.llm.pricing import estimate
from app.models import LLMCredential, LLMProviderKind, LLMUsage, ModelTier
from app.observability.logging import get_logger
from app.security.secrets import SecretContext, SecretError, resolve_secret
from app.tenancy.policy import PolicySnapshot

log = get_logger("llm.gateway")

#: Which settings key holds the deployment-managed key for each provider.
_DEPLOYMENT_KEYS: dict[str, str] = {
    "ANTHROPIC": "anthropic_api_key",
    "OPENAI": "openai_api_key",
    "AZURE_OPENAI": "azure_openai_api_key",
    "GOOGLE": "google_api_key",
    "MISTRAL": "mistral_api_key",
    "GROQ": "groq_api_key",
    "DEEPSEEK": "deepseek_api_key",
    "TOGETHER": "together_api_key",
}


class NoModelAvailable(LLMError):
    """Nothing this project is permitted to use is configured.

    Deliberately its own type: it is a configuration and policy answer, not a
    vendor failure, and the message says which of the two it was.
    """

    def __init__(self, message: str):
        super().__init__(
            message, retryable=False, error_type="NO_MODEL_AVAILABLE"
        )


@dataclass
class Route:
    """A resolved decision about which model to call, and on whose key."""

    provider: LLMProvider
    provider_kind: str
    model: str
    tier: str
    #: True when this is the customer's own key rather than the deployment's.
    byok: bool
    credential_id: str | None
    max_tokens: int
    temperature: float | None

    def describe(self) -> dict[str, Any]:
        """Safe to log, safe to show. No credential, ever."""
        return {
            "provider": self.provider_kind,
            "model": self.model,
            "tier": self.tier,
            "byok": self.byok,
            "credential_id": self.credential_id,
        }


# ---------------------------------------------------------------------------
# Credential resolution
# ---------------------------------------------------------------------------
async def credentials_for(
    db: AsyncSession, project_id: str, *, provider: str | None = None
) -> list[LLMCredential]:
    stmt = select(LLMCredential).where(
        LLMCredential.project_id == project_id,
        LLMCredential.is_active.is_(True),
    )
    if provider:
        stmt = stmt.where(LLMCredential.provider == LLMProviderKind(provider.upper()))
    rows = (
        await db.execute(stmt.order_by(LLMCredential.is_default.desc(), LLMCredential.created_at))
    ).scalars().all()
    return list(rows)


def config_from_credential(credential: LLMCredential) -> ProviderConfig:
    """Turn a stored credential into a live config.

    This is the only place a project's key becomes plaintext, and it does so on
    an object that is never serialized. `SecretError` is deliberately not
    swallowed: a credential that cannot be read must fail loudly, because the
    alternative is silently falling through to somebody else's key.
    """
    api_key = ""
    if credential.secret_ref:
        api_key = resolve_secret(
            credential.secret_ref,
            SecretContext(
                company_id=credential.company_id,
                project_id=credential.project_id,
                purpose="llm_key",
            ),
        )
    return ProviderConfig(
        provider=credential.provider.value,
        api_key=api_key,
        base_url=credential.base_url or "",
        region=credential.region or "",
        extra=dict(credential.config or {}),
        timeout_seconds=settings.llm_timeout_seconds,
        max_retries=settings.llm_max_retries,
    )


def deployment_config(provider: str) -> ProviderConfig | None:
    """The deployment's own key for a provider, if it has one."""
    key_attr = _DEPLOYMENT_KEYS.get(provider.upper())
    api_key = str(getattr(settings, key_attr, "") or "") if key_attr else ""
    spec = spec_for(provider)
    base_url = ""
    extra: dict[str, Any] = {}
    if provider.upper() == "OPENAI":
        base_url = settings.openai_base_url
    elif provider.upper() == "AZURE_OPENAI":
        base_url = settings.azure_openai_endpoint
        extra["api_version"] = settings.azure_openai_api_version
    elif provider.upper() == "OLLAMA":
        base_url = settings.ollama_base_url

    if spec.requires_api_key and not api_key:
        return None
    if spec.requires_base_url and not base_url:
        return None
    return ProviderConfig(
        provider=provider.upper(),
        api_key=api_key,
        base_url=base_url,
        extra=extra,
        timeout_seconds=settings.llm_timeout_seconds,
        max_retries=settings.llm_max_retries,
    )


def model_for(credential: LLMCredential | None, provider: str, tier: str) -> str:
    """Which model name to send, in order of who chose it."""
    tier = (tier or ModelTier.BALANCED.value).upper()
    if credential is not None:
        chosen = (credential.tier_models or {}).get(tier)
        if chosen:
            return str(chosen)
    configured = {
        "FAST": settings.llm_fast_model,
        "BALANCED": settings.llm_balanced_model,
        "ADVANCED": settings.llm_advanced_model,
    }.get(tier, "")
    if configured:
        return configured
    if provider.upper() == "ANTHROPIC" and settings.claude_model:
        # Back-compatible: existing deployments configure CLAUDE_MODEL.
        return settings.claude_model
    return default_model(provider, tier)


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------
async def resolve_routes(
    db: AsyncSession,
    *,
    project_id: str,
    policy: PolicySnapshot | None = None,
    tier: str = ModelTier.BALANCED.value,
    require_tools: bool = True,
) -> list[Route]:
    """Every route this project may use, best first.

    The list is ordered, not chosen from at random: index 0 is what runs, and
    anything after it is a fallback that only takes effect if the project has
    enabled fallback.
    """
    tier = (tier or ModelTier.BALANCED.value).upper()
    routes: list[Route] = []
    blocked_by_policy: list[str] = []

    def permitted(kind: str) -> bool:
        if policy is not None and not policy.provider_allowed(kind.upper()):
            blocked_by_policy.append(kind.upper())
            return False
        return True

    # 1. The project's own credentials.
    for credential in await credentials_for(db, project_id):
        kind = credential.provider.value
        if not permitted(kind):
            continue
        spec = spec_for(kind)
        if not spec.implemented or (require_tools and not spec.supports_tools):
            continue
        try:
            provider = build_provider(kind, config_from_credential(credential))
        except (LLMError, SecretError) as exc:
            # One unreadable credential must not take down the others.
            log.warning(
                "llm.credential_unusable",
                project_id=project_id,
                credential_id=credential.id,
                provider=kind,
                error=type(exc).__name__,
            )
            continue
        routes.append(
            Route(
                provider=provider,
                provider_kind=kind,
                model=model_for(credential, kind, tier),
                tier=tier,
                byok=True,
                credential_id=credential.id,
                max_tokens=settings.llm_max_tokens,
                temperature=settings.llm_temperature,
            )
        )

    # 2. The deployment's own key — only when the operator has opted in.
    if settings.feature_platform_managed_ai:
        for kind in _deployment_provider_order():
            if not permitted(kind):
                continue
            spec = spec_for(kind)
            if not spec.implemented:
                continue
            config = deployment_config(kind)
            if config is None:
                continue
            try:
                provider = build_provider(kind, config)
            except LLMError:
                continue
            routes.append(
                Route(
                    provider=provider,
                    provider_kind=kind,
                    model=model_for(None, kind, tier),
                    tier=tier,
                    byok=False,
                    credential_id=None,
                    max_tokens=settings.llm_max_tokens,
                    temperature=settings.llm_temperature,
                )
            )

    if not routes:
        raise NoModelAvailable(_no_model_message(blocked_by_policy))
    return routes


def _deployment_provider_order() -> list[str]:
    """The configured default first, then the rest, so intent wins."""
    default = (settings.default_llm_provider or "ANTHROPIC").upper()
    rest = [k for k in _DEPLOYMENT_KEYS if k != default]
    return [default, *rest]


def _no_model_message(blocked: list[str]) -> str:
    if blocked:
        return (
            "No AI provider is available for this project. "
            f"{', '.join(sorted(set(blocked)))} is configured but this project's "
            "policy does not permit it. A project admin can widen the allowed "
            "providers, or add a credential for one that is permitted."
        )
    if not settings.feature_platform_managed_ai:
        return (
            "No AI provider is configured for this project. Add your own API key "
            "under AI providers. This deployment does not provide a shared key."
        )
    return (
        "No AI provider is configured. Add a project credential under AI "
        "providers, or set a provider key in the deployment configuration."
    )


# ---------------------------------------------------------------------------
# Calling
# ---------------------------------------------------------------------------
async def complete(
    db: AsyncSession,
    *,
    company_id: str,
    project_id: str,
    system: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    policy: PolicySnapshot | None = None,
    tier: str = ModelTier.BALANCED.value,
    agent_run_id: str | None = None,
    tool_choice: dict[str, Any] | None = None,
    max_tokens: int | None = None,
    client: httpx.AsyncClient | None = None,
    record_usage: bool = True,
) -> tuple[LLMResponse, Route]:
    """Run one model turn through the best available route.

    Fallback to a second provider happens only when the project has enabled it
    *and* the first failure was retryable. A credential rejection is never
    failed over: sending the same customer data to a second vendor because the
    first said "unauthorized" is a data-residency decision, not a retry.
    """
    routes = await resolve_routes(
        db, project_id=project_id, policy=policy, tier=tier
    )
    allow_fallback = bool(policy.allow_llm_fallback) if policy else False
    attempts = routes if allow_fallback else routes[:1]

    last: LLMError | None = None
    for index, route in enumerate(attempts):
        try:
            response = await route.provider.complete(
                system=system,
                messages=messages,
                tools=tools,
                model=route.model,
                max_tokens=max_tokens or route.max_tokens,
                temperature=route.temperature,
                tool_choice=tool_choice,
                client=client,
            )
        except LLMError as exc:
            last = exc
            log.warning(
                "llm.call_failed",
                project_id=project_id,
                provider=route.provider_kind,
                model=route.model,
                error_type=exc.error_type,
                retryable=exc.retryable,
                will_fall_back=bool(exc.retryable and index + 1 < len(attempts)),
            )
            if exc.retryable and index + 1 < len(attempts):
                continue
            raise
        else:
            if record_usage:
                await record(
                    db,
                    company_id=company_id,
                    project_id=project_id,
                    agent_run_id=agent_run_id,
                    route=route,
                    response=response,
                )
            if index > 0:
                log.info(
                    "llm.fell_back",
                    project_id=project_id,
                    to_provider=route.provider_kind,
                )
            return response, route

    raise last or NoModelAvailable("No AI provider could be reached.")


async def record(
    db: AsyncSession,
    *,
    company_id: str,
    project_id: str,
    agent_run_id: str | None,
    route: Route,
    response: LLMResponse,
) -> LLMUsage:
    """Persist what one call cost. Counts and money only — never content."""
    priced = estimate(
        route.provider_kind,
        response.model,
        response.input_tokens,
        response.output_tokens,
        response.cache_read_tokens,
    )
    row = LLMUsage(
        company_id=company_id,
        project_id=project_id,
        agent_run_id=agent_run_id,
        credential_id=route.credential_id,
        provider=route.provider_kind,
        model=response.model,
        tier=route.tier,
        input_tokens=response.input_tokens,
        output_tokens=response.output_tokens,
        estimated_cost_usd=float(priced["cost_usd"]),
        byok=route.byok,
    )
    db.add(row)
    await db.flush()
    return row


async def test_credential(
    db: AsyncSession,
    credential: LLMCredential,
    *,
    tier: str = ModelTier.BALANCED.value,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Make a real call with this credential and report what happened.

    Live on purpose. A test that only checked the key's shape would report
    "Connected" for a revoked key, which is precisely the kind of lie this
    product refuses to tell.
    """
    kind = credential.provider.value
    model = model_for(credential, kind, tier)
    try:
        provider = build_provider(kind, config_from_credential(credential))
        result = await provider.check(model, client=client)
    except SecretError:
        return {
            "success": False,
            "error_type": "SECRET_STORE_UNAVAILABLE",
            "message": (
                "The stored key for this credential could not be read. Re-enter it."
            ),
            "model": model,
        }
    except LLMError as exc:
        return {**exc.to_dict(), "model": model}
    return {**result, "tier": tier}


def describe_routing(routes: list[Route]) -> dict[str, Any]:
    """What the AI settings screen shows about where work will actually go."""
    return {
        "primary": routes[0].describe() if routes else None,
        "fallbacks": [r.describe() for r in routes[1:]],
        "count": len(routes),
    }
