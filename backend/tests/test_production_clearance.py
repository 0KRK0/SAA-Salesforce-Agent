"""Three levels have to agree before the agent may change a production org.

The bug this closes was not that the control failed — it worked exactly as
written. It was that the control had the wrong *shape* and could not explain
itself. A project administrator ticked "allow production mutations", the row
saved, and the effective value stayed `no`, because a deployment-wide
environment variable was refusing. Nothing in the response said which level had
refused, so the model — reading only "enable production mutations for this
project" — told the user to ask a project administrator to adjust the policy.
That advice could not possibly have worked, and it is worse than no advice,
because someone will spend an afternoon acting on it.

The second problem is multi-tenancy. A single global switch means either no
customer may ever touch production, or every customer's production posture
rests entirely on their own project settings. Neither is what an enterprise
buys. One customer can be cleared while another is in trial.

So: **deployment ceiling AND company clearance AND project policy**, with the
outermost refusal named.
"""

from __future__ import annotations

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select

from app.config import settings
from app.db import get_session
from app.main import app
from app.models import (
    AuditEvent,
    CompanyMembership,
    CompanyRole,
    ProjectMembership,
    ProjectRole,
    RiskLevel,
    User,
)
from app.security.auth import create_session_token
from app.tenancy.policy import production_posture

API = settings.api_v1


@pytest_asyncio.fixture
async def client(db):
    async def _override():
        yield db

    app.dependency_overrides[get_session] = _override
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def auth(user, project):
    return {
        "Authorization": f"Bearer {create_session_token(user.id, project.id)}",
        "X-Project-Id": project.id,
    }


@pytest.fixture
def deployment_permits(monkeypatch):
    monkeypatch.setattr(settings, "allow_production_mutations", True)


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------
def test_all_three_levels_must_agree(monkeypatch):
    monkeypatch.setattr(settings, "allow_production_mutations", True)
    assert production_posture(company_allows=True, project_allows=True) == (True, "")


@pytest.mark.parametrize(
    ("deployment", "company", "project", "blocked_by"),
    [
        (False, True, True, "deployment"),
        (True, False, True, "company"),
        (True, True, False, "project"),
        # When several refuse, the *outermost* is named: telling someone to tick
        # their project box while the deployment ceiling is down wastes their day.
        (False, False, False, "deployment"),
        (True, False, False, "company"),
    ],
)
def test_the_outermost_refusing_level_is_the_one_named(
    monkeypatch, deployment, company, project, blocked_by
):
    monkeypatch.setattr(settings, "allow_production_mutations", deployment)
    allowed, named = production_posture(company_allows=company, project_allows=project)
    assert allowed is False
    assert named == blocked_by


async def test_a_project_cannot_out_vote_its_company(db, project, company, monkeypatch):
    """The case from the screenshot, inverted: the project says yes, the
    company has not been cleared, and the answer is still no."""
    monkeypatch.setattr(settings, "allow_production_mutations", True)
    from app.tenancy import service as tenancy

    row = await tenancy.policy_row(db, project.id, company.id)
    row.allow_production_mutations = True
    company.allow_production_mutations = False
    await db.commit()

    resolved = await tenancy.policy_for(db, project.id, company.id)
    assert resolved.allow_production_mutations is False
    assert resolved.production_blocked_by == "company"


async def test_with_every_level_saying_yes_it_is_permitted(
    db, project, company, monkeypatch
):
    monkeypatch.setattr(settings, "allow_production_mutations", True)
    from app.tenancy import service as tenancy

    row = await tenancy.policy_row(db, project.id, company.id)
    row.allow_production_mutations = True
    company.allow_production_mutations = True
    await db.commit()

    resolved = await tenancy.policy_for(db, project.id, company.id)
    assert resolved.allow_production_mutations is True
    assert resolved.production_blocked_by == ""


# ---------------------------------------------------------------------------
# The refusal has to name the remedy
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("level", "must_mention"),
    [
        ("deployment", "ALLOW_PRODUCTION_MUTATIONS"),
        ("company", "company administrator"),
        ("project", "project administrator"),
    ],
)
def test_each_level_refuses_with_advice_that_would_actually_work(level, must_mention):
    from app.risk.engine import _production_refusal

    assert must_mention in _production_refusal(level)


