"""Approval binding, end to end through the runtime.

An APPROVED row is not authority to execute. Before a mutation runs, the
runtime re-checks three things: the approval is still inside its window, the
arguments are still the ones a human saw, and the org state it was proposed
against has not moved. These tests drive the real runtime and assert that a
failure of any one of them leaves Salesforce untouched.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select

from app.agent.runtime import AgentRuntime
from app.models import AgentRun, Approval, ApprovalState, AuditEvent
from app.salesforce.client import SalesforceClient
from tests.test_agent import ModelStub, final_text, tool_use


@pytest.fixture
def patch_agent(monkeypatch, fake_sf):
    def _apply(responses: list[Any]) -> ModelStub:
        stub = ModelStub(responses)
        monkeypatch.setattr("app.agent.runtime.call_model", stub)
        http = fake_sf.client()

        def factory(connection, db=None, **_):
            return SalesforceClient(connection, db, http=http)

        monkeypatch.setattr("app.agent.runtime.SalesforceClient", factory)
        return stub

    return _apply


async def collect(gen) -> list[dict[str, Any]]:
    return [event async for event in gen]


async def _propose(patch_agent, db, user, conversation, connection) -> Approval:
    """Run the agent until it proposes a mutation and pauses for approval."""
    patch_agent(
        [tool_use("create_record", {"object": "Account", "values": {"Name": "Acme"}})]
    )
    runtime = AgentRuntime(db, user, conversation, connection)
    await collect(runtime.start("Create an Account called Acme"))
    return (await db.execute(select(Approval))).scalars().one()


async def _resume(db, user, conversation, connection) -> list[dict[str, Any]]:
    run = (await db.execute(select(AgentRun))).scalars().one()
    return await collect(AgentRuntime(db, user, conversation, connection).resume(run))


def _result_text(stub: ModelStub) -> str:
    """The tool_result content the runtime fed back to the model."""
    blocks = stub.calls[-1]["messages"][-1]["content"]
    return " ".join(str(b.get("content", "")) for b in blocks)


# ---------------------------------------------------------------------------
async def test_a_proposal_binds_the_arguments_and_sets_an_expiry(
    patch_agent, db, user, conversation, connection
):
    approval = await _propose(patch_agent, db, user, conversation, connection)
    assert approval.state is ApprovalState.PENDING
    assert approval.change_hash
    assert approval.expires_at is not None
    assert approval.approvals_required >= 1
    assert approval.eligible_roles


async def test_an_expired_approval_does_not_execute(
    patch_agent, db, user, conversation, connection, fake_sf
):
    approval = await _propose(patch_agent, db, user, conversation, connection)
    approval.state = ApprovalState.APPROVED
    approval.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await db.commit()

    stub = patch_agent([final_text("The approval expired.")])
    events = await _resume(db, user, conversation, connection)

    assert "approval.invalidated" in [e["type"] for e in events]
    assert fake_sf.created == []
    assert "APPROVAL_EXPIRED" in _result_text(stub)

    await db.refresh(approval)
    assert approval.state is ApprovalState.EXPIRED
    assert approval.invalidated_reason


async def test_arguments_swapped_after_approval_do_not_execute(
    patch_agent, db, user, conversation, connection, fake_sf
):
    """The change_hash is what stops an approved 'create Acme' being used to
    create something else."""
    approval = await _propose(patch_agent, db, user, conversation, connection)
    approval.state = ApprovalState.APPROVED
    # Simulate the arguments being altered without going through the API that
    # would have rebound the hash.
    approval.modified_arguments = {
        "object": "Account",
        "values": {"Name": "Something Else Entirely"},
    }
    await db.commit()

    stub = patch_agent([final_text("The approved change no longer matches.")])
    events = await _resume(db, user, conversation, connection)

    assert "approval.invalidated" in [e["type"] for e in events]
    assert fake_sf.created == []
    assert "APPROVAL_ARGUMENTS_CHANGED" in _result_text(stub)


async def test_org_drift_between_proposal_and_execution_stops_the_change(
    patch_agent, db, user, conversation, connection, fake_sf
):
    """A field created by someone else while the approval sat in a queue means
    the approved plan is no longer the right plan."""
    patch_agent(
        [
            tool_use(
                "create_field",
                {"object": "Account", "api_name": "Tier__c", "type": "Text"},
            )
        ]
    )
    runtime = AgentRuntime(db, user, conversation, connection)
    await collect(runtime.start("Add a Tier field to Account"))

    approval = (await db.execute(select(Approval))).scalars().one()
    assert approval.state_fingerprint == {
        "object": "Account",
        "field": "Tier__c",
        "field_exists": False,
    }
    approval.state = ApprovalState.APPROVED
    await db.commit()

    # Someone else adds the field while the approval waits.
    fake_sf.extra_fields.append(
        {
            "name": "Tier__c",
            "label": "Tier",
            "type": "string",
            "createable": True,
            "updateable": True,
            "custom": True,
            "nillable": True,
        }
    )

    stub = patch_agent([final_text("The org changed while this was awaiting approval.")])
    events = await _resume(db, user, conversation, connection)

    assert "approval.invalidated" in [e["type"] for e in events]
    assert "APPROVAL_STATE_DRIFTED" in _result_text(stub)


async def test_an_unchanged_approval_still_executes(
    patch_agent, db, user, conversation, connection, fake_sf
):
    """The guards must not block the normal path."""
    approval = await _propose(patch_agent, db, user, conversation, connection)
    approval.state = ApprovalState.APPROVED
    await db.commit()

    patch_agent([final_text("Created the Acme account.")])
    events = await _resume(db, user, conversation, connection)

    assert "approval.approved" in [e["type"] for e in events]
    assert fake_sf.created and fake_sf.created[0][1]["Name"] == "Acme"


async def test_invalidation_is_written_to_the_audit_log(
    patch_agent, db, user, conversation, connection
):
    approval = await _propose(patch_agent, db, user, conversation, connection)
    approval.state = ApprovalState.APPROVED
    approval.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await db.commit()

    patch_agent([final_text("Expired.")])
    await _resume(db, user, conversation, connection)

    entries = (
        (
            await db.execute(
                select(AuditEvent).where(AuditEvent.action == "approval.invalidated")
            )
        )
        .scalars()
        .all()
    )
    assert entries
    assert entries[0].outcome == "blocked"
    assert entries[0].project_id == conversation.project_id


async def test_every_row_the_runtime_writes_carries_the_tenant(
    patch_agent, db, user, conversation, connection
):
    """Tenant isolation is only real if the writer sets the key."""
    from app.models import ToolExecution

    patch_agent(
        [
            tool_use("query_salesforce", {"soql": "SELECT Id FROM Account LIMIT 5"}),
            final_text("Found them."),
        ]
    )
    await collect(AgentRuntime(db, user, conversation, connection).start("List accounts"))

    org_id = conversation.project_id
    for model in (AgentRun, ToolExecution, AuditEvent):
        rows = (await db.execute(select(model))).scalars().all()
        assert rows
        assert all(r.project_id == org_id for r in rows), model.__name__


async def test_the_tenant_policy_sets_the_step_ceiling(
    patch_agent, db, user, conversation, connection
):
    from app.tenancy.policy import PolicySnapshot

    patch_agent([final_text("Done.")])
    policy = PolicySnapshot(project_id=conversation.project_id, max_agent_steps=3)
    runtime = AgentRuntime(
        db,
        user,
        conversation,
        connection,
        project_id=conversation.project_id,
        policy=policy,
    )
    await collect(runtime.start("Say hello"))
    run = (await db.execute(select(AgentRun))).scalars().one()
    assert run.max_steps == 3


async def test_a_tenant_disabled_tool_is_never_offered_to_the_model(
    patch_agent, db, user, conversation, connection
):
    from app.tenancy.policy import PolicySnapshot

    stub = patch_agent([final_text("I cannot do that here.")])
    policy = PolicySnapshot(
        project_id=conversation.project_id,
        disabled_tools=frozenset({"create_field", "bulk_update"}),
    )
    runtime = AgentRuntime(
        db,
        user,
        conversation,
        connection,
        project_id=conversation.project_id,
        policy=policy,
    )
    await collect(runtime.start("Add a field"))

    offered = {t["name"] for t in stub.calls[0]["tools"]}
    assert "create_field" not in offered
    assert "bulk_update" not in offered
    assert "describe_object" in offered
