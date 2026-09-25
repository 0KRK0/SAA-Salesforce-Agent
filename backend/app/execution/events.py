"""The durable run timeline.

Every event the agent emits is written here before it reaches a browser, and
the browser reads *from here*, never from a live in-memory queue. That single
inversion is what makes a run survive a closed tab: the connection is a view
onto a log, not the thing producing it.

`sequence` is a per-run counter, allocated by the worker that owns the run.
A reconnecting client says "I have up to N" and gets exactly what it missed —
no duplicates, no gaps, no guessing from timestamps.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import RunEvent
from app.observability.logging import redact

#: Events whose payload can carry Salesforce data. Their bodies go through the
#: same redaction as audit rows before being stored.
_REDACTED_TYPES = frozenset(
    {"tool.finished", "tool.result", "approval.requested", "assistant.text"}
)

#: A single event body is capped. A run that returns a huge payload should not
#: be able to make its own timeline unreadable — or unbounded in the database.
MAX_EVENT_BYTES = 64_000


async def next_sequence(db: AsyncSession, agent_run_id: str) -> int:
    current = (
        await db.execute(
            select(func.max(RunEvent.sequence)).where(
                RunEvent.agent_run_id == agent_run_id
            )
        )
    ).scalar()
    return int(current or 0) + 1


async def append(
    db: AsyncSession,
    *,
    company_id: str,
    project_id: str,
    agent_run_id: str,
    event_type: str,
    data: dict[str, Any] | None = None,
    sequence: int | None = None,
) -> RunEvent:
    """Append one event to a run's timeline."""
    payload = dict(data or {})
    if event_type in _REDACTED_TYPES:
        payload = redact(payload)
    payload = _cap(payload)

    row = RunEvent(
        company_id=company_id,
        project_id=project_id,
        agent_run_id=agent_run_id,
        sequence=sequence
        if sequence is not None
        else await next_sequence(db, agent_run_id),
        event_type=event_type,
        data=payload,
    )
    db.add(row)
    await db.flush()
    return row


def _cap(payload: dict[str, Any]) -> dict[str, Any]:
    import json

    encoded = json.dumps(payload, default=str)
    if len(encoded) <= MAX_EVENT_BYTES:
        return payload
    # Truncating is better than dropping: the shape of what happened is often
    # the useful part, and the full record is in tool_executions anyway.
    return {
        "truncated": True,
        "original_bytes": len(encoded),
        "preview": encoded[:2000],
        "note": (
            "This event was too large to store in full. The complete record is "
            "in the tool execution history for this run."
        ),
    }


async def read(
    db: AsyncSession,
    *,
    agent_run_id: str,
    project_id: str,
    after: int = 0,
    limit: int = 500,
) -> list[RunEvent]:
    """Events after `after`, in order. Project-scoped, like every other read."""
    rows = (
        await db.execute(
            select(RunEvent)
            .where(
                RunEvent.agent_run_id == agent_run_id,
                RunEvent.project_id == project_id,
                RunEvent.sequence > after,
            )
            .order_by(RunEvent.sequence)
            .limit(limit)
        )
    ).scalars().all()
    return list(rows)


def serialize(event: RunEvent) -> dict[str, Any]:
    return {
        "sequence": event.sequence,
        "type": event.event_type,
        "data": event.data or {},
        "at": event.created_at.isoformat() if event.created_at else None,
    }
