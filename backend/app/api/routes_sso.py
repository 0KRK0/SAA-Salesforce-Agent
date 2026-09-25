"""Company-level enterprise identity: SSO, group mappings and SCIM.

Company administrators only. These endpoints decide who can sign in to a
customer's tenant and what they can do once inside, so they are the highest-
authority surface in the product after the deployment's own configuration.

Two things are never returned: the OIDC client secret and the SCIM bearer
token. Both go into the secret store on the way in and only a fingerprint comes
back out — there is no endpoint that resolves either.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.config import settings
from app.models import (
    AuditEvent,
    Company,
    IdentityProviderKind,
    Project,
    ProjectRole,
    SSOConfiguration,
)
from app.security import mapping as sso_mapping
from app.security.auth import DbSession, Tenant
from app.security.secrets import SecretContext, fingerprint, random_token, store_secret

router = APIRouter(prefix="/sso", tags=["sso"])


class SSOIn(BaseModel):
    kind: IdentityProviderKind = IdentityProviderKind.OIDC
    enabled: bool = False
    issuer: str | None = None
    client_id: str | None = None
    #: Write-only. No endpoint returns it.
    client_secret: str | None = None
    redirect_uri: str | None = None
    scopes: str = "openid email profile"
    #: Email domains this company owns. Login routing is by domain.
    domains: list[str] | None = None
    #: {"idp-group-name": "PROJECT_ROLE"} or {"group": "<project_id>:ROLE"}
    group_mappings: dict[str, Any] | None = None
    default_project_id: str | None = None
    default_project_role: ProjectRole = ProjectRole.VIEWER
    scim_enabled: bool = False


def _out(config: SSOConfiguration, company: Company) -> dict[str, Any]:
    return {
        "id": config.id,
        "kind": config.kind.value,
        "enabled": config.enabled,
        "implemented": config.kind is not IdentityProviderKind.SAML,
        "issuer": config.issuer,
        "client_id": config.client_id,
        # Whether a secret exists, and which one. Never the secret.
        "has_client_secret": bool(config.client_secret_ref),
        "client_secret_fingerprint": fingerprint(config.client_secret_ref)[:12]
        if config.client_secret_ref
        else "",
        "redirect_uri": config.redirect_uri,
        "scopes": config.scopes,
        "domains": company.sso_domains or [],
        "group_mappings": config.group_mappings or {},
        "default_project_id": config.default_project_id,
        "default_project_role": config.default_project_role.value,
        "scim_enabled": config.scim_enabled,
        "has_scim_token": bool(config.scim_token_ref),
        "updated_at": config.updated_at.isoformat() if config.updated_at else None,
    }


@router.get("")
async def get_sso(tenant: Tenant, db: DbSession) -> dict[str, Any]:
    """This company's SSO configuration, and what the deployment supports."""
    tenant.require_company_admin()
    rows = (
        await db.execute(
            select(SSOConfiguration).where(
                SSOConfiguration.company_id == tenant.company_id
            )
        )
    ).scalars().all()

    projects = await _project_choices(db, tenant.company_id)
    return {
        "configurations": [_out(r, tenant.company) for r in rows],
        "supported": {
            "OIDC": True,
            # Stated rather than implied by omission.
            "SAML": settings.feature_saml,
            "SCIM": settings.feature_scim,
        },
        "saml_note": (
            "SAML is not implemented in this build. It requires XML-signature "
            "verification and assertion replay protection, which should not be "
            "approximated. Use OIDC."
        ),
        "assignable_roles": [r.value for r in ProjectRole],
        "projects": projects,
        "group_claims_read": list(sso_mapping.GROUP_CLAIMS),
    }


@router.put("")
async def put_sso(payload: SSOIn, tenant: Tenant, db: DbSession) -> dict[str, Any]:
    """Create or replace this company's SSO configuration."""
    tenant.require_company_admin()

    if payload.kind is IdentityProviderKind.SAML and not settings.feature_saml:
        raise HTTPException(
            status_code=400,
            detail=(
                "SAML is not implemented in this build. Enabling it here would "
                "leave a login method that cannot authenticate anyone. Use OIDC."
            ),
        )
    if payload.enabled and payload.kind is IdentityProviderKind.OIDC:
        missing = [
            name
            for name, value in (
                ("issuer", payload.issuer),
                ("client_id", payload.client_id),
            )
            if not value
        ]
        if missing:
            raise HTTPException(
                status_code=400,
                detail=f"OIDC needs {', '.join(missing)} before it can be enabled.",
            )

    if payload.default_project_id:
        await _require_own_project(db, tenant, payload.default_project_id)
    for target in _mapping_targets(payload.group_mappings):
        await _require_own_project(db, tenant, target)

    config = (
        await db.execute(
            select(SSOConfiguration).where(
                SSOConfiguration.company_id == tenant.company_id,
                SSOConfiguration.kind == payload.kind,
            )
        )
    ).scalar_one_or_none()
    if config is None:
        config = SSOConfiguration(company_id=tenant.company_id, kind=payload.kind)
        db.add(config)

    config.enabled = payload.enabled
    config.issuer = payload.issuer
    config.client_id = payload.client_id
    config.redirect_uri = payload.redirect_uri
    config.scopes = payload.scopes
    config.group_mappings = payload.group_mappings
    config.default_project_id = payload.default_project_id
    config.default_project_role = payload.default_project_role
    config.scim_enabled = payload.scim_enabled and settings.feature_scim
    if payload.client_secret:
        config.client_secret_ref = store_secret(
            payload.client_secret,
            SecretContext(company_id=tenant.company_id, purpose="oidc_client_secret"),
        )
    if payload.domains is not None:
        tenant.company.sso_domains = [
            d.strip().lower().lstrip("@") for d in payload.domains if d.strip()
        ]

    db.add(
        AuditEvent(
            company_id=tenant.company_id,
            user_id=tenant.user_id,
            action="sso.configuration_updated",
            arguments={
                "kind": payload.kind.value,
                "enabled": payload.enabled,
                "domains": tenant.company.sso_domains,
                "mapped_groups": sorted((payload.group_mappings or {}).keys()),
                "secret_rotated": bool(payload.client_secret),
            },
            outcome="ok",
        )
    )
    await db.commit()
    return _out(config, tenant.company)


