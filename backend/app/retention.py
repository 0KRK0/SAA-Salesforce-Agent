"""Deleting what a customer did not ask us to keep.

The product's default is **no customer-data retention**. Salesforce records,
Jira descriptions, repository contents and model prompts are processed and
discarded; what stays is identity, security and governance metadata.

But "processed and discarded" has to be a job that runs, not a sentence in a
README. A tool result sits in `tool_executions.result` and a transcript sits in
`agent_runs.transcript` because the run needed them — and unless something
removes them afterwards, the default is quietly the opposite of what was
promised.

Four retentions, all per project, all defaulting to zero (delete as soon as the
work is finished):

  * `retain_conversation_days` — message text and run transcripts
  * `retain_tool_payload_days` — tool arguments and results
  * `retain_document_days`     — uploaded documents
  * `audit_retention_days`     — audit rows, default 365

Two rules the sweeper never breaks:

  1. **A live run is never touched.** Deleting the transcript of a run that is
     waiting for approval would destroy the thing the approval authorizes.
  2. **Audit metadata outlives audit payloads.** The row that says *what*
     happened, *who* approved it and *when* is what a compliance question is
     asked about; the argument blob is not. Payloads are cleared long before
     the row itself is, and a row is only removed after `audit_retention_days`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    TERMINAL_RUN_STATES,
    AgentRun,
    AuditEvent,
    Conversation,
    Message,
    Project,
    ProjectPolicy,
    RunEvent,
    ToolExecution,
)
from app.observability.logging import get_logger

log = get_logger("retention")

#: A floor under audit deletion, whatever a project configures. An audit trail
#: shorter than this is not an audit trail, and a policy edit should not be
#: able to erase last week's approvals.
MIN_AUDIT_DAYS = 30


@dataclass
class SweepResult:
    project_id: str = ""
    transcripts_cleared: int = 0
    messages_deleted: int = 0
    tool_payloads_cleared: int = 0
    run_events_deleted: int = 0
    audit_payloads_cleared: int = 0
    audit_rows_deleted: int = 0
    skipped_live_runs: int = 0
    notes: list[str] = field(default_factory=list)

    def total(self) -> int:
        return (
            self.transcripts_cleared
            + self.messages_deleted
            + self.tool_payloads_cleared
            + self.run_events_deleted
            + self.audit_payloads_cleared
            + self.audit_rows_deleted
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "transcripts_cleared": self.transcripts_cleared,
            "messages_deleted": self.messages_deleted,
            "tool_payloads_cleared": self.tool_payloads_cleared,
            "run_events_deleted": self.run_events_deleted,
            "audit_payloads_cleared": self.audit_payloads_cleared,
            "audit_rows_deleted": self.audit_rows_deleted,
            "skipped_live_runs": self.skipped_live_runs,
            "total": self.total(),
            "notes": self.notes,
        }


def _cutoff(days: int, now: datetime) -> datetime:
    """When a retention of `days` expires. Zero means "as soon as it is done"."""
    return now - timedelta(days=max(0, int(days)))


async def sweep_project(
    db: AsyncSession,
    project: Project,
    policy: ProjectPolicy | None,
    *,
    now: datetime | None = None,
    dry_run: bool = False,
) -> SweepResult:
    """Apply one project's retention policy."""
    moment = now or datetime.now(UTC)
    result = SweepResult(project_id=project.id)

    conversation_days = int(getattr(policy, "retain_conversation_days", 0) or 0)
    payload_days = int(getattr(policy, "retain_tool_payload_days", 0) or 0)
    audit_days = max(
        MIN_AUDIT_DAYS, int(getattr(policy, "audit_retention_days", 365) or 365)
    )

    live_runs = await _live_run_ids(db, project.id)
    result.skipped_live_runs = len(live_runs)

    # --- conversation content -------------------------------------------
    cutoff = _cutoff(conversation_days, moment)
    runs = (
        await db.execute(
            select(AgentRun).where(
                AgentRun.project_id == project.id,
                AgentRun.state.in_(list(TERMINAL_RUN_STATES)),
                AgentRun.created_at <= cutoff,
            )
        )
    ).scalars().all()
    for run in runs:
        if run.id in live_runs:  # pragma: no cover - belt and braces
            continue
        if run.transcript or run.pending_tool_results:
            if not dry_run:
                # The final text stays: it is what the user was told, and a run
                # that reports nothing is indistinguishable from one that never
                # happened.
                run.transcript = None
                run.pending_tool_results = None
            result.transcripts_cleared += 1

    # Message bodies follow the same clock as transcripts: they are the same
    # content, and keeping one without the other would be arbitrary.
    conditions = [
        Conversation.project_id == project.id,
        Message.created_at <= cutoff,
    ]
    if live_runs:
        conditions.append(Message.agent_run_id.not_in(live_runs))
    stale_messages = (
        await db.execute(
            select(Message.id)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(*conditions)
        )
    ).scalars().all()
    if stale_messages and not dry_run:
        await db.execute(delete(Message).where(Message.id.in_(stale_messages)))
    result.messages_deleted = len(stale_messages)

    # --- tool payloads ---------------------------------------------------
    payload_cutoff = _cutoff(payload_days, moment)
    executions = (
        await db.execute(
            select(ToolExecution).where(
                ToolExecution.project_id == project.id,
                ToolExecution.created_at <= payload_cutoff,
            )
        )
    ).scalars().all()
    for execution in executions:
        if execution.agent_run_id in live_runs:
            continue
        if execution.arguments is None and execution.result is None:
            continue
        if not dry_run:
            # Everything that identifies the operation survives: tool name,
            # object, record ids, risk, approval state, outcome. Only the
            # payloads go.
            execution.arguments = None
            execution.result = None
        result.tool_payloads_cleared += 1

    # Run events carry the same content as the timeline the user watched.
    event_conditions = [
        RunEvent.project_id == project.id,
        RunEvent.created_at <= payload_cutoff,
    ]
    if live_runs:
        event_conditions.append(RunEvent.agent_run_id.not_in(live_runs))
    event_rows = (
        await db.execute(select(RunEvent.id).where(*event_conditions))
    ).scalars().all()
    if event_rows and not dry_run:
        await db.execute(delete(RunEvent).where(RunEvent.id.in_(event_rows)))
    result.run_events_deleted = len(event_rows)

    # --- audit -----------------------------------------------------------
    # Payloads first, on the *tool payload* clock: an audit row's `arguments`
    # is the same customer data as a tool execution's, and there is no reason
    # for it to outlive the thing it describes.
    audit_payloads = (
        await db.execute(
            select(AuditEvent).where(
                AuditEvent.project_id == project.id,
                AuditEvent.created_at <= payload_cutoff,
            )
        )
    ).scalars().all()
    for row in audit_payloads:
        if not any((row.arguments, row.result_summary, row.before_values, row.after_values)):
            continue
        if not dry_run:
            row.arguments = None
            row.result_summary = None
            row.before_values = None
            row.after_values = None
        result.audit_payloads_cleared += 1

    # The rows themselves last far longer, and never less than the floor.
    audit_cutoff = _cutoff(audit_days, moment)
    stale_audit = (
        await db.execute(
            select(AuditEvent.id).where(
                AuditEvent.project_id == project.id,
                AuditEvent.created_at <= audit_cutoff,
            )
        )
    ).scalars().all()
    if stale_audit and not dry_run:
        await db.execute(delete(AuditEvent).where(AuditEvent.id.in_(stale_audit)))
    result.audit_rows_deleted = len(stale_audit)

    if result.skipped_live_runs:
        result.notes.append(
            f"{result.skipped_live_runs} run(s) are still in progress and were "
            "left untouched. Deleting the transcript of a run waiting for "
            "approval would destroy what the approval authorizes."
        )
    if not dry_run and result.total():
        await db.flush()
    return result


