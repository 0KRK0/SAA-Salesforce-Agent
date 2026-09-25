"""Session, company, project and membership endpoints.

Login establishes *who* the caller is. Which project they act in is a separate,
explicit choice: the session carries a project, and every other endpoint
resolves it through `TenantContext` rather than trusting a body parameter.

Naming an id you have no membership of returns 404, not 403 — the API never
confirms that another tenant's company, project or user exists.
"""

from __future__ import annotations

import hmac
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Cookie, HTTPException, Response
from fastapi.responses import RedirectResponse
from jose import JWTError, jwt
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select

from app.config import settings
from app.models import (
    AuditEvent,
    Company,
    CompanyRole,
    IdentityProviderKind,
    Project,
    ProjectMembership,
    ProjectRole,
    User,
)
from app.observability.logging import get_logger
from app.security import mapping as sso_mapping
from app.security.auth import (
    SESSION_COOKIE,
    DbSession,
    Tenant,
    create_session_token,
    get_or_create_user,
    revoke_sessions,
)
from app.security.identity import IdentityError, describe_providers, oidc_provider
from app.tenancy import service as tenancy

router = APIRouter(prefix="/auth", tags=["auth"])
log = get_logger("api.auth")

#: Short-lived cookie holding the in-flight OIDC state/PKCE/nonce.
OIDC_COOKIE = "sfagent_oidc"


class LoginRequest(BaseModel):
    email: EmailStr
    display_name: str = ""
    project_id: str | None = None


class UserOut(BaseModel):
    id: str
    email: str
    display_name: str
    company_id: str
    company_name: str
    project_id: str
    project_name: str
    role: str
    company_role: str


def _set_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        samesite="lax",
        secure=settings.environment not in {"local", "dev"},
        max_age=settings.session_ttl_seconds,
        path="/",
    )


async def _user_out(
    db: Any, user: User, membership: ProjectMembership
) -> UserOut:
    project = await db.get(Project, membership.project_id)
    company = await db.get(Company, membership.company_id)
    company_membership = await tenancy.company_membership(
        db, user_id=user.id, company_id=membership.company_id
    )
    return UserOut(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        company_id=company.id if company else "",
        company_name=company.name if company else "",
        project_id=project.id if project else "",
        project_name=project.name if project else "",
        role=membership.role.value,
        company_role=(
            company_membership.role.value
            if company_membership
            else CompanyRole.COMPANY_MEMBER.value
        ),
    )


@router.post("/login", response_model=UserOut)
async def login(payload: LoginRequest, response: Response, db: DbSession) -> UserOut:
    user = await get_or_create_user(db, str(payload.email), payload.display_name)
    memberships = await tenancy.project_memberships_for(db, user.id)
    if not memberships:
        memberships = [await tenancy.ensure_personal_workspace(db, user)]

    membership = memberships[0]
    if payload.project_id:
        match = next(
            (m for m in memberships if m.project_id == payload.project_id), None
        )
        if match is None:
            raise HTTPException(status_code=404, detail="Project not found")
        membership = match

    user.last_seen_at = datetime.now(UTC)
    await db.commit()
    _set_cookie(
        response,
        create_session_token(
            user.id,
            membership.project_id,
            membership.company_id,
            user.session_version,
        ),
    )
    return await _user_out(db, user, membership)


@router.post("/logout")
async def logout(response: Response) -> dict[str, bool]:
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"success": True}


@router.get("/me", response_model=UserOut)
async def me(tenant: Tenant) -> UserOut:
    return UserOut(
        id=tenant.user.id,
        email=tenant.user.email,
        display_name=tenant.user.display_name,
        company_id=tenant.company_id,
        company_name=tenant.company.name,
        project_id=tenant.project_id,
        project_name=tenant.project.name,
        role=tenant.role.value,
        company_role=tenant.company_role.value,
    )


@router.get("/providers")
async def providers() -> dict[str, Any]:
    """What identity providers this deployment actually has configured."""
    return describe_providers()


# ---------------------------------------------------------------------------
# Companies and projects
# ---------------------------------------------------------------------------
@router.get("/companies")
async def list_companies(tenant: Tenant, db: DbSession) -> dict[str, Any]:
    memberships = await tenancy.company_memberships_for(db, tenant.user_id)
    out = []
    for m in memberships:
        company = await db.get(Company, m.company_id)
        if company is None or not company.is_active:
            continue
        out.append(
            {
                "id": company.id,
                "name": company.name,
                "slug": company.slug,
                "plan": company.plan.value,
                "role": m.role.value,
                "current": company.id == tenant.company_id,
            }
        )
    return {"count": len(out), "companies": out}