def test_the_deployment_refusal_says_no_customer_setting_can_override_it():
    """The specific correction. Someone told to "enable it for this project"
    while the deployment ceiling is down will tick the box, see no change, and
    conclude the product is broken."""
    from app.risk.engine import _production_refusal

    message = _production_refusal("deployment")
    assert "No project or company setting can override it" in message


# ---------------------------------------------------------------------------
# The API explains itself
# ---------------------------------------------------------------------------
async def test_the_policy_endpoint_names_the_blocking_level(client, auth, monkeypatch):
    monkeypatch.setattr(settings, "allow_production_mutations", False)
    body = (await client.get(f"{API}/project/policy", headers=auth)).json()

    assert body["production"]["permitted"] is False
    assert body["production"]["blocked_by"] == "deployment"
    levels = {entry["level"]: entry for entry in body["production"]["levels"]}
    assert set(levels) == {"deployment", "company", "project"}
    assert "restart" in levels["deployment"]["how"].lower()


async def test_the_policy_endpoint_shows_the_company_level(client, auth):
    body = (await client.get(f"{API}/project/policy", headers=auth)).json()
    assert body["company"]["allow_production_mutations"] is False
    assert body["company"]["you_can_change_it"] is True


async def test_salesforce_permissions_are_still_named_as_the_ceiling(client, auth):
    """The platform may only ever reduce what the connected Salesforce user
    could already do. Nothing on this screen implies otherwise."""
    body = (await client.get(f"{API}/project/policy", headers=auth)).json()
    assert "only ever reduce" in body["production"]["note"]


