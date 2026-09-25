"""Deterministic approval policy: who must approve what, for how long.

This module answers four questions, and nothing else does:

  1. How many humans must approve a proposed change, and in which roles?
  2. How long does that approval stay valid?
  3. Is a given human eligible to cast one of those approvals?
  4. When policy blocks something, what may the user do instead?

None of it is reachable by the model. Claude proposes; this decides.

The fourth question matters more than it looks. A platform that answers a
blocked action with "permission denied" teaches people to route around it. One
that answers with "not in production, but here is the sandbox path" keeps them
inside the guard rails.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from app.config import settings
from app.models import Environment, ProjectMembership, ProjectPolicy, ProjectRole

# ---------------------------------------------------------------------------
# Change categories. A tool declares its category through its tags, so a new
# tool inherits the right approver policy without anyone remembering to
# configure it.
# ---------------------------------------------------------------------------
CATEGORY_DATA = "data"
CATEGORY_METADATA = "metadata"
CATEGORY_AUTOMATION = "automation"
CATEGORY_APEX = "apex"
CATEGORY_SECURITY = "security"
CATEGORY_DEPLOYMENT = "deployment"
CATEGORY_BULK = "bulk"
CATEGORY_EXTERNAL = "external"
CATEGORY_REPOSITORY = "repository"

ALL_CATEGORIES = (
    CATEGORY_DATA,
    CATEGORY_METADATA,
    CATEGORY_AUTOMATION,
    CATEGORY_APEX,
    CATEGORY_SECURITY,
    CATEGORY_DEPLOYMENT,
    CATEGORY_BULK,
    CATEGORY_EXTERNAL,
    CATEGORY_REPOSITORY,
)

_ADMINS = [ProjectRole.PROJECT_ADMIN.value, ProjectRole.SALESFORCE_ADMIN.value]
_RELEASE = [
    ProjectRole.PROJECT_ADMIN.value,
    ProjectRole.SALESFORCE_ADMIN.value,
    ProjectRole.RELEASE_MANAGER.value,
]
_BUILDERS = [
    ProjectRole.PROJECT_ADMIN.value,
    ProjectRole.SALESFORCE_ADMIN.value,
    ProjectRole.DEVELOPER.value,
]
_SECURITY = [ProjectRole.PROJECT_ADMIN.value, ProjectRole.SECURITY_ADMIN.value]

#: category -> risk -> {count, roles, ttl_seconds}
#: `roles` lists who may approve. `count` is how many *distinct* humans must.
DEFAULT_APPROVER_MATRIX: dict[str, dict[str, dict[str, Any]]] = {
    CATEGORY_DATA: {
        "MEDIUM": {"count": 1, "roles": [*_BUILDERS, ProjectRole.USER.value], "ttl_seconds": 3600},
        "HIGH": {"count": 1, "roles": _ADMINS, "ttl_seconds": 1800},
        "CRITICAL": {"count": 2, "roles": _ADMINS, "ttl_seconds": 900},
    },
    CATEGORY_METADATA: {
        "MEDIUM": {"count": 1, "roles": _BUILDERS, "ttl_seconds": 3600},
        "HIGH": {"count": 1, "roles": _ADMINS, "ttl_seconds": 1800},
        "CRITICAL": {"count": 2, "roles": _ADMINS, "ttl_seconds": 900},
    },
    CATEGORY_AUTOMATION: {
        "MEDIUM": {"count": 1, "roles": _BUILDERS, "ttl_seconds": 3600},
        "HIGH": {"count": 1, "roles": _ADMINS, "ttl_seconds": 1800},
        "CRITICAL": {"count": 2, "roles": _ADMINS, "ttl_seconds": 900},
    },
    CATEGORY_APEX: {
        "MEDIUM": {"count": 1, "roles": _BUILDERS, "ttl_seconds": 3600},
        "HIGH": {"count": 1, "roles": _ADMINS, "ttl_seconds": 1800},
        "CRITICAL": {"count": 2, "roles": _ADMINS, "ttl_seconds": 900},
    },
    CATEGORY_BULK: {
        "MEDIUM": {"count": 1, "roles": _BUILDERS, "ttl_seconds": 1800},
        "HIGH": {"count": 1, "roles": _ADMINS, "ttl_seconds": 900},
        "CRITICAL": {"count": 2, "roles": _ADMINS, "ttl_seconds": 600},
    },
    CATEGORY_SECURITY: {
        # Security posture is never a one-person job above MEDIUM.
        "MEDIUM": {"count": 1, "roles": _SECURITY, "ttl_seconds": 1800},
        "HIGH": {"count": 2, "roles": [*_SECURITY, ProjectRole.SALESFORCE_ADMIN.value],
                 "ttl_seconds": 900},
        "CRITICAL": {"count": 2, "roles": _SECURITY, "ttl_seconds": 600},
    },
    CATEGORY_DEPLOYMENT: {
        "MEDIUM": {"count": 1, "roles": _RELEASE, "ttl_seconds": 3600},
        "HIGH": {"count": 2, "roles": _RELEASE, "ttl_seconds": 900},
        "CRITICAL": {"count": 2, "roles": _RELEASE, "ttl_seconds": 600},
    },
    CATEGORY_REPOSITORY: {
        "MEDIUM": {"count": 1, "roles": _BUILDERS, "ttl_seconds": 3600},
        "HIGH": {"count": 1, "roles": _RELEASE, "ttl_seconds": 1800},
        "CRITICAL": {"count": 2, "roles": _RELEASE, "ttl_seconds": 900},
    },
    CATEGORY_EXTERNAL: {
        "MEDIUM": {"count": 1, "roles": _BUILDERS, "ttl_seconds": 3600},
        "HIGH": {"count": 1, "roles": _ADMINS, "ttl_seconds": 1800},
        "CRITICAL": {"count": 2, "roles": _ADMINS, "ttl_seconds": 900},
    },
}

FALLBACK_REQUIREMENT: dict[str, dict[str, Any]] = {
    "MEDIUM": {"count": 1, "roles": _BUILDERS, "ttl_seconds": 3600},
    "HIGH": {"count": 1, "roles": _ADMINS, "ttl_seconds": 1800},
    "CRITICAL": {"count": 2, "roles": _ADMINS, "ttl_seconds": 900},
}

#: Tool tags mapped to a category. Strictest match wins — a tool tagged both
#: `apex` and `deployment` is governed by the deployment rules.
_TAG_PRIORITY = (
    (CATEGORY_SECURITY, {"security", "permissions"}),
    (CATEGORY_DEPLOYMENT, {"deploy", "deployment", "release"}),
    (CATEGORY_BULK, {"bulk"}),
    (CATEGORY_REPOSITORY, {"git", "repository", "github", "bitbucket"}),
    (CATEGORY_APEX, {"apex"}),
    (CATEGORY_AUTOMATION, {"flow", "automation", "trigger"}),
    (CATEGORY_METADATA, {"metadata"}),
    (CATEGORY_EXTERNAL, {"external", "mcp", "jira"}),
    (CATEGORY_DATA, {"data", "records", "write"}),
)


def category_for(tags: list[str] | None) -> str:
    """Map a tool's tags onto a policy category (strictest match wins)."""
    have = {t.lower() for t in (tags or [])}
    for category, markers in _TAG_PRIORITY:
        if have & markers:
            return category
    return CATEGORY_DATA


