"""Tenancy, approval policy and approval binding.

These are the controls a security review actually turns on, so each test states
the property it is defending rather than just exercising a code path.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.models import Environment, ProjectMembership, ProjectPolicy, ProjectRole
from app.tenancy.policy import (
    CATEGORY_DEPLOYMENT,
    CATEGORY_METADATA,
    CATEGORY_SECURITY,
    DEFAULT_APPROVER_MATRIX,
    alternatives_for,
    can_decide,
    category_for,
    change_hash,
    expires_at,
    fingerprint_drifted,
    is_expired,
    snapshot_from,
)
from app.tenancy.service import (
    create_company,
    create_project,
    project_membership,
    slugify,
)


# --------------------------------------------------------------- categories
def test_tags_map_to_the_strictest_matching_category():
    """A tool that is both Apex and a deployment is governed by deployment
    rules, which are stricter."""
    assert category_for(["apex", "metadata", "deploy"]) == CATEGORY_DEPLOYMENT
    assert category_for(["security", "permissions"]) == CATEGORY_SECURITY
    assert category_for(["flow", "automation"]) == "automation"
    assert category_for(["metadata"]) == CATEGORY_METADATA
    assert category_for([]) == "data"


def test_security_and_production_need_two_approvers_by_default():
    assert DEFAULT_APPROVER_MATRIX[CATEGORY_SECURITY]["HIGH"]["count"] == 2
    assert DEFAULT_APPROVER_MATRIX[CATEGORY_DEPLOYMENT]["HIGH"]["count"] == 2


def test_every_category_defines_a_critical_tier():
    """CRITICAL exists precisely for irreversible production changes; a category
    without it would silently fall back to a weaker requirement."""
    for category, tiers in DEFAULT_APPROVER_MATRIX.items():
        assert "CRITICAL" in tiers, category
        assert tiers["CRITICAL"]["count"] >= 2, category


# ------------------------------------------------------------------- policy
def test_absent_policy_inherits_deployment_defaults():
    snapshot = snapshot_from(None, "prj_1", "co_1")
    assert snapshot.project_id == "prj_1"
    assert snapshot.company_id == "co_1"
    assert snapshot.allow_production_mutations is False
    assert snapshot.max_agent_steps > 0


def test_a_project_cannot_enable_production_mutations_the_deployment_forbids():
    """The deployment-wide setting is a ceiling, not a default."""
    row = ProjectPolicy(project_id="prj_1", allow_production_mutations=True)
    snapshot = snapshot_from(row, "prj_1")
    assert snapshot.allow_production_mutations is False


def test_production_is_stripped_when_the_feature_flag_is_off():
    """A project cannot list an environment the deployment has not enabled."""
    row = ProjectPolicy(
        project_id="prj_1", allowed_environments=[e.value for e in Environment]
    )
    snapshot = snapshot_from(row, "prj_1")
    assert snapshot.environment_allowed(Environment.SANDBOX) is True
    assert snapshot.environment_allowed(Environment.PRODUCTION) is False


def test_a_project_matrix_can_only_make_a_requirement_stricter():
    row = ProjectPolicy(
        project_id="prj_1",
        approver_matrix={
            CATEGORY_METADATA: {"MEDIUM": {"count": 3, "ttl_seconds": 60}},
        },
    )
    snapshot = snapshot_from(row, "prj_1")
    requirement = snapshot.requirement(CATEGORY_METADATA, "MEDIUM")
    assert requirement["count"] == 3
    assert requirement["ttl_seconds"] <= 3600


def test_a_project_matrix_cannot_loosen_the_approver_count():
    row = ProjectPolicy(
        project_id="prj_1",
        approver_matrix={CATEGORY_SECURITY: {"HIGH": {"count": 1}}},
    )
    snapshot = snapshot_from(row, "prj_1")
    # The built-in floor of two approvers survives the override.
    assert snapshot.requirement(CATEGORY_SECURITY, "HIGH")["count"] == 2


def test_project_admin_can_always_approve_even_when_roles_are_narrowed():
    row = ProjectPolicy(
        project_id="prj_1",
        approver_matrix={CATEGORY_METADATA: {"MEDIUM": {"roles": ["DEVELOPER"]}}},
    )
    snapshot = snapshot_from(row, "prj_1")
    roles = snapshot.requirement(CATEGORY_METADATA, "MEDIUM")["roles"]
    assert ProjectRole.PROJECT_ADMIN.value in roles


def test_provider_allowlist_is_permissive_only_when_empty():
    open_policy = snapshot_from(ProjectPolicy(project_id="prj_1"), "prj_1")
    assert open_policy.provider_allowed("ANTHROPIC") is True

    restricted = snapshot_from(
        ProjectPolicy(project_id="prj_1", allowed_llm_providers=["ANTHROPIC"]), "prj_1"
    )
    assert restricted.provider_allowed("ANTHROPIC") is True
    assert restricted.provider_allowed("OPENAI") is False


# ------------------------------------------------------------ change binding
def test_change_hash_is_stable_under_key_order_and_sensitive_to_values():
    a = change_hash("create_field", {"object": "Account", "type": "Text"})
    b = change_hash("create_field", {"type": "Text", "object": "Account"})
    c = change_hash("create_field", {"object": "Account", "type": "Number"})
    assert a == b
    assert a != c


def test_change_hash_is_sensitive_to_the_tool_name():
    args = {"object": "Account"}
    assert change_hash("create_field", args) != change_hash("delete_field", args)


def test_expiry_is_evaluated_against_a_real_clock():
    past = datetime.now(UTC) - timedelta(seconds=5)
    future = datetime.now(UTC) + timedelta(hours=1)
    assert is_expired(past) is True
    assert is_expired(future) is False
    assert is_expired(None) is False


def test_naive_timestamps_from_sqlite_are_treated_as_utc():
    """SQLite round-trips naive datetimes; comparing one to an aware 'now'
    raises unless we normalize, which would crash the approval path."""
    naive_past = (datetime.now(UTC) - timedelta(hours=1)).replace(tzinfo=None)
    assert is_expired(naive_past) is True


def test_expires_at_enforces_a_minimum_window():
    """A one-second TTL would make every approval unusable."""
    issued = datetime(2026, 1, 1, tzinfo=UTC)
    assert expires_at(1, now=issued) >= issued + timedelta(seconds=60)


def test_fingerprint_drift_detects_org_state_moving():
    recorded = {"field_exists": False, "object": "Account"}
    assert fingerprint_drifted(recorded, {"field_exists": False, "object": "Account"}) is False
    assert fingerprint_drifted(recorded, {"field_exists": True, "object": "Account"}) is True
    # No fingerprint recorded means nothing to compare against.
    assert fingerprint_drifted(None, {"anything": 1}) is False


# -------------------------------------------------------------- eligibility
def _membership(role: ProjectRole, user_id: str = "usr_1") -> ProjectMembership:
    return ProjectMembership(
        company_id="co_1", project_id="prj_1", user_id=user_id, role=role, is_active=True
    )


def test_a_role_outside_the_eligible_set_cannot_approve():
    result = can_decide(
        membership=_membership(ProjectRole.VIEWER),
        eligible_roles=["PROJECT_ADMIN", "SALESFORCE_ADMIN"],
        requester_user_id="usr_2",
        require_separate_approver=False,
        already_voted=False,
    )
    assert not result.allowed
    assert "VIEWER" in result.reason


def test_an_inactive_member_cannot_approve():
    membership = _membership(ProjectRole.PROJECT_ADMIN)
    membership.is_active = False
    result = can_decide(
        membership=membership,
        eligible_roles=["PROJECT_ADMIN"],
        requester_user_id="usr_2",
        require_separate_approver=False,
        already_voted=False,
    )
    assert not result.allowed


def test_nobody_can_vote_twice_on_the_same_approval():
    """Otherwise one person satisfies a two-approver policy by clicking twice."""
    result = can_decide(
        membership=_membership(ProjectRole.SALESFORCE_ADMIN),
        eligible_roles=["SALESFORCE_ADMIN"],
        requester_user_id="usr_2",
        require_separate_approver=False,
        already_voted=True,
    )
    assert not result.allowed
    assert "already" in result.reason.lower()


def test_separation_of_duties_blocks_the_requester():
    result = can_decide(
        membership=_membership(ProjectRole.SALESFORCE_ADMIN, user_id="usr_1"),
        eligible_roles=["SALESFORCE_ADMIN"],
        requester_user_id="usr_1",
        require_separate_approver=True,
        already_voted=False,
    )
    assert not result.allowed
    assert "cannot approve" in result.reason


def test_separation_of_duties_allows_a_different_eligible_person():
    result = can_decide(
        membership=_membership(ProjectRole.SALESFORCE_ADMIN, user_id="usr_2"),
        eligible_roles=["SALESFORCE_ADMIN"],
        requester_user_id="usr_1",
        require_separate_approver=True,
        already_voted=False,
    )
    assert result.allowed


# --------------------------------------------------------------- alternatives
def test_a_blocked_action_offers_something_the_user_can_actually_do():
    """A platform that answers 'denied' teaches people to route around it."""
    options = alternatives_for("PRODUCTION_BLOCKED")
    assert options
    assert all({"label", "detail", "action"} <= set(o) for o in options)
    assert any("sandbox" in o["detail"].lower() for o in options)


def test_an_unknown_reason_code_offers_nothing_rather_than_filler():
    assert alternatives_for("SOMETHING_ELSE") == []


# ----------------------------------------------------------------- services
def test_slugify_produces_url_safe_identifiers():
    assert slugify("Acme Corp. (EMEA)") == "acme-corp-emea"
    assert slugify("   ") == "workspace"


@pytest.mark.asyncio
async def test_creating_a_project_makes_the_owner_an_admin_with_a_policy(db, user, company):
    project, membership = await create_project(
        db, company_id=company.id, name="Second Project", owner=user
    )
    await db.commit()
    assert membership.role is ProjectRole.PROJECT_ADMIN
    assert await project_membership(db, user_id=user.id, project_id=project.id) is not None
    assert (
        await project_membership(db, user_id="usr_nonexistent", project_id=project.id)
    ) is None


@pytest.mark.asyncio
async def test_creating_a_company_provisions_a_subscription(db, user):
    """Entitlements have to exist from the first moment, or the first check
    against them silently passes."""
    from app.tenancy.service import subscription_for

    company, membership = await create_company(db, name="Acme", owner=user)
    await db.commit()
    subscription = await subscription_for(db, company.id)
    assert subscription.max_projects >= 1
    assert subscription.company_id == company.id


@pytest.mark.asyncio
async def test_project_slugs_are_unique_within_a_company_only(db, user, company):
    """Two customers may both have a project called 'Sales Cloud'."""
    other, _ = await create_company(db, name="Other Co", owner=user)
    a, _ = await create_project(db, company_id=company.id, name="Sales Cloud", owner=user)
    b, _ = await create_project(db, company_id=company.id, name="Sales Cloud", owner=user)
    c, _ = await create_project(db, company_id=other.id, name="Sales Cloud", owner=user)
    await db.commit()
    assert a.slug != b.slug
    assert c.slug == a.slug
