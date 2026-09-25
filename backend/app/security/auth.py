"""Application identity, sessions and project-scoped authorization.

Scope note (explicit, not a mock): the local provider is a real, working
identity layer — signed JWT sessions over a users table, verified on every
request. Enterprise SSO is pluggable through `app.security.identity`; what is
and is not implemented there is stated in that module and in docs/security.md.

Authorization has three layers and all three are mandatory:

  * **Authentication** — who is calling (`current_user`).
  * **Company** — which paying customer they are acting for, and with what
    company-level authority (`CompanyRole`).
  * **Project** — which project they are acting in, and with what project role.
    **The project is the security boundary.** Every handler that can reach
    Salesforce takes a `TenantContext`, never a bare user, so a request can
    never read or write another project's rows.

`TenantContext.project_id` is the value every query must filter on. It is
derived from the session or the `X-Project-Id` header and validated against an
active membership — a caller cannot name a project they do not belong to, and
naming one they do not belong to yields 404, not 403, so ids cannot be probed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import Cookie, Depends, Header, HTTPException, status
from jose import JWTError, jwt
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_session
from app.models import (
    PROJECT_ROLE_RANK,
    Company,
    CompanyMembership,
    CompanyRole,
    Project,
    ProjectMembership,
    ProjectRole,
    User,
)
from app.tenancy import service as tenancy
from app.tenancy.policy import PolicySnapshot

ALGORITHM = "HS256"
SESSION_COOKIE = "sfagent_session"
PROJECT_HEADER = "X-Project-Id"
COMPANY_HEADER = "X-Company-Id"


def create_session_token(
    user_id: str,
    project_id: str | None = None,
    company_id: str | None = None,
    session_version: int = 1,
) -> str:
    """Mint a session.

    `sv` is the user's session version at the moment of minting. Bumping the
    version on the user row invalidates every token ever issued to them, in
    every browser, immediately — which is what makes "revoke access now" a fact
    rather than a promise about cookie expiry.
    """
    now = datetime.now(UTC)
    payload: dict[str, object] = {
        "sub": user_id,
        "sv": int(session_version),
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=settings.session_ttl_seconds)).timestamp()),
        "iss": "sfagent",
    }
    if project_id:
        payload["prj"] = project_id
    if company_id:
        payload["co"] = company_id
    return jwt.encode(payload, settings.session_secret, algorithm=ALGORITHM)


def decode_session(token: str) -> dict[str, str]:
    try:
        payload = jwt.decode(token, settings.session_secret, algorithms=[ALGORITHM])
    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid session"
        ) from exc
    sub = payload.get("sub")
    if not sub:
        raise HTTPException(status_code=401, detail="Invalid session")
    return {
        "sub": str(sub),
        "prj": str(payload.get("prj") or ""),
        "co": str(payload.get("co") or ""),
        # Absent on tokens minted before session versioning; treated as 1 so an
        # in-flight session is not invalidated by the upgrade itself.
        "sv": str(payload.get("sv") or 1),
    }


def decode_session_token(token: str) -> str:
    """Back-compatible helper: returns just the user id."""
    return decode_session(token)["sub"]


async def get_or_create_user(session: AsyncSession, email: str, name: str = "") -> User:
    from sqlalchemy import select

    email = email.strip().lower()
    result = await session.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()
    if user is None:
        user = User(email=email, display_name=name or email.split("@")[0])
        session.add(user)
        await session.flush()
        await tenancy.ensure_personal_workspace(session, user)
    return user


def _bearer(authorization: str | None) -> str | None:
    if authorization and authorization.lower().startswith("bearer "):
        return authorization.split(" ", 1)[1].strip()
    return None


async def current_user(
    session: Annotated[AsyncSession, Depends(get_session)],
    sfagent_session: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    authorization: Annotated[str | None, Header()] = None,
) -> User:
    token = sfagent_session or _bearer(authorization)
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    claims = decode_session(token)
    user = await session.get(User, claims["sub"])
    _require_live_session(user, claims)
    return user


def _require_live_session(user: User | None, claims: dict[str, str]) -> User:
    """Reject a session whose user is gone, disabled, or has been revoked.

    All three answer with the same message. Telling an attacker whether an
    account exists, is disabled, or merely had its sessions revoked is three
    pieces of information they did not have.
    """
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="Session is no longer valid")
    if int(claims.get("sv") or 1) != int(user.session_version or 1):
        raise HTTPException(status_code=401, detail="Session is no longer valid")
    return user


async def revoke_sessions(session: AsyncSession, user: User) -> int:
    """Invalidate every session this user holds. Returns the new version."""
    user.session_version = int(user.session_version or 1) + 1
    await session.flush()
    return user.session_version


@dataclass
class TenantContext:
    """The authenticated caller, resolved into one project of one company."""

    user: User
    company: Company
    project: Project
    membership: ProjectMembership
    company_membership: CompanyMembership | None
    policy: PolicySnapshot

    # -- isolation keys ---------------------------------------------------
    @property
    def project_id(self) -> str:
        return self.project.id

    @property
    def company_id(self) -> str:
        return self.company.id

    @property
    def user_id(self) -> str:
        return self.user.id

    # -- roles ------------------------------------------------------------
    @property
    def role(self) -> ProjectRole:
        return self.membership.role

    @property
    def company_role(self) -> CompanyRole:
        return (
            self.company_membership.role
            if self.company_membership
            else CompanyRole.COMPANY_MEMBER
        )

    @property
    def is_company_admin(self) -> bool:
        return self.company_role in (
            CompanyRole.COMPANY_ADMIN,
            CompanyRole.PLATFORM_OWNER,
        )

    def has_role(self, *roles: ProjectRole) -> bool:
        return self.membership.role in roles

    def at_least(self, role: ProjectRole) -> bool:
        return PROJECT_ROLE_RANK.get(self.membership.role, 0) >= PROJECT_ROLE_RANK.get(
            role, 99
        )

    def require(self, *roles: ProjectRole) -> None:
        if self.membership.role not in roles:
            raise HTTPException(
                status_code=403,
                detail=(
                    f"This action requires one of: {', '.join(r.value for r in roles)}. "
                    f"Your role in {self.project.name} is {self.membership.role.value}."
                ),
            )

    def require_at_least(self, role: ProjectRole) -> None:
        if not self.at_least(role):
            raise HTTPException(
                status_code=403,
                detail=(
                    f"This action requires at least the {role.value} role; "
                    f"your role is {self.membership.role.value}."
                ),
            )

    def require_company_admin(self) -> None:
        if not self.is_company_admin:
            raise HTTPException(
                status_code=403,
                detail=(
                    "This action is restricted to company administrators of "
                    f"{self.company.name}."
                ),
            )


async def _resolve_membership(
    session: AsyncSession, user: User, requested_project: str
) -> ProjectMembership:
    if requested_project:
        membership = await tenancy.project_membership(
            session, user_id=user.id, project_id=requested_project
        )
        if membership is None:
            # Do not disclose whether the project exists.
            raise HTTPException(status_code=404, detail="Project not found")
        return membership

    candidates = await tenancy.project_memberships_for(session, user.id)
    if candidates:
        return candidates[0]
    membership = await tenancy.ensure_personal_workspace(session, user)
    await session.commit()
    return membership


async def tenant_context(
    session: Annotated[AsyncSession, Depends(get_session)],
    sfagent_session: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    authorization: Annotated[str | None, Header()] = None,
    x_project_id: Annotated[str | None, Header(alias=PROJECT_HEADER)] = None,
) -> TenantContext:
    token = sfagent_session or _bearer(authorization)
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    claims = decode_session(token)
    user = await session.get(User, claims["sub"])
    _require_live_session(user, claims)

    membership = await _resolve_membership(
        session, user, x_project_id or claims.get("prj") or ""
    )

    project = await session.get(Project, membership.project_id)
    if project is None or not project.is_active:
        raise HTTPException(status_code=403, detail="Project is inactive")

    company = await session.get(Company, project.company_id)
    if company is None or not company.is_active:
        raise HTTPException(status_code=403, detail="Company is inactive")

    company_membership = await tenancy.company_membership(
        session, user_id=user.id, company_id=company.id
    )

    policy = await tenancy.policy_for(session, project.id, company.id)
    return TenantContext(
        user=user,
        company=company,
        project=project,
        membership=membership,
        company_membership=company_membership,
        policy=policy,
    )


CurrentUser = Annotated[User, Depends(current_user)]
Tenant = Annotated[TenantContext, Depends(tenant_context)]
DbSession = Annotated[AsyncSession, Depends(get_session)]
