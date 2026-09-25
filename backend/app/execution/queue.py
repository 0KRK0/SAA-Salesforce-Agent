"""Run ownership: claiming, heartbeating, cancelling, reclaiming.

A run belongs to exactly one worker at a time, and the claim is an atomic
conditional UPDATE rather than a read-then-write. That is the whole
concurrency design: two workers racing for the same run produce one winner and
one no-op, on SQLite and on Postgres alike, without a lock table.

A worker that dies mid-run leaves a claim behind. Rather than trusting a
process to clean up after itself — the one thing a crashed process cannot do —
ownership expires: a claim whose heartbeat has gone stale is reclaimable.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.execution.state import transition
from app.models import TERMINAL_RUN_STATES, AgentRun, RunState
from app.observability.logging import get_logger

log = get_logger("execution.queue")


def worker_id() -> str:
    """This process's identity when it claims work."""
    import os
    import socket

    return settings.worker_id or f"{socket.gethostname()}:{os.getpid()}"


def _aware(value: datetime | None) -> datetime | None:
    """SQLite round-trips naive datetimes; comparing one to `now` raises."""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


async def enqueue(db: AsyncSession, run: AgentRun) -> AgentRun:
    """Mark a run ready for a worker to pick up."""
    run.state = transition(run.state, RunState.QUEUED)
    run.claimed_by = None
    run.claimed_at = None
    run.heartbeat_at = None
    if run.deadline_at is None:
        run.deadline_at = datetime.now(UTC) + timedelta(
            seconds=settings.max_execution_seconds
        )
    await db.flush()
    return run


async def claim(db: AsyncSession, run_id: str, owner: str) -> AgentRun | None:
    """Take ownership of one queued run, or return None if someone else did.

    The `claimed_by IS NULL` predicate in the UPDATE is what makes this safe:
    the database decides the winner, not the order two workers happened to read
    in.
    """
    now = datetime.now(UTC)
    result = await db.execute(
        update(AgentRun)
        .where(
            AgentRun.id == run_id,
            AgentRun.state == RunState.QUEUED,
            AgentRun.claimed_by.is_(None),
        )
        .values(claimed_by=owner, claimed_at=now, heartbeat_at=now)
    )
    if result.rowcount == 0:
        return None
    await db.commit()

    run = await db.get(AgentRun, run_id)
    if run is not None:
        # The claim was a bulk UPDATE, so any instance already in this
        # session's identity map still holds the pre-claim values — and the
        # session is configured not to expire on commit. Refreshing is what
        # makes the returned object agree with the row that was just written;
        # without it the worker would see claimed_by as None and could release
        # a claim it actually holds.
        await db.refresh(run)
    return run


async def claim_next(db: AsyncSession, owner: str) -> AgentRun | None:
    """Claim the oldest unclaimed queued run, if there is one."""
    candidates = (
        await db.execute(
            select(AgentRun.id)
            .where(
                AgentRun.state == RunState.QUEUED,
                AgentRun.claimed_by.is_(None),
            )
            .order_by(AgentRun.created_at)
            .limit(10)
        )
    ).scalars().all()
    for run_id in candidates:
        claimed = await claim(db, run_id, owner)
        if claimed is not None:
            return claimed
    return None


async def heartbeat(db: AsyncSession, run: AgentRun) -> None:
    """Say the worker is still alive and still owns this run."""
    run.heartbeat_at = datetime.now(UTC)
    await db.flush()


async def release(db: AsyncSession, run: AgentRun) -> None:
    """Give up ownership without finishing — the run returns to the queue."""
    run.claimed_by = None
    run.claimed_at = None
    run.heartbeat_at = None
    await db.flush()


async def request_cancel(db: AsyncSession, run: AgentRun, user_id: str) -> bool:
    """Ask a run to stop.

    Cancellation is cooperative on purpose. A run is stopped between steps, not
    mid-call: killing a worker during a Salesforce write would leave the org
    changed and this system unsure whether it was. A run that is not yet claimed
    is cancelled outright, because nothing is in flight.
    """
    if run.state in TERMINAL_RUN_STATES:
        return False

    run.cancel_requested = True
    run.cancel_requested_by = user_id

    if run.claimed_by is None:
        run.state = transition(run.state, RunState.CANCELLED)
        run.finished_at = datetime.now(UTC)
    await db.flush()
    return True


def cancel_requested(run: AgentRun) -> bool:
    return bool(run.cancel_requested)


def is_overdue(run: AgentRun, *, now: datetime | None = None) -> bool:
    deadline = _aware(run.deadline_at)
    if deadline is None:
        return False
    return (now or datetime.now(UTC)) >= deadline


async def finish(
    db: AsyncSession, run: AgentRun, state: RunState, error: str | None = None
) -> None:
    run.state = transition(run.state, state)
    run.finished_at = datetime.now(UTC)
    run.claimed_by = None
    run.heartbeat_at = None
    if error:
        run.error = error
    await db.flush()


# ---------------------------------------------------------------------------
# Reclaiming abandoned work
# ---------------------------------------------------------------------------
async def reclaim_stale(
    db: AsyncSession, *, timeout_seconds: int | None = None, now: datetime | None = None
) -> list[str]:
    """Return runs whose worker stopped heartbeating to the queue.

    A crashed worker cannot clean up after itself, so ownership expires instead.
    The run goes back to QUEUED rather than to FAILED: nothing is known to have
    gone wrong with the *work*, only with the process that was doing it.
    """
    cutoff = (now or datetime.now(UTC)) - timedelta(
        seconds=timeout_seconds or settings.run_heartbeat_timeout_seconds
    )
    rows = (
        await db.execute(
            select(AgentRun).where(
                AgentRun.claimed_by.is_not(None),
                AgentRun.state.not_in(list(TERMINAL_RUN_STATES)),
                AgentRun.state != RunState.WAITING_FOR_APPROVAL,
            )
        )
    ).scalars().all()

    reclaimed: list[str] = []
    for run in rows:
        beat = _aware(run.heartbeat_at) or _aware(run.claimed_at)
        if beat is not None and beat > cutoff:
            continue
        log.warning(
            "execution.reclaimed_stale_run",
            agent_run_id=run.id,
            previous_owner=run.claimed_by,
            last_heartbeat=beat.isoformat() if beat else None,
        )
        run.claimed_by = None
        run.claimed_at = None
        run.heartbeat_at = None
        run.state = RunState.QUEUED
        reclaimed.append(run.id)
    if reclaimed:
        await db.flush()
    return reclaimed


async def expire_overdue(
    db: AsyncSession, *, now: datetime | None = None
) -> list[str]:
    """Fail runs that have passed their deadline.

    EXPIRED rather than FAILED, because "we stopped waiting" is a different
    statement from "the work went wrong", and an operator reading the audit
    trail needs to be able to tell them apart.
    """
    reference = now or datetime.now(UTC)
    rows = (
        await db.execute(
            select(AgentRun).where(
                AgentRun.state.not_in(list(TERMINAL_RUN_STATES)),
                AgentRun.deadline_at.is_not(None),
            )
        )
    ).scalars().all()

    expired: list[str] = []
    for run in rows:
        if not is_overdue(run, now=reference):
            continue
        run.state = RunState.EXPIRED
        run.finished_at = reference
        run.claimed_by = None
        run.heartbeat_at = None
        run.error = run.error or (
            "This run exceeded its time limit and was stopped. Any changes "
            "already made are recorded in the run's history."
        )
        run.error_code = run.error_code or "RUN_EXPIRED"
        expired.append(run.id)
    if expired:
        await db.flush()
    return expired