# ---------------------------------------------------------------------------
# Policy snapshot
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PolicySnapshot:
    """A project's effective policy, resolved once per run.

    Immutable on purpose: a policy edit mid-run must not change the rules the
    run is already operating under.
    """

    project_id: str
    company_id: str = ""
    #: The AND of three levels: the deployment ceiling, the company's own
    #: clearance, and this project's setting.
    allow_production_mutations: bool = False
    #: Which level is saying no, when one is. Carried so a refusal can name the
    #: remedy instead of sending people to an administrator who cannot help —
    #: the earlier version told users to ask a *project* administrator even when
    #: the deployment ceiling was what blocked them, which is unactionable
    #: advice dressed up as help.
    production_blocked_by: str = ""
    approval_ttl_seconds: int = 3600
    max_agent_steps: int = 20
    max_execution_seconds: int = 1800
    max_tool_calls: int = 120
    require_separate_approver: bool = False
    max_bulk_records: int = 50_000
    disabled_tools: frozenset[str] = field(default_factory=frozenset)
    allowed_environments: frozenset[str] = field(
        default_factory=lambda: frozenset(e.value for e in Environment)
    )
    allowed_llm_providers: frozenset[str] = field(default_factory=frozenset)
    allow_llm_fallback: bool = False
    retain_conversation_days: int = 0
    retain_tool_payload_days: int = 0
    retain_document_days: int = 0
    approver_matrix: dict[str, dict[str, dict[str, Any]]] = field(
        default_factory=lambda: DEFAULT_APPROVER_MATRIX
    )

    def requirement(self, category: str, risk: str) -> dict[str, Any]:
        table = self.approver_matrix.get(category) or FALLBACK_REQUIREMENT
        req = (
            table.get(risk)
            or FALLBACK_REQUIREMENT.get(risk)
            or FALLBACK_REQUIREMENT["MEDIUM"]
        )
        ttl = int(req.get("ttl_seconds") or self.approval_ttl_seconds)
        return {
            "count": max(1, int(req.get("count", 1))),
            "roles": list(req.get("roles") or _ADMINS),
            "ttl_seconds": min(ttl, self.approval_ttl_seconds)
            if self.approval_ttl_seconds
            else ttl,
        }

    def environment_allowed(self, environment: Environment | str | None) -> bool:
        if environment is None:
            return True
        value = environment.value if isinstance(environment, Environment) else str(environment)
        return value in self.allowed_environments

    def provider_allowed(self, provider: str) -> bool:
        return not self.allowed_llm_providers or provider in self.allowed_llm_providers


