from app.models import RiskLevel
from app.risk.engine import OrgContext, classify

SANDBOX = OrgContext(is_sandbox=True, org_type="sandbox")
PRODUCTION = OrgContext(is_sandbox=False, org_type="Enterprise Edition")


def test_read_tool_is_low_and_needs_no_approval():
    d = classify("query_salesforce", {"soql": "SELECT Id FROM Account"}, RiskLevel.LOW, SANDBOX, False)
    assert d.risk is RiskLevel.LOW
    assert not d.requires_approval
    assert not d.blocked


def test_record_mutation_requires_approval():
    d = classify("create_record", {"object": "Account"}, RiskLevel.MEDIUM, SANDBOX, True)
    assert d.requires_approval


def test_security_object_escalates_to_high():
    d = classify("update_record", {"object": "PermissionSet"}, RiskLevel.MEDIUM, SANDBOX, True)
    assert d.risk is RiskLevel.HIGH
    assert d.requires_approval


def test_delete_is_high_risk():
    d = classify("delete_record", {"object": "Account"}, RiskLevel.MEDIUM, SANDBOX, False)
    assert d.risk is RiskLevel.HIGH
    assert d.requires_approval


def test_bulk_blast_radius_escalates():
    d = classify(
        "bulk_update", {"object": "Account", "record_ids": [str(i) for i in range(50)]},
        RiskLevel.MEDIUM, SANDBOX, True,
    )
    assert d.risk is RiskLevel.HIGH


def test_production_metadata_is_blocked_by_default():
    d = classify("create_field", {"object": "Account"}, RiskLevel.MEDIUM, PRODUCTION, True)
    assert d.blocked
    assert "production" in d.blocked_reason.lower() or "sandbox" in d.blocked_reason.lower()


def test_production_metadata_allowed_when_policy_permits():
    org = OrgContext(is_sandbox=False, org_type="Enterprise", allow_production_mutations=True)
    d = classify("create_field", {"object": "Account"}, RiskLevel.MEDIUM, org, True)
    assert not d.blocked
    assert d.requires_approval
    assert d.risk is RiskLevel.HIGH


def test_production_record_mutation_escalates_but_is_not_blocked():
    d = classify("update_record", {"object": "Account"}, RiskLevel.MEDIUM, PRODUCTION, True)
    assert not d.blocked
    assert d.risk is RiskLevel.HIGH
    assert d.requires_approval


# ---------------------------------------------------------------------------
# Environment awareness
#
# `environment` is the operator's declared intent; `is_sandbox` is Salesforce's
# own fact. The rules below all defend one property: a declaration may raise the
# posture, never lower it.
# ---------------------------------------------------------------------------
from app.models import Environment  # noqa: E402
from app.tenancy.policy import PolicySnapshot  # noqa: E402

UAT = OrgContext(is_sandbox=True, org_type="sandbox", environment=Environment.UAT)
DECLARED_PRODUCTION = OrgContext(
    is_sandbox=True, org_type="sandbox", environment=Environment.PRODUCTION
)
MISLABELLED = OrgContext(
    is_sandbox=False, org_type="Enterprise Edition", environment=Environment.SANDBOX
)
DEVELOPER_EDITION = OrgContext(
    is_sandbox=False, org_type="Developer Edition", environment=Environment.DEVELOPMENT
)


def test_declaring_an_org_as_sandbox_does_not_remove_production_controls():
    """Otherwise relabelling a production connection deletes its guard rails,
    which would make the environment field an attack surface rather than a
    control."""
    assert MISLABELLED.is_production is True
    d = classify("create_field", {"object": "Account"}, RiskLevel.MEDIUM, MISLABELLED, True)
    assert d.blocked
    assert d.reason_code == "PRODUCTION_BLOCKED"


def test_the_mislabelling_is_explained_rather_than_left_mysterious():
    """An operator looking at a connection named 'Sandbox' needs to be told why
    their change was gated."""
    d = classify("update_record", {"object": "Account"}, RiskLevel.MEDIUM, MISLABELLED, True)
    assert any("labelled SANDBOX" in r for r in d.reasons)


def test_declaring_a_sandbox_as_production_does_raise_the_posture():
    assert DECLARED_PRODUCTION.is_production is True
    d = classify(
        "create_field", {"object": "Account"}, RiskLevel.MEDIUM, DECLARED_PRODUCTION, True
    )
    assert d.blocked


def test_a_developer_edition_org_is_not_treated_as_production():
    """Developer Edition is the one org that is neither a sandbox nor anyone's
    production; gating it would make every trial account unusable."""
    assert DEVELOPER_EDITION.is_production is False
    d = classify(
        "create_field", {"object": "Account"}, RiskLevel.MEDIUM, DEVELOPER_EDITION, True
    )
    assert not d.blocked


