"""SCIM 2.0 user provisioning (RFC 7644), Users resource.

Implemented for real: an IdP (Okta, Entra ID, JumpCloud…) can create, list,
update, deactivate and delete users in one project. It is deliberately scoped
to Users — Groups are not implemented, and the ServiceProviderConfig below says
so rather than advertising support that does not exist.

Provisioning targets a **project**, because a project is the security boundary:
"this person exists in our tenant" is not the same statement as "this person may
act on this Salesforce org". Company membership is granted alongside it, at the
lowest company-level role, so the IdP never has to know about two resources.

Authentication is a bearer token (`SCIM_BEARER_TOKEN`) plus the target project
id. Without the token the endpoints return 404, not 401, so a deployment that
has not enabled SCIM does not advertise the surface.
"""

from __future__ import annotations

import hmac
from typing import Annotated, Any

from fastapi import APIRouter, Header, HTTPException, Query, Request, Response
from sqlalchemy import func, select

from app.config import settings
from app.models import (
    AuditEvent,
    CompanyMembership,
    CompanyRole,
    IdentityProviderKind,
    Project,
    ProjectMembership,
    ProjectRole,
    User,
)
from app.security.auth import DbSession

router = APIRouter(prefix="/scim/v2", tags=["scim"])

USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
LIST_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:ListResponse"
ERROR_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:Error"
PATCH_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:PatchOp"

#: SCIM roles an IdP may assign, mapped onto project roles. Anything else is
#: rejected rather than silently downgraded, so a typo in an IdP mapping cannot
#: quietly grant or remove authority.
SCIM_ROLE_MAP = {r.value.lower(): r for r in ProjectRole}


def _unavailable() -> HTTPException:
    return HTTPException(status_code=404, detail="SCIM provisioning is not enabled.")


def _scim_error(status: int, detail: str) -> HTTPException:
    return HTTPException(
        status_code=status,
        detail={"schemas": [ERROR_SCHEMA], "detail": detail, "status": str(status)},
    )


async def _authorize(
    db: Any, authorization: str | None, project_id: str | None
) -> Project:
    """Authenticate a provisioning call and resolve its target project.

    Two token sources, checked in this order:

      1. **The company's own SCIM token**, minted through the SSO settings.
         This is the one an enterprise customer configures in their IdP, and it
         only works for their own projects.
      2. **The deployment-wide `SCIM_BEARER_TOKEN`**, for single-tenant
         installs. It works anywhere, which is exactly why the per-company
         token exists: a shared token across tenants would let one customer's
         IdP provision into another's project.

    Both comparisons are constant-time. A provisioning token is a credential.
    """
    if not settings.feature_scim:
        raise _unavailable()
    token = ""
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1].strip()
    if not token:
        raise _scim_error(401, "Invalid SCIM bearer token.")
    if not project_id:
        raise _scim_error(400, "The X-Project-Id header is required.")

    project = await db.get(Project, project_id)
    if project is None or not project.is_active:
        # Same answer as a bad token: whether a project id exists is not
        # something an unauthenticated caller gets to learn.
        raise _scim_error(401, "Invalid SCIM bearer token.")

    if await _company_token_matches(db, project.company_id, token):
        return project
    if settings.scim_bearer_token and hmac.compare_digest(
        token, settings.scim_bearer_token
    ):
        return project
    raise _scim_error(401, "Invalid SCIM bearer token.")


async def _company_token_matches(db: Any, company_id: str, token: str) -> bool:
    """Does this token match the company's own SCIM credential?"""
    from app.models import IdentityProviderKind, SSOConfiguration
    from app.security.secrets import SecretContext, SecretError, resolve_secret

    config = (
        await db.execute(
            select(SSOConfiguration).where(
                SSOConfiguration.company_id == company_id,
                SSOConfiguration.kind == IdentityProviderKind.OIDC,
            )
        )
    ).scalar_one_or_none()
    if config is None or not config.scim_enabled or not config.scim_token_ref:
        return False
    try:
        stored = resolve_secret(
            config.scim_token_ref,
            SecretContext(company_id=company_id, purpose="scim_token"),
        )
    except SecretError:
        # An unreadable token is not a match. Failing open here would turn a
        # secret-store outage into an open provisioning endpoint.
        return False
    return hmac.compare_digest(token, stored)


def _user_resource(
    user: User, membership: ProjectMembership, request: Request | None
) -> dict[str, Any]:
    base = str(request.base_url).rstrip("/") if request else ""
    return {
        "schemas": [USER_SCHEMA],
        "id": user.id,
        "externalId": membership.external_id,
        "userName": user.email,
        "name": {"formatted": user.display_name},
        "displayName": user.display_name,
        "emails": [{"value": user.email, "primary": True, "type": "work"}],
        "active": bool(user.is_active and membership.is_active),
        "roles": [{"value": membership.role.value, "primary": True}],
        "meta": {
            "resourceType": "User",
            "created": user.created_at.isoformat(),
            "lastModified": membership.updated_at.isoformat(),
            "location": f"{base}{settings.api_v1}/scim/v2/Users/{user.id}",
        },
    }