#: Which level blocks production, in the order a person should act on. The
#: deployment ceiling is first because nothing a customer does can get past it.
PRODUCTION_LEVELS = ("deployment", "company", "project")


def production_posture(
    company_allows: bool, project_allows: bool
) -> tuple[bool, str]:
    """Resolve production clearance across all three levels.

    Returns whether it is permitted, and — when it is not — the *outermost*
    level saying no. Outermost matters: telling someone to tick their project
    box while the deployment ceiling is down wastes their afternoon.
    """
    if not settings.allow_production_mutations:
        return False, "deployment"
    if not company_allows:
        return False, "company"
    if not project_allows:
        return False, "project"
    return True, ""


def snapshot_from(
    policy: ProjectPolicy | None,
    project_id: str,
    company_id: str = "",
    *,
    company_allows_production: bool = False,
) -> PolicySnapshot:
    """Resolve a policy row (or its absence) into an immutable snapshot.

    Absent a row, the project inherits the deployment-wide defaults — never
    something more permissive than them.
    """
    if policy is None:
        allowed, blocked_by = production_posture(
            company_allows_production, settings.allow_production_mutations
        )
        return PolicySnapshot(
            project_id=project_id,
            company_id=company_id,
            allow_production_mutations=allowed,
            production_blocked_by=blocked_by,
            approval_ttl_seconds=settings.approval_ttl_seconds,
            max_agent_steps=settings.max_agent_steps,
            max_execution_seconds=settings.max_execution_seconds,
            max_tool_calls=settings.max_tool_calls,
            require_separate_approver=settings.require_separate_approver,
            max_bulk_records=settings.max_bulk_records,
        )

    matrix = DEFAULT_APPROVER_MATRIX
    if isinstance(policy.approver_matrix, dict) and policy.approver_matrix:
        matrix = merge_matrix(DEFAULT_APPROVER_MATRIX, policy.approver_matrix)

    environments = policy.allowed_environments or [e.value for e in Environment]
    # A project cannot enable production mutations the deployment forbids, and
    # cannot list PRODUCTION as allowed if production deployment is off.
    if not settings.feature_production_deployment:
        environments = [e for e in environments if e != Environment.PRODUCTION.value]

    allowed, blocked_by = production_posture(
        company_allows_production, bool(policy.allow_production_mutations)
    )
    return PolicySnapshot(
        project_id=project_id,
        company_id=company_id,
        allow_production_mutations=allowed,
        production_blocked_by=blocked_by,
        approval_ttl_seconds=min(
            int(policy.approval_ttl_seconds or settings.approval_ttl_seconds),
            settings.approval_ttl_seconds,
        ),
        max_agent_steps=min(int(policy.max_agent_steps or 20), settings.max_agent_steps),
        max_execution_seconds=min(
            int(policy.max_execution_seconds or 0) or settings.max_execution_seconds,
            settings.max_execution_seconds,
        ),
        max_tool_calls=min(
            int(policy.max_tool_calls or 0) or settings.max_tool_calls,
            settings.max_tool_calls,
        ),
        require_separate_approver=bool(
            policy.require_separate_approver or settings.require_separate_approver
        ),
        max_bulk_records=min(
            int(policy.max_bulk_records or 0) or settings.max_bulk_records,
            settings.max_bulk_records,
        ),
        disabled_tools=frozenset(policy.disabled_tools or []),
        allowed_environments=frozenset(environments),
        allowed_llm_providers=frozenset(policy.allowed_llm_providers or []),
        allow_llm_fallback=bool(policy.allow_llm_fallback),
        retain_conversation_days=int(policy.retain_conversation_days or 0),
        retain_tool_payload_days=int(policy.retain_tool_payload_days or 0),
        retain_document_days=int(policy.retain_document_days or 0),
        approver_matrix=matrix,
    )