def test_a_change_to_an_environment_outside_project_policy_is_blocked():
    policy = PolicySnapshot(
        project_id="prj_1", allowed_environments=frozenset({Environment.SANDBOX.value})
    )
    d = classify(
        "create_field",
        {"object": "Account"},
        RiskLevel.MEDIUM,
        UAT,
        True,
        policy=policy,
        mutating=True,
    )
    assert d.blocked
    assert d.reason_code == "ENVIRONMENT_NOT_ALLOWED"


def test_reading_an_environment_outside_project_policy_is_still_allowed():
    """Environment scoping governs what may be **changed**, not what may be read.

    This test asserted the opposite until a read was refused in the product with
    "does not permit changes in the PRODUCTION environment" — against an org it
    was only looking at. The message was untrue, and the behaviour removed the
    most common legitimate use of a production connection: inspecting and
    diagnosing an org you are deliberately not allowed to touch. An engineer who
    cannot read production cannot work out what is wrong with it.

    Reading is not thereby unconditional. The connection is the permission,
    Salesforce's own field-level security applies to every row returned, and the
    read is audited like any other operation.
    """
    policy = PolicySnapshot(
        project_id="prj_1", allowed_environments=frozenset({Environment.SANDBOX.value})
    )
    d = classify(
        "query_salesforce",
        {"soql": "SELECT Id FROM Account"},
        RiskLevel.LOW,
        UAT,
        False,
        policy=policy,
        mutating=False,
    )
    assert not d.blocked


def test_a_read_is_not_treated_as_a_change_because_its_risk_tier_is_medium():
    """The engine used to infer "is this a mutation?" from the risk tier. A read
    can be MEDIUM for reasons that have nothing to do with writing — volume,
    sensitivity of the object — and would then be refused as a change."""
    policy = PolicySnapshot(
        project_id="prj_1", allowed_environments=frozenset({Environment.SANDBOX.value})
    )
    d = classify(
        "inspect_permissions",
        {"object": "Account"},
        RiskLevel.MEDIUM,
        UAT,
        False,
        policy=policy,
        mutating=False,
    )
    assert not d.blocked


def test_every_registered_tool_declares_whether_it_writes():
    """The flag the environment gate now depends on. A tool that forgot to
    declare it would silently fall back to the risk-tier guess this replaced."""
    from app.tools.registry import build_registry

    for tool in build_registry().all():
        assert isinstance(tool.mutating, bool), tool.name


def test_a_blocked_decision_carries_alternatives_the_user_can_act_on():
    d = classify("create_field", {"object": "Account"}, RiskLevel.MEDIUM, PRODUCTION, True)
    payload = d.to_dict()
    assert payload["alternatives"]
    assert all("label" in a for a in payload["alternatives"])


def test_an_allowed_decision_carries_no_alternatives():
    d = classify("query_salesforce", {}, RiskLevel.LOW, SANDBOX, False)
    assert d.to_dict()["alternatives"] == []


# ---------------------------------------------------------------------------
# CRITICAL tier
# ---------------------------------------------------------------------------
def test_irreversible_deletion_against_production_is_critical():
    d = classify("delete_record", {"object": "Account"}, RiskLevel.MEDIUM, PRODUCTION, True)
    assert d.risk is RiskLevel.CRITICAL


def test_critical_always_demands_a_separate_approver():
    """Separation of duties for an irreversible production change is not a
    project preference — a policy that switched it off would be the one case
    where it matters most."""
    policy = PolicySnapshot(project_id="prj_1", require_separate_approver=False)
    d = classify(
        "delete_record", {"object": "Account"}, RiskLevel.MEDIUM, PRODUCTION, True,
        policy=policy,
    )
    assert d.risk is RiskLevel.CRITICAL
    assert d.require_separate_approver is True


def test_deletion_in_a_sandbox_is_high_but_not_critical():
    d = classify("delete_record", {"object": "Account"}, RiskLevel.MEDIUM, SANDBOX, True)
    assert d.risk is RiskLevel.HIGH


def test_a_disabled_tool_is_blocked_with_a_reason_code():
    policy = PolicySnapshot(project_id="prj_1", disabled_tools=frozenset({"deploy_metadata"}))
    d = classify("deploy_metadata", {}, RiskLevel.HIGH, SANDBOX, True, policy=policy)
    assert d.blocked
    assert d.reason_code == "TOOL_DISABLED"
    assert d.to_dict()["alternatives"]


def test_exceeding_the_bulk_limit_is_blocked_with_a_reason_code():
    policy = PolicySnapshot(project_id="prj_1", max_bulk_records=100)
    d = classify(
        "bulk_update",
        {"object": "Account", "estimated_records": 5000},
        RiskLevel.MEDIUM,
        SANDBOX,
        True,
        policy=policy,
    )
    assert d.blocked
    assert d.reason_code == "BULK_LIMIT_EXCEEDED"