@router.get("/ServiceProviderConfig")
async def service_provider_config() -> dict[str, Any]:
    # Gated on the feature, not on the deployment-wide token: a company using
    # its own SCIM credential still needs to be able to read this document,
    # and every IdP fetches it before provisioning anything.
    if not settings.feature_scim:
        raise _unavailable()
    return {
        "schemas": ["urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig"],
        "patch": {"supported": True},
        "bulk": {"supported": False, "maxOperations": 0, "maxPayloadSize": 0},
        "filter": {"supported": True, "maxResults": 200},
        "changePassword": {"supported": False},
        "sort": {"supported": False},
        "etag": {"supported": False},
        "authenticationSchemes": [
            {
                "type": "oauthbearertoken",
                "name": "OAuth Bearer Token",
                "description": "Static provisioning token configured as SCIM_BEARER_TOKEN.",
            }
        ],
        "documentationUri": "https://datatracker.ietf.org/doc/html/rfc7644",
        # Stated plainly rather than implied by omission.
        "implementedResources": ["Users"],
        "notImplemented": ["Groups", "Bulk", "Sort", "ETag"],
    }


@router.get("/Users")
async def list_users(
    request: Request,
    db: DbSession,
    authorization: Annotated[str | None, Header()] = None,
    x_project_id: Annotated[str | None, Header(alias="X-Project-Id")] = None,
    filter: str | None = Query(None),
    startIndex: int = Query(1, ge=1),
    count: int = Query(100, ge=1, le=200),
) -> dict[str, Any]:
    project = await _authorize(db, authorization, x_project_id)
    stmt = select(ProjectMembership).where(
        ProjectMembership.project_id == project.id
    )

    # SCIM filters: support the one every IdP actually sends.
    email_filter: str | None = None
    if filter:
        normalized = filter.strip()
        if normalized.lower().startswith("username eq"):
            email_filter = normalized.split("eq", 1)[1].strip().strip('"').lower()
        else:
            raise _scim_error(
                400,
                'Only filters of the form \'userName eq "value"\' are supported.',
            )

    rows = (await db.execute(stmt)).scalars().all()
    resources = []
    for m in rows:
        u = await db.get(User, m.user_id)
        if u is None:
            continue
        if email_filter and u.email.lower() != email_filter:
            continue
        resources.append(_user_resource(u, m, request))

    total = len(resources)
    window = resources[startIndex - 1 : startIndex - 1 + count]
    return {
        "schemas": [LIST_SCHEMA],
        "totalResults": total,
        "startIndex": startIndex,
        "itemsPerPage": len(window),
        "Resources": window,
    }


@router.post("/Users", status_code=201)
async def create_user(
    request: Request,
    payload: dict[str, Any],
    db: DbSession,
    authorization: Annotated[str | None, Header()] = None,
    x_project_id: Annotated[str | None, Header(alias="X-Project-Id")] = None,
) -> dict[str, Any]:
    project = await _authorize(db, authorization, x_project_id)
    email = str(payload.get("userName") or "").strip().lower()
    if not email or "@" not in email:
        raise _scim_error(400, "userName must be an email address.")

    role = _role_from(payload)
    user = (
        await db.execute(select(User).where(func.lower(User.email) == email))
    ).scalar_one_or_none()
    if user is None:
        user = User(
            email=email,
            display_name=str(
                payload.get("displayName") or (payload.get("name") or {}).get("formatted") or ""
            )
            or email.split("@")[0],
            idp_kind=IdentityProviderKind.OIDC if settings.oidc_configured else
            IdentityProviderKind.LOCAL,
            idp_subject=str(payload.get("externalId") or "") or None,
        )
        db.add(user)
        await db.flush()

    membership = (
        await db.execute(
            select(ProjectMembership).where(
                ProjectMembership.project_id == project.id,
                ProjectMembership.user_id == user.id,
            )
        )
    ).scalar_one_or_none()
    if membership is not None:
        raise _scim_error(409, "User already provisioned in this project.")

    membership = ProjectMembership(
        company_id=project.company_id,
        project_id=project.id,
        user_id=user.id,
        role=role,
        external_id=str(payload.get("externalId") or "") or None,
        is_active=bool(payload.get("active", True)),
    )
    db.add(membership)

    # Company membership at the lowest level, so the person exists in the
    # tenant without gaining any company-wide authority from being provisioned.
    company_membership = (
        await db.execute(
            select(CompanyMembership).where(
                CompanyMembership.company_id == project.company_id,
                CompanyMembership.user_id == user.id,
            )
        )
    ).scalar_one_or_none()
    if company_membership is None:
        db.add(
            CompanyMembership(
                company_id=project.company_id,
                user_id=user.id,
                role=CompanyRole.COMPANY_MEMBER,
                external_id=str(payload.get("externalId") or "") or None,
            )
        )
    db.add(
        AuditEvent(
            company_id=project.company_id,
            project_id=project.id,
            user_id=user.id,
            action="scim.user_provisioned",
            arguments={"email": email, "role": role.value},
            outcome="ok",
        )
    )
    await db.commit()
    return _user_resource(user, membership, request)


