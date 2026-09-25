"""Durable, server-owned execution.

The promise this phase makes to a customer is narrow and testable: **closing a
tab does not stop a change to your Salesforce org, and reopening one does not
start a second.** Everything here defends some part of that.

The worker is driven explicitly rather than by its background loop, so these
tests are deterministic — no sleeping, no flakes.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.db import SessionLocal
from app.execution import events as run_events
from app.execution import queue as run_queue
from app.execution.state import (
    IllegalTransition,
    can_transition,
    is_terminal,
    transition,
)
from app.execution.worker import RunWorker
from app.models import AgentRun, Approval, ApprovalState, Message, RunEvent, RunState
from app.salesforce.client import SalesforceClient
from tests.test_agent import ModelStub, final_text, tool_use


@pytest.fixture
def patch_model(monkeypatch, fake_sf):
    """Stub the gateway and the Salesforce transport for worker-run code.

    Patched at module level so it applies inside the worker's own sessions, not
    just the test's.
    """

    def _apply(responses):
        stub = ModelStub(responses)
        monkeypatch.setattr("app.agent.runtime.call_model", stub)
        http = fake_sf.client()
        monkeypatch.setattr(
            "app.agent.runtime.SalesforceClient",
            lambda connection, db=None, **_: SalesforceClient(connection, db, http=http),
        )
        return stub

    return _apply


@pytest_asyncio.fixture
async def worker(db):
    """A worker over the real session factory, driven one tick at a time."""
    return RunWorker(SessionLocal, owner="test-worker", concurrency=2)


async def _drain(worker: RunWorker, limit: int = 20) -> None:
    """Run ticks until the worker has nothing in flight."""
    import asyncio

    for _ in range(limit):
        await worker.tick()
        if not worker._active:
            return
        await asyncio.sleep(0.01)


# ---------------------------------------------------------------------------
# The state machine
# ---------------------------------------------------------------------------
def test_a_finished_run_cannot_be_reopened():
    """The single most important rule. A COMPLETED run that could go back to
    EXECUTING would let the same approval authorize a second change."""
    for terminal in (
        RunState.COMPLETED,
        RunState.FAILED,
        RunState.CANCELLED,
        RunState.EXPIRED,
    ):
        assert is_terminal(terminal)
        for target in RunState:
            if target is terminal:
                continue
            assert not can_transition(terminal, target)


def test_a_waiting_run_cannot_jump_straight_to_completed():
    """That would mean claiming success for work the approval authorized but
    nothing ever executed."""
    assert not can_transition(
        RunState.WAITING_FOR_APPROVAL, RunState.COMPLETED
    )
    assert can_transition(RunState.WAITING_FOR_APPROVAL, RunState.EXECUTING)


def test_re_asserting_the_current_state_is_not_an_error():
    """The runtime sets PLANNING on every step; it should not have to remember
    whether it was already there."""
    assert can_transition(RunState.PLANNING, RunState.PLANNING)
    assert transition(RunState.PLANNING, RunState.PLANNING) is RunState.PLANNING


def test_an_illegal_transition_raises_rather_than_silently_correcting():
    with pytest.raises(IllegalTransition):
        transition(RunState.COMPLETED, RunState.EXECUTING)


# ---------------------------------------------------------------------------
# Claiming
# ---------------------------------------------------------------------------
async def test_only_one_worker_can_claim_a_run(db, conversation, project):
    """Two workers racing must produce one winner and one no-op, or the same
    change is made twice."""
    run = AgentRun(
        company_id=project.company_id,
        project_id=project.id,
        conversation_id=conversation.id,
        user_id=conversation.user_id,
        state=RunState.QUEUED,
    )
    db.add(run)
    await db.commit()

    first = await run_queue.claim(db, run.id, "worker-a")
    second = await run_queue.claim(db, run.id, "worker-b")
    assert first is not None
    assert second is None
    assert first.claimed_by == "worker-a"


async def test_claiming_skips_runs_that_are_not_queued(db, conversation, project):
    run = AgentRun(
        company_id=project.company_id,
        project_id=project.id,
        conversation_id=conversation.id,
        user_id=conversation.user_id,
        state=RunState.COMPLETED,
    )
    db.add(run)
    await db.commit()
    assert await run_queue.claim(db, run.id, "worker-a") is None


async def test_a_stale_claim_is_reclaimed_rather_than_stranded(
    db, conversation, project
):
    """A crashed worker cannot release its own claim. Ownership expires instead,
    and the run goes back to QUEUED — nothing is known to be wrong with the
    *work*, only with the process that was doing it."""
    run = AgentRun(
        company_id=project.company_id,
        project_id=project.id,
        conversation_id=conversation.id,
        user_id=conversation.user_id,
        state=RunState.EXECUTING,
        claimed_by="dead-worker",
        claimed_at=datetime.now(UTC) - timedelta(hours=1),
        heartbeat_at=datetime.now(UTC) - timedelta(hours=1),
    )
    db.add(run)
    await db.commit()

    reclaimed = await run_queue.reclaim_stale(db, timeout_seconds=60)
    await db.commit()
    assert run.id in reclaimed
    assert run.state is RunState.QUEUED
    assert run.claimed_by is None


async def test_a_live_claim_is_left_alone(db, conversation, project):
    run = AgentRun(
        company_id=project.company_id,
        project_id=project.id,
        conversation_id=conversation.id,
        user_id=conversation.user_id,
        state=RunState.EXECUTING,
        claimed_by="busy-worker",
        heartbeat_at=datetime.now(UTC),
    )
    db.add(run)
    await db.commit()
    assert await run_queue.reclaim_stale(db, timeout_seconds=60) == []
    assert run.claimed_by == "busy-worker"


async def test_a_run_waiting_on_a_human_is_never_reclaimed(db, conversation, project):
    """A person taking an hour to approve something is not a stalled worker."""
    run = AgentRun(
        company_id=project.company_id,
        project_id=project.id,
        conversation_id=conversation.id,
        user_id=conversation.user_id,
        state=RunState.WAITING_FOR_APPROVAL,
        claimed_by="worker",
        heartbeat_at=datetime.now(UTC) - timedelta(days=1),
    )
    db.add(run)
    await db.commit()
    assert await run_queue.reclaim_stale(db, timeout_seconds=60) == []


async def test_an_overdue_run_is_expired_not_failed(db, conversation, project):
    """"We stopped waiting" is a different statement from "the work went
    wrong", and an operator reading audit needs to tell them apart."""
    run = AgentRun(
        company_id=project.company_id,
        project_id=project.id,
        conversation_id=conversation.id,
        user_id=conversation.user_id,
        state=RunState.EXECUTING,
        deadline_at=datetime.now(UTC) - timedelta(minutes=1),
    )
    db.add(run)
    await db.commit()

    expired = await run_queue.expire_overdue(db)
    await db.commit()
    assert run.id in expired
    assert run.state is RunState.EXPIRED
    assert run.error_code == "RUN_EXPIRED"


