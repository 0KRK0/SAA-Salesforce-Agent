"""API-level tests (ASGI transport, no network)."""

from __future__ import annotations

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select

from app.config import settings
from app.db import get_session
from app.main import app
from app.models import AgentRun, Approval, Conversation, RunState
from app.security.auth import create_session_token
from app.tenancy.policy import change_hash

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


async def test_health_lists_tools(client):
    resp = await client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert "describe_object" in body["tools"]
    assert "create_field" in body["tools"]


async def test_tool_catalog_exposes_risk_metadata(client, auth):
    body = (await client.get(f"{API}/tools", headers=auth)).json()
    by_name = {t["name"]: t for t in body["tools"]}
    assert by_name["query_salesforce"]["risk"] == "LOW"
    assert by_name["query_salesforce"]["requires_approval"] is False
    assert by_name["deploy_metadata"]["risk"] == "HIGH"
    assert by_name["create_field"]["requires_approval"] is True


async def test_requires_authentication(client):
    assert (await client.get(f"{API}/conversations")).status_code == 401


async def test_login_and_me(client):
    resp = await client.post(
        f"{API}/auth/login", json={"email": "person@example.com", "display_name": "Person"}
    )
    assert resp.status_code == 200
    me = await client.get(f"{API}/auth/me")
    assert me.status_code == 200
    assert me.json()["email"] == "person@example.com"


async def test_conversation_crud(client, auth, db, connection):
    created = await client.post(
        f"{API}/conversations",
        json={"title": "Org cleanup", "salesforce_connection_id": connection.id},
        headers=auth,
    )
    assert created.status_code == 200
    cid = created.json()["id"]

    listed = await client.get(f"{API}/conversations", headers=auth)
    assert [c["id"] for c in listed.json()] == [cid]

    detail = await client.get(f"{API}/conversations/{cid}", headers=auth)
    assert detail.json()["conversation"]["title"] == "Org cleanup"

    deleted = await client.delete(f"{API}/conversations/{cid}", headers=auth)
    assert deleted.json() == {"success": True}


async def test_cannot_read_another_tenants_conversation(client, auth, db, user):
    """Project isolation: a conversation id from another project must read as
    'not found', not as someone else's data."""
    from app.models import Company, Project, ProjectMembership, ProjectRole, User

    other_company = Company(name="Rival Corp", slug="rival-corp")
    other = User(email="other@example.com")
    db.add_all([other_company, other])
    await db.flush()
    other_project = Project(
        company_id=other_company.id, name="Rival Project", slug="rival-project"
    )
    db.add(other_project)
    await db.flush()
    db.add(
        ProjectMembership(
            company_id=other_company.id,
            project_id=other_project.id,
            user_id=other.id,
            role=ProjectRole.PROJECT_ADMIN,
        )
    )
    conv = Conversation(
        company_id=other_company.id,
        project_id=other_project.id,
        user_id=other.id,
        title="theirs",
    )
    db.add(conv)
    await db.commit()

    resp = await client.get(f"{API}/conversations/{conv.id}", headers=auth)
    assert resp.status_code == 404


async def test_salesforce_config_endpoint(client, auth):
    """Tenant-scoped, because "is Salesforce configured" became a per-company
    question the moment a company could register its own External Client App:
    a company with one can connect an org on a deployment that has no shared
    app at all."""
    body = (await client.get(f"{API}/salesforce/config", headers=auth)).json()
    assert body["configured"] is True
    assert body["api_version"]
    # The exact string to paste into the External Client App, derived from the
    # route that is actually mounted.
    assert body["callback_url"].endswith("/salesforce/oauth/callback")
    assert body["callback_problem"] is None


async def test_approval_decision_flow(client, auth, db, user, conversation, connection):
    """One eligible approver satisfies a single-approver policy."""
    run = AgentRun(
        company_id=conversation.company_id,
        project_id=conversation.project_id,
        conversation_id=conversation.id,
        user_id=user.id,
        salesforce_connection_id=connection.id,
        state=RunState.WAITING_FOR_APPROVAL,
    )
    db.add(run)
    await db.flush()
    approval = Approval(
        company_id=conversation.company_id,
        project_id=conversation.project_id,
        agent_run_id=run.id,
        conversation_id=conversation.id,
        user_id=user.id,
        salesforce_connection_id=connection.id,
        tool_name="create_field",
        tool_use_id="toolu_1",
        arguments={"object": "Account"},
        plan={"title": "Create field"},
        change_hash=change_hash("create_field", {"object": "Account"}),
        eligible_roles=["PROJECT_ADMIN", "SALESFORCE_ADMIN"],
    )
    db.add(approval)
    run.pending_approval_ids = [approval.id]
    await db.commit()

    listed = await client.get(f"{API}/approvals?state=PENDING", headers=auth)
    assert listed.json()["count"] == 1

    decided = await client.post(
        f"{API}/approvals/{approval.id}/decision",
        json={"decision": "approve", "note": "ok in sandbox"},
        headers=auth,
    )
    body = decided.json()
    assert body["approval"]["state"] == "APPROVED"
    assert body["resume_ready"] is True

    again = await client.post(
        f"{API}/approvals/{approval.id}/decision", json={"decision": "reject"}, headers=auth
    )
    assert again.status_code == 409

    audit = await client.get(f"{API}/audit", headers=auth)
    assert any(e["action"] == "approval.approved" for e in audit.json()["entries"])


async def test_resume_rejects_run_not_awaiting_approval(client, auth, db, user, conversation):
    run = AgentRun(
        company_id=conversation.company_id,
        project_id=conversation.project_id,
        conversation_id=conversation.id,
        user_id=user.id,
        state=RunState.COMPLETED,
    )
    db.add(run)
    await db.commit()
    resp = await client.post(f"{API}/runs/{run.id}/resume", headers=auth)
    assert resp.status_code == 409


async def test_posting_a_message_queues_a_run_rather_than_doing_the_work(
    client, auth, db, conversation
):
    """The request that accepts work does not do it.

    202, and a run id. That separation is what lets the browser go away without
    taking the customer's change with it.
    """
    resp = await client.post(
        f"{API}/conversations/{conversation.id}/messages",
        json={"message": "Inspect the Account object"},
        headers=auth,
    )
    assert resp.status_code == 202
    body = resp.json()
    assert body["run_id"]
    assert body["state"] == RunState.QUEUED.value

    run = (await db.execute(select(AgentRun))).scalars().one()
    assert run.state is RunState.QUEUED
    # Nothing has been claimed or executed yet.
    assert run.claimed_by is None
    assert run.steps_used == 0
