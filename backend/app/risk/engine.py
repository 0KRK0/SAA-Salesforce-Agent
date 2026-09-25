"""Deterministic risk classification and approval policy.

This layer sits OUTSIDE the model. Claude can propose anything; only this
engine decides whether an action may execute, and only explicit human approval
records (as many as the tenant policy demands) can unblock a gated action.

The engine is intentionally boring: no scoring, no model input, no heuristics
that can be talked out of a decision. It reads the tool's declared risk, the
arguments, the target org's posture and the tenant policy, and returns a
decision that the runtime obeys.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.config import settings
from app.models import Environment, RiskLevel
from app.tenancy.policy import (
    CATEGORY_BULK,
    CATEGORY_DEPLOYMENT,
    CATEGORY_SECURITY,
    PolicySnapshot,
    alternatives_for,
    category_for,
)


@dataclass
class RiskDecision:
    risk: RiskLevel
    requires_approval: bool
    reasons: list[str]
    blocked: bool = False
    blocked_reason: str = ""
    #: Stable machine code for a block, so the UI and the alternatives table can
    #: react to *why* rather than parsing prose.
    reason_code: str = ""
    category: str = "data"
    # Populated from the tenant policy when approval is required.
    approvals_required: int = 1
    eligible_roles: list[str] = field(default_factory=list)
    ttl_seconds: int = 3600
    require_separate_approver: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "risk": self.risk.value,
            "requires_approval": self.requires_approval,
            "reasons": self.reasons,
            "blocked": self.blocked,
            "blocked_reason": self.blocked_reason,
            "reason_code": self.reason_code,
            "alternatives": alternatives_for(self.reason_code) if self.blocked else [],
            "category": self.category,
            "approvals_required": self.approvals_required,
            "eligible_roles": self.eligible_roles,
            "ttl_seconds": self.ttl_seconds,
            "require_separate_approver": self.require_separate_approver,
        }


# Objects whose modification is treated as security-sensitive.
SECURITY_OBJECTS = {
    "user",
    "profile",
    "permissionset",
    "permissionsetassignment",
    "permissionsetgroup",
    "userrole",
    "group",
    "groupmember",
    "objectpermissions",
    "fieldpermissions",
    "sharingrules",
    "networkmember",
    "loginiphistory",
    "connectedapplication",
}

# Metadata types whose deployment can change org-wide security posture.
SECURITY_METADATA_TYPES = {
    "profile",
    "permissionset",
    "permissionsetgroup",
    "sharingrules",
    "sharingcriteriarule",
    "sharingownerrule",
    "role",
    "customobjectsharingsettings",
    "connectedapp",
    "namedcredential",
    "remotesitesetting",
}

# Tools that reach production only under an explicit deployment policy.
#: What to tell someone when production is blocked, per level.
#:
#: Naming the level is the whole point. The previous single message told every
#: user to "enable production mutations for this project", which is useless
#: advice when the deployment ceiling is what refuses — they tick the box, the
#: answer stays no, and the product looks broken. Worse, the model, reading only
#: that message, invented a plausible next step ("ask a project administrator")
#: that could not possibly have worked.
PRODUCTION_REFUSAL = {
    "deployment": (
        "Metadata changes to production are switched off for this entire "
        "deployment (ALLOW_PRODUCTION_MUTATIONS is false). No project or company "
        "setting can override it — whoever operates this installation has to "
        "change it and restart the backend. Target a sandbox in the meantime."
    ),
    "company": (
        "This company has not been cleared for production changes. A company "
        "administrator enables it under Settings, and it applies to every "
        "project. Until then, target a sandbox."
    ),
    "project": (
        "This project does not permit production changes. A project "
        "administrator can enable it under Settings for this project only. "
        "Target a sandbox in the meantime."
    ),
    # A second, near-duplicate deployment switch. Named separately so the
    # message points at the flag that is actually off rather than at the other
    # one, which someone may already have turned on and be staring at.
    "deployment_feature": (
        "Production deployment is switched off as a feature for this entire "
        "deployment (FEATURE_PRODUCTION_DEPLOYMENT is false), which removes "
        "PRODUCTION from every project's allowed environments. Whoever operates "
        "this installation has to enable it and restart the backend. Reading "
        "and diagnosing production orgs is unaffected."
    ),
}


def _production_refusal(blocked_by: str) -> str:
    return PRODUCTION_REFUSAL.get(
        blocked_by,
        "Metadata deployment to production is disabled by policy. Target a sandbox.",
    )


PRODUCTION_GATED_TOOLS = {
    "create_field",
    "deploy_metadata",
    "create_flow",
    "update_flow",
    "activate_flow",
    "deactivate_flow",
    "delete_flow",
    "deploy_apex",
    "write_apex",
    "create_report",
    "deploy_change_set",
    "modify_permissions",
}


@dataclass
class OrgContext:
    """The posture of the org a change would land in.

    `environment` is authoritative when set. `is_sandbox` is the Salesforce
    flag, which is a weaker signal: a sandbox can legitimately be a team's UAT
    environment, and a developer edition is not production even though
    Salesforce does not call it a sandbox.
    """

    is_sandbox: bool = True
    org_type: str = "unknown"
    allow_production_mutations: bool = False
    environment: Environment | None = None

    @property
    def is_production(self) -> bool:
        """Whether production controls apply. The stricter signal wins.

        There are two signals and they can disagree: `environment` is the
        operator's declared intent, `is_sandbox` is Salesforce's own fact. A
        declaration may only ever *raise* the posture. Letting it lower one
        would mean an operator can delete production's guard rails by labelling
        the org "SANDBOX", which is not a control at all.

        Developer Edition is the single case where the Salesforce flag misleads:
        such an org is not a sandbox and is also nobody's production.
        """
        if self.environment is Environment.PRODUCTION:
            return True
        if self.is_sandbox:
            return False
        return "developer" not in (self.org_type or "").lower()

    @property
    def label_disagrees(self) -> bool:
        """The declared environment is softer than the org actually is.

        Worth saying out loud in the decision, because an operator looking at a
        connection labelled "Sandbox" will not otherwise understand why the
        change was gated.
        """
        return (
            self.is_production
            and self.environment is not None
            and self.environment is not Environment.PRODUCTION
        )


def _policy_or_default(policy: PolicySnapshot | None) -> PolicySnapshot:
    if policy is not None:
        return policy
    return PolicySnapshot(
        project_id="",
        allow_production_mutations=settings.allow_production_mutations,
        approval_ttl_seconds=settings.approval_ttl_seconds,
        max_agent_steps=settings.max_agent_steps,
        require_separate_approver=settings.require_separate_approver,
        max_bulk_records=settings.max_bulk_records,
    )


def classify(
    tool_name: str,
    arguments: dict[str, Any],
    base_risk: RiskLevel,
    org: OrgContext,
    tool_requires_approval: bool,
    *,
    tags: list[str] | None = None,
    policy: PolicySnapshot | None = None,
    provider: str = "native",
    mutating: bool | None = None,
) -> RiskDecision:
    resolved = _policy_or_default(policy)
    reasons: list[str] = []
    risk = base_risk
    requires_approval = tool_requires_approval
    category = category_for(tags)

    obj = str(arguments.get("object") or arguments.get("sobject") or "").lower()
    # Every tool declares whether it writes. Take that when it is given; fall
    # back to inferring from the risk tier only for callers that do not pass it.
    # The inference is a proxy and a coarse one — a read can be MEDIUM because
    # it returns a lot of rows — which is exactly how a read-only tool ended up
    # refused for changing an environment it never touched.
    is_mutation = (
        bool(mutating)
        if mutating is not None
        else base_risk in (RiskLevel.MEDIUM, RiskLevel.HIGH, RiskLevel.CRITICAL)
    )

    # --- tenant kill switch ---
    if tool_name in resolved.disabled_tools:
        return RiskDecision(
            risk=RiskLevel.HIGH,
            requires_approval=True,
            reasons=[f"'{tool_name}' is disabled for this project."],
            blocked=True,
            blocked_reason=(
                f"The tool '{tool_name}' is disabled by this project's policy. "
                "A project admin can re-enable it in Settings."
            ),
            reason_code="TOOL_DISABLED",
            category=category,
        )

    # --- environment gate ---
    #
    # A project may be scoped to a subset of environments. That scoping governs
    # what the agent may **change**, and deliberately not what it may read.
    #
    # Gating reads here was a real defect: with PRODUCTION outside the allowed
    # set, `describe_object` — a pure read — was refused with "does not permit
    # changes in the PRODUCTION environment", against an org it was only
    # looking at. That is wrong twice over. It is untrue, since nothing was
    # being changed; and it removes the most common legitimate use of a
    # production connection, which is inspecting and diagnosing an org you are
    # not allowed to touch. An engineer who cannot read production cannot work
    # out what is wrong with it.
    #
    # Reading is still not unconditional: the connection itself is the
    # permission, Salesforce's own field-level security applies to every row
    # returned, and the audit trail records the read like any other operation.
    if (
        is_mutation
        and org.environment is not None
        and not resolved.environment_allowed(org.environment)
    ):
        if org.environment is Environment.PRODUCTION:
            # Production is refused by two separate mechanisms — the allowed-
            # environment list and the three-level clearance — and a user hit
            # by one should not get a different explanation than a user hit by
            # the other. Both end here, with the message that names a remedy.
            blocked_by = resolved.production_blocked_by or (
                "deployment_feature"
                if not settings.feature_production_deployment
                else "deployment"
            )
            return RiskDecision(
                risk=RiskLevel.HIGH,
                requires_approval=True,
                reasons=[f"Target environment {org.environment.value} is outside project policy."],
                blocked=True,
                blocked_reason=_production_refusal(blocked_by),
                reason_code="PRODUCTION_BLOCKED",
                category=category,
            )
        return RiskDecision(
            risk=RiskLevel.HIGH,
            requires_approval=True,
            reasons=[f"Target environment {org.environment.value} is outside project policy."],
            blocked=True,
            blocked_reason=(
                f"This project's policy does not permit changes in the "
                f"{org.environment.value} environment. Reading and diagnosing "
                f"this org is still allowed."
            ),
            reason_code="ENVIRONMENT_NOT_ALLOWED",
            category=category,
        )

    # --- self-describing tool providers ---
    # An MCP server describes its own tools. That description is a claim, not
    # evidence, so an MCP tool never sits below the configured floor.
    #
    # First-party integrations (Jira, GitHub, Bitbucket) are excluded on
    # purpose: their tools are implemented in this repository, their risk is
    # declared here, and their arguments are validated here. Flooring them
    # would mean reading a ticket needed an approval, which teaches people to
    # approve without reading.
    if is_self_describing(provider):
        floor = RiskLevel(settings.mcp_default_risk)
        if _rank(risk) < _rank(floor):
            risk = floor
            reasons.append(
                f"Tool is provided by '{provider}', which describes its own "
                f"capabilities; risk floored at {floor.value}."
            )
        if risk in (RiskLevel.MEDIUM, RiskLevel.HIGH, RiskLevel.CRITICAL):
            is_mutation = True

    # --- object sensitivity escalation ---
    if obj in SECURITY_OBJECTS and is_mutation:
        risk = RiskLevel.HIGH
        requires_approval = True
        category = CATEGORY_SECURITY
        reasons.append(f"'{obj}' is a security/permission object.")

    # --- security-sensitive metadata ---
    md_types = _metadata_types(arguments)
    sensitive = md_types & SECURITY_METADATA_TYPES
    if sensitive and is_mutation:
        risk = RiskLevel.HIGH
        requires_approval = True
        category = CATEGORY_SECURITY
        reasons.append(
            "Deployment includes security metadata: " + ", ".join(sorted(sensitive)) + "."
        )

    # --- destructive intent escalation ---
    if tool_name.startswith("delete") or arguments.get("hard_delete"):
        risk = RiskLevel.HIGH
        requires_approval = True
        reasons.append("Deletion is irreversible.")
        # Irreversible loss in production is the one combination that always
        # needs two people, whatever the tool declared about itself.
        if org.is_production:
            risk = RiskLevel.CRITICAL
            reasons.append("Irreversible deletion against production.")

    # --- bulk blast radius ---
    record_ids = arguments.get("record_ids")
    if isinstance(record_ids, list) and len(record_ids) > 20:
        risk = RiskLevel.HIGH
        requires_approval = True
        category = CATEGORY_BULK
        reasons.append(f"Affects {len(record_ids)} records in one operation.")

    estimated = _estimated_rows(arguments)
    if estimated is not None and is_mutation:
        if estimated > resolved.max_bulk_records:
            return RiskDecision(
                risk=RiskLevel.HIGH,
                requires_approval=True,
                reasons=[*reasons, f"Estimated {estimated} affected records."],
                blocked=True,
                blocked_reason=(
                    f"This operation would affect about {estimated:,} records, above this "
                    f"project's limit of {resolved.max_bulk_records:,}. Narrow the "
                    "filter or raise the limit in the project's policy."
                ),
                reason_code="BULK_LIMIT_EXCEEDED",
                category=CATEGORY_BULK,
            )
        if estimated > 200:
            risk = RiskLevel.HIGH
            requires_approval = True
            category = CATEGORY_BULK
            reasons.append(f"Estimated {estimated:,} affected records.")

    # --- Apex / automation touching production behaviour ---
    if category in {"apex", "automation"} and is_mutation:
        requires_approval = True
        reasons.append("Changes org automation behaviour.")

    # --- production posture ---
    if is_mutation and org.is_production:
        requires_approval = True
        if org.label_disagrees:
            reasons.append(
                f"This connection is labelled {org.environment.value}, but Salesforce "
                f"reports it as a non-sandbox {org.org_type} org, so production "
                "controls apply."
            )
        else:
            reasons.append("Target org is production.")
        if risk == RiskLevel.MEDIUM:
            risk = RiskLevel.HIGH
        allowed = org.allow_production_mutations or resolved.allow_production_mutations
        if tool_name in PRODUCTION_GATED_TOOLS and not allowed:
            return RiskDecision(
                risk=RiskLevel.HIGH,
                requires_approval=True,
                reasons=[*reasons, "Metadata changes to production are disabled."],
                blocked=True,
                blocked_reason=_production_refusal(resolved.production_blocked_by),
                reason_code="PRODUCTION_BLOCKED",
                category=category,
            )

    # --- metadata deployments that skip validation ---
    if tool_name in {"deploy_metadata", "deploy_change_set"} and arguments.get(
        "check_only"
    ) is False:
        requires_approval = True
        category = CATEGORY_DEPLOYMENT
        reasons.append("Real (non-validation) metadata deployment.")

    if not reasons:
        reasons.append(f"Base risk for {tool_name} is {risk.value}.")

    decision = RiskDecision(
        risk=risk,
        requires_approval=requires_approval,
        reasons=reasons,
        category=category,
    )
    if requires_approval:
        requirement = resolved.requirement(category, risk.value)
        decision.approvals_required = requirement["count"]
        decision.eligible_roles = requirement["roles"]
        decision.ttl_seconds = requirement["ttl_seconds"]
        decision.require_separate_approver = resolved.require_separate_approver and risk in (
            RiskLevel.HIGH,
            RiskLevel.CRITICAL,
        )
        # Separation of duties is not optional for CRITICAL, whatever the
        # project configured: an irreversible production change is never a
        # one-person decision.
        if risk is RiskLevel.CRITICAL:
            decision.require_separate_approver = True
    return decision


#: Tool providers implemented in this repository. Their declared risk is
#: evidence, not a claim, because we wrote it.
FIRST_PARTY_PROVIDERS = frozenset({"native", "jira", "github", "bitbucket"})


def is_self_describing(provider: str) -> bool:
    """Whether a provider's own description of its tools is all we have."""
    return provider not in FIRST_PARTY_PROVIDERS


def _rank(risk: RiskLevel) -> int:
    return {
        RiskLevel.LOW: 1,
        RiskLevel.MEDIUM: 2,
        RiskLevel.HIGH: 3,
        RiskLevel.CRITICAL: 4,
    }[risk]


def _metadata_types(arguments: dict[str, Any]) -> set[str]:
    """Metadata type names mentioned anywhere in the arguments."""
    types: set[str] = set()
    manifest = arguments.get("types") or arguments.get("package_manifest")
    if isinstance(manifest, dict):
        types |= {str(k).lower() for k in manifest}
    files = arguments.get("files")
    if isinstance(files, dict):
        for path in files:
            head = str(path).split("/")[0].rstrip("s").lower()
            if head:
                types.add(head)
    if arguments.get("metadata_type"):
        types.add(str(arguments["metadata_type"]).lower())
    return types


def _estimated_rows(arguments: dict[str, Any]) -> int | None:
    for key in ("estimated_records", "record_count", "affected_records"):
        value = arguments.get(key)
        if isinstance(value, int):
            return value
    records = arguments.get("records")
    if isinstance(records, list):
        return len(records)
    return None