@router.get("/Users/{user_id}")
async def get_user(
    user_id: str,
    request: Request,
    db: DbSession,
    authorization: Annotated[str | None, Header()] = None,
    x_project_id: Annotated[str | None, Header(alias="X-Project-Id")] = None,
) -> dict[str, Any]:
    project = await _authorize(db, authorization, x_project_id)
    user, membership = await _lookup(db, project.id, user_id)
    return _user_resource(user, membership, request)


@router.put("/Users/{user_id}")
async def replace_user(
    user_id: str,
    payload: dict[str, Any],
    request: Request,
    db: DbSession,
    authorization: Annotated[str | None, Header()] = None,
    x_project_id: Annotated[str | None, Header(alias="X-Project-Id")] = None,
) -> dict[str, Any]:
    project = await _authorize(db, authorization, x_project_id)
    user, membership = await _lookup(db, project.id, user_id)
    if payload.get("displayName"):
        user.display_name = str(payload["displayName"])
    if "active" in payload:
        membership.is_active = bool(payload["active"])
    if payload.get("roles"):
        membership.role = _role_from(payload)
    await db.commit()
    return _user_resource(user, membership, request)


@router.patch("/Users/{user_id}")
async def patch_user(
    user_id: str,
    payload: dict[str, Any],
    request: Request,
    db: DbSession,
    authorization: Annotated[str | None, Header()] = None,
    x_project_id: Annotated[str | None, Header(alias="X-Project-Id")] = None,
) -> dict[str, Any]:
    """PatchOp. Deactivation — `active: false` — is the operation that matters:
    it is how an IdP cuts off access, so it takes effect immediately."""
    project = await _authorize(db, authorization, x_project_id)
    user, membership = await _lookup(db, project.id, user_id)
    if PATCH_SCHEMA not in (payload.get("schemas") or []):
        raise _scim_error(400, "Expected a PatchOp payload.")

    for operation in payload.get("Operations") or []:
        op = str(operation.get("op", "")).lower()
        path = str(operation.get("path") or "").lower()
        value = operation.get("value")
        if op not in {"replace", "add"}:
            if op == "remove" and path == "active":
                membership.is_active = False
                continue
            raise _scim_error(400, f"Unsupported PatchOp operation '{op}'.")
        if path == "active" or (isinstance(value, dict) and "active" in value):
            active = value if isinstance(value, bool) else bool(value.get("active"))
            membership.is_active = active
        elif path in {"displayname", "name.formatted"}:
            user.display_name = str(value)
        elif path == "roles":
            membership.role = _role_from({"roles": value})
        elif isinstance(value, dict):
            if "displayName" in value:
                user.display_name = str(value["displayName"])
            if "roles" in value:
                membership.role = _role_from(value)
        else:
            raise _scim_error(400, f"Unsupported PatchOp path '{path}'.")

    db.add(
        AuditEvent(
            company_id=project.company_id,
            project_id=project.id,
            user_id=user.id,
            action="scim.user_patched",
            arguments={"active": membership.is_active, "role": membership.role.value},
            outcome="ok",
        )
    )
    await db.commit()
    return _user_resource(user, membership, request)


@router.delete("/Users/{user_id}", status_code=204)
async def delete_user(
    user_id: str,
    db: DbSession,
    authorization: Annotated[str | None, Header()] = None,
    x_project_id: Annotated[str | None, Header(alias="X-Project-Id")] = None,
) -> Response:
    """De-provision from this project.

    The membership is removed, which revokes all access. The user row itself is
    kept so audit history stays attributable — deleting it would erase who did
    what, which is the opposite of what a compliance-driven de-provision wants.
    """
    project = await _authorize(db, authorization, x_project_id)
    _, membership = await _lookup(db, project.id, user_id)
    db.add(
        AuditEvent(
            company_id=project.company_id,
            project_id=project.id,
            user_id=user_id,
            action="scim.user_deprovisioned",
            outcome="ok",
        )
    )
    await db.delete(membership)
    await db.commit()
    return Response(status_code=204)


def _role_from(payload: dict[str, Any]) -> ProjectRole:
    roles = payload.get("roles") or []
    if isinstance(roles, dict):
        roles = [roles]
    for entry in roles:
        raw = entry.get("value") if isinstance(entry, dict) else entry
        if raw is None:
            continue
        mapped = SCIM_ROLE_MAP.get(str(raw).strip().lower())
        if mapped is None:
            raise _scim_error(
                400,
                f"Unknown role '{raw}'. Valid roles: "
                + ", ".join(sorted(SCIM_ROLE_MAP)),
            )
        return mapped
    # No role asserted by the IdP means least privilege, not most.
    return ProjectRole.VIEWER


async def _lookup(db: Any, project_id: str, user_id: str) -> tuple[User, ProjectMembership]:
    membership = (
        await db.execute(
            select(ProjectMembership).where(
                ProjectMembership.project_id == project_id,
                ProjectMembership.user_id == user_id,
            )
        )
    ).scalar_one_or_none()
    user = await db.get(User, user_id)
    if membership is None or user is None:
        raise _scim_error(404, "User not found in this project.")
    return user, membership