# ---------------------------------------------------------------------------
# Granting it
# ---------------------------------------------------------------------------
async def test_a_company_admin_can_clear_the_company(
    client, auth, db, company, deployment_permits
):
    resp = await client.put(
        f"{API}/project/production-access",
        headers=auth,
        json={"allow_production_mutations": True, "confirm": company.name},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["allow_production_mutations"] is True


async def test_enabling_it_requires_typing_the_company_name(
    client, auth, deployment_permits
):
    """This is the one setting whose blast radius is a live Salesforce org. A
    checkbox toggled by accident looks identical to one toggled on purpose."""
    resp = await client.put(
        f"{API}/project/production-access",
        headers=auth,
        json={"allow_production_mutations": True, "confirm": "whatever"},
    )
    assert resp.status_code == 400
    assert "Type the company name" in resp.json()["detail"]


async def test_it_cannot_be_enabled_above_the_deployment_ceiling(
    client, auth, company, monkeypatch
):
    """Recording a permission nothing honours is how a product ends up with a
    settings screen that lies."""
    monkeypatch.setattr(settings, "allow_production_mutations", False)
    resp = await client.put(
        f"{API}/project/production-access",
        headers=auth,
        json={"allow_production_mutations": True, "confirm": company.name},
    )
    assert resp.status_code == 400
    assert "ALLOW_PRODUCTION_MUTATIONS" in resp.json()["detail"]


async def test_withdrawing_it_needs_no_confirmation(
    client, auth, db, company, deployment_permits
):
    """Asking someone to type a phrase to make things *safer* is friction in
    the wrong direction."""
    company.allow_production_mutations = True
    await db.commit()

    resp = await client.put(
        f"{API}/project/production-access",
        headers=auth,
        json={"allow_production_mutations": False},
    )
    assert resp.status_code == 200
    assert resp.json()["allow_production_mutations"] is False


async def test_withdrawing_it_blocks_every_project_immediately(
    client, auth, db, company, project, deployment_permits
):
    """Whatever each project's own box says."""
    from app.tenancy import service as tenancy

    row = await tenancy.policy_row(db, project.id, company.id)
    row.allow_production_mutations = True
    company.allow_production_mutations = True
    await db.commit()
    assert (await tenancy.policy_for(db, project.id, company.id)).allow_production_mutations

    await client.put(
        f"{API}/project/production-access",
        headers=auth,
        json={"allow_production_mutations": False},
    )

    resolved = await tenancy.policy_for(db, project.id, company.id)
    assert resolved.allow_production_mutations is False
    assert resolved.production_blocked_by == "company"


async def test_the_change_is_audited_as_high_risk(
    client, auth, db, company, deployment_permits
):
    await client.put(
        f"{API}/project/production-access",
        headers=auth,
        json={"allow_production_mutations": True, "confirm": company.name},
    )
    event = (
        await db.execute(
            select(AuditEvent).where(
                AuditEvent.action == "company.production_access_changed"
            )
        )
    ).scalars().one()

    assert event.risk_level is RiskLevel.HIGH
    assert event.arguments["from"] is False
    assert event.arguments["to"] is True


async def test_a_project_admin_who_is_not_a_company_admin_cannot_grant_it(
    client, db, company, project, deployment_permits
):
    """Running one team's workspace does not make somebody accountable for the
    customer's production Salesforce org."""
    member = User(email="lead@acme-prod.example.com", display_name="Team Lead")
    db.add(member)
    await db.flush()
    db.add_all(
        [
            CompanyMembership(
                company_id=company.id, user_id=member.id, role=CompanyRole.COMPANY_MEMBER
            ),
            ProjectMembership(
                company_id=company.id,
                project_id=project.id,
                user_id=member.id,
                # A full project administrator — and still not enough.
                role=ProjectRole.PROJECT_ADMIN,
            ),
        ]
    )
    await db.commit()

    resp = await client.put(
        f"{API}/project/production-access",
        headers={
            "Authorization": f"Bearer {create_session_token(member.id, project.id)}",
            "X-Project-Id": project.id,
        },
        json={"allow_production_mutations": True, "confirm": company.name},
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Developer Edition
#
# The risk engine has always known that a Developer Edition org "is not a
# sandbox and is also nobody's production". Nothing could reach that knowledge,
# because the connection was stamped PRODUCTION at creation from the same
# sandbox flag the carve-out exists to correct — and a declared environment may
# only ever raise the posture, never lower it.
#
# The result: creating a field in a throwaway DE org required production
# clearance, and the red PRODUCTION badge on the screen was wrong.
# ---------------------------------------------------------------------------
def test_a_developer_edition_org_is_not_production_to_the_risk_engine():
    from app.models import Environment as Env
    from app.risk.engine import OrgContext

    org = OrgContext(is_sandbox=False, org_type="Developer Edition")
    assert org.is_production is False

    # ...but a real production org still is.
    assert OrgContext(is_sandbox=False, org_type="Professional Edition").is_production
    # ...and an explicit declaration still raises it.
    assert OrgContext(
        is_sandbox=False, org_type="Developer Edition", environment=Env.PRODUCTION
    ).is_production


def test_an_auto_derived_production_label_is_corrected_for_developer_edition():
    from app.api.routes_salesforce import _settle_environment
    from app.models import Environment as Env
    from app.models import SalesforceConnection

    conn = SalesforceConnection(
        company_id="co_1",
        project_id="prj_1",
        connected_by="usr_1",
        sf_org_id="00D",
        instance_url="https://x.my.salesforce.com",
        access_token_ref="ref",
        is_sandbox=False,
        org_type="Developer Edition",
        environment=Env.PRODUCTION,
    )
    _settle_environment(conn, declared=None)
    assert conn.environment is Env.DEVELOPMENT


def test_an_operators_explicit_production_declaration_is_never_overridden():
    """A declaration may only raise the posture. Someone who said PRODUCTION
    meant it, whatever edition the org turns out to be."""
    from app.api.routes_salesforce import _settle_environment
    from app.models import Environment as Env
    from app.models import SalesforceConnection

    conn = SalesforceConnection(
        company_id="co_1",
        project_id="prj_1",
        connected_by="usr_1",
        sf_org_id="00D",
        instance_url="https://x.my.salesforce.com",
        access_token_ref="ref",
        is_sandbox=False,
        org_type="Developer Edition",
        environment=Env.PRODUCTION,
    )
    _settle_environment(conn, declared=Env.PRODUCTION)
    assert conn.environment is Env.PRODUCTION


def test_a_real_production_org_keeps_its_label():
    from app.api.routes_salesforce import _settle_environment
    from app.models import Environment as Env
    from app.models import SalesforceConnection

    conn = SalesforceConnection(
        company_id="co_1",
        project_id="prj_1",
        connected_by="usr_1",
        sf_org_id="00D",
        instance_url="https://x.my.salesforce.com",
        access_token_ref="ref",
        is_sandbox=False,
        org_type="Enterprise Edition",
        environment=Env.PRODUCTION,
    )
    _settle_environment(conn, declared=None)
    assert conn.environment is Env.PRODUCTION


# ---------------------------------------------------------------------------
# Reading a production org
#
# The refusal that started this: `describe_object` — a pure read — was blocked
# with "changes in the PRODUCTION environment are not permitted", against an org
# it was only inspecting, while the settings screen said production was
# permitted. Two separate mechanisms refuse production, and the one that fired
# gated reads as well as writes.
# ---------------------------------------------------------------------------
def _org(environment):
    from app.risk.engine import OrgContext

    return OrgContext(
        is_sandbox=False,
        org_type="Enterprise Edition",
        environment=environment,
        allow_production_mutations=False,
    )


def test_describing_an_object_in_production_is_never_blocked_by_policy():
    """The exact case. A read against a production org the project may not
    change is still a read."""
    from app.models import Environment as Env
    from app.risk.engine import classify
    from app.tenancy.policy import PolicySnapshot

    policy = PolicySnapshot(
        project_id="prj_1",
        allowed_environments=frozenset({Env.SANDBOX.value}),
        allow_production_mutations=False,
    )
    decision = classify(
        "describe_object",
        {"object": "Account"},
        RiskLevel.LOW,
        _org(Env.PRODUCTION),
        False,
        policy=policy,
        mutating=False,
    )
    assert not decision.blocked


def test_changing_the_same_object_in_the_same_org_is_blocked():
    """The control still does its job — it just stopped catching reads."""
    from app.models import Environment as Env
    from app.risk.engine import classify
    from app.tenancy.policy import PolicySnapshot

    policy = PolicySnapshot(
        project_id="prj_1",
        allowed_environments=frozenset({Env.SANDBOX.value}),
        allow_production_mutations=False,
        production_blocked_by="project",
    )
    decision = classify(
        "create_field",
        {"object": "Account", "api_name": "Tier__c"},
        RiskLevel.MEDIUM,
        _org(Env.PRODUCTION),
        True,
        policy=policy,
        mutating=True,
    )
    assert decision.blocked
    # ...and it refuses through the path that names a remedy, not the older
    # message that only said "not permitted".
    assert decision.reason_code == "PRODUCTION_BLOCKED"
    assert "project administrator" in decision.blocked_reason


def test_the_second_production_switch_names_itself(monkeypatch):
    """Two deployment flags refuse production. Someone who already turned on
    ALLOW_PRODUCTION_MUTATIONS and is staring at it needs to be pointed at the
    other one by name."""
    from app.models import Environment as Env
    from app.risk.engine import classify
    from app.tenancy.policy import PolicySnapshot

    monkeypatch.setattr(settings, "allow_production_mutations", True)
    monkeypatch.setattr(settings, "feature_production_deployment", False)

    policy = PolicySnapshot(
        project_id="prj_1",
        allowed_environments=frozenset({Env.SANDBOX.value}),
        allow_production_mutations=True,
        production_blocked_by="",
    )
    decision = classify(
        "create_field",
        {"object": "Account"},
        RiskLevel.MEDIUM,
        _org(Env.PRODUCTION),
        True,
        policy=policy,
        mutating=True,
    )
    assert decision.blocked
    assert "FEATURE_PRODUCTION_DEPLOYMENT" in decision.blocked_reason
    assert "Reading and diagnosing" in decision.blocked_reason