@router.get("/projects")
async def list_projects(tenant: Tenant, db: DbSession) -> dict[str, Any]:
    memberships = await tenancy.project_memberships_for(db, tenant.user_id)
    out = []
    for m in memberships:
        project = await db.get(Project, m.project_id)
        if project is None or not project.is_active:
            continue
        company = await db.get(Company, project.company_id)
        out.append(
            {
                "id": project.id,
                "name": project.name,
                "slug": project.slug,
                "description": project.description,
                "company_id": project.company_id,
                "company_name": company.name if company else "",
                "role": m.role.value,
                "current": project.id == tenant.project_id,
            }
        )
    return {"count": len(out), "projects": out}


class SwitchRequest(BaseModel):
    project_id: str


@router.post("/projects/switch", response_model=UserOut)
async def switch_project(
    payload: SwitchRequest, response: Response, tenant: Tenant, db: DbSession
) -> UserOut:
    membership = await tenancy.project_membership(
        db, user_id=tenant.user_id, project_id=payload.project_id
    )
    if membership is None:
        raise HTTPException(status_code=404, detail="Project not found")
    _set_cookie(
        response,
        create_session_token(
            tenant.user_id,
            membership.project_id,
            membership.company_id,
            tenant.user.session_version,
        ),
    )
    return await _user_out(db, tenant.user, membership)


class CreateProjectRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = ""


@router.post("/projects", status_code=201)
async def create_project(
    payload: CreateProjectRequest, tenant: Tenant, db: DbSession
) -> dict[str, Any]:
    """Create another project inside the caller's company.

    Restricted to company administrators: a project is a security boundary, and
    anyone who can mint one could otherwise mint themselves an unpoliced one.
    """
    tenant.require_company_admin()

    subscription = await tenancy.subscription_for(db, tenant.company_id)
    existing = await tenancy.projects_in_company(db, tenant.company_id)
    if len(existing) >= subscription.max_projects:
        raise HTTPException(
            status_code=402,
            detail=(
                f"The {subscription.plan.value.title()} plan includes "
                f"{subscription.max_projects} project(s). Upgrade to add more."
            ),
        )

    project, membership = await tenancy.create_project(
        db,
        company_id=tenant.company_id,
        name=payload.name,
        owner=tenant.user,
        description=payload.description,
    )
    await db.commit()
    return {
        "id": project.id,
        "name": project.name,
        "slug": project.slug,
        "company_id": project.company_id,
        "role": membership.role.value,
    }


# ---------------------------------------------------------------------------
# Project membership
# ---------------------------------------------------------------------------
class InviteRequest(BaseModel):
    email: EmailStr
    role: ProjectRole = ProjectRole.VIEWER
    display_name: str = ""


@router.post("/projects/members")
async def add_member(
    payload: InviteRequest, tenant: Tenant, db: DbSession
) -> dict[str, Any]:
    """Add (or re-role) a member of the caller's project."""
    tenant.require(ProjectRole.PROJECT_ADMIN)

    subscription = await tenancy.subscription_for(db, tenant.company_id)
    email = str(payload.email).strip().lower()
    user = (
        await db.execute(select(User).where(User.email == email))
    ).scalar_one_or_none()
    if user is None:
        user = User(email=email, display_name=payload.display_name or email.split("@")[0])
        db.add(user)
        await db.flush()

    membership = await tenancy.project_membership(
        db, user_id=user.id, project_id=tenant.project_id
    )
    if membership is None:
        current = (
            await db.execute(
                select(ProjectMembership.user_id).where(
                    ProjectMembership.company_id == tenant.company_id,
                    ProjectMembership.is_active.is_(True),
                )
            )
        ).scalars().all()
        if len(set(current)) >= subscription.max_users:
            raise HTTPException(
                status_code=402,
                detail=(
                    f"The {subscription.plan.value.title()} plan includes "
                    f"{subscription.max_users} users. Upgrade to add more."
                ),
            )
        membership = ProjectMembership(
            company_id=tenant.company_id,
            project_id=tenant.project_id,
            user_id=user.id,
            role=payload.role,
            invited_by=tenant.user_id,
        )
        db.add(membership)
    else:
        membership.role = payload.role
        membership.is_active = True

    if not await tenancy.company_membership(
        db, user_id=user.id, company_id=tenant.company_id
    ):
        from app.models import CompanyMembership

        db.add(
            CompanyMembership(
                company_id=tenant.company_id,
                user_id=user.id,
                role=CompanyRole.COMPANY_MEMBER,
            )
        )

    await db.commit()
    return {
        "success": True,
        "user_id": user.id,
        "email": user.email,
        "role": membership.role.value,
    }


