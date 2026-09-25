"""AI provider configuration: bring your own key, tiers, routing and spend.

The rule that shapes every endpoint here: **the browser never receives a
provider secret after submission.** A key is posted once, handed to the secret
store, and from then on only its fingerprint and last four characters are ever
returned. There is no "reveal" endpoint, because there is nothing to reveal —
the API has no path that resolves a stored key back to the caller.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.config import settings
from app.llm import catalog, spec_for
from app.llm.base import LLMError, ProviderNotImplemented
from app.llm.gateway import (
    NoModelAvailable,
    describe_routing,
    model_for,
    resolve_routes,
    test_credential,
)
from app.llm.pricing import describe as describe_pricing
from app.models import (
    AuditEvent,
    LLMCredential,
    LLMProviderKind,
    LLMUsage,
    ModelTier,
    ProjectRole,
)
from app.security.auth import DbSession, Tenant
from app.security.secrets import SecretContext, fingerprint, masked, store_secret
from app.tenancy import service as tenancy

router = APIRouter(prefix="/ai", tags=["ai"])


class CredentialIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    provider: LLMProviderKind
    #: Write-only. Accepted here, never returned by any endpoint.
    api_key: str | None = None
    base_url: str | None = None
    region: str | None = None
    config: dict[str, Any] | None = None
    tier_models: dict[str, str] | None = None
    is_default: bool = False


class CredentialPatch(BaseModel):
    name: str | None = None
    api_key: str | None = None
    base_url: str | None = None
    region: str | None = None
    config: dict[str, Any] | None = None
    tier_models: dict[str, str] | None = None
    is_default: bool | None = None
    is_active: bool | None = None


def _out(credential: LLMCredential) -> dict[str, Any]:
    """Everything about a credential except the credential."""
    return {
        "id": credential.id,
        "name": credential.name,
        "provider": credential.provider.value,
        "base_url": credential.base_url,
        "region": credential.region,
        "config": credential.config or {},
        "tier_models": credential.tier_models or {},
        "is_default": credential.is_default,
        "is_active": credential.is_active,
        # Proof that a key is stored, and which one, without any of it.
        "has_key": bool(credential.secret_ref),
        "key_fingerprint": fingerprint(credential.secret_ref)[:12]
        if credential.secret_ref
        else "",
        "last_tested_at": credential.last_tested_at.isoformat()
        if credential.last_tested_at
        else None,
        "last_test_ok": credential.last_test_ok,
        "last_test_error": credential.last_test_error,
        "created_at": credential.created_at.isoformat(),
    }


def _secret_context(tenant: Tenant) -> SecretContext:
    return SecretContext(
        company_id=tenant.company_id, project_id=tenant.project_id, purpose="llm_key"
    )


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------
@router.get("/providers")
async def list_providers(tenant: Tenant) -> dict[str, Any]:
    """Every provider this platform knows, and whether it actually works here.

    `implemented: false` entries are listed deliberately rather than hidden, so
    the answer to "do you support Bedrock?" is a specific no with a reason
    instead of silence.
    """
    allowed = tenant.policy.allowed_llm_providers
    providers = [
        {
            **spec,
            "permitted_by_policy": not allowed or spec["kind"] in allowed,
        }
        for spec in catalog()
    ]
    return {
        "providers": providers,
        "tiers": [t.value for t in ModelTier],
        "policy": {
            "allowed_llm_providers": sorted(allowed),
            "allow_llm_fallback": tenant.policy.allow_llm_fallback,
        },
        "platform_managed_ai": settings.feature_platform_managed_ai,
        "deployment_providers": settings.deployment_llm_providers
        if settings.feature_platform_managed_ai
        else [],
        "byok_required": not settings.ai_available_without_byok,
    }


@router.get("/pricing")
async def pricing() -> dict[str, Any]:
    """Where the cost estimates come from, and what they are not."""
    return describe_pricing()


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------
@router.get("/credentials")
async def list_credentials(tenant: Tenant, db: DbSession) -> dict[str, Any]:
    rows = (
        await db.execute(
            select(LLMCredential)
            .where(LLMCredential.project_id == tenant.project_id)
            .order_by(LLMCredential.created_at)
        )
    ).scalars().all()
    return {"count": len(rows), "credentials": [_out(c) for c in rows]}


@router.post("/credentials", status_code=201)
async def create_credential(
    payload: CredentialIn, tenant: Tenant, db: DbSession
) -> dict[str, Any]:
    """Store a project's own provider key.

    The key is written to the secret store immediately and the plaintext is not
    retained anywhere in this process beyond the call.
    """
    tenant.require(ProjectRole.PROJECT_ADMIN)
    kind = payload.provider.value

    try:
        spec = spec_for(kind)
    except ProviderNotImplemented as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc
    if not spec.implemented:
        raise HTTPException(status_code=400, detail=spec.notes)
    if not tenant.policy.provider_allowed(kind):
        raise HTTPException(
            status_code=403,
            detail=(
                f"This project's policy does not permit {spec.label}. A project "
                "admin can widen the allowed providers in project settings."
            ),
        )
    if spec.requires_api_key and not payload.api_key:
        raise HTTPException(status_code=400, detail=f"{spec.label} requires an API key.")
    if spec.requires_base_url and not payload.base_url:
        raise HTTPException(
            status_code=400, detail=f"{spec.label} requires an endpoint URL."
        )

    duplicate = (
        await db.execute(
            select(LLMCredential).where(
                LLMCredential.project_id == tenant.project_id,
                LLMCredential.name == payload.name,
            )
        )
    ).scalar_one_or_none()
    if duplicate is not None:
        raise HTTPException(status_code=409, detail=f"'{payload.name}' already exists.")

    credential = LLMCredential(
        company_id=tenant.company_id,
        project_id=tenant.project_id,
        name=payload.name,
        provider=payload.provider,
        secret_ref=store_secret(payload.api_key, _secret_context(tenant))
        if payload.api_key
        else None,
        base_url=payload.base_url,
        region=payload.region,
        config=payload.config,
        tier_models=_clean_tiers(payload.tier_models),
        is_default=payload.is_default,
    )
    db.add(credential)
    await db.flush()
    if payload.is_default:
        await _clear_other_defaults(db, tenant.project_id, credential.id)

    db.add(
        AuditEvent(
            company_id=tenant.company_id,
            project_id=tenant.project_id,
            user_id=tenant.user_id,
            action="ai.credential_added",
            # The provider and the last four characters, never the key.
            arguments={
                "provider": kind,
                "name": payload.name,
                "key_suffix": masked(payload.api_key or ""),
            },
            outcome="ok",
        )
    )
    await db.commit()
    return _out(credential)


@router.patch("/credentials/{credential_id}")
async def update_credential(
    credential_id: str, payload: CredentialPatch, tenant: Tenant, db: DbSession
) -> dict[str, Any]:
    tenant.require(ProjectRole.PROJECT_ADMIN)
    credential = await _owned(db, tenant, credential_id)

    if payload.name is not None:
        credential.name = payload.name
    if payload.base_url is not None:
        credential.base_url = payload.base_url
    if payload.region is not None:
        credential.region = payload.region
    if payload.config is not None:
        credential.config = payload.config
    if payload.tier_models is not None:
        credential.tier_models = _clean_tiers(payload.tier_models)
    if payload.is_active is not None:
        credential.is_active = payload.is_active
    if payload.api_key:
        # Rotation: a new reference replaces the old one. The previous key is
        # not recoverable from here, which is the point.
        credential.secret_ref = store_secret(payload.api_key, _secret_context(tenant))
        credential.last_test_ok = None
        credential.last_test_error = None
        credential.last_tested_at = None
    if payload.is_default:
        credential.is_default = True
        await _clear_other_defaults(db, tenant.project_id, credential.id)
    elif payload.is_default is False:
        credential.is_default = False

    db.add(
        AuditEvent(
            company_id=tenant.company_id,
            project_id=tenant.project_id,
            user_id=tenant.user_id,
            action="ai.credential_updated",
            arguments={
                "credential_id": credential.id,
                "provider": credential.provider.value,
                "key_rotated": bool(payload.api_key),
            },
            outcome="ok",
        )
    )
    await db.commit()
    return _out(credential)


@router.delete("/credentials/{credential_id}")
async def delete_credential(
    credential_id: str, tenant: Tenant, db: DbSession
) -> dict[str, bool]:
    tenant.require(ProjectRole.PROJECT_ADMIN)
    credential = await _owned(db, tenant, credential_id)
    db.add(
        AuditEvent(
            company_id=tenant.company_id,
            project_id=tenant.project_id,
            user_id=tenant.user_id,
            action="ai.credential_removed",
            arguments={"provider": credential.provider.value, "name": credential.name},
            outcome="ok",
        )
    )
    await db.delete(credential)
    await db.commit()
    return {"success": True}


@router.post("/credentials/{credential_id}/test")
async def test_connection(
    credential_id: str,
    tenant: Tenant,
    db: DbSession,
    tier: ModelTier = ModelTier.BALANCED,
) -> dict[str, Any]:
    """Make a real call with this key and report exactly what happened.

    Deliberately a live request. A test that only validated the shape of a key
    would show "Connected" for a revoked one — the precise failure mode this
    product exists to avoid.
    """
    tenant.require(ProjectRole.PROJECT_ADMIN, ProjectRole.DEVELOPER)
    credential = await _owned(db, tenant, credential_id)

    result = await test_credential(db, credential, tier=tier.value)
    credential.last_tested_at = datetime.now(UTC)
    credential.last_test_ok = bool(result.get("success"))
    credential.last_test_error = None if result.get("success") else result.get("message")
    await db.commit()
    return result


# ---------------------------------------------------------------------------
# Routing and spend
# ---------------------------------------------------------------------------
@router.get("/routing")
async def routing(
    tenant: Tenant, db: DbSession, tier: ModelTier = ModelTier.BALANCED
) -> dict[str, Any]:
    """Where this project's work would actually go, right now.

    Answering "which model is my data going to?" with a live resolution rather
    than a settings dump is the difference between a claim and a fact.
    """
    try:
        routes = await resolve_routes(
            db, project_id=tenant.project_id, policy=tenant.policy, tier=tier.value
        )
    except NoModelAvailable as exc:
        return {
            "available": False,
            "message": exc.message,
            "primary": None,
            "fallbacks": [],
            "fallback_enabled": tenant.policy.allow_llm_fallback,
        }
    described = describe_routing(routes)
    return {
        "available": True,
        **described,
        "fallback_enabled": tenant.policy.allow_llm_fallback,
        "fallback_note": (
            "Fallback is off. A failed call is reported rather than silently "
            "retried against a different vendor."
            if not tenant.policy.allow_llm_fallback
            else "A retryable failure will move this project's data to the next "
            "provider listed."
        ),
    }


@router.get("/usage")
async def usage(tenant: Tenant, db: DbSession, days: int = 30) -> dict[str, Any]:
    """Token and cost totals. Counts and money only — never prompts."""
    since = datetime.now(UTC) - _days(days)
    rows = (
        await db.execute(
            select(
                LLMUsage.provider,
                LLMUsage.model,
                LLMUsage.byok,
                func.count(LLMUsage.id),
                func.sum(LLMUsage.input_tokens),
                func.sum(LLMUsage.output_tokens),
                func.sum(LLMUsage.estimated_cost_usd),
            )
            .where(
                LLMUsage.project_id == tenant.project_id,
                LLMUsage.created_at >= since,
            )
            .group_by(LLMUsage.provider, LLMUsage.model, LLMUsage.byok)
        )
    ).all()

    breakdown = [
        {
            "provider": provider,
            "model": model,
            "byok": bool(byok),
            "requests": int(requests or 0),
            "input_tokens": int(input_tokens or 0),
            "output_tokens": int(output_tokens or 0),
            "estimated_cost_usd": round(float(cost or 0.0), 4),
        }
        for provider, model, byok, requests, input_tokens, output_tokens, cost in rows
    ]
    return {
        "days": days,
        "totals": {
            "requests": sum(b["requests"] for b in breakdown),
            "input_tokens": sum(b["input_tokens"] for b in breakdown),
            "output_tokens": sum(b["output_tokens"] for b in breakdown),
            "estimated_cost_usd": round(
                sum(b["estimated_cost_usd"] for b in breakdown), 4
            ),
        },
        "breakdown": sorted(
            breakdown, key=lambda b: b["estimated_cost_usd"], reverse=True
        ),
        "pricing": describe_pricing(),
    }


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _days(days: int):
    from datetime import timedelta

    return timedelta(days=max(1, min(int(days), 365)))


def _clean_tiers(tiers: dict[str, str] | None) -> dict[str, str] | None:
    """Keep only real tiers, so a typo cannot become a silently-ignored setting."""
    if not tiers:
        return None
    known = {t.value for t in ModelTier}
    cleaned = {
        k.upper(): v for k, v in tiers.items() if k.upper() in known and str(v).strip()
    }
    return cleaned or None


async def _clear_other_defaults(db: Any, project_id: str, keep_id: str) -> None:
    rows = (
        await db.execute(
            select(LLMCredential).where(
                LLMCredential.project_id == project_id,
                LLMCredential.id != keep_id,
                LLMCredential.is_default.is_(True),
            )
        )
    ).scalars().all()
    for row in rows:
        row.is_default = False


async def _owned(db: Any, tenant: Tenant, credential_id: str) -> LLMCredential:
    credential = await tenancy.owned(
        db, LLMCredential, credential_id, tenant.project_id
    )
    if credential is None:
        raise HTTPException(status_code=404, detail="Credential not found")
    return credential


__all__ = ["LLMError", "model_for", "router"]
