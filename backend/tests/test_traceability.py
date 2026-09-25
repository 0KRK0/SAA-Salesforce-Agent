"""One piece of work, across three systems.

The property under test: **an operation the agent described but did not perform
does not appear in the trail.** Everything here has a recorded API call behind
it, and the endpoint reads that record rather than reconstructing a story from
timestamps.
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
    AuditEvent,
    ExecutionState,
    RiskLevel,
    RunState,
    ToolExecution,
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
async def cross_system_run(db, user, conversation, connection, project):
    """A run that read Jira, changed Salesforce and committed to a repository."""
    run = AgentRun(
        company_id=project.company_id,
        project_id=project.id,
        conversation_id=conversation.id,
        user_id=user.id,
        salesforce_connection_id=connection.id,
        state=RunState.COMPLETED,
        user_request="Add a Tier field to Account, ticket SF-142",
        llm_provider="ANTHROPIC",
        model="claude-sonnet-4-5",
    )
    db.add(run)
    await db.flush()

    common = {
        "company_id": project.company_id,
        "project_id": project.id,
        "user_id": user.id,
        "agent_run_id": run.id,
        "correlation_id": run.correlation_id,
    }
    db.add_all(
        [
            AuditEvent(
                **common,
                action="jira.get_issue",
                tool_name="jira_get_issue",
                arguments={"issue_key": "SF-142"},
                outcome="ok",
            ),
            AuditEvent(
                **common,
                action="tool.create_field",
                tool_name="create_field",
                salesforce_object="Account",
                arguments={"object": "Account", "api_name": "Tier__c"},
                risk_level=RiskLevel.MEDIUM,
                outcome="ok",
            ),
            AuditEvent(
                **common,
                action="repository.commit_metadata",
                tool_name="commit_metadata_to_repository",
                arguments={"repository": "acme/sfdx", "branch": "agent/sf-142"},
                outcome="ok",
            ),
            AuditEvent(
                **common,
                action="jira.comment",
                tool_name="jira_comment",
                arguments={"issue_key": "SF-142"},
                outcome="ok",
            ),
        ]
    )
    db.add(
        ToolExecution(
            company_id=project.company_id,
            project_id=project.id,
            agent_run_id=run.id,
            conversation_id=conversation.id,
            user_id=user.id,
            tool_name="create_field",
            execution_state=ExecutionState.SUCCEEDED,
            risk_level=RiskLevel.MEDIUM,
            approval_state=ApprovalState.APPROVED,
            salesforce_object="Account",
        )
    )
    db.add(
        Approval(
            company_id=project.company_id,
            project_id=project.id,
            agent_run_id=run.id,
            conversation_id=conversation.id,
            user_id=user.id,
            tool_name="create_field",
            state=ApprovalState.APPROVED,
            risk_level=RiskLevel.MEDIUM,
            approved_by=user.id,
        )
    )
    await db.commit()
    return run


# ---------------------------------------------------------------------------
# The trail
# ---------------------------------------------------------------------------
async def test_a_run_reports_what_it_touched_in_each_system(
    client, auth, cross_system_run
):
    body = (
        await client.get(f"{API}/traceability/runs/{cross_system_run.id}", headers=auth)
    ).json()
    assert set(body["systems"]) >= {"jira", "salesforce", "repository"}
    assert body["systems"]["jira"]["count"] == 2
    assert body["systems"]["salesforce"]["count"] == 1
    assert body["systems"]["repository"]["count"] == 1


async def test_the_salesforce_view_carries_the_executions_and_approvals(
    client, auth, cross_system_run
):
    body = (
        await client.get(f"{API}/traceability/runs/{cross_system_run.id}", headers=auth)
    ).json()
    executions = body["salesforce"]["tool_executions"]
    assert executions[0]["tool"] == "create_field"
    assert executions[0]["object"] == "Account"
    assert body["approvals"][0]["state"] == "APPROVED"


async def test_the_trail_states_that_it_records_only_real_operations(
    client, auth, cross_system_run
):
    """The distinction this endpoint exists to make."""
    body = (
        await client.get(f"{API}/traceability/runs/{cross_system_run.id}", headers=auth)
    ).json()
    assert "did not perform does not appear" in body["note"]


async def test_a_correlation_id_gathers_every_run_in_the_chain(
    client, auth, db, cross_system_run, user, conversation, project
):
    """A run that pauses for approval and resumes is one piece of work."""
    second = AgentRun(
        company_id=project.company_id,
        project_id=project.id,
        conversation_id=conversation.id,
        user_id=user.id,
        state=RunState.COMPLETED,
        correlation_id=cross_system_run.correlation_id,
    )
    db.add(second)
    await db.commit()

    body = (
        await client.get(
            f"{API}/traceability/correlation/{cross_system_run.correlation_id}",
            headers=auth,
        )
    ).json()
    assert {r["id"] for r in body["runs"]} == {cross_system_run.id, second.id}


async def test_an_unclassified_action_is_reported_as_other_not_guessed(
    client, auth, db, cross_system_run, project, user
):
    """A wrong system attribution in an audit trail is worse than an
    unclassified one."""
    db.add(
        AuditEvent(
            company_id=project.company_id,
            project_id=project.id,
            user_id=user.id,
            agent_run_id=cross_system_run.id,
            correlation_id=cross_system_run.correlation_id,
            action="something.unrecognised",
            outcome="ok",
        )
    )
    await db.commit()
    body = (
        await client.get(f"{API}/traceability/runs/{cross_system_run.id}", headers=auth)
    ).json()
    assert body["systems"]["other"]["count"] == 1


async def test_a_run_from_another_project_is_not_traceable(
    client, auth, db, user, project
):
    from app.models import Company, Conversation, Project

    other_company = Company(name="Rival Trace", slug="rival-trace")
    db.add(other_company)
    await db.flush()
    other_project = Project(
        company_id=other_company.id, name="Theirs", slug="theirs-trace"
    )
    db.add(other_project)
    await db.flush()
    other_conversation = Conversation(
        company_id=other_company.id, project_id=other_project.id, user_id=user.id
    )
    db.add(other_conversation)
    await db.flush()
    theirs = AgentRun(
        company_id=other_company.id,
        project_id=other_project.id,
        conversation_id=other_conversation.id,
        user_id=user.id,
        state=RunState.COMPLETED,
    )
    db.add(theirs)
    await db.commit()

    assert (
        await client.get(f"{API}/traceability/runs/{theirs.id}", headers=auth)
    ).status_code == 404
    assert (
        await client.get(
            f"{API}/traceability/correlation/{theirs.correlation_id}", headers=auth
        )
    ).status_code == 404


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------
async def test_searching_by_ticket_finds_the_work_that_touched_it(
    client, auth, cross_system_run
):
    """The question an auditor asks is "what did this system do about SF-142",
    not "show me run 7f3a"."""
    body = (
        await client.get(f"{API}/traceability/search?reference=SF-142", headers=auth)
    ).json()
    assert body["match_count"] >= 2
    assert cross_system_run.correlation_id in body["correlation_ids"]
    systems = {m["system"] for m in body["matches"]}
    assert "jira" in systems
    # The branch was named after the ticket, so the commit matches too — which
    # is the useful answer, not a false positive.
    assert "repository" in systems


async def test_searching_by_object_name_finds_the_salesforce_change(
    client, auth, cross_system_run
):
    body = (
        await client.get(f"{API}/traceability/search?reference=Account", headers=auth)
    ).json()
    assert any(m["action"] == "tool.create_field" for m in body["matches"])


async def test_searching_by_repository_finds_the_commit(client, auth, cross_system_run):
    body = (
        await client.get(f"{API}/traceability/search?reference=acme/sfdx", headers=auth)
    ).json()
    assert any(m["system"] == "repository" for m in body["matches"])


async def test_search_finds_nothing_for_an_unrelated_reference(
    client, auth, cross_system_run
):
    body = (
        await client.get(f"{API}/traceability/search?reference=NOPE-999", headers=auth)
    ).json()
    assert body["match_count"] == 0
    assert body["correlation_ids"] == []


# ---------------------------------------------------------------------------
# The runtime writes the thread
# ---------------------------------------------------------------------------
async def test_the_runtime_stamps_audit_rows_with_the_runs_correlation_id(
    db, user, conversation, connection, monkeypatch, fake_sf
):
    """Without this the trail cannot be assembled at all, and every test above
    would be testing fixtures rather than behaviour."""
    from sqlalchemy import select

    from app.agent.runtime import AgentRuntime
    from app.salesforce.client import SalesforceClient
    from tests.test_agent import ModelStub, final_text, tool_use

    stub = ModelStub(
        [tool_use("describe_object", {"object": "Account"}), final_text("Done.")]
    )
    monkeypatch.setattr("app.agent.runtime.call_model", stub)
    http = fake_sf.client()
    monkeypatch.setattr(
        "app.agent.runtime.SalesforceClient",
        lambda connection, db=None, **_: SalesforceClient(connection, db, http=http),
    )

    runtime = AgentRuntime(db, user, conversation, connection)
    events = [e async for e in runtime.start("Describe Account")]
    assert events

    run = (await db.execute(select(AgentRun))).scalars().one()
    audit = (await db.execute(select(AuditEvent))).scalars().all()
    assert audit
    assert all(a.correlation_id == run.correlation_id for a in audit)


# ---------------------------------------------------------------------------
# The classifier is derived, not guessed
# ---------------------------------------------------------------------------
def test_every_registered_tool_classifies_to_a_real_system():
    """The first version of this hardcoded action prefixes and filed 39 of 52
    tools under "other" — every Salesforce tool whose audit_action did not
    happen to start with `tool.`. A trail that files most of a change under
    "other" is worse than no trail, because it reads as complete."""
    from app.api.routes_traceability import _system_for
    from app.tools.registry import build_registry

    for tool in build_registry().all():
        action = tool.audit_action or f"tool.{tool.name}"
        assert _system_for(action) != "other", f"{tool.name} -> {action}"


def test_a_tool_is_classified_by_the_provider_it_declares():
    from app.api.routes_traceability import _system_for
    from app.tools.registry import build_registry

    registry = build_registry()
    assert _system_for(registry.get("describe_object").audit_action) == "salesforce"
    assert _system_for(registry.get("jira_search").audit_action) == "jira"
    assert _system_for(registry.get("commit_to_repository").audit_action) == "repository"


def test_platform_events_are_classified_by_prefix():
    from app.api.routes_traceability import _system_for

    assert _system_for("approval.approved") == "approval"
    assert _system_for("agent.run_failed") == "agent"
    assert _system_for("auth.sso_login") == "platform"
    assert _system_for("retention.swept") == "platform"


def test_an_mcp_tool_is_named_as_mcp_not_as_salesforce():
    """An MCP server's tool is not a first-party Salesforce operation, and an
    audit reader needs to see which is which."""
    from app.api.routes_traceability import _system_for

    assert _system_for("tool.mcp__acme__search") == "mcp"


def test_a_genuinely_unknown_action_is_still_reported_as_other():
    """The fallback stays honest: better unclassified than misattributed."""
    from app.api.routes_traceability import _system_for

    assert _system_for("something.nobody.wrote") == "other"