@router.get("/projects/members")
async def list_members(tenant: Tenant, db: DbSession) -> dict[str, Any]:
    rows = (
        await db.execute(
            select(ProjectMembership).where(
                ProjectMembership.project_id == tenant.project_id,
                ProjectMembership.is_active.is_(True),
            )
        )
    ).scalars().all()
    members = []
    for m in rows:
        u = await db.get(User, m.user_id)
        if u is None:
            continue
        members.append(
            {
                "user_id": u.id,
                "email": u.email,
                "display_name": u.display_name,
                "role": m.role.value,
                "idp": u.idp_kind.value,
                "last_seen_at": u.last_seen_at.isoformat() if u.last_seen_at else None,
            }
        )
    return {"count": len(members), "members": members}


@router.delete("/projects/members/{user_id}")
async def remove_member(user_id: str, tenant: Tenant, db: DbSession) -> dict[str, Any]:
    tenant.require(ProjectRole.PROJECT_ADMIN)
    if user_id == tenant.user_id:
        raise HTTPException(
            status_code=400,
            detail=(
                "You cannot remove yourself from a project you administer. Ask "
                "another project admin to do it."
            ),
        )
    membership = await tenancy.project_membership(
        db, user_id=user_id, project_id=tenant.project_id
    )
    if membership is None:
        raise HTTPException(status_code=404, detail="Member not found")
    membership.is_active = False
    await db.commit()
    return {"success": True}


# ---------------------------------------------------------------------------
# Invitations
# ---------------------------------------------------------------------------
class InvitationRequest(BaseModel):
    email: EmailStr
    role: ProjectRole = ProjectRole.VIEWER
    ttl_hours: int = Field(default=168, ge=1, le=720)


@router.post("/invitations", status_code=201)
async def create_invitation(
    payload: InvitationRequest, tenant: Tenant, db: DbSession
) -> dict[str, Any]:
    """Issue an invitation. The token is shown once and stored only as a hash."""
    tenant.require(ProjectRole.PROJECT_ADMIN)
    invitation, token = await tenancy.create_invitation(
        db,
        company_id=tenant.company_id,
        project_id=tenant.project_id,
        email=str(payload.email),
        role=payload.role,
        invited_by=tenant.user_id,
        ttl_hours=payload.ttl_hours,
    )
    await db.commit()
    front = settings.frontend_base_url.rstrip("/")
    return {
        "id": invitation.id,
        "email": invitation.email,
        "role": invitation.role.value,
        "expires_at": invitation.expires_at.isoformat(),
        # Returned exactly once. It is never retrievable again from the API.
        "invite_url": f"{front}/invite/{token}",
    }


class RedeemRequest(BaseModel):
    token: str
    email: EmailStr
    display_name: str = ""


@router.post("/invitations/redeem", response_model=UserOut)
async def redeem_invitation(
    payload: RedeemRequest, response: Response, db: DbSession
) -> UserOut:
    user = await get_or_create_user(db, str(payload.email), payload.display_name)
    membership, error = await tenancy.redeem_invitation(db, payload.token, user)
    if membership is None:
        await db.rollback()
        raise HTTPException(status_code=400, detail=error)
    await db.commit()
    _set_cookie(
        response,
        create_session_token(
            user.id,
            membership.project_id,
            membership.company_id,
            user.session_version,
        ),
    )
    return await _user_out(db, user, membership)


# ---------------------------------------------------------------------------
# Session control
# ---------------------------------------------------------------------------
@router.post("/sessions/revoke")
async def revoke_my_sessions(
    response: Response, tenant: Tenant, db: DbSession
) -> dict[str, Any]:
    """Sign out everywhere, immediately.

    Not "expire the cookie" — every token ever minted for this user stops
    working on the next request, in every browser, including ones this session
    cannot reach.
    """
    version = await revoke_sessions(db, tenant.user)
    await db.commit()
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {
        "success": True,
        "session_version": version,
        "message": "All sessions for this account have been signed out.",
    }


@router.post("/projects/members/{user_id}/revoke-sessions")
async def revoke_member_sessions(
    user_id: str, tenant: Tenant, db: DbSession
) -> dict[str, Any]:
    """Cut off a member's access now, without waiting for a cookie to expire.

    Separate from removing them from the project: an administrator responding
    to a suspected compromise wants both, and wants the fast one first.
    """
    tenant.require(ProjectRole.PROJECT_ADMIN, ProjectRole.SECURITY_ADMIN)
    membership = await tenancy.project_membership(
        db, user_id=user_id, project_id=tenant.project_id
    )
    if membership is None:
        raise HTTPException(status_code=404, detail="Member not found")
    target = await db.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="Member not found")

    version = await revoke_sessions(db, target)
    db.add(
        AuditEvent(
            company_id=tenant.company_id,
            project_id=tenant.project_id,
            user_id=tenant.user_id,
            action="auth.sessions_revoked",
            arguments={"target_user_id": user_id, "session_version": version},
            outcome="ok",
        )
    )
    await db.commit()
    return {"success": True, "message": "Their sessions have been signed out."}