# ---------------------------------------------------------------------------
# The worker actually runs the work
# ---------------------------------------------------------------------------
async def test_a_queued_run_is_executed_by_the_worker(
    db, conversation, connection, user, worker, patch_model
):
    """The core promise: nobody is watching, and the work still happens."""
    from app.agent.runtime import AgentRuntime

    patch_model(
        [
            tool_use("describe_object", {"object": "Account"}),
            final_text("Account has 4 fields."),
        ]
    )
    runtime = AgentRuntime(db, user, conversation, connection)
    run = await runtime.create_run("Describe Account")
    assert run.state is RunState.QUEUED

    await _drain(worker)

    async with SessionLocal() as fresh:
        reloaded = await fresh.get(AgentRun, run.id)
        assert reloaded.state is RunState.COMPLETED
        assert reloaded.final_text
        # Ownership is released on completion; a finished run holding a claim
        # would look abandoned to the reclaimer forever.
        assert reloaded.claimed_by is None


async def test_the_timeline_is_written_durably_as_the_run_progresses(
    db, conversation, connection, user, worker, patch_model
):
    from app.agent.runtime import AgentRuntime

    patch_model(
        [
            tool_use("describe_object", {"object": "Account"}),
            final_text("Done."),
        ]
    )
    run = await AgentRuntime(db, user, conversation, connection).create_run("Go")
    await _drain(worker)

    async with SessionLocal() as fresh:
        rows = (
            await fresh.execute(
                select(RunEvent)
                .where(RunEvent.agent_run_id == run.id)
                .order_by(RunEvent.sequence)
            )
        ).scalars().all()

    assert rows, "the run left no timeline"
    kinds = [r.event_type for r in rows]
    assert "run.started" in kinds
    assert "tool.started" in kinds
    assert "run.completed" in kinds
    # Sequences are dense and monotonic, which is what makes resume exact.
    assert [r.sequence for r in rows] == list(range(1, len(rows) + 1))
    assert all(r.project_id == conversation.project_id for r in rows)


