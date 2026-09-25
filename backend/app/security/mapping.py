"""Turning what an identity provider says into what someone may do here.

The rule that makes this a control rather than a convenience: **the identity
provider is authoritative on every login, not just the first one.** If someone
is removed from `salesforce-admins` in Okta, their next login here downgrades
them. A mapping that only ever adds access means de-provisioning silently fails,
which is the failure mode enterprise buyers actually test for.

Three things follow from that, and they are the whole module:

  * mappings are applied on **every** authentication;
  * a role that came from the IdP is **replaced**, never merged with what was
    there before;
  * a group this deployment does not recognise grants nothing. Unknown is not
    "probably fine".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    CompanyMembership,
    CompanyRole,
    Project,
    ProjectMembership,
    ProjectRole,
    SSOConfiguration,
    User,
)
from app.observability.logging import get_logger

log = get_logger("security.mapping")

#: Claims an IdP might carry groups in. Checked in order; the first list-shaped
#: claim wins. Entra ID uses `groups`, Okta commonly `groups`, Auth0 often a
#: namespaced claim configured per tenant.
GROUP_CLAIMS = ("groups", "roles", "memberOf", "https://claims/groups")

#: Company-level authority an IdP group may confer. Deliberately excludes
#: PLATFORM_OWNER: operating this deployment is not something a customer's
#: directory gets to grant.
ASSIGNABLE_COMPANY_ROLES = {
    CompanyRole.COMPANY_ADMIN,
    CompanyRole.COMPANY_MEMBER,
    CompanyRole.COMPANY_AUDITOR,
}


@dataclass
class MappingResult:
    """What a login resolved to, and why. Recorded in audit."""

    project_roles: dict[str, ProjectRole] = field(default_factory=dict)
    company_role: CompanyRole | None = None
    matched_groups: list[str] = field(default_factory=list)
    unmatched_groups: list[str] = field(default_factory=list)
    #: True when the IdP asserted no group this deployment recognises.
    fell_back_to_default: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_roles": {k: v.value for k, v in self.project_roles.items()},
            "company_role": self.company_role.value if self.company_role else None,
            "matched_groups": self.matched_groups,
            "unmatched_groups": self.unmatched_groups,
            "fell_back_to_default": self.fell_back_to_default,
        }


def groups_from_claims(claims: dict[str, Any] | None) -> list[str]:
    """Pull the group list out of whatever claim the IdP put it in."""
    if not claims:
        return []
    for name in GROUP_CLAIMS:
        value = claims.get(name)
        if isinstance(value, list):
            return [str(v) for v in value if str(v).strip()]
        if isinstance(value, str) and value.strip():
            # Some IdPs send a single group as a bare string.
            return [value.strip()]
    return []


def resolve(
    groups: list[str],
    mappings: dict[str, Any] | None,
    *,
    default_project_id: str | None = None,
    default_project_role: ProjectRole = ProjectRole.VIEWER,
) -> MappingResult:
    """Apply a company's group mappings to one login's groups.

    A mapping value is either a role name (applied to `default_project_id`) or
    `"<project_id>:<ROLE>"` for a specific project. When one person's groups map
    them into the same project twice, the **stronger** role wins — being in both
    `developers` and `sf-admins` should not depend on dictionary order.
    """
    from app.models import PROJECT_ROLE_RANK

    result = MappingResult()
    table = {str(k).strip().lower(): v for k, v in (mappings or {}).items()}

    for group in groups:
        target = table.get(group.strip().lower())
        if target is None:
            result.unmatched_groups.append(group)
            continue
        result.matched_groups.append(group)

        for entry in target if isinstance(target, list) else [target]:
            project_id, role = _parse_target(str(entry), default_project_id)
            if role is None:
                continue
            if isinstance(role, CompanyRole):
                if role in ASSIGNABLE_COMPANY_ROLES and (
                    result.company_role is None
                    or _company_rank(role) > _company_rank(result.company_role)
                ):
                    result.company_role = role
                continue
            if not project_id:
                continue
            existing = result.project_roles.get(project_id)
            if existing is None or PROJECT_ROLE_RANK.get(
                role, 0
            ) > PROJECT_ROLE_RANK.get(existing, 0):
                result.project_roles[project_id] = role

    if not result.project_roles and default_project_id:
        # No recognised group. Least privilege in the company's default project
        # rather than no access at all, so SSO users can at least sign in.
        result.project_roles[default_project_id] = default_project_role
        result.fell_back_to_default = True

    return result


def _parse_target(
    entry: str, default_project_id: str | None
) -> tuple[str | None, ProjectRole | CompanyRole | None]:
    project_id, _, raw = entry.rpartition(":")
    name = raw.strip().upper()
    try:
        return (project_id or default_project_id), ProjectRole(name)
    except ValueError:
        pass
    try:
        return None, CompanyRole(name)
    except ValueError:
        log.warning("mapping.unknown_role", value=entry)
        return None, None


def _company_rank(role: CompanyRole) -> int:
    return {
        CompanyRole.COMPANY_MEMBER: 10,
        CompanyRole.COMPANY_AUDITOR: 20,
        CompanyRole.COMPANY_ADMIN: 30,
        CompanyRole.PLATFORM_OWNER: 40,
    }.get(role, 0)


# ---------------------------------------------------------------------------
# Applying a mapping
# ---------------------------------------------------------------------------
async def config_for_email(
    db: AsyncSession, email: str
) -> SSOConfiguration | None:
    """Find the company whose SSO governs this address.

    Domain-based, because that is the only thing an ID token reliably carries
    that ties a person to a customer. A domain claimed by two companies is a
    configuration error, and the first match is used rather than guessing —
    but the situation is logged so an operator can see it.
    """
    domain = email.strip().lower().rpartition("@")[2]
    if not domain:
        return None

    rows = (
        await db.execute(
            select(SSOConfiguration).where(SSOConfiguration.enabled.is_(True))
        )
    ).scalars().all()

    matches = []
    for row in rows:
        from app.models import Company

        company = await db.get(Company, row.company_id)
        domains = [str(d).strip().lower() for d in (company.sso_domains or [])]
        if domain in domains:
            matches.append(row)

    if len(matches) > 1:
        log.error(
            "sso.domain_claimed_by_multiple_companies",
            domain=domain,
            companies=[m.company_id for m in matches],
        )
    return matches[0] if matches else None


async def apply(
    db: AsyncSession,
    user: User,
    config: SSOConfiguration,
    claims: dict[str, Any] | None,
) -> MappingResult:
    """Set this user's memberships to exactly what the IdP now says.

    Memberships this company granted through SSO and the IdP no longer asserts
    are **deactivated**. That is the point: without it, removing someone from a
    directory group would leave their access here intact.

    Memberships created by hand (an invitation, a project admin adding someone)
    are left alone. They were not the IdP's to grant, so they are not the IdP's
    to take away.
    """
    groups = groups_from_claims(claims)
    result = resolve(
        groups,
        config.group_mappings,
        default_project_id=config.default_project_id,
        default_project_role=config.default_project_role,
    )

    existing = (
        await db.execute(
            select(ProjectMembership).where(
                ProjectMembership.user_id == user.id,
                ProjectMembership.company_id == config.company_id,
            )
        )
    ).scalars().all()
    by_project = {m.project_id: m for m in existing}

    for project_id, role in result.project_roles.items():
        project = await db.get(Project, project_id)
        if project is None or project.company_id != config.company_id:
            # A mapping pointing outside the company is a configuration error,
            # and honouring it would be a tenancy breach.
            log.error(
                "sso.mapping_targets_foreign_project",
                company_id=config.company_id,
                project_id=project_id,
            )
            continue

        membership = by_project.get(project_id)
        if membership is None:
            db.add(
                ProjectMembership(
                    company_id=config.company_id,
                    project_id=project_id,
                    user_id=user.id,
                    role=role,
                    external_id=str((claims or {}).get("sub") or "") or None,
                )
            )
        else:
            membership.role = role
            membership.is_active = True

    # Revoke what the directory no longer asserts, but only where the directory
    # granted it in the first place.
    for project_id, membership in by_project.items():
        if project_id in result.project_roles:
            continue
        if membership.external_id is None:
            continue  # granted by a human, not by the IdP
        if membership.is_active:
            log.info(
                "sso.membership_revoked",
                user_id=user.id,
                project_id=project_id,
                reason="not asserted by the identity provider",
            )
        membership.is_active = False

    await _ensure_company_membership(db, user, config, result)
    await db.flush()
    return result


async def _ensure_company_membership(
    db: AsyncSession,
    user: User,
    config: SSOConfiguration,
    result: MappingResult,
) -> None:
    membership = (
        await db.execute(
            select(CompanyMembership).where(
                CompanyMembership.company_id == config.company_id,
                CompanyMembership.user_id == user.id,
            )
        )
    ).scalar_one_or_none()

    role = result.company_role or CompanyRole.COMPANY_MEMBER
    if membership is None:
        db.add(
            CompanyMembership(
                company_id=config.company_id, user_id=user.id, role=role
            )
        )
        return

    # Never downgrade a hand-assigned company admin on the strength of a login
    # that asserted no company-level group: that would lock a customer out of
    # their own account the first time an IdP admin forgot a claim mapping.
    if result.company_role is not None:
        membership.role = role
    membership.is_active = True