async def _live_run_ids(db: AsyncSession, project_id: str) -> list[str]:
    rows = (
        await db.execute(
            select(AgentRun.id).where(
                AgentRun.project_id == project_id,
                AgentRun.state.not_in(list(TERMINAL_RUN_STATES)),
            )
        )
    ).scalars().all()
    return list(rows)


async def sweep_all(
    db: AsyncSession, *, now: datetime | None = None, dry_run: bool = False
) -> list[SweepResult]:
    """Run retention for every active project."""
    projects = (
        await db.execute(select(Project).where(Project.is_active.is_(True)))
    ).scalars().all()

    results: list[SweepResult] = []
    for project in projects:
        policy = (
            await db.execute(
                select(ProjectPolicy).where(ProjectPolicy.project_id == project.id)
            )
        ).scalar_one_or_none()
        result = await sweep_project(db, project, policy, now=now, dry_run=dry_run)
        if result.total():
            log.info("retention.swept", **result.to_dict())
        results.append(result)

    if not dry_run and any(r.total() for r in results):
        await db.commit()
    return results


def describe(policy: ProjectPolicy | None) -> dict[str, Any]:
    """What this project keeps, and for how long, in plain terms."""

    def phrase(days: int, what: str) -> str:
        if days <= 0:
            return f"{what} are deleted as soon as the work finishes."
        return f"{what} are kept for {days} day{'s' if days != 1 else ''}."

    conversation = int(getattr(policy, "retain_conversation_days", 0) or 0)
    payload = int(getattr(policy, "retain_tool_payload_days", 0) or 0)
    document = int(getattr(policy, "retain_document_days", 0) or 0)
    audit = max(MIN_AUDIT_DAYS, int(getattr(policy, "audit_retention_days", 365) or 365))

    return {
        "retain_conversation_days": conversation,
        "retain_tool_payload_days": payload,
        "retain_document_days": document,
        "audit_retention_days": audit,
        "audit_minimum_days": MIN_AUDIT_DAYS,
        "explained": [
            phrase(conversation, "Message text and run transcripts"),
            phrase(payload, "Tool arguments and results"),
            phrase(document, "Uploaded documents"),
            f"Audit rows are kept for {audit} days — never fewer than "
            f"{MIN_AUDIT_DAYS}, whatever the policy says.",
        ],
        "always_kept": [
            "Who did what, when, with what risk and whose approval.",
            "Which object and record ids an operation touched.",
            "Deployment ids, outcomes and verification results.",
        ],
        "never_stored": [
            "Salesforce field values beyond the before/after of an approved "
            "change, and those only for the tool-payload retention period.",
            "Model prompts and completions.",
            "Provider credentials in any form other than a secret-store "
            "reference.",
        ],
    }
