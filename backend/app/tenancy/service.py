"""Company and project resolution, bootstrap, and scoped access.

Every read or write that can reach a customer's Salesforce org, repository or
issue tracker goes through `owned()` or `scoped()` here. The rule is one line
long: **a query is only correct if it filters on `project_id`.** `company_id`
narrows to a customer; `user_id` narrows ownership inside a project; neither is
an isolation boundary on its own.

`owned()` returns None for a row belonging to another project rather than the
row. Callers turn that into 404 — never 403 — so an id guessed or leaked from
another tenant does not even confirm that it exists.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any, TypeVar

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Company,
    CompanyMembership,
    CompanyRole,
    Environment,
    Invitation,
    PlanTier,
    Project,
    ProjectMembership,
    ProjectPolicy,
    ProjectRole,
    Subscription,
    User,
)
from app.tenancy.policy import PolicySnapshot, snapshot_from

T = TypeVar("T")

_SLUG_RE = re.compile(r"[^a-z0-9]+")

#: Entitlements per plan. Enforced; payment is not implemented.
PLAN_ENTITLEMENTS: dict[PlanTier, dict[str, Any]] = {
    PlanTier.TRIAL: {
        "max_projects": 1,
        "max_users": 3,
        "max_salesforce_connections": 1,
        "monthly_run_allowance": 100,
        "features": {"sso": False, "scim": False, "byok": True, "production": False},
    },
    PlanTier.STARTER: {
        "max_projects": 1,
        "max_users": 5,
        "max_salesforce_connections": 2,
        "monthly_run_allowance": 1_000,
        "features": {"sso": False, "scim": False, "byok": True, "production": False},
    },
    PlanTier.BUSINESS: {
        "max_projects": 5,
        "max_users": 25,
        "max_salesforce_connections": 10,
        "monthly_run_allowance": 10_000,
        "features": {"sso": True, "scim": False, "byok": True, "production": True},
    },
    PlanTier.ENTERPRISE: {
        "max_projects": 100,
        "max_users": 5_000,
        "max_salesforce_connections": 200,
        "monthly_run_allowance": 1_000_000,
        "features": {"sso": True, "scim": True, "byok": True, "production": True},
    },
}


def slugify(value: str) -> str:
    slug = _SLUG_RE.sub("-", (value or "").strip().lower()).strip("-")
    return slug or "workspace"


async def unique_company_slug(db: AsyncSession, base: str) -> str:
    slug = slugify(base)
    candidate, n = slug, 1
    while (
        await db.execute(select(Company.id).where(Company.slug == candidate))
    ).scalar_one_or_none() is not None:
        n += 1
        candidate = f"{slug}-{n}"
    return candidate


async def unique_project_slug(db: AsyncSession, company_id: str, base: str) -> str:
    slug = slugify(base)
    candidate, n = slug, 1
    while (
        await db.execute(
            select(Project.id).where(
                Project.company_id == company_id, Project.slug == candidate
            )
        )
    ).scalar_one_or_none() is not None:
        n += 1
        candidate = f"{slug}-{n}"
    return candidate


# ---------------------------------------------------------------------------
# Creation
# ---------------------------------------------------------------------------
async def create_company(
    db: AsyncSession,
    *,
    name: str,
    owner: User,
    plan: PlanTier = PlanTier.TRIAL,
    role: CompanyRole = CompanyRole.COMPANY_ADMIN,
) -> tuple[Company, CompanyMembership]:
    company = Company(name=name, slug=await unique_company_slug(db, name), plan=plan)
    db.add(company)
    await db.flush()

    membership = CompanyMembership(company_id=company.id, user_id=owner.id, role=role)
    db.add(membership)

    entitlements = PLAN_ENTITLEMENTS[plan]
    db.add(
        Subscription(
            company_id=company.id,
            plan=plan,
            max_projects=entitlements["max_projects"],
            max_users=entitlements["max_users"],
            max_salesforce_connections=entitlements["max_salesforce_connections"],
            monthly_run_allowance=entitlements["monthly_run_allowance"],
            features=entitlements["features"],
        )
    )
    await db.flush()
    return company, membership


async def create_project(
    db: AsyncSession,
    *,
    company_id: str,
    name: str,
    owner: User,
    description: str = "",
    role: ProjectRole = ProjectRole.PROJECT_ADMIN,
) -> tuple[Project, ProjectMembership]:
    project = Project(
        company_id=company_id,
        name=name,
        slug=await unique_project_slug(db, company_id, name),
        description=description,
        promotion_path=[e.value for e in Environment],
    )
    db.add(project)
    await db.flush()

    membership = ProjectMembership(
        company_id=company_id, project_id=project.id, user_id=owner.id, role=role
    )
    db.add(membership)
    db.add(ProjectPolicy(company_id=company_id, project_id=project.id))
    await db.flush()
    return project, membership


async def ensure_personal_workspace(db: AsyncSession, user: User) -> ProjectMembership:
    """Give a freshly created user somewhere to land.

    Self-serve signup has to arrive somewhere: a company they administer, with
    one project. Enterprise tenants are created explicitly and users are
    invited or provisioned into them.
    """
    existing = await project_memberships_for(db, user.id)
    if existing:
        return existing[0]

    label = user.display_name or user.email.split("@")[0]
    company, _ = await create_company(db, name=f"{label}'s workspace", owner=user)
    _, membership = await create_project(
        db, company_id=company.id, name="Default project", owner=user
    )
    return membership


# ---------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------
async def company_memberships_for(db: AsyncSession, user_id: str) -> list[CompanyMembership]:
    rows = (
        await db.execute(
            select(CompanyMembership)
            .where(
                CompanyMembership.user_id == user_id,
                CompanyMembership.is_active.is_(True),
            )
            .order_by(CompanyMembership.created_at)
        )
    ).scalars().all()
    return list(rows)


async def project_memberships_for(
    db: AsyncSession, user_id: str, company_id: str | None = None
) -> list[ProjectMembership]:
    stmt = select(ProjectMembership).where(
        ProjectMembership.user_id == user_id,
        ProjectMembership.is_active.is_(True),
    )
    if company_id:
        stmt = stmt.where(ProjectMembership.company_id == company_id)
    rows = (
        await db.execute(stmt.order_by(ProjectMembership.created_at))
    ).scalars().all()
    return list(rows)


async def project_membership(
    db: AsyncSession, *, user_id: str, project_id: str
) -> ProjectMembership | None:
    return (
        await db.execute(
            select(ProjectMembership).where(
                ProjectMembership.user_id == user_id,
                ProjectMembership.project_id == project_id,
                ProjectMembership.is_active.is_(True),
            )
        )
    ).scalar_one_or_none()


async def company_membership(
    db: AsyncSession, *, user_id: str, company_id: str
) -> CompanyMembership | None:
    return (
        await db.execute(
            select(CompanyMembership).where(
                CompanyMembership.user_id == user_id,
                CompanyMembership.company_id == company_id,
                CompanyMembership.is_active.is_(True),
            )
        )
    ).scalar_one_or_none()


async def projects_in_company(db: AsyncSession, company_id: str) -> list[Project]:
    rows = (
        await db.execute(
            select(Project)
            .where(Project.company_id == company_id, Project.is_active.is_(True))
            .order_by(Project.created_at)
        )
    ).scalars().all()
    return list(rows)


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------
async def policy_for(
    db: AsyncSession, project_id: str, company_id: str = ""
) -> PolicySnapshot:
    row = (
        await db.execute(
            select(ProjectPolicy).where(ProjectPolicy.project_id == project_id)
        )
    ).scalar_one_or_none()
    # The company's own production clearance is one of the three levels the
    # snapshot resolves, so it has to be read here rather than assumed.
    company_allows = False
    if company_id:
        company = await db.get(Company, company_id)
        company_allows = bool(company and company.allow_production_mutations)
    return snapshot_from(
        row, project_id, company_id, company_allows_production=company_allows
    )


async def policy_row(
    db: AsyncSession, project_id: str, company_id: str = ""
) -> ProjectPolicy:
    row = (
        await db.execute(
            select(ProjectPolicy).where(ProjectPolicy.project_id == project_id)
        )
    ).scalar_one_or_none()
    if row is None:
        row = ProjectPolicy(company_id=company_id, project_id=project_id)
        db.add(row)
        await db.flush()
    return row


async def subscription_for(db: AsyncSession, company_id: str) -> Subscription:
    row = (
        await db.execute(
            select(Subscription).where(Subscription.company_id == company_id)
        )
    ).scalar_one_or_none()
    if row is None:
        entitlements = PLAN_ENTITLEMENTS[PlanTier.TRIAL]
        row = Subscription(
            company_id=company_id,
            plan=PlanTier.TRIAL,
            max_projects=entitlements["max_projects"],
            max_users=entitlements["max_users"],
            max_salesforce_connections=entitlements["max_salesforce_connections"],
            monthly_run_allowance=entitlements["monthly_run_allowance"],
            features=entitlements["features"],
        )
        db.add(row)
        await db.flush()
    return row


async def runs_used_this_period(db: AsyncSession, company_id: str) -> int:
    """How many runs this company has started in the current billing period."""
    from sqlalchemy import func

    from app.models import AgentRun

    subscription = await subscription_for(db, company_id)
    start = subscription.current_period_start
    if start.tzinfo is None:
        start = start.replace(tzinfo=UTC)
    return int(
        (
            await db.execute(
                select(func.count(AgentRun.id)).where(
                    AgentRun.company_id == company_id,
                    AgentRun.created_at >= start,
                )
            )
        ).scalar()
        or 0
    )


async def check_run_allowance(db: AsyncSession, company_id: str) -> str:
    """Why this company cannot start another run, or "" if it can.

    Enforced at run creation rather than at execution: refusing after a worker
    has already spent a model call and a Salesforce round-trip would charge for
    the thing being refused.
    """
    subscription = await subscription_for(db, company_id)
    allowance = int(subscription.monthly_run_allowance or 0)
    if allowance <= 0:
        return ""
    used = await runs_used_this_period(db, company_id)
    if used < allowance:
        return ""
    return (
        f"This company has used all {allowance:,} runs included in the "
        f"{subscription.plan.value.title()} plan for the current period. "
        "Upgrade the plan, or wait for the period to reset."
    )


# ---------------------------------------------------------------------------
# Scoped access — the isolation primitives
# ---------------------------------------------------------------------------
def scoped(stmt: Select[Any], model: Any, project_id: str) -> Select[Any]:
    """Apply the project filter to a select. Use this, not a hand-written where."""
    return stmt.where(model.project_id == project_id)


def company_scoped(stmt: Select[Any], model: Any, company_id: str) -> Select[Any]:
    """Filter to a company. Only for genuinely company-wide views (audit, billing)."""
    return stmt.where(model.company_id == company_id)


async def owned(
    db: AsyncSession, model: type[T], row_id: str, project_id: str
) -> T | None:
    """Fetch a row by id *only* if it belongs to this project.

    Returning None for a foreign row — rather than the row — is what stops an
    id guessed or leaked from another project being usable here.
    """
    if not row_id:
        return None
    row = await db.get(model, row_id)
    if row is None:
        return None
    if getattr(row, "project_id", None) != project_id:
        return None
    return row


async def owned_by_company(
    db: AsyncSession, model: type[T], row_id: str, company_id: str
) -> T | None:
    """Company-scoped fetch, for rows that legitimately span projects."""
    if not row_id:
        return None
    row = await db.get(model, row_id)
    if row is None:
        return None
    if getattr(row, "company_id", None) != company_id:
        return None
    return row


# ---------------------------------------------------------------------------
# Invitations
# ---------------------------------------------------------------------------
def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def create_invitation(
    db: AsyncSession,
    *,
    company_id: str,
    project_id: str,
    email: str,
    role: ProjectRole,
    invited_by: str,
    ttl_hours: int = 168,
) -> tuple[Invitation, str]:
    """Create an invitation. The raw token is returned once and never stored."""
    token = secrets.token_urlsafe(32)
    invitation = Invitation(
        company_id=company_id,
        project_id=project_id,
        email=email.strip().lower(),
        role=role,
        token_hash=_hash_token(token),
        invited_by=invited_by,
        expires_at=datetime.now(UTC) + timedelta(hours=ttl_hours),
    )
    db.add(invitation)
    await db.flush()
    return invitation, token


async def redeem_invitation(
    db: AsyncSession, token: str, user: User
) -> tuple[ProjectMembership | None, str]:
    """Accept an invitation. Returns (membership, error_message)."""
    row = (
        await db.execute(
            select(Invitation).where(Invitation.token_hash == _hash_token(token))
        )
    ).scalar_one_or_none()
    if row is None:
        return None, "This invitation link is not valid."
    if row.revoked_at is not None:
        return None, "This invitation has been revoked."
    if row.accepted_at is not None:
        return None, "This invitation has already been used."

    expiry = row.expires_at
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=UTC)
    if expiry <= datetime.now(UTC):
        return None, "This invitation has expired. Ask for a new one."
    if row.email.lower() != user.email.lower():
        # Do not reveal who it was for.
        return None, "This invitation was issued for a different email address."

    if not await company_membership(db, user_id=user.id, company_id=row.company_id):
        db.add(
            CompanyMembership(
                company_id=row.company_id,
                user_id=user.id,
                role=CompanyRole.COMPANY_MEMBER,
            )
        )

    membership = await project_membership(db, user_id=user.id, project_id=row.project_id)
    if membership is None:
        membership = ProjectMembership(
            company_id=row.company_id,
            project_id=row.project_id,
            user_id=user.id,
            role=row.role,
            invited_by=row.invited_by,
        )
        db.add(membership)
    else:
        membership.role = row.role
        membership.is_active = True

    row.accepted_at = datetime.now(UTC)
    await db.flush()
    return membership, ""