async def test_reading_from_a_cursor_returns_exactly_what_was_missed(
    db, conversation, connection, user, worker, patch_model
):
    """A reconnecting client must get no gaps and no duplicates."""
    from app.agent.runtime import AgentRuntime

    patch_model([tool_use("describe_object", {"object": "Account"}), final_text("Done.")])
    run = await AgentRuntime(db, user, conversation, connection).create_run("Go")
    await _drain(worker)

    async with SessionLocal() as fresh:
        everything = await run_events.read(
            fresh, agent_run_id=run.id, project_id=conversation.project_id, after=0
        )
        halfway = everything[len(everything) // 2].sequence
        rest = await run_events.read(
            fresh,
            agent_run_id=run.id,
            project_id=conversation.project_id,
            after=halfway,
        )

    assert [e.sequence for e in rest] == [
        e.sequence for e in everything if e.sequence > halfway
    ]


async def test_a_timeline_cannot_be_read_from_another_project(
    db, conversation, connection, user, worker, patch_model
):
    from app.agent.runtime import AgentRuntime

    patch_model([final_text("Done.")])
    run = await AgentRuntime(db, user, conversation, connection).create_run("Go")
    await _drain(worker)

    async with SessionLocal() as fresh:
        assert (
            await run_events.read(
                fresh, agent_run_id=run.id, project_id="prj_someone_else", after=0
            )
            == []
        )


async def test_a_run_whose_conversation_vanished_fails_with_a_reason(
    db, conversation, connection, user, worker, patch_model, monkeypatch
):
    """The worker rebuilds everything from persisted state. When it cannot, it
    says so rather than guessing around the gap.

    Reaching that branch needs care about *how* the conversation goes missing.
    An earlier version of this test simply wrote `run.conversation_id =
    "conv_deleted"`. That passes on SQLite, which does not enforce foreign keys
    by default, and fails on Postgres, which does — the row cannot be written
    at all. So the test was asserting behaviour for a state the shipping
    database will not produce, and the assertion was worth nothing.

    The defensive branch still earns its place: a restored backup, a DBA
    deleting rows with constraints disabled, or a future schema that nullifies
    the column all reach it. What the test must not do is manufacture the state
    by writing an illegal row. Missing the lookup is the engine-independent way
    to get there, and it exercises the worker rather than the database.
    """
    from app.agent.runtime import AgentRuntime
    from app.models import Project

    patch_model([final_text("Done.")])
    run = await AgentRuntime(db, user, conversation, connection).create_run("Go")
    await db.commit()

    # Point the worker's conversation lookup at a table that cannot hold a
    # `conv_` id, so `db.get(...)` returns None exactly as it would for a row
    # that is genuinely gone.
    monkeypatch.setattr("app.execution.worker.Conversation", Project)

    await _drain(worker)

    async with SessionLocal() as fresh:
        reloaded = await fresh.get(AgentRun, run.id)
        assert reloaded.state is RunState.FAILED
        assert reloaded.error_code == "RUN_CONTEXT_MISSING"


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------
async def test_cancelling_an_unclaimed_run_stops_it_outright(
    db, conversation, connection, user
):
    """Nothing is in flight, so there is nothing to wait for."""
    from app.agent.runtime import AgentRuntime

    run = await AgentRuntime(db, user, conversation, connection).create_run("Go")
    accepted = await run_queue.request_cancel(db, run, user.id)
    await db.commit()

    assert accepted is True
    assert run.state is RunState.CANCELLED
    assert run.cancel_requested_by == user.id


async def test_a_cancelled_run_stops_before_the_next_model_call(
    db, conversation, connection, user, worker, patch_model
):
    """Cancellation is cooperative: the run stops at a step boundary, not
    mid-call, so the org is never left in a state this system cannot describe.
    """
    from app.agent.runtime import AgentRuntime

    stub = patch_model(
        [
            tool_use("describe_object", {"object": "Account"}),
            final_text("Should never be reached."),
        ]
    )
    run = await AgentRuntime(db, user, conversation, connection).create_run("Go")

    # Cancel before the worker starts. The run is claimed, then stops at its
    # first boundary check.
    run.cancel_requested = True
    run.cancel_requested_by = user.id
    await db.commit()

    await _drain(worker)

    async with SessionLocal() as fresh:
        reloaded = await fresh.get(AgentRun, run.id)
        assert reloaded.state is RunState.CANCELLED
        assert reloaded.finished_at is not None
    # Nothing was asked of the model at all.
    assert stub.calls == []


async def test_cancelling_a_finished_run_is_refused_not_pretended(
    db, conversation, connection, user, worker, patch_model
):
    from app.agent.runtime import AgentRuntime

    patch_model([final_text("Done.")])
    run = await AgentRuntime(db, user, conversation, connection).create_run("Go")
    await _drain(worker)

    async with SessionLocal() as fresh:
        reloaded = await fresh.get(AgentRun, run.id)
        accepted = await run_queue.request_cancel(fresh, reloaded, user.id)
        await fresh.commit()
    assert accepted is False


async def test_a_cancelled_run_records_what_it_had_already_done(
    db, conversation, connection, user, worker, patch_model
):
    """A user who cancels still needs to know exactly what was changed before
    the stop took effect."""
    from app.agent.runtime import AgentRuntime

    patch_model([final_text("unused")])
    run = await AgentRuntime(db, user, conversation, connection).create_run("Go")
    run.cancel_requested = True
    await db.commit()
    await _drain(worker)

    async with SessionLocal() as fresh:
        reloaded = await fresh.get(AgentRun, run.id)
        assert "cancelled" in (reloaded.final_text or "").lower()
        messages = (
            await fresh.execute(
                select(Message).where(Message.agent_run_id == run.id)
            )
        ).scalars().all()
        # The user's message and an assistant message explaining the stop.
        assert any(m.role == "assistant" for m in messages)


# ---------------------------------------------------------------------------
# Approval handoff
# ---------------------------------------------------------------------------
async def test_a_run_waiting_for_approval_releases_its_worker_slot(
    db, conversation, connection, user, worker, patch_model
):
    """Holding a claim for however long a human takes would block the slot and
    make the run look abandoned to the reclaimer."""
    from app.agent.runtime import AgentRuntime

    patch_model(
        [
            tool_use(
                "create_field",
                {
                    "object": "Account",
                    "api_name": "Tier",
                    "label": "Tier",
                    "type": "Text",
                    "length": 40,
                },
            ),
            final_text("unused"),
        ]
    )
    run = await AgentRuntime(db, user, conversation, connection).create_run("Add a field")
    await _drain(worker)

    async with SessionLocal() as fresh:
        reloaded = await fresh.get(AgentRun, run.id)
        assert reloaded.state is RunState.WAITING_FOR_APPROVAL
        assert reloaded.claimed_by is None
        approvals = (
            await fresh.execute(
                select(Approval).where(Approval.agent_run_id == run.id)
            )
        ).scalars().all()
        assert approvals
        assert approvals[0].state is ApprovalState.PENDING


async def test_resuming_re_queues_rather_than_running_inline(
    db, conversation, connection, user, worker, patch_model
):
    """Resume is the same durability contract as the first message: queue it,
    let a worker do it, stream the log."""
    from app.agent.runtime import AgentRuntime

    patch_model(
        [
            tool_use(
                "create_field",
                {
                    "object": "Account",
                    "api_name": "Tier",
                    "label": "Tier",
                    "type": "Text",
                    "length": 40,
                },
            ),
            final_text("Created."),
        ]
    )
    run = await AgentRuntime(db, user, conversation, connection).create_run("Add a field")
    await _drain(worker)

    async with SessionLocal() as fresh:
        reloaded = await fresh.get(AgentRun, run.id)
        approval = (
            await fresh.execute(
                select(Approval).where(Approval.agent_run_id == run.id)
            )
        ).scalars().first()
        approval.state = ApprovalState.APPROVED
        await run_queue.enqueue(fresh, reloaded)
        await fresh.commit()
        assert reloaded.state is RunState.QUEUED

    await _drain(worker)

    async with SessionLocal() as fresh:
        finished = await fresh.get(AgentRun, run.id)
        assert finished.state is RunState.COMPLETED


# ---------------------------------------------------------------------------
# Event storage limits
# ---------------------------------------------------------------------------
async def test_one_enormous_string_is_truncated_by_redaction(db, project):
    """The first of two caps. A single huge value is cut where it is read, so
    it never reaches the event cap at all."""
    from app.observability.redaction import MAX_STRING

    row = await run_events.append(
        db,
        company_id=project.company_id,
        project_id=project.id,
        agent_run_id="run_x",
        event_type="tool.finished",
        data={"blob": "x" * 200_000},
    )
    await db.commit()
    assert len(row.data["blob"]) < MAX_STRING + 100
    assert "truncated" in row.data["blob"]


async def test_a_payload_of_many_strings_is_truncated_by_the_event_cap(db, project):
    """The second cap. Redaction bounds each value; this bounds the whole
    event, which is what stops a run making its own timeline unreadable — or
    the table unbounded — with a thousand moderate strings."""
    row = await run_events.append(
        db,
        company_id=project.company_id,
        project_id=project.id,
        agent_run_id="run_y",
        event_type="tool.finished",
        data={f"field_{i}": "y" * 500 for i in range(300)},
    )
    await db.commit()
    assert row.data["truncated"] is True
    assert "tool execution history" in row.data["note"]


async def test_event_payloads_go_through_the_same_redaction_as_audit(db, project):
    row = await run_events.append(
        db,
        company_id=project.company_id,
        project_id=project.id,
        agent_run_id="run_y",
        event_type="tool.finished",
        data={"access_token": "sk-live-SECRET", "object": "Account"},
    )
    await db.commit()
    assert "sk-live-SECRET" not in str(row.data)
    assert row.data["object"] == "Account"


# ---------------------------------------------------------------------------
# The HTTP contract a browser actually relies on
# ---------------------------------------------------------------------------
import httpx  # noqa: E402
import pytest_asyncio as _pa  # noqa: E402

from app.config import settings  # noqa: E402
from app.db import get_session  # noqa: E402
from app.main import app  # noqa: E402
from app.security.auth import create_session_token  # noqa: E402

API = settings.api_v1


@_pa.fixture
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


def _events(body: str) -> list[str]:
    return [
        line[len("event: ") :]
        for line in body.splitlines()
        if line.startswith("event: ")
    ]


async def test_a_client_that_never_connects_still_gets_its_work_done(
    client, auth, db, conversation, worker, patch_model
):
    """The whole promise, at the HTTP boundary: post, walk away, come back."""
    patch_model(
        [tool_use("describe_object", {"object": "Account"}), final_text("Done.")]
    )
    accepted = await client.post(
        f"{API}/conversations/{conversation.id}/messages",
        json={"message": "Describe Account"},
        headers=auth,
    )
    assert accepted.status_code == 202
    run_id = accepted.json()["run_id"]

    # Nobody is watching. The work happens anyway.
    await _drain(worker)

    detail = await client.get(f"{API}/runs/{run_id}", headers=auth)
    body = detail.json()
    assert body["state"] == RunState.COMPLETED.value
    assert body["terminal"] is True
    assert body["last_sequence"] > 0


async def test_reconnecting_replays_only_what_was_missed(
    client, auth, db, conversation, worker, patch_model
):
    """`after=N` is the reconnect contract. Off by one either way is a
    duplicated or a vanished step in the user's history."""
    patch_model(
        [tool_use("describe_object", {"object": "Account"}), final_text("Done.")]
    )
    run_id = (
        await client.post(
            f"{API}/conversations/{conversation.id}/messages",
            json={"message": "Describe Account"},
            headers=auth,
        )
    ).json()["run_id"]
    await _drain(worker)

    everything = (
        await client.get(f"{API}/runs/{run_id}/timeline", headers=auth)
    ).json()
    assert everything["count"] > 2

    midpoint = everything["events"][1]["sequence"]
    rest = (
        await client.get(
            f"{API}/runs/{run_id}/timeline?after={midpoint}", headers=auth
        )
    ).json()
    assert [e["sequence"] for e in rest["events"]] == [
        e["sequence"] for e in everything["events"] if e["sequence"] > midpoint
    ]


async def test_the_stream_of_a_finished_run_replays_its_history_and_closes(
    client, auth, db, conversation, worker, patch_model
):
    """Opening the page an hour later must show what happened, not an empty
    box or a hung connection."""
    patch_model(
        [tool_use("describe_object", {"object": "Account"}), final_text("Done.")]
    )
    run_id = (
        await client.post(
            f"{API}/conversations/{conversation.id}/messages",
            json={"message": "Describe Account"},
            headers=auth,
        )
    ).json()["run_id"]
    await _drain(worker)

    streamed = await client.get(f"{API}/runs/{run_id}/events", headers=auth)
    assert streamed.status_code == 200
    kinds = _events(streamed.text)
    assert "run.started" in kinds
    assert "tool.started" in kinds
    assert "run.completed" in kinds
    assert kinds[-1] == "stream.end"
    # `id:` frames are what make a browser's automatic reconnect resume.
    assert "id: 1" in streamed.text


async def test_the_stream_honours_last_event_id_on_reconnect(
    client, auth, db, conversation, worker, patch_model
):
    """A browser resends `Last-Event-ID` by itself. Honouring it means resume
    needs no client-side bookkeeping at all."""
    patch_model(
        [tool_use("describe_object", {"object": "Account"}), final_text("Done.")]
    )
    run_id = (
        await client.post(
            f"{API}/conversations/{conversation.id}/messages",
            json={"message": "Describe Account"},
            headers=auth,
        )
    ).json()["run_id"]
    await _drain(worker)

    resumed = await client.get(
        f"{API}/runs/{run_id}/events",
        headers={**auth, "Last-Event-ID": "2"},
    )
    assert "id: 1\n" not in resumed.text
    assert "id: 2\n" not in resumed.text
    assert "id: 3\n" in resumed.text


async def test_cancelling_through_the_api_stops_the_run(
    client, auth, db, conversation, worker, patch_model
):
    patch_model([final_text("unused")])
    run_id = (
        await client.post(
            f"{API}/conversations/{conversation.id}/messages",
            json={"message": "Do something long"},
            headers=auth,
        )
    ).json()["run_id"]

    cancelled = await client.post(f"{API}/runs/{run_id}/cancel", headers=auth)
    assert cancelled.json()["success"] is True

    await _drain(worker)
    detail = (await client.get(f"{API}/runs/{run_id}", headers=auth)).json()
    assert detail["state"] == RunState.CANCELLED.value


async def test_cancelling_a_finished_run_says_so_rather_than_claiming_success(
    client, auth, db, conversation, worker, patch_model
):
    patch_model([final_text("Done.")])
    run_id = (
        await client.post(
            f"{API}/conversations/{conversation.id}/messages",
            json={"message": "Go"},
            headers=auth,
        )
    ).json()["run_id"]
    await _drain(worker)

    resp = await client.post(f"{API}/runs/{run_id}/cancel", headers=auth)
    assert resp.json()["success"] is False
    assert "already finished" in resp.json()["message"]


async def test_a_run_from_another_project_is_not_watchable(
    client, auth, db, project, user
):
    """A run id guessed from another project must read as 'not found' on every
    surface, including the stream."""
    from app.models import Company, Conversation, Project

    other_company = Company(name="Rival Runs", slug="rival-runs")
    db.add(other_company)
    await db.flush()
    other_project = Project(
        company_id=other_company.id, name="Theirs", slug="theirs-runs"
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
        state=RunState.QUEUED,
    )
    db.add(theirs)
    await db.commit()

    for path in (
        f"{API}/runs/{theirs.id}",
        f"{API}/runs/{theirs.id}/timeline",
        f"{API}/runs/{theirs.id}/events",
    ):
        assert (await client.get(path, headers=auth)).status_code == 404
    assert (
        await client.post(f"{API}/runs/{theirs.id}/cancel", headers=auth)
    ).status_code == 404


async def test_a_conversation_reports_which_of_its_runs_are_still_live(
    client, auth, db, conversation, worker, patch_model
):
    """How a returning client finds work in progress without polling every id."""
    patch_model([final_text("Done.")])
    run_id = (
        await client.post(
            f"{API}/conversations/{conversation.id}/messages",
            json={"message": "Go"},
            headers=auth,
        )
    ).json()["run_id"]

    before = (
        await client.get(f"{API}/conversations/{conversation.id}/runs", headers=auth)
    ).json()
    assert before["active"] == [run_id]

    await _drain(worker)
    after = (
        await client.get(f"{API}/conversations/{conversation.id}/runs", headers=auth)
    ).json()
    assert after["active"] == []
    assert after["runs"][0]["state_label"] == "Completed"
