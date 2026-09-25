"""The agent runtime.

Claude is the reasoning engine; this module is the machine that keeps it safe:
state machine, tool dispatch, deterministic validation, risk classification,
human approval gating, idempotency, verification, audit and step limits.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.context import compact_transcript, serialize_tool_result
from app.agent.prompts import build_system_prompt
from app.config import settings
from app.execution import events as run_events
from app.execution import queue as run_queue
from app.execution.state import IllegalTransition, transition
from app.llm import LLMError, NoModelAvailable
from app.llm import complete as call_model
from app.llm.base import LLMNotConfigured
from app.models import (
    AgentRun,
    Approval,
    ApprovalState,
    AuditEvent,
    Conversation,
    ExecutionState,
    Message,
    ModelTier,
    RiskLevel,
    RunState,
    SalesforceConnection,
    ToolExecution,
    User,
    utcnow,
)
from app.observability.logging import get_logger, redact, run_id_var
from app.risk.engine import OrgContext, RiskDecision, classify
from app.salesforce.client import SalesforceClient
from app.salesforce.errors import SalesforceError
from app.tenancy.policy import (
    PolicySnapshot,
    change_hash,
    expires_at,
    fingerprint_drifted,
    is_expired,
    snapshot_from,
)
from app.tools.base import Tool, ToolContext, ToolValidationError
from app.tools.registry import build_registry

log = get_logger("agent.runtime")

Event = dict[str, Any]


def _canonical(args: dict[str, Any]) -> str:
    return json.dumps(args, sort_keys=True, default=str)


def idempotency_key(run_id: str, tool_name: str, args: dict[str, Any]) -> str:
    return hashlib.sha256(f"{run_id}|{tool_name}|{_canonical(args)}".encode()).hexdigest()


class AgentRuntime:
    def __init__(
        self,
        db: AsyncSession,
        user: User,
        conversation: Conversation,
        connection: SalesforceConnection | None,
        project_id: str | None = None,
        company_id: str | None = None,
        policy: PolicySnapshot | None = None,
        model_tier: str = ModelTier.BALANCED.value,
    ) -> None:
        self.db = db
        self.user = user
        self.conversation = conversation
        self.connection = connection
        # Security boundary for every row this runtime writes and reads.
        self.project_id = project_id or conversation.project_id
        self.company_id = company_id or conversation.company_id
        self.policy = policy or snapshot_from(None, self.project_id, self.company_id)
        # Which capability tier this run asks for. The gateway turns it into a
        # concrete model using the project's own credential when it has one.
        self.model_tier = model_tier
        self.registry = build_registry()
        # Per-run tool view: native tools plus this tenant's MCP tools. MCP
        # tools are never installed into the process-wide registry, because
        # that registry is shared by every tenant in the process.
        self._tools: dict[str, Tool] = {}
        self._queue: asyncio.Queue[Event | None] = asyncio.Queue()
        self.sf: SalesforceClient | None = None
        #: The run this instance is executing. Set once work begins; used to
        #: attach every emitted event to a durable timeline.
        self._run_id: str = ""

    # ------------------------------------------------------------ event plumbing
    async def emit(self, type_: str, data: dict[str, Any] | None = None) -> None:
        """Record an event, then hand it to whoever is watching.

        Durable first, live second. The browser reads the stored timeline, so an
        event that was not written did not happen as far as a reconnecting
        client is concerned — writing before delivering is what makes a
        reconnect show the same history the first viewer saw.

        A failure to store an event never fails the run. Losing a line of
        telemetry is bad; abandoning a half-finished change to a customer's org
        because telemetry failed is far worse.
        """
        payload = data or {}
        event: Event = {"type": type_, "data": payload}
        if self._run_id:
            try:
                row = await run_events.append(
                    self.db,
                    company_id=self.company_id,
                    project_id=self.project_id,
                    agent_run_id=self._run_id,
                    event_type=type_,
                    data=payload,
                )
                await self.db.commit()
                event["sequence"] = row.sequence
            except Exception as exc:  # pragma: no cover - defensive
                log.warning(
                    "agent.event_not_persisted",
                    agent_run_id=self._run_id,
                    event_type=type_,
                    error=type(exc).__name__,
                )
        await self._queue.put(event)

    async def _drain(self, task: asyncio.Task) -> AsyncIterator[Event]:
        while True:
            event = await self._queue.get()
            if event is None:
                break
            yield event
        exc = task.exception() if task.done() else None
        if exc:  # pragma: no cover - surfaced as an error event already
            raise exc

    # ----------------------------------------------------------------- lifecycle
    async def create_run(self, user_text: str) -> AgentRun:
        """Persist a run and the user's message, ready for a worker.

        Deliberately separate from executing it. The request that accepts the
        work does not do the work, which is what lets the browser go away
        without taking the run with it.
        """
        run = AgentRun(
            company_id=self.company_id,
            project_id=self.project_id,
            conversation_id=self.conversation.id,
            user_id=self.user.id,
            salesforce_connection_id=self.connection.id if self.connection else None,
            state=RunState.CREATED,
            user_request=user_text,
            max_steps=self.policy.max_agent_steps,
            model="",
            transcript=[],
        )
        self.db.add(run)
        self.db.add(
            Message(
                conversation_id=self.conversation.id,
                role="user",
                text=user_text,
                agent_run_id=run.id,
            )
        )
        if self.conversation.title == "New conversation":
            self.conversation.title = user_text.strip()[:120] or "New conversation"
        await self.db.flush()
        await run_queue.enqueue(self.db, run)
        await self.db.commit()
        return run

    async def execute(self, run: AgentRun) -> AsyncIterator[Event]:
        """Run the work for an already-created run, streaming its events."""
        self._run_id = run.id
        history = await self._load_history(exclude_run=run.id)
        messages = [*history, {"role": "user", "content": run.user_request}]
        task = asyncio.create_task(self._guarded(run, messages))
        async for event in self._drain(task):
            yield event

    async def start(self, user_text: str) -> AsyncIterator[Event]:
        """Create and execute in one call.

        Kept for direct, in-process use — tests and any caller that genuinely
        wants the work inline. The API does not use it: it creates the run and
        lets a worker execute it.
        """
        run = await self.create_run(user_text)
        async for event in self.execute(run):
            yield event

    async def resume(self, run: AgentRun) -> AsyncIterator[Event]:
        self._run_id = run.id
        task = asyncio.create_task(self._guarded_resume(run))
        async for event in self._drain(task):
            yield event

    async def _guarded(self, run: AgentRun, messages: list[dict[str, Any]]) -> None:
        try:
            await self._loop(run, messages)
        except Exception as exc:
            await self._fail(run, exc)
        finally:
            await self._queue.put(None)

    async def _guarded_resume(self, run: AgentRun) -> None:
        try:
            messages = await self._rebuild_after_approval(run)
            if messages is not None:
                await self._loop(run, messages)
        except Exception as exc:
            await self._fail(run, exc)
        finally:
            await self._queue.put(None)

    async def _fail(self, run: AgentRun, exc: Exception) -> None:
        message = str(exc)
        error_code: str | None = None
        # The model layer is the one dependency a user can fix themselves, so
        # its failures are reported as configuration problems with the actual
        # remedy rather than as an opaque agent crash.
        if isinstance(exc, NoModelAvailable | LLMNotConfigured):
            error_code = exc.error_type
            message = exc.message
        elif isinstance(exc, LLMError):
            error_code = exc.error_type
            message = f"The model call failed. {exc.message}"
        log.error("agent.run_failed", error=message, exc_info=True)
        run.state = RunState.FAILED
        run.error = message
        run.error_code = error_code
        run.claimed_by = None
        run.heartbeat_at = None
        run.finished_at = utcnow()
        await self.db.commit()
        await self._audit(run, action="agent.run_failed", outcome="error", error=message)
        await self.emit("state", {"state": RunState.FAILED.value})
        await self.emit("error", {"message": message})

    # -------------------------------------------------------------------- history
    async def _load_history(self, exclude_run: str | None = None) -> list[dict[str, Any]]:
        rows = (
            (
                await self.db.execute(
                    select(Message)
                    .where(Message.conversation_id == self.conversation.id)
                    .order_by(Message.created_at)
                )
            )
            .scalars()
            .all()
        )
        messages: list[dict[str, Any]] = []
        for row in rows:
            if exclude_run and row.agent_run_id == exclude_run:
                continue
            if row.role not in {"user", "assistant"}:
                continue
            messages.append({"role": row.role, "content": row.text})
        return compact_transcript(messages)

    # ----------------------------------------------------------------- main loop
    async def _loop(self, run: AgentRun, messages: list[dict[str, Any]]) -> None:
        run_id_var.set(run.id)
        started = time.perf_counter()
        await self.emit("run.started", {"run_id": run.id, "model": run.model})

        org_ctx = self._org_context()
        knowledge = await self._recall_knowledge(run.user_request or "")
        system = build_system_prompt(
            {
                "sf_org_id": self.connection.sf_org_id,
                "instance_url": self.connection.instance_url,
                "org_type": self.connection.org_type,
                "is_sandbox": self.connection.is_sandbox,
                "api_version": self.connection.api_version,
                "username": self.connection.username,
            }
            if self.connection
            else None,
            extra=knowledge,
        )
        await self._load_tools()
        tools = [
            t.tool_schema()
            for t in self._tools.values()
            if t.name not in self.policy.disabled_tools
        ]

        async with self._salesforce() as sf:
            self.sf = sf
            while True:
                # Stopping conditions are checked at the top of every step,
                # never mid-call. A worker killed during a Salesforce write
                # would leave the org changed and this system unsure whether it
                # was — so cancellation waits for a boundary where the answer
                # is unambiguous.
                if await self._should_stop(run, messages, started):
                    return

                if run.steps_used >= run.max_steps:
                    await self._halt_max_steps(run, messages)
                    return

                run.steps_used += 1
                run.heartbeat_at = utcnow()
                await self._set_state(
                    run,
                    RunState.PLANNING if run.steps_used > 1 else RunState.INSPECTING,
                )
                response, route = await call_model(
                    self.db,
                    company_id=self.company_id,
                    project_id=self.project_id,
                    system=system,
                    messages=compact_transcript(messages),
                    tools=tools,
                    policy=self.policy,
                    tier=self.model_tier,
                    agent_run_id=run.id,
                )
                run.input_tokens += response.input_tokens
                run.output_tokens += response.output_tokens
                run.model = response.model
                run.llm_provider = route.provider_kind
                run.model_tier = route.tier
                messages.append({"role": "assistant", "content": response.content})

                if response.text:
                    await self.emit("assistant.text", {"text": response.text})

                if response.stop_reason != "tool_use":
                    await self._complete(run, messages, response.text, started)
                    return

                await self._set_state(run, RunState.EXECUTING)
                if await self._should_stop(run, messages, started):
                    return
                results, paused = await self._dispatch(run, response.tool_uses, org_ctx)

                if paused:
                    run.transcript = messages
                    run.pending_tool_results = results
                    run.state = transition(run.state, RunState.WAITING_FOR_APPROVAL)
                    # Ownership is released while a human decides. Holding a
                    # claim for hours would make the run look abandoned to the
                    # reclaimer and block the worker slot for no reason.
                    run.claimed_by = None
                    run.heartbeat_at = None
                    run.duration_ms = (time.perf_counter() - started) * 1000
                    await self.db.commit()
                    await self.emit("state", {"state": RunState.WAITING_FOR_APPROVAL.value})
                    await self.emit(
                        "run.paused",
                        {"run_id": run.id, "approval_ids": run.pending_approval_ids or []},
                    )
                    return

                messages.append({"role": "user", "content": results})
                run.transcript = messages
                await self.db.commit()

    @asynccontextmanager
    async def _salesforce(self) -> AsyncIterator[SalesforceClient | None]:
        if self.connection is None:
            yield None
            return
        async with SalesforceClient(self.connection, self.db) as client:
            yield client

    # ------------------------------------------------------------------ dispatch
    async def _dispatch(
        self, run: AgentRun, tool_uses: list[dict[str, Any]], org_ctx: OrgContext
    ) -> tuple[list[dict[str, Any]], bool]:
        results: list[dict[str, Any]] = []
        pending_approvals: list[str] = []

        for block in tool_uses:
            name = block.get("name", "")
            args = block.get("input") or {}
            tool = self._tools.get(name)
            if tool is None:
                results.append(
                    self._tool_result(
                        block["id"],
                        {
                            "success": False,
                            "error_type": "UNKNOWN_TOOL",
                            "message": f"No tool named '{name}' is registered.",
                            "retryable": False,
                        },
                        is_error=True,
                    )
                )
                continue

            await self.emit(
                "tool.started",
                {
                    "tool": name,
                    "tool_use_id": block["id"],
                    "arguments": redact(args),
                    "risk": tool.risk.value,
                },
            )

            ctx = self._tool_context(run)

            # 1. deterministic validation
            try:
                if tool.validate:
                    await tool.validate(ctx, args)
            except ToolValidationError as exc:
                payload = exc.to_dict()
                await self._record_execution(
                    run, tool, block, args, payload, ExecutionState.FAILED, RiskLevel.LOW,
                    ApprovalState.NOT_REQUIRED, 0.0, error=exc.message,
                )
                await self.emit("tool.failed", {"tool": name, "error": exc.message})
                results.append(
                    self._tool_result(block["id"], payload, True, tool.provider)
                )
                continue
            except SalesforceError as exc:
                payload = exc.to_dict()
                await self.emit("tool.failed", {"tool": name, "error": exc.message})
                results.append(
                    self._tool_result(block["id"], payload, True, tool.provider)
                )
                continue

            # 2. risk classification (outside the model)
            decision = classify(
                name,
                args,
                tool.risk,
                org_ctx,
                tool.requires_approval,
                tags=tool.tags,
                policy=self.policy,
                provider=tool.provider,
                # Declared by the tool, not inferred from its risk tier.
                mutating=tool.mutating,
            )
            if decision.blocked:
                payload = {
                    "success": False,
                    "error_type": "BLOCKED_BY_POLICY",
                    "message": decision.blocked_reason,
                    "retryable": False,
                    "risk": decision.to_dict(),
                    "suggested_action": (
                        "Explain the policy to the user; do not attempt a workaround."
                    ),
                }
                await self._record_execution(
                    run, tool, block, args, payload, ExecutionState.SKIPPED, decision.risk,
                    ApprovalState.NOT_REQUIRED, 0.0, error=decision.blocked_reason,
                )
                await self.emit("tool.blocked", {"tool": name, "reason": decision.blocked_reason})
                results.append(
                    self._tool_result(block["id"], payload, True, tool.provider)
                )
                continue

            # 3. approval gate
            if decision.requires_approval:
                plan = await tool.plan(ctx, args) if tool.plan else {"title": name}
                approval = Approval(
                    company_id=self.company_id,
                    project_id=self.project_id,
                    agent_run_id=run.id,
                    conversation_id=self.conversation.id,
                    user_id=self.user.id,
                    salesforce_connection_id=self.connection.id if self.connection else None,
                    tool_name=name,
                    tool_use_id=block["id"],
                    arguments=args,
                    plan=plan | {"risk": decision.to_dict()},
                    risk_level=decision.risk,
                    state=ApprovalState.PENDING,
                    # Bind the approval to exactly this operation, this org
                    # state, and a bounded window.
                    change_hash=change_hash(name, args),
                    state_fingerprint=await self._fingerprint(tool, ctx, args),
                    expires_at=expires_at(decision.ttl_seconds),
                    approvals_required=decision.approvals_required,
                    eligible_roles=decision.eligible_roles,
                    require_separate_approver=decision.require_separate_approver,
                )
                self.db.add(approval)
                await self.db.flush()
                pending_approvals.append(approval.id)
                await self._audit(
                    run,
                    action="approval.requested",
                    tool_name=name,
                    arguments=args,
                    risk=decision.risk,
                    approval_state=ApprovalState.PENDING,
                    execution_state=ExecutionState.PROPOSED,
                )
                await self.emit(
                    "approval.requested",
                    {
                        "approval_id": approval.id,
                        "tool": name,
                        "provider": tool.provider,
                        "risk": decision.risk.value,
                        "category": decision.category,
                        "plan": plan,
                        "reasons": decision.reasons,
                        "approvals_required": decision.approvals_required,
                        "eligible_roles": decision.eligible_roles,
                        "expires_at": approval.expires_at.isoformat()
                        if approval.expires_at
                        else None,
                    },
                )
                continue

            # 4. execute
            payload = await self._execute_tool(run, tool, block, args, decision)
            results.append(
                self._tool_result(
                    block["id"],
                    payload,
                    not payload.get("success", False),
                    tool.provider,
                )
            )

        if pending_approvals:
            run.pending_approval_ids = pending_approvals
            await self.db.flush()
            return results, True
        return results, False

    def _tool_context(self, run: AgentRun) -> ToolContext:
        return ToolContext(
            user=self.user,
            db=self.db,
            connection=self.connection,
            sf=self.sf,
            agent_run_id=run.id,
            conversation_id=self.conversation.id,
            emit=self.emit,
            company_id=self.company_id,
            project_id=self.project_id,
            policy=self.policy,
        )

    def _org_context(self) -> OrgContext:
        return OrgContext(
            is_sandbox=bool(self.connection.is_sandbox) if self.connection else True,
            org_type=self.connection.org_type if self.connection else "unknown",
            allow_production_mutations=self.policy.allow_production_mutations,
            environment=self.connection.environment if self.connection else None,
        )

    async def _execute_tool(
        self,
        run: AgentRun,
        tool: Tool,
        block: dict[str, Any],
        args: dict[str, Any],
        decision: RiskDecision,
        approval_state: ApprovalState = ApprovalState.NOT_REQUIRED,
    ) -> dict[str, Any]:
        ctx = self._tool_context(run)
        key = idempotency_key(run.id, tool.name, args)

        # Idempotency: an identical mutation already succeeded in this run.
        if tool.mutating:
            prior = (
                await self.db.execute(
                    select(ToolExecution).where(
                        ToolExecution.agent_run_id == run.id,
                        ToolExecution.idempotency_key == key,
                        ToolExecution.execution_state == ExecutionState.SUCCEEDED,
                    )
                )
            ).scalar_one_or_none()
            if prior is not None:
                await self.emit("tool.idempotent_hit", {"tool": tool.name})
                return (prior.result or {}) | {
                    "idempotent_replay": True,
                    "note": (
                        "This exact mutation already succeeded earlier in this run; "
                        "the previous result is returned instead of executing again."
                    ),
                }

        t0 = time.perf_counter()
        budget = (
            settings.long_tool_timeout_seconds
            if tool.long_running
            else settings.tool_timeout_seconds
        )
        try:
            payload = await asyncio.wait_for(tool.execute(ctx, args), timeout=budget)
        except TimeoutError:
            payload = {
                "success": False,
                "error_type": "TOOL_TIMEOUT",
                "message": f"{tool.name} exceeded {budget:.0f}s.",
                "retryable": True,
                "suggested_action": (
                    "The operation may still be running in Salesforce. Re-inspect state "
                    "before retrying."
                ),
            }
        except SalesforceError as exc:
            payload = exc.to_dict()
        except ToolValidationError as exc:
            payload = exc.to_dict()
        except Exception as exc:
            log.error("tool.unhandled_error", tool=tool.name, exc_info=True)
            payload = {
                "success": False,
                "error_type": "TOOL_INTERNAL_ERROR",
                "message": f"{type(exc).__name__}: {exc}",
                "retryable": False,
                "suggested_action": "Report the failure to the user; do not retry blindly.",
            }
        duration = (time.perf_counter() - t0) * 1000

        # 5. verification against real Salesforce state
        verification: dict[str, Any] | None = None
        if payload.get("success") and tool.verify:
            await self._set_state(run, RunState.VERIFYING)
            await self.emit("tool.verifying", {"tool": tool.name})
            try:
                verification = await tool.verify(ctx, args, payload)
            except Exception as exc:
                verification = {"verified": False, "reason": f"Verification failed: {exc}"}
            payload["verification"] = verification
            payload["verified"] = bool(verification.get("verified"))
            if not payload["verified"]:
                payload["success"] = False
                payload.setdefault("error_type", "VERIFICATION_FAILED")
                payload.setdefault(
                    "message",
                    "The operation was accepted by Salesforce but could not be verified.",
                )
                payload["suggested_action"] = (
                    "Do not report this as complete. Re-inspect the org and tell the user "
                    "what was and was not confirmed."
                )

        await self._record_knowledge(tool, args, payload)

        state = ExecutionState.SUCCEEDED if payload.get("success") else ExecutionState.FAILED
        await self._record_execution(
            run, tool, block, args, payload, state, decision.risk, approval_state, duration,
            idem_key=key if tool.mutating else None,
            error=None if payload.get("success") else payload.get("message"),
        )
        await self.emit(
            "tool.finished" if payload.get("success") else "tool.failed",
            {
                "tool": tool.name,
                "tool_use_id": block["id"],
                "success": bool(payload.get("success")),
                "verified": payload.get("verified"),
                "duration_ms": round(duration, 1),
                "summary": _summarize(payload),
            },
        )
        return payload

    # ----------------------------------------------------------------- approvals
    async def _rebuild_after_approval(self, run: AgentRun) -> list[dict[str, Any]] | None:
        approvals = (
            (
                await self.db.execute(
                    select(Approval).where(Approval.agent_run_id == run.id)
                )
            )
            .scalars()
            .all()
        )
        pending_ids = set(run.pending_approval_ids or [])
        relevant = [a for a in approvals if a.id in pending_ids]
        undecided = [a for a in relevant if a.state == ApprovalState.PENDING]
        if undecided:
            await self.emit(
                "run.waiting",
                {"approval_ids": [a.id for a in undecided], "message": "Approval still pending."},
            )
            return None

        # Every approval has been decided, so the run is executing again — say
        # so before running anything. Executing a tool while the run still
        # reads WAITING_FOR_APPROVAL would make the timeline claim the change
        # happened during the wait.
        await self._set_state(run, RunState.EXECUTING)

        await self._load_tools()
        messages = list(run.transcript or [])
        results = list(run.pending_tool_results or [])
        org_ctx = self._org_context()

        async with self._salesforce() as sf:
            self.sf = sf
            for approval in relevant:
                tool = self._tools.get(approval.tool_name)
                if tool is None:  # pragma: no cover - defensive
                    continue
                if approval.state != ApprovalState.APPROVED:
                    payload = {
                        "success": False,
                        "error_type": "APPROVAL_REJECTED",
                        "message": (
                            f"A human rejected the proposed {approval.tool_name} change."
                            + (f" Note: {approval.decision_note}" if approval.decision_note else "")
                        ),
                        "retryable": False,
                        "suggested_action": (
                            "Do not retry. Acknowledge the rejection and ask what the user "
                            "would like instead."
                        ),
                    }
                    await self._audit(
                        run,
                        action="approval.rejected",
                        tool_name=approval.tool_name,
                        arguments=approval.arguments,
                        risk=approval.risk_level,
                        approval_state=ApprovalState.REJECTED,
                        execution_state=ExecutionState.SKIPPED,
                    )
                    await self.emit("approval.rejected", {"approval_id": approval.id})
                    results.append(
                        self._tool_result(approval.tool_use_id, payload, is_error=True)
                    )
                    continue

                args = approval.effective_arguments()
                ctx = self._tool_context(run)

                # An APPROVED row is not yet authority to execute. It must still
                # be inside its window, still describe this exact operation, and
                # still match the org state it was proposed against.
                guard = await self._approval_still_authorizes(tool, ctx, approval, args)
                if guard is not None:
                    await self._invalidate_approval(run, approval, guard["message"])
                    results.append(
                        self._tool_result(approval.tool_use_id, guard, is_error=True)
                    )
                    continue

                await self.emit(
                    "approval.approved",
                    {
                        "approval_id": approval.id,
                        "tool": approval.tool_name,
                        "approved_by": approval.approved_by,
                    },
                )
                # Re-validate: the org may have changed while awaiting the human.
                try:
                    if tool.validate:
                        await tool.validate(ctx, args)
                except ToolValidationError as exc:
                    payload = exc.to_dict()
                    payload["message"] = (
                        f"Re-validation after approval failed: {exc.message}"
                    )
                    results.append(
                        self._tool_result(
                            approval.tool_use_id, payload, True, tool.provider
                        )
                    )
                    continue

                decision = classify(
                    approval.tool_name,
                    args,
                    tool.risk,
                    org_ctx,
                    tool.requires_approval,
                    tags=tool.tags,
                    policy=self.policy,
                    provider=tool.provider,
                    mutating=tool.mutating,
                )
                block = {"id": approval.tool_use_id, "name": approval.tool_name, "input": args}
                payload = await self._execute_tool(
                    run, tool, block, args, decision, ApprovalState.APPROVED
                )
                results.append(
                    self._tool_result(
                        approval.tool_use_id,
                        payload,
                        not payload.get("success", False),
                        tool.provider,
                    )
                )

        messages.append({"role": "user", "content": results})
        run.pending_tool_results = None
        run.pending_approval_ids = None
        run.transcript = messages
        await self.db.commit()
        return messages

    async def _fingerprint(
        self, tool: Tool, ctx: ToolContext, args: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Snapshot the org state a change is being proposed against."""
        if tool.fingerprint is None:
            return None
        try:
            return await tool.fingerprint(ctx, args)
        except Exception as exc:  # a fingerprint failure must not block review
            log.warning("approval.fingerprint_failed", tool=tool.name, error=str(exc))
            return {"_error": f"{type(exc).__name__}: {exc}"}

    async def _approval_still_authorizes(
        self, tool: Tool, ctx: ToolContext, approval: Approval, args: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Return an error payload if this approval no longer authorizes `args`.

        Three independent ways an approval stops being valid:
          * it aged out of its window;
          * the arguments differ from the ones a human actually saw;
          * the org state it was proposed against moved underneath it.
        """
        if is_expired(approval.expires_at):
            return {
                "success": False,
                "error_type": "APPROVAL_EXPIRED",
                "message": (
                    f"The approval for {approval.tool_name} expired before it was "
                    "executed. Nothing was changed."
                ),
                "retryable": False,
                "suggested_action": (
                    "Tell the user the approval expired and propose the change again "
                    "so a fresh approval can be reviewed."
                ),
            }

        current_hash = change_hash(approval.tool_name, args)
        if approval.change_hash and current_hash != approval.change_hash:
            return {
                "success": False,
                "error_type": "APPROVAL_ARGUMENTS_CHANGED",
                "message": (
                    "The operation no longer matches what was approved, so the approval "
                    "does not authorize it. Nothing was changed."
                ),
                "retryable": False,
                "suggested_action": (
                    "Propose the change again and let a human approve the new version."
                ),
            }

        current_fp = await self._fingerprint(tool, ctx, args)
        if fingerprint_drifted(approval.state_fingerprint, current_fp):
            return {
                "success": False,
                "error_type": "APPROVAL_STATE_DRIFTED",
                "message": (
                    "The Salesforce org changed while this change was awaiting approval, "
                    "so the approved plan may no longer be correct. Nothing was changed."
                ),
                "retryable": False,
                "suggested_action": (
                    "Re-inspect the org, explain what moved, and propose the change again."
                ),
                "details": {"approved_state": approval.state_fingerprint,
                            "current_state": current_fp},
            }
        return None

    async def _invalidate_approval(
        self, run: AgentRun, approval: Approval, reason: str
    ) -> None:
        approval.state = ApprovalState.EXPIRED
        approval.invalidated_reason = reason
        await self.db.flush()
        await self._audit(
            run,
            action="approval.invalidated",
            tool_name=approval.tool_name,
            arguments=approval.effective_arguments(),
            risk=approval.risk_level,
            approval_state=ApprovalState.EXPIRED,
            execution_state=ExecutionState.SKIPPED,
            outcome="blocked",
            error=reason,
        )
        await self.emit(
            "approval.invalidated",
            {"approval_id": approval.id, "tool": approval.tool_name, "reason": reason},
        )

    # ------------------------------------------------------------------ helpers
    def _tool_result(
        self,
        tool_use_id: str,
        payload: dict[str, Any],
        is_error: bool = False,
        provider: str = "native",
    ) -> dict[str, Any]:
        return {
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "is_error": is_error,
            "content": serialize_tool_result(payload, provider=provider),
        }

    async def _recall_knowledge(self, request: str) -> str:
        """Relevant prior observations about this org, within a budget.

        A hint that saves a tool call is worth its tokens; a dump of the org is
        not. `store.recall` enforces the budget, and the rendered block tells
        the model explicitly that these observations may be stale.
        """
        if self.connection is None or not request:
            return ""
        try:
            from app.knowledge import store as knowledge_store

            entries = await knowledge_store.recall(
                self.db, salesforce_connection_id=self.connection.id, query=request
            )
            if entries:
                await self.emit("knowledge.recalled", {"count": len(entries)})
            return knowledge_store.render_for_prompt(entries)
        except Exception as exc:  # knowledge is an optimization, never a blocker
            log.warning("knowledge.recall_failed", error=str(exc))
            return ""

    async def _record_knowledge(
        self, tool: Tool, args: dict[str, Any], payload: dict[str, Any]
    ) -> None:
        """Persist what a tool learned about the org, when it is worth keeping.

        Only org-sourced facts: a describe result, a deployment outcome. The
        model's own prose is never written here.
        """
        if self.connection is None:
            return
        try:
            from app.knowledge import store as knowledge_store

            if tool.name == "describe_object" and payload.get("success"):
                name = str(payload.get("object") or args.get("object") or "")
                if name and payload.get("fields"):
                    # The tool returns summarized fields; record_schema wants the
                    # describe shape, so map the two keys it actually reads.
                    await knowledge_store.record_schema(
                        self.db,
                        company_id=self.company_id,
                        project_id=self.project_id,
                        salesforce_connection_id=self.connection.id,
                        object_name=name,
                        describe={
                            "name": name,
                            "label": payload.get("label"),
                            "createable": (payload.get("permissions") or {}).get(
                                "createable"
                            ),
                            "updateable": (payload.get("permissions") or {}).get(
                                "updateable"
                            ),
                            "fields": payload["fields"],
                        },
                    )
            elif payload.get("deploy_id") or payload.get("deployment_id"):
                label = str(
                    payload.get("field")
                    or payload.get("api_name")
                    or payload.get("name")
                    or payload.get("change_set_id")
                    or tool.name
                )
                await knowledge_store.record_deployment(
                    self.db,
                    company_id=self.company_id,
                    project_id=self.project_id,
                    salesforce_connection_id=self.connection.id,
                    label=f"{tool.name}: {label}",
                    succeeded=bool(payload.get("success")),
                    detail={
                        "tool": tool.name,
                        "message": payload.get("message"),
                        "error_type": payload.get("error_type"),
                        "deploy_id": payload.get("deploy_id"),
                    },
                )
        except Exception as exc:  # never fail a run over bookkeeping
            log.warning("knowledge.record_failed", tool=tool.name, error=str(exc))

    async def _load_tools(self) -> None:
        """Assemble the tool view for this run: native + tenant MCP tools."""
        if self._tools:
            return
        self._tools = {t.name: t for t in self.registry.all()}
        if not settings.mcp_enabled or not self.project_id:
            return
        try:
            from app.mcp.manager import tools_for_tenant

            external = await tools_for_tenant(self.db, self.project_id)
        except Exception as exc:
            # A misconfigured MCP server must not take the agent down; it just
            # means those capabilities are unavailable for this run.
            log.warning("mcp.tool_load_failed", error=str(exc))
            return
        for tool in external:
            # Native tools always win a name collision.
            self._tools.setdefault(tool.name, tool)
        if external:
            await self.emit(
                "tools.loaded",
                {
                    "native": len(self.registry.all()),
                    "external": len(external),
                    "providers": sorted({t.provider for t in external}),
                },
            )

    async def _set_state(self, run: AgentRun, state: RunState) -> None:
        """Move the run, refusing moves the state machine does not allow.

        An illegal transition is a bug in this file, not a condition to recover
        from, so it is logged loudly — but it is not allowed to abandon a run
        that may already have changed a customer's org.
        """
        if run.state is state:
            return
        try:
            run.state = transition(run.state, state)
        except IllegalTransition as exc:
            log.error(
                "agent.illegal_transition",
                agent_run_id=run.id,
                current=run.state.value,
                target=state.value,
            )
            raise RuntimeError(str(exc)) from exc
        await self.emit("state", {"state": state.value})

    async def _should_stop(
        self, run: AgentRun, messages: list[dict[str, Any]], started: float
    ) -> bool:
        """Has someone asked this run to stop, or has it run out of time?

        Both answers end the run cleanly with its work so far recorded, rather
        than dropping it. A user who cancels still needs to know exactly what
        was changed before the stop took effect.
        """
        await self.db.refresh(run, ["cancel_requested", "deadline_at"])

        if run_queue.cancel_requested(run):
            await self._stop(
                run,
                messages,
                started,
                RunState.CANCELLED,
                "This run was cancelled. Anything already changed in Salesforce is "
                "listed in the run history above and was not rolled back.",
                "run.cancelled",
                "cancelled",
            )
            return True

        if run_queue.is_overdue(run):
            await self._stop(
                run,
                messages,
                started,
                RunState.EXPIRED,
                "This run exceeded its time limit and was stopped. Anything already "
                "changed in Salesforce is listed in the run history above.",
                "run.expired",
                "expired",
                error_code="RUN_EXPIRED",
            )
            return True

        return False

    async def _stop(
        self,
        run: AgentRun,
        messages: list[dict[str, Any]],
        started: float,
        state: RunState,
        message: str,
        event: str,
        outcome: str,
        error_code: str | None = None,
    ) -> None:
        run.state = transition(run.state, state)
        run.final_text = message
        run.transcript = messages
        run.error_code = error_code
        run.duration_ms = (time.perf_counter() - started) * 1000
        run.finished_at = utcnow()
        run.claimed_by = None
        run.heartbeat_at = None
        self.db.add(
            Message(
                conversation_id=self.conversation.id,
                role="assistant",
                text=message,
                agent_run_id=run.id,
            )
        )
        await self.db.commit()
        await self._audit(run, action=f"agent.run_{outcome}", outcome=outcome)
        await self.emit("state", {"state": state.value})
        await self.emit(event, {"run_id": run.id, "message": message})

    async def _halt_max_steps(self, run: AgentRun, messages: list[dict[str, Any]]) -> None:
        message = (
            f"Stopped after {run.max_steps} agent steps without completing the request. "
            "This is a safety limit against runaway tool loops; the operation needs "
            "manual review before continuing."
        )
        run.state = transition(run.state, RunState.FAILED)
        run.error = message
        run.error_code = "MAX_STEPS_EXCEEDED"
        run.final_text = message
        run.transcript = messages
        run.finished_at = utcnow()
        run.claimed_by = None
        run.heartbeat_at = None
        self.db.add(
            Message(
                conversation_id=self.conversation.id,
                role="assistant",
                text=message,
                agent_run_id=run.id,
            )
        )
        await self.db.commit()
        await self._audit(run, action="agent.max_steps_exceeded", outcome="halted")
        await self.emit("state", {"state": RunState.FAILED.value})
        await self.emit("run.halted", {"reason": "max_steps_exceeded", "message": message})

    async def _complete(
        self, run: AgentRun, messages: list[dict[str, Any]], text: str, started: float
    ) -> None:
        run.state = transition(run.state, RunState.COMPLETED)
        run.final_text = text
        run.transcript = messages
        run.duration_ms = (time.perf_counter() - started) * 1000
        run.finished_at = utcnow()
        run.claimed_by = None
        run.heartbeat_at = None
        self.db.add(
            Message(
                conversation_id=self.conversation.id,
                role="assistant",
                text=text,
                agent_run_id=run.id,
            )
        )
        await self.db.commit()
        await self._audit(run, action="agent.run_completed", outcome="ok")
        await self.emit("state", {"state": RunState.COMPLETED.value})
        await self.emit(
            "run.completed",
            {
                "run_id": run.id,
                "steps": run.steps_used,
                "input_tokens": run.input_tokens,
                "output_tokens": run.output_tokens,
                "duration_ms": round(run.duration_ms, 1),
            },
        )

    async def _record_execution(
        self,
        run: AgentRun,
        tool: Tool,
        block: dict[str, Any],
        args: dict[str, Any],
        payload: dict[str, Any],
        state: ExecutionState,
        risk: RiskLevel,
        approval_state: ApprovalState,
        duration_ms: float,
        idem_key: str | None = None,
        error: str | None = None,
    ) -> ToolExecution:
        record_ids = []
        if payload.get("record_id"):
            record_ids = [payload["record_id"]]
        execution = ToolExecution(
            company_id=self.company_id,
            project_id=self.project_id,
            agent_run_id=run.id,
            conversation_id=self.conversation.id,
            user_id=self.user.id,
            salesforce_connection_id=self.connection.id if self.connection else None,
            tool_name=tool.name,
            tool_use_id=block.get("id", ""),
            arguments=redact(args),
            result=redact(_summarize(payload, full=True)),
            risk_level=risk,
            approval_state=approval_state,
            execution_state=state,
            idempotency_key=idem_key,
            salesforce_object=str(args.get("object") or "") or None,
            record_ids=record_ids or None,
            error_type=payload.get("error_type"),
            error_message=error,
            duration_ms=duration_ms,
        )
        self.db.add(execution)
        await self.db.flush()
        await self._audit(
            run,
            action=tool.audit_action or f"tool.{tool.name}",
            tool_name=tool.name,
            arguments=args,
            result_summary=_summarize(payload),
            risk=risk,
            approval_state=approval_state,
            execution_state=state,
            tool_execution_id=execution.id,
            sobject=str(args.get("object") or "") or None,
            record_ids=record_ids or None,
            before=payload.get("before"),
            after=(payload.get("verification") or {}).get("after"),
            deployment_id=payload.get("deployment_id"),
            outcome="ok" if payload.get("success") else "error",
            error=error,
        )
        await self.db.commit()
        return execution

    async def _audit(
        self,
        run: AgentRun,
        *,
        action: str,
        tool_name: str | None = None,
        arguments: dict[str, Any] | None = None,
        result_summary: dict[str, Any] | None = None,
        risk: RiskLevel | None = None,
        approval_state: ApprovalState | None = None,
        execution_state: ExecutionState | None = None,
        tool_execution_id: str | None = None,
        sobject: str | None = None,
        record_ids: list[str] | None = None,
        before: dict[str, Any] | None = None,
        after: dict[str, Any] | None = None,
        deployment_id: str | None = None,
        outcome: str = "ok",
        error: str | None = None,
    ) -> None:
        self.db.add(
            AuditEvent(
                company_id=self.company_id,
                project_id=self.project_id,
                user_id=self.user.id,
                salesforce_connection_id=self.connection.id if self.connection else None,
                sf_org_id=self.connection.sf_org_id if self.connection else None,
                conversation_id=self.conversation.id,
                agent_run_id=run.id,
                # The thread that ties a Jira read, a Salesforce write and a Git
                # commit together as one piece of work. Without it, an auditor
                # reconstructs the story from timestamps and hopes.
                correlation_id=run.correlation_id,
                tool_execution_id=tool_execution_id,
                action=action,
                tool_name=tool_name,
                arguments=redact(arguments) if arguments else None,
                result_summary=redact(result_summary) if result_summary else None,
                salesforce_object=sobject,
                record_ids=record_ids,
                before_values=before,
                after_values=after,
                risk_level=risk,
                approval_state=approval_state,
                execution_state=execution_state,
                deployment_id=deployment_id,
                outcome=outcome,
                error=error,
            )
        )
        await self.db.flush()


def _summarize(payload: dict[str, Any], full: bool = False) -> dict[str, Any]:
    """Audit/UI-safe summary: never store or display whole record dumps."""
    out = {
        k: v
        for k, v in payload.items()
        if k
        in {
            "success",
            "object",
            "field",
            "record_id",
            "count",
            "total_size",
            "deploy_id",
            "deployment_id",
            "status",
            "verified",
            "error_type",
            "message",
            "already_exists",
            "check_only",
        }
    }
    if full and payload.get("verification"):
        out["verification"] = payload["verification"]
    if payload.get("errors"):
        out["errors"] = payload["errors"][:5]
    return out


async def touch_conversation(db: AsyncSession, conversation: Conversation) -> None:
    conversation.updated_at = datetime.now(UTC)
    await db.commit()