def merge_matrix(
    base: dict[str, dict[str, dict[str, Any]]], override: dict[str, Any]
) -> dict[str, dict[str, dict[str, Any]]]:
    """Overlay a project matrix on the defaults.

    An override may only make a requirement *stricter*: raise the approver
    count, shorten the TTL, narrow the eligible roles. It can never drop below
    the built-in floor, which is what stops a project policy from being used to
    quietly disable the security controls.
    """
    merged: dict[str, dict[str, dict[str, Any]]] = {
        cat: {risk: dict(req) for risk, req in risks.items()} for cat, risks in base.items()
    }
    for category, risks in (override or {}).items():
        if not isinstance(risks, dict):
            continue
        target = merged.setdefault(
            category, {r: dict(v) for r, v in FALLBACK_REQUIREMENT.items()}
        )
        for risk, req in risks.items():
            if not isinstance(req, dict):
                continue
            current = target.setdefault(
                risk, dict(FALLBACK_REQUIREMENT.get(risk, {"count": 1}))
            )
            if "count" in req:
                current["count"] = max(int(current.get("count", 1)), int(req["count"]))
            if "ttl_seconds" in req:
                current["ttl_seconds"] = min(
                    int(current.get("ttl_seconds", 3600)), int(req["ttl_seconds"])
                )
            if req.get("roles"):
                allowed = {str(r).upper() for r in req["roles"]}
                floor = set(current.get("roles") or [])
                # A project admin can always approve; otherwise intersect.
                narrowed = (floor & allowed) | {ProjectRole.PROJECT_ADMIN.value}
                current["roles"] = sorted(narrowed)
    return merged


# ---------------------------------------------------------------------------
# Change binding
# ---------------------------------------------------------------------------
def change_hash(tool_name: str, arguments: dict[str, Any]) -> str:
    """Bind an approval to exactly the operation shown to the human.

    Any edit to the arguments produces a different hash, and an approval whose
    hash no longer matches authorizes nothing.
    """
    canonical = json.dumps(arguments or {}, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(f"{tool_name}\x00{canonical}".encode()).hexdigest()


def expires_at(ttl_seconds: int, *, now: datetime | None = None) -> datetime:
    return (now or datetime.now(UTC)) + timedelta(seconds=max(60, int(ttl_seconds)))


def is_expired(expiry: datetime | None, *, now: datetime | None = None) -> bool:
    if expiry is None:
        return False
    reference = now or datetime.now(UTC)
    if expiry.tzinfo is None:  # SQLite round-trips naive datetimes
        expiry = expiry.replace(tzinfo=UTC)
    return reference >= expiry


def fingerprint_drifted(recorded: dict[str, Any] | None, current: dict[str, Any] | None) -> bool:
    """True when the org state a change was proposed against has moved."""
    if not recorded:
        return False
    return json.dumps(recorded, sort_keys=True, default=str) != json.dumps(
        current or {}, sort_keys=True, default=str
    )


# ---------------------------------------------------------------------------
# Eligibility
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class EligibilityResult:
    allowed: bool
    reason: str = ""

    def __bool__(self) -> bool:  # pragma: no cover - convenience
        return self.allowed


def can_decide(
    *,
    membership: ProjectMembership | None,
    eligible_roles: list[str] | None,
    requester_user_id: str,
    require_separate_approver: bool,
    already_voted: bool,
) -> EligibilityResult:
    """Decide whether this human may cast a decision on this approval."""
    if membership is None or not membership.is_active:
        return EligibilityResult(False, "You are not an active member of this project.")
    role = (
        membership.role.value
        if isinstance(membership.role, ProjectRole)
        else str(membership.role)
    )
    allowed = {str(r).upper() for r in (eligible_roles or [])}
    if allowed and role not in allowed:
        return EligibilityResult(
            False,
            f"This change requires one of {', '.join(sorted(allowed))}; your role in "
            f"this project is {role}.",
        )
    if already_voted:
        return EligibilityResult(False, "You have already recorded a decision on this approval.")
    if require_separate_approver and membership.user_id == requester_user_id:
        return EligibilityResult(
            False,
            "Separation of duties: the person who requested this change cannot approve "
            "it. Ask another eligible approver to review it.",
        )
    return EligibilityResult(True)


# ---------------------------------------------------------------------------
# Alternatives when policy blocks an action
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Alternative:
    """Something the user can actually do instead of the blocked action."""

    label: str
    detail: str
    action: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"label": self.label, "detail": self.detail, "action": self.action}