# ---------------------------------------------------------------------------
# OIDC single sign-on
# ---------------------------------------------------------------------------
@router.get("/oidc/start")
async def oidc_start(response: Response, db: DbSession) -> dict[str, str]:
    """Begin an OIDC login.

    The state, PKCE verifier and nonce are held in a short-lived signed cookie
    rather than in a table: they are single-use, they expire in minutes, and
    keeping them out of the database means a login attempt leaves no trace to
    clean up.
    """
    if not oidc_provider.configured:
        raise HTTPException(
            status_code=404,
            detail=(
                "OIDC single sign-on is not configured for this deployment. Set "
                "OIDC_ISSUER, OIDC_CLIENT_ID and OIDC_CLIENT_SECRET to enable it."
            ),
        )
    try:
        request = await oidc_provider.begin()
    except IdentityError as exc:
        raise HTTPException(status_code=502, detail=exc.message) from exc

    response.set_cookie(
        OIDC_COOKIE,
        jwt.encode(
            {
                "state": request.state,
                "verifier": request.code_verifier,
                "nonce": request.nonce,
                "exp": int(datetime.now(UTC).timestamp()) + 600,
            },
            settings.session_secret,
            algorithm="HS256",
        ),
        httponly=True,
        samesite="lax",
        secure=settings.environment not in {"local", "dev"},
        max_age=600,
        path="/",
    )
    return {"authorize_url": request.url}


@router.get("/oidc/callback")
async def oidc_callback(
    response: Response,
    db: DbSession,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    oidc_login: Annotated[str | None, Cookie(alias=OIDC_COOKIE)] = None,
) -> RedirectResponse:
    front = settings.frontend_base_url.rstrip("/")
    if error:
        return RedirectResponse(f"{front}/?auth_error={error}")
    if not code or not state or not oidc_login:
        return RedirectResponse(f"{front}/?auth_error=missing_state")

    try:
        pending = jwt.decode(oidc_login, settings.session_secret, algorithms=["HS256"])
    except JWTError:
        return RedirectResponse(f"{front}/?auth_error=expired_state")
    # Constant-time state comparison: this is the CSRF defence for the flow.
    if not hmac.compare_digest(str(pending.get("state") or ""), state):
        return RedirectResponse(f"{front}/?auth_error=state_mismatch")

    try:
        identity = await oidc_provider.complete(
            code=code,
            code_verifier=str(pending.get("verifier") or ""),
            nonce=str(pending.get("nonce") or ""),
        )
    except IdentityError as exc:
        log.warning("auth.oidc_failed", error=exc.message)
        return RedirectResponse(f"{front}/?auth_error=verification_failed")

    user = (
        await db.execute(select(User).where(User.email == identity.email))
    ).scalar_one_or_none()
    if user is None:
        user = User(
            email=identity.email,
            display_name=identity.display_name,
            idp_kind=IdentityProviderKind.OIDC,
            idp_subject=identity.subject,
        )
        db.add(user)
        await db.flush()
    else:
        user.idp_kind = IdentityProviderKind.OIDC
        user.idp_subject = identity.subject

    # The directory is authoritative on *every* login, not just the first. A
    # user removed from a group in the IdP is downgraded here on their next
    # sign-in; without this, de-provisioning would silently fail.
    config = await sso_mapping.config_for_email(db, identity.email)
    if config is not None:
        result = await sso_mapping.apply(db, user, config, identity.claims)
        db.add(
            AuditEvent(
                company_id=config.company_id,
                user_id=user.id,
                actor_type="system",
                action="auth.sso_login",
                # Group names, not claims: a raw id_token can carry a great deal
                # more about a person than an audit trail needs.
                arguments=result.to_dict(),
                outcome="ok",
            )
        )
    else:
        # No enterprise SSO claims this domain. Self-serve: the user lands in
        # their own workspace rather than inheriting somebody else's tenant.
        if not await tenancy.project_memberships_for(db, user.id):
            await tenancy.ensure_personal_workspace(db, user)

    memberships = await tenancy.project_memberships_for(db, user.id)
    if not memberships:
        # SSO recognised the domain but granted nothing this deployment knows
        # about. Refusing is correct — quietly creating a personal workspace
        # would hand an enterprise user an unmanaged tenant.
        await db.rollback()
        log.warning("auth.sso_no_membership", email_domain=identity.email.rpartition("@")[2])
        return RedirectResponse(f"{front}/?auth_error=no_access")
    user.last_seen_at = datetime.now(UTC)
    await db.commit()

    _set_cookie(
        response,
        create_session_token(
            user.id,
            memberships[0].project_id,
            memberships[0].company_id,
            user.session_version,
        ),
    )
    redirect = RedirectResponse(f"{front}/")
    redirect.headers["set-cookie"] = response.headers.get("set-cookie", "")
    redirect.delete_cookie(OIDC_COOKIE, path="/")
    return redirect
