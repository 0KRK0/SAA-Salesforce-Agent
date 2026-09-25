"""Agent execution endpoints.

Runs are owned by the server. Posting a message *creates* a run and returns
its id; a worker executes it; this module streams the run's **stored** timeline
to whoever is watching.

That inversion is the whole point. The SSE connection is a view onto a durable
log, not the thing producing it, so:

  * closing a tab does not stop a change to a customer's Salesforce org;
  * reopening one does not start a second run;
  * a client that reconnects with `?after=N` gets exactly what it missed —
    no gaps, no duplicates, no reconstructing history from timestamps.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.agent.runtime import AgentRuntime
from app.api.routes_conversations import _owned as owned_conversation
from app.api.routes_salesforce import resolve_connection
from app.db import SessionLocal
from app.execution import events as run_events
from app.execution import queue as run_queue
from app.execution.state import describe as describe_state
from app.models import TERMINAL_RUN_STATES, AgentRun, ModelTier, ProjectRole, RunState
from app.observability.logging import get_logger
from app.security.auth import DbSession, Tenant
from app.tenancy import service as tenancy
from app.tools.registry import build_registry

router = APIRouter(tags=["agent"])
log = get_logger("api.agent")

#: How often the stream looks for new events. Short enough to feel live, long
#: enough that an idle watcher is not a meaningful load.
POLL_SECONDS = 0.2

#: A stream held open on a finished run is closed rather than left hanging.
IDLE_TIMEOUT_SECONDS = 900


class SendMessage(BaseModel):
    message: str = Field(min_length=1, max_length=20_000)
    #: Which capability tier to spend on this request. The gateway turns it
    #: into a concrete model using the project's own credential.
    tier: ModelTier = ModelTier.BALANCED


def _sse(event_type: str, data: dict[str, Any], sequence: int | None = None) -> str:
    """One SSE frame.

    `id:` carries the sequence number so a browser's automatic reconnect sends
    `Last-Event-ID` and resumes exactly where it stopped, with no client code.
    """
    frame = ""
    if sequence is not None:
        frame += f"id: {sequence}\n"
    payload = json.dumps(data, default=str)
    return f"{frame}event: {event_type}\ndata: {payload}\n\n"


def _runtime(
    db: Any,
    tenant: Any,
    conversation: Any,
    connection: Any,
    tier: ModelTier = ModelTier.BALANCED,
) -> AgentRuntime:
    return AgentRuntime(
        db,
        tenant.user,
        conversation,
        connection,
        project_id=tenant.project_id,
        company_id=tenant.company_id,
        policy=tenant.policy,
        model_tier=tier.value,
    )


# ---------------------------------------------------------------------------
# Starting work
# ---------------------------------------------------------------------------
@router.post("/conversations/{conversation_id}/messages", status_code=202)
async def send_message(
    conversation_id: str, payload: SendMessage, tenant: Tenant, db: DbSession
) -> dict[str, Any]:
    """Accept a message and queue a run. Returns immediately.

    202, not 200: the work has been accepted, not completed. Watch it at
    `/runs/{id}/events`.
    """
    # A VIEWER can read history but cannot drive the agent, because even a
    # read-only request spends org API calls and can be steered into mutations
    # that then sit in someone else's approval queue.
    tenant.require_at_least(ProjectRole.USER)

    # Checked here, before any work is queued. Refusing after a worker has
    # already spent a model call and a Salesforce round-trip would charge for
    # the thing being refused.
    refusal = await tenancy.check_run_allowance(db, tenant.company_id)
    if refusal:
        raise HTTPException(status_code=402, detail=refusal)

    conversation = await owned_conversation(db, tenant.project_id, conversation_id)
    connection = await resolve_connection(
        db, tenant.project_id, conversation.salesforce_connection_id
    )
    runtime = _runtime(db, tenant, conversation, connection, payload.tier)
    run = await runtime.create_run(payload.message)
    return {
        "run_id": run.id,
        "conversation_id": conversation.id,
        "state": run.state.value,
        "events_url": f"/runs/{run.id}/events",
    }


@router.post("/runs/{run_id}/resume", status_code=202)
async def resume_run(run_id: str, tenant: Tenant, db: DbSession) -> dict[str, Any]:
    """Continue a run whose approvals have all been decided."""
    run = await _owned_run(db, tenant, run_id)
    if run.state != RunState.WAITING_FOR_APPROVAL:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Run is in state {run.state.value}; only WAITING_FOR_APPROVAL "
                "can resume."
            ),
        )
    await run_queue.enqueue(db, run)
    await db.commit()
    return {
        "run_id": run.id,
        "state": run.state.value,
        "events_url": f"/runs/{run.id}/events",
    }


@router.post("/runs/{run_id}/cancel")
async def cancel_run(run_id: str, tenant: Tenant, db: DbSession) -> dict[str, Any]:
    """Ask a run to stop.

    Cooperative on purpose: a claimed run stops at its next step boundary, not
    mid-call. Interrupting a Salesforce write would leave the org changed and
    this system unable to say whether it was.
    """
    tenant.require_at_least(ProjectRole.USER)
    run = await _owned_run(db, tenant, run_id)
    accepted = await run_queue.request_cancel(db, run, tenant.user_id)
    await db.commit()
    if not accepted:
        return {
            "success": False,
            "state": run.state.value,
            "message": f"This run already finished ({describe_state(run.state)}).",
        }
    return {
        "success": True,
        "state": run.state.value,
        "message": (
            "Cancelled."
            if run.state is RunState.CANCELLED
            else "Cancellation requested. The run will stop at its next step; "
            "anything already changed in Salesforce is listed in its history."
        ),
    }


# ---------------------------------------------------------------------------
# Watching work
# ---------------------------------------------------------------------------
@router.get("/runs/{run_id}/events")
async def stream_events(
    run_id: str,
    request: Request,
    tenant: Tenant,
    db: DbSession,
    after: int = Query(0, ge=0, description="Last sequence number already seen"),
) -> StreamingResponse:
    """Stream a run's timeline from `after`, then follow it live.

    Safe to call at any point: before the worker starts, while it runs, or long
    after it finished. A finished run replays its history and closes.
    """
    run = await _owned_run(db, tenant, run_id)
    # `Last-Event-ID` is what a browser sends on an automatic reconnect. Honour
    # it so resuming needs no client-side bookkeeping at all.
    resume_from = _last_event_id(request) if after == 0 else after

    return StreamingResponse(
        _follow(run.id, tenant.project_id, resume_from, request),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


def _last_event_id(request: Request) -> int:
    raw = request.headers.get("last-event-id") or ""
    try:
        return max(0, int(raw))
    except ValueError:
        return 0


async def _follow(
    run_id: str, project_id: str, after: int, request: Request
) -> AsyncIterator[str]:
    """Replay stored events, then tail until the run reaches a terminal state.

    Uses its own database session: this outlives the request's transaction by
    design, and must not hold one open for the life of a long run.
    """
    cursor = after
    waited = 0.0
    try:
        while True:
            if await request.is_disconnected():
                # The run keeps going. Only this view of it ends.
                return

            async with SessionLocal() as db:
                rows = await run_events.read(
                    db, agent_run_id=run_id, project_id=project_id, after=cursor
                )
                for row in rows:
                    cursor = row.sequence
                    yield _sse(row.event_type, row.data or {}, row.sequence)

                if not rows:
                    run = await db.get(AgentRun, run_id)
                    if run is None or run.state in TERMINAL_RUN_STATES:
                        yield _sse(
                            "stream.end",
                            {
                                "run_id": run_id,
                                "state": run.state.value if run else "MISSING",
                                "last_sequence": cursor,
                            },
                        )
                        return
                    if run.state is RunState.WAITING_FOR_APPROVAL:
                        # Nothing more will happen until a human decides, so
                        # the stream closes rather than holding a connection
                        # open for however long that takes.
                        yield _sse(
                            "stream.end",
                            {
                                "run_id": run_id,
                                "state": run.state.value,
                                "last_sequence": cursor,
                                "waiting_for_approval": True,
                            },
                        )
                        return

            if rows:
                waited = 0.0
                continue

            await asyncio.sleep(POLL_SECONDS)
            waited += POLL_SECONDS
            if waited >= IDLE_TIMEOUT_SECONDS:
                yield _sse(
                    "stream.end",
                    {"run_id": run_id, "reason": "idle_timeout", "last_sequence": cursor},
                )
                return
            if waited % 15 < POLL_SECONDS:
                # A comment frame keeps proxies from closing an idle stream and
                # is ignored by every SSE client.
                yield ": keep-alive\n\n"
    except asyncio.CancelledError:  # pragma: no cover - client vanished
        raise
    except Exception as exc:
        log.error("agent.stream_error", agent_run_id=run_id, exc_info=True)
        yield _sse("error", {"message": f"The event stream failed: {type(exc).__name__}"})
        yield _sse("stream.end", {"run_id": run_id, "last_sequence": cursor})


@router.get("/runs/{run_id}")
async def get_run(run_id: str, tenant: Tenant, db: DbSession) -> dict[str, Any]:
    run = await _owned_run(db, tenant, run_id)
    last = await run_events.read(
        db, agent_run_id=run.id, project_id=tenant.project_id, after=0, limit=1000
    )
    return {
        "id": run.id,
        "conversation_id": run.conversation_id,
        "state": run.state.value,
        "state_label": describe_state(run.state),
        "terminal": run.state in TERMINAL_RUN_STATES,
        "steps_used": run.steps_used,
        "max_steps": run.max_steps,
        "provider": run.llm_provider,
        "model": run.model,
        "model_tier": run.model_tier,
        "input_tokens": run.input_tokens,
        "output_tokens": run.output_tokens,
        "estimated_cost_usd": run.estimated_cost_usd,
        "duration_ms": run.duration_ms,
        "error": run.error,
        "error_code": run.error_code,
        "final_text": run.final_text,
        "pending_approval_ids": run.pending_approval_ids or [],
        "cancel_requested": run.cancel_requested,
        "claimed": bool(run.claimed_by),
        "last_sequence": last[-1].sequence if last else 0,
        "created_at": run.created_at.isoformat(),
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
    }


@router.get("/runs/{run_id}/timeline")
async def run_timeline(
    run_id: str,
    tenant: Tenant,
    db: DbSession,
    after: int = Query(0, ge=0),
    limit: int = Query(500, ge=1, le=1000),
) -> dict[str, Any]:
    """The stored timeline, without a stream.

    For clients that would rather poll than hold a connection — a phone on a
    flaky network, or a page restoring history before it starts following.
    """
    run = await _owned_run(db, tenant, run_id)
    rows = await run_events.read(
        db,
        agent_run_id=run.id,
        project_id=tenant.project_id,
        after=after,
        limit=limit,
    )
    return {
        "run_id": run.id,
        "state": run.state.value,
        "terminal": run.state in TERMINAL_RUN_STATES,
        "count": len(rows),
        "last_sequence": rows[-1].sequence if rows else after,
        "events": [run_events.serialize(r) for r in rows],
    }


@router.get("/conversations/{conversation_id}/runs")
async def conversation_runs(
    conversation_id: str, tenant: Tenant, db: DbSession
) -> dict[str, Any]:
    """Runs for one conversation, so a returning client can find live work."""
    from sqlalchemy import select

    conversation = await owned_conversation(db, tenant.project_id, conversation_id)
    rows = (
        await db.execute(
            select(AgentRun)
            .where(AgentRun.conversation_id == conversation.id)
            .order_by(AgentRun.created_at)
        )
    ).scalars().all()
    return {
        "count": len(rows),
        "active": [r.id for r in rows if r.state not in TERMINAL_RUN_STATES],
        "runs": [
            {
                "id": r.id,
                "state": r.state.value,
                "state_label": describe_state(r.state),
                "terminal": r.state in TERMINAL_RUN_STATES,
                "created_at": r.created_at.isoformat(),
            }
            for r in rows
        ],
    }


async def _owned_run(db: Any, tenant: Tenant, run_id: str) -> AgentRun:
    run = await tenancy.owned(db, AgentRun, run_id, tenant.project_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return run


@router.get("/tools")
async def list_tools(tenant: Tenant) -> dict[str, Any]:
    """The tool catalog as this project sees it.

    Disabled tools are reported so an admin can see what is switched off,
    but they are never offered to the model and the risk engine refuses them.
    """
    registry = build_registry()
    disabled = set(tenant.policy.disabled_tools)
    catalog = [{**t, "enabled": t["name"] not in disabled} for t in registry.catalog()]
    return {
        "count": len(catalog),
        "enabled_count": sum(1 for t in catalog if t["enabled"]),
        "providers": sorted({t["provider"] for t in catalog}),
        "tools": catalog,
    }