@router.post("/scim-token")
async def rotate_scim_token(tenant: Tenant, db: DbSession) -> dict[str, Any]:
    """Mint a SCIM provisioning token. Shown once, stored only as a reference."""
    tenant.require_company_admin()
    if not settings.feature_scim:
        raise HTTPException(
            status_code=400, detail="SCIM provisioning is disabled in this deployment."
        )

    config = (
        await db.execute(
            select(SSOConfiguration).where(
                SSOConfiguration.company_id == tenant.company_id,
                SSOConfiguration.kind == IdentityProviderKind.OIDC,
            )
        )
    ).scalar_one_or_none()
    if config is None:
        config = SSOConfiguration(
            company_id=tenant.company_id, kind=IdentityProviderKind.OIDC
        )
        db.add(config)

    token = random_token(32)
    config.scim_token_ref = store_secret(
        token, SecretContext(company_id=tenant.company_id, purpose="scim_token")
    )
    config.scim_enabled = True
    db.add(
        AuditEvent(
            company_id=tenant.company_id,
            user_id=tenant.user_id,
            action="sso.scim_token_rotated",
            outcome="ok",
        )
    )
    await db.commit()
    return {
        "success": True,
        # Returned exactly once. Rotating replaces it; there is no way to read
        # it back, by design.
        "token": token,
        "message": (
            "Copy this now — it is shown once and cannot be retrieved again. "
            "Any previously issued SCIM token has stopped working."
        ),
    }


@router.post("/preview")
async def preview_mapping(
    groups: list[str], tenant: Tenant, db: DbSession
) -> dict[str, Any]:
    """Show what a given set of IdP groups would grant, without signing anyone in.

    Group mappings are the one part of SSO that fails silently: a typo grants
    nothing and nobody finds out until a person cannot get in. This makes the
    mapping inspectable before it is trusted.
    """
    tenant.require_company_admin()
    config = (
        await db.execute(
            select(SSOConfiguration).where(
                SSOConfiguration.company_id == tenant.company_id
            )
        )
    ).scalars().first()
    if config is None:
        raise HTTPException(status_code=404, detail="No SSO configuration yet.")

    result = sso_mapping.resolve(
        groups,
        config.group_mappings,
        default_project_id=config.default_project_id,
        default_project_role=config.default_project_role,
    )
    projects = {p["id"]: p["name"] for p in await _project_choices(db, tenant.company_id)}
    return {
        **result.to_dict(),
        "explained": [
            {
                "project_id": pid,
                "project_name": projects.get(pid, "(unknown project)"),
                "role": role.value,
            }
            for pid, role in result.project_roles.items()
        ],
        "note": (
            "Groups this deployment does not recognise grant nothing. Unknown is "
            "not 'probably fine'."
        ),
    }


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
async def _project_choices(db: Any, company_id: str) -> list[dict[str, str]]:
    rows = (
        await db.execute(
            select(Project).where(
                Project.company_id == company_id, Project.is_active.is_(True)
            )
        )
    ).scalars().all()
    return [{"id": p.id, "name": p.name} for p in rows]


def _mapping_targets(mappings: dict[str, Any] | None) -> list[str]:
    """Project ids referenced by a mapping table."""
    targets: list[str] = []
    for value in (mappings or {}).values():
        for entry in value if isinstance(value, list) else [value]:
            project_id, _, _ = str(entry).rpartition(":")
            if project_id:
                targets.append(project_id)
    return targets


async def _require_own_project(db: Any, tenant: Tenant, project_id: str) -> None:
    """A mapping pointing at another company's project is a tenancy breach."""
    project = await db.get(Project, project_id)
    if project is None or project.company_id != tenant.company_id:
        raise HTTPException(
            status_code=404, detail=f"Project '{project_id}' not found in this company."
        )


class DomainsIn(BaseModel):
    domains: list[str] = Field(default_factory=list)


__all__ = ["DomainsIn", "router"]