#: Reason code -> the alternatives worth offering. Kept here, deterministic, so
#: the suggestions are policy rather than something the model improvised.
_ALTERNATIVES: dict[str, list[Alternative]] = {
    "PRODUCTION_BLOCKED": [
        Alternative(
            "Run it against a sandbox",
            "Point the conversation at a sandbox connection and make the same change "
            "there first.",
            "switch_environment",
        ),
        Alternative(
            "Prepare a release instead",
            "Build the change as a change set so it can be validated, diffed and "
            "approved for a later production window.",
            "create_change_set",
        ),
        Alternative(
            "Ask an administrator to enable production changes",
            "A project admin can allow production mutations in project policy; the "
            "deployment must also permit them.",
            "open_policy",
        ),
    ],
    "BULK_LIMIT_EXCEEDED": [
        Alternative(
            "Narrow the filter",
            "Add conditions so the change affects fewer records, then propose it again.",
            "refine_filter",
        ),
        Alternative(
            "Split it into batches",
            "Run the change over subsets, verifying each before continuing.",
            "batch",
        ),
        Alternative(
            "Raise the project limit",
            "A project admin can increase the maximum records per bulk change.",
            "open_policy",
        ),
    ],
    "TOOL_DISABLED": [
        Alternative(
            "Use a different capability",
            "Another tool may achieve the same outcome within policy.",
            "",
        ),
        Alternative(
            "Ask an administrator to enable it",
            "A project admin controls which tools this project may use.",
            "open_policy",
        ),
    ],
    "DESTRUCTIVE_BLOCKED": [
        Alternative(
            "Export the records first",
            "Take a copy so the operation is recoverable before anything is removed.",
            "export",
        ),
        Alternative(
            "Archive instead of deleting",
            "Set a status or move the records rather than destroying them.",
            "archive",
        ),
        Alternative(
            "Produce a deletion plan for review",
            "List exactly what would be deleted and have it approved explicitly.",
            "plan_deletion",
        ),
    ],
    "ENVIRONMENT_NOT_ALLOWED": [
        Alternative(
            "Choose a permitted environment",
            "This project's policy limits which environments the agent may change.",
            "switch_environment",
        ),
        Alternative(
            "Ask an administrator to permit it",
            "A project admin can widen the allowed environments.",
            "open_policy",
        ),
    ],
    "PROVIDER_NOT_ALLOWED": [
        Alternative(
            "Use a permitted model provider",
            "This project restricts which AI providers may process its data.",
            "open_ai_settings",
        ),
    ],
}


def alternatives_for(reason_code: str) -> list[dict[str, str]]:
    """What to offer instead when policy blocks an action.

    Returns an empty list when there is genuinely nothing useful to suggest —
    an empty list is more honest than a generic "contact your administrator".
    """
    return [a.to_dict() for a in _ALTERNATIVES.get(reason_code, [])]
