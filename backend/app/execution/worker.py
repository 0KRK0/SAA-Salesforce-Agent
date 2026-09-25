"""The worker that actually runs agent work.

This exists so that no customer's change depends on a browser staying open.
The HTTP request that accepts a message creates a run and returns; this loop
picks it up, owns it, heartbeats while it works, and writes every event to the
durable timeline. Closing the tab, switching networks, or a laptop going to
sleep changes nothing about whether the work finishes.

The worker holds its own database session, separate from any request's, so a
run's lifetime is not tied to a request's transaction.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.execution import queue as run_queue
from app.execution.state import IllegalTransition
from app.models import (
    AgentRun,
    Conversation,
    RunState,
    SalesforceConnection,
    User,
)
from app.observability.logging import get_logger, run_id_var
from app.tenancy import service as tenancy

log = get_logger("execution.worker")

SessionFactory = Callable[[], Any]


class RunWorker:
    """Claims queued runs and executes them.

    Deliberately simple and poll-based. A notification channel would shave a
    second off pickup latency and add a component that can be down; polling a
    table cannot get into a state where work exists and nothing knows about it.
    """

    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        owner: str | None = None,
        concurrency: int | None = None,
        poll_seconds: float | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.owner = owner or run_queue.worker_id()
        self.concurrency = concurrency or settings.run_worker_concurrency
        self.poll_seconds = poll_seconds or settings.run_worker_poll_seconds
        self._running = False
        self._task: asyncio.Task | None = None
        self._active: set[str] = set()

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if self._task is not None:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="run-worker")
        log.info("worker.started", owner=self.owner, concurrency=self.concurrency)

    async def stop(self) -> None:
        """Stop claiming new work and let in-flight runs finish.

        In-flight runs are not cancelled on shutdown. A run interrupted between
        a Salesforce write and its verification is the one state this system
        cannot describe honestly, so shutdown waits rather than creating one.
        """
        self._running = False
        if self._task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.wait_for(self._task, timeout=30)
            self._task = None
        log.info("worker.stopped", owner=self.owner)

    async def _loop(self) -> None:
        while self._running:
            try:
                await self.tick()
            except asyncio.CancelledError:  # pragma: no cover - shutdown
                raise
            except Exception:  # pragma: no cover - a worker must not die
                log.error("worker.tick_failed", exc_info=True)
            await asyncio.sleep(self.poll_seconds)

    # ------------------------------------------------------------------- one pass
    async def tick(self) -> int:
        """One pass: reclaim, expire, then claim and run what fits.

        Returns how many runs were started, which makes the loop testable
        without any sleeping.
        """
        await self.maintain()
        started = 0
        while len(self._active) < self.concurrency:
            run_id = await self._claim_one()
            if run_id is None:
                break
            self._active.add(run_id)
            task = asyncio.create_task(self._execute(run_id), name=f"run:{run_id}")
            task.add_done_callback(lambda _, rid=run_id: self._active.discard(rid))
            started += 1
        return started

    async def maintain(self) -> dict[str, list[str]]:
        """Housekeeping that only makes sense from outside a run.

        A crashed worker cannot release its own claim and an overrun run cannot
        notice its own deadline, so both are handled here.
        """
        async with self.session_factory() as db:
            reclaimed = await run_queue.reclaim_stale(db)
            expired = await run_queue.expire_overdue(db)
            if reclaimed or expired:
                await db.commit()
            return {"reclaimed": reclaimed, "expired": expired}

    async def _claim_one(self) -> str | None:
        async with self.session_factory() as db:
            run = await run_queue.claim_next(db, self.owner)
            return run.id if run else None

    async def _execute(self, run_id: str) -> None:
        """Run one claimed run to a terminal (or waiting) state."""
        token = run_id_var.set(run_id)
        try:
            async with self.session_factory() as db:
                run = await db.get(AgentRun, run_id)
                if run is None:  # pragma: no cover - deleted mid-flight
                    return
                context = await self._context(db, run)
                if context is None:
                    await self._fail(
                        db,
                        run,
                        "This run's conversation or user no longer exists, so it "
                        "cannot be executed.",
                        "RUN_CONTEXT_MISSING",
                    )
                    return

                runtime, heartbeat = context, None
                heartbeat = asyncio.create_task(self._heartbeat(run_id))
                try:
                    async for _ in runtime.execute(run):
                        # Events are persisted as they are emitted; the worker
                        # does not need them, it just has to drain the stream.
                        pass
                finally:
                    heartbeat.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await heartbeat
                    await self._release_if_unfinished(db, run)
        except Exception:
            log.error("worker.run_failed", agent_run_id=run_id, exc_info=True)
            await self._fail_by_id(run_id)
        finally:
            run_id_var.reset(token)

    async def _context(self, db: AsyncSession, run: AgentRun) -> Any:
        """Rebuild everything the runtime needs, from persisted state alone.

        Nothing is carried over from the request that created the run — that
        request is long gone. If it cannot be rebuilt from the database, the run
        cannot be executed, and that is reported rather than guessed around.
        """
        from app.agent.runtime import AgentRuntime

        conversation = await db.get(Conversation, run.conversation_id)
        user = await db.get(User, run.user_id)
        if conversation is None or user is None:
            return None

        connection = None
        if run.salesforce_connection_id:
            connection = await db.get(
                SalesforceConnection, run.salesforce_connection_id
            )
            if connection is not None and connection.project_id != run.project_id:
                # Should be impossible; refusing beats reaching into another
                # project's org on the strength of a stale id.
                connection = None

        policy = await tenancy.policy_for(db, run.project_id, run.company_id)
        return AgentRuntime(
            db,
            user,
            conversation,
            connection,
            project_id=run.project_id,
            company_id=run.company_id,
            policy=policy,
            model_tier=run.model_tier or "BALANCED",
        )

    async def _heartbeat(self, run_id: str) -> None:
        """Say "still alive" often enough that nobody reclaims this run."""
        interval = max(5.0, settings.run_heartbeat_timeout_seconds / 4)
        while True:
            await asyncio.sleep(interval)
            try:
                async with self.session_factory() as db:
                    run = await db.get(AgentRun, run_id)
                    if run is None or run.claimed_by != self.owner:
                        return
                    await run_queue.heartbeat(db, run)
                    await db.commit()
            except Exception:  # pragma: no cover - a missed beat is survivable
                log.warning("worker.heartbeat_failed", agent_run_id=run_id)

    async def _release_if_unfinished(self, db: AsyncSession, run: AgentRun) -> None:
        """Never leave a claim on a run that is not being worked on."""
        await db.refresh(run)
        if run.state in {RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED, RunState.EXPIRED}:
            return
        if run.state is RunState.WAITING_FOR_APPROVAL:
            run.claimed_by = None
            run.heartbeat_at = None
            await db.commit()
            return
        # The generator ended without reaching a terminal state, which should
        # not happen. Return the run to the queue rather than stranding it.
        log.warning(
            "worker.run_left_running", agent_run_id=run.id, state=run.state.value
        )
        with contextlib.suppress(IllegalTransition):
            await run_queue.enqueue(db, run)
        await db.commit()

    async def _fail_by_id(self, run_id: str) -> None:
        try:
            async with self.session_factory() as db:
                run = await db.get(AgentRun, run_id)
                if run is not None:
                    await self._fail(
                        db,
                        run,
                        "This run stopped unexpectedly. Any Salesforce changes it "
                        "had already made are listed in its history.",
                        "WORKER_FAILURE",
                    )
        except Exception:  # pragma: no cover - last resort
            log.error("worker.fail_record_failed", agent_run_id=run_id, exc_info=True)

    async def _fail(
        self, db: AsyncSession, run: AgentRun, message: str, code: str
    ) -> None:
        from app.execution import events as run_events

        if run.state in {RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED}:
            return
        run.state = RunState.FAILED
        run.error = message
        run.error_code = code
        run.claimed_by = None
        run.heartbeat_at = None
        await run_events.append(
            db,
            company_id=run.company_id,
            project_id=run.project_id,
            agent_run_id=run.id,
            event_type="error",
            data={"message": message, "code": code},
        )
        await db.commit()


async def pending_count(db: AsyncSession) -> int:
    rows = (
        await db.execute(
            select(AgentRun.id).where(AgentRun.state == RunState.QUEUED)
        )
    ).scalars().all()
    return len(rows)
