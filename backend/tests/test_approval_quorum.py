"""When a change needs more than one person.

The failure that prompted this file: a production field change required two
approvals from different people, the project had exactly one member, and the
card rendered a button reading **"Approve (2/2)"**. Clicking it did nothing
visible — the API refused it, correctly, because that person had already voted.

The control was right. Every part of it was right. What was missing was the
sentence saying it can never be satisfied by one person. A control that stops
and explains nothing is indistinguishable from a control that is broken, and
the person on the other side concludes the product does not work.

So the properties here are about what the product *says*, not only what it
enforces: who may still decide, how many are outstanding, and — the one that
matters — whether this project even contains enough eligible people to finish.
"""

from __future__ import annotations

import httpx
import pytest
import pytest_asyncio

from app.config import settings
from app.db import get_session
from app.main import app
from app.models import (
    AgentRun,
    Approval,
    ApprovalState,
    Environment,
    ProjectMembership,
    ProjectRole,
    RiskLevel,
    RunState,
    User,
)
from app.security.auth import create_session_token

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


@pytest_asyncio.fixture
async def two_approver_change(db, user, project, company, conversation, connection):
    """A production metadata change: HIGH risk, two approvals, admin roles.

    This is exactly what `create_field` against a production org produces — the
    deployment category at HIGH requires two.
    """
    run = AgentRun(
        company_id=company.id,
        project_id=project.id,
        conversation_id=conversation.id,
        user_id=user.id,
        salesforce_connection_id=connection.id,
        state=RunState.WAITING_FOR_APPROVAL,
    )
    db.add(run)
    await db.flush()

    row = Approval(
        company_id=company.id,
        project_id=project.id,
        agent_run_id=run.id,
        conversation_id=conversation.id,
        user_id=user.id,
        salesforce_connection_id=connection.id,
        tool_name="create_field",
        arguments={"object": "Account", "api_name": "Phonev2__c", "type": "Phone"},
        risk_level=RiskLevel.HIGH,
        environment=Environment.PRODUCTION,
        state=ApprovalState.PENDING,
        approvals_required=2,
        eligible_roles=[
            ProjectRole.PROJECT_ADMIN.value,
            ProjectRole.SALESFORCE_ADMIN.value,
            ProjectRole.RELEASE_MANAGER.value,
        ],
    )
    db.add(row)
    await db.commit()
    return row


# ---------------------------------------------------------------------------
# A control that stops has to say why
#
# The failure that prompted this: a production field change needed two
# approvals from different people, the project had one member, and the card
# rendered a button reading "Approve (2/2)". Clicking it did nothing visible —
# the API refused it, correctly, because that person had already voted. From
# the outside, a product that offers an action and then declines it is
# indistinguishable from a broken one.
#
# The control itself was right. What was missing was the sentence explaining
# that it can never be satisfied by one person.
# ---------------------------------------------------------------------------
async def test_a_second_vote_from_the_same_person_is_refused(
    client, auth, db, two_approver_change
):
    """Two approvals means two people. It has always meant that."""
    first = await client.post(
        f"{API}/approvals/{two_approver_change.id}/decision",
        headers=auth,
        json={"decision": "approve"},
    )
    assert first.status_code == 200

    second = await client.post(
        f"{API}/approvals/{two_approver_change.id}/decision",
        headers=auth,
        json={"decision": "approve"},
    )
    assert second.status_code == 403
    assert "already recorded a decision" in second.json()["detail"]


async def test_the_card_stops_offering_a_button_it_would_refuse(
    client, auth, two_approver_change
):
    await client.post(
        f"{API}/approvals/{two_approver_change.id}/decision",
        headers=auth,
        json={"decision": "approve"},
    )
    body = (
        await client.get(f"{API}/approvals/{two_approver_change.id}", headers=auth)
    ).json()

    assert body["you_have_decided"] is True
    assert body["you_can_decide"] is False
    assert "already recorded a decision" in body["you_cannot_decide_because"]
    assert body["outstanding_approvals"] == 1


async def test_a_project_with_too_few_approvers_is_reported_as_deadlocked(
    client, auth, two_approver_change
):
    """The sentence that was missing. One person cannot supply two approvals,
    and the product should say so rather than let them keep clicking."""
    body = (
        await client.get(f"{API}/approvals/{two_approver_change.id}", headers=auth)
    ).json()

    assert body["deadlocked"] is True
    assert body["eligible_approver_count"] == 1
    detail = body["deadlock_detail"]
    assert "Invite another approver" in detail
    # ...and the one thing that is never negotiable stays stated.
    assert "always need two people" in detail


async def test_adding_a_second_eligible_approver_clears_the_deadlock(
    client, auth, db, project, company, two_approver_change
):
    from app.models import ProjectRole, User

    colleague = User(email="second@acme-approve.example.com", display_name="Second")
    db.add(colleague)
    await db.flush()
    db.add(
        ProjectMembership(
            company_id=company.id,
            project_id=project.id,
            user_id=colleague.id,
            role=ProjectRole.PROJECT_ADMIN,
        )
    )
    await db.commit()

    body = (
        await client.get(f"{API}/approvals/{two_approver_change.id}", headers=auth)
    ).json()
    assert body["deadlocked"] is False
    assert body["eligible_approver_count"] == 2


async def test_two_different_people_do_satisfy_it(
    client, auth, db, project, company, two_approver_change
):
    """The control working as designed, end to end."""
    from app.models import ApprovalState, ProjectMembership, ProjectRole
    from app.security.auth import create_session_token

    colleague = User(email="third@acme-approve.example.com", display_name="Third")
    db.add(colleague)
    await db.flush()
    db.add(
        ProjectMembership(
            company_id=company.id,
            project_id=project.id,
            user_id=colleague.id,
            role=ProjectRole.PROJECT_ADMIN,
        )
    )
    await db.commit()

    await client.post(
        f"{API}/approvals/{two_approver_change.id}/decision",
        headers=auth,
        json={"decision": "approve"},
    )
    second = await client.post(
        f"{API}/approvals/{two_approver_change.id}/decision",
        headers={
            "Authorization": f"Bearer {create_session_token(colleague.id, project.id)}",
            "X-Project-Id": project.id,
        },
        json={"decision": "approve"},
    )
    assert second.status_code == 200

    await db.refresh(two_approver_change)
    assert two_approver_change.state is ApprovalState.APPROVED
