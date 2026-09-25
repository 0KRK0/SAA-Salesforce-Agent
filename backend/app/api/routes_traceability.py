"""One question, answered across three systems: what actually happened?

A single request — "add a Tier field, the ticket is SF-142" — touches Jira,
Salesforce and a Git repository. Afterwards, three different people need three
different views of it: the admin wants to know what changed in the org, the
release manager wants the pull request, and whoever is on the hook for the
audit wants to know that the ticket, the change and the commit are the same
piece of work.

Every row this platform writes already carries `correlation_id`. This endpoint
is what makes that worth having: it reads the trail rather than reconstructing
it from timestamps, and it reports only operations that have a recorded API
call behind them.

Nothing here is inferred. If the agent said it updated Jira and no Jira write
was recorded, this shows no Jira write — which is the point.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select

from app.execution.state import describe as describe_state
from app.models import (
    TERMINAL_RUN_STATES,
    AgentRun,
    Approval,
    AuditEvent,
    ChangeSet,
    Deployment,
    ToolExecution,
)
from app.security.auth import DbSession, Tenant
from app.tenancy import service as tenancy

router = APIRouter(prefix="/traceability", tags=["traceability"])

#: Which system a **non-tool** action belongs to. Platform events — approvals,
#: sign-ins, policy edits — have stable prefixes and no tool behind them.
SYSTEM_BY_PREFIX: tuple[tuple[str, str], ...] = (
    ("agent.", "agent"),
    ("approval.", "approval"),
    ("integration.", "repository"),
    ("retention.", "platform"),
    ("ai.", "platform"),
    ("project.", "platform"),
    ("sso.", "platform"),
    ("scim.", "platform"),
    ("auth.", "platform"),
)

#: Tool provider -> system. A tool's provider is declared on the tool itself,
#: so this is read rather than inferred.
SYSTEM_BY_PROVIDER: dict[str, str] = {
    "native": "salesforce",
    "jira": "jira",
    "github": "repository",
    "bitbucket": "repository",
}


def _tool_systems() -> dict[str, str]:
    """audit action -> system, built from the live tool registry.

    Derived rather than guessed. The first version of this hardcoded action
    prefixes and classified 39 of 52 tools as "other" — every Salesforce tool
    whose `audit_action` did not happen to start with `tool.`. A cross-system
    trail that files most of a Salesforce change under "other" is worse than
    no trail, because it reads as complete.
    """
    from app.tools.registry import build_registry

    mapping: dict[str, str] = {}
    for tool in build_registry().all():
        action = tool.audit_action or f"tool.{tool.name}"
        if tool.provider.startswith("mcp"):
            mapping[action] = "mcp"
            continue
        mapping[action] = SYSTEM_BY_PROVIDER.get(tool.provider, "salesforce")
    return mapping


def _system_for(action: str) -> str:
    """Which system an audited action touched.

    Tools are classified from the registry; platform events from their prefix.
    Anything else is reported as "other" rather than guessed at — a wrong
    system attribution in an audit trail is worse than an unclassified one.
    """
    known = _tool_systems()
    if action in known:
        return known[action]
    for prefix, system in SYSTEM_BY_PREFIX:
        if action.startswith(prefix):
            return system
    # An MCP tool's audit action carries its namespaced name.
    if action.startswith("tool.mcp__") or action.startswith("mcp__"):
        return "mcp"
    if action.startswith("tool."):
        return "salesforce"
    return "other"


@router.get("/runs/{run_id}")
async def trace_run(run_id: str, tenant: Tenant, db: DbSession) -> dict[str, Any]:
    """Everything one run touched, grouped by the system it touched."""
    run = await tenancy.owned(db, AgentRun, run_id, tenant.project_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return await _trace(db, tenant, run.correlation_id, run)


@router.get("/correlation/{correlation_id}")
async def trace_correlation(
    correlation_id: str, tenant: Tenant, db: DbSession
) -> dict[str, Any]:
    """Everything under one correlation id, across runs.

    A conversation that spans several runs — propose, wait for approval,
    resume — shares one correlation id, so this is the view that answers "what
    did this piece of work do" rather than "what did this run do".
    """
    runs = (
        await db.execute(
            select(AgentRun)
            .where(
                AgentRun.project_id == tenant.project_id,
                AgentRun.correlation_id == correlation_id,
            )
            .order_by(AgentRun.created_at)
        )
    ).scalars().all()
    if not runs:
        raise HTTPException(status_code=404, detail="Nothing found for that id")
    return await _trace(db, tenant, correlation_id, runs[0], runs=list(runs))


async def _trace(
    db: Any,
    tenant: Tenant,
    correlation_id: str,
    primary: AgentRun,
    runs: list[AgentRun] | None = None,
) -> dict[str, Any]:
    runs = runs or [primary]
    run_ids = [r.id for r in runs]

    events = (
        await db.execute(
            select(AuditEvent)
            .where(
                AuditEvent.project_id == tenant.project_id,
                AuditEvent.correlation_id == correlation_id,
            )
            .order_by(AuditEvent.created_at)
        )
    ).scalars().all()
    if not events:
        # Older runs predate correlation ids on audit rows. Falling back to the
        # run id keeps the view useful rather than showing an empty trail.
        events = (
            await db.execute(
                select(AuditEvent)
                .where(
                    AuditEvent.project_id == tenant.project_id,
                    AuditEvent.agent_run_id.in_(run_ids),
                )
                .order_by(AuditEvent.created_at)
            )
        ).scalars().all()

    executions = (
        await db.execute(
            select(ToolExecution)
            .where(
                ToolExecution.project_id == tenant.project_id,
                ToolExecution.agent_run_id.in_(run_ids),
            )
            .order_by(ToolExecution.created_at)
        )
    ).scalars().all()

    approvals = (
        await db.execute(
            select(Approval)
            .where(
                Approval.project_id == tenant.project_id,
                Approval.agent_run_id.in_(run_ids),
            )
            .order_by(Approval.created_at)
        )
    ).scalars().all()

    deployments = (
        await db.execute(
            select(Deployment).where(
                Deployment.project_id == tenant.project_id,
                Deployment.agent_run_id.in_(run_ids),
            )
        )
    ).scalars().all()

    change_sets = (
        await db.execute(
            select(ChangeSet).where(
                ChangeSet.project_id == tenant.project_id,
                ChangeSet.agent_run_id.in_(run_ids),
            )
        )
    ).scalars().all()

    by_system: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        by_system.setdefault(_system_for(event.action), []).append(
            {
                "action": event.action,
                "tool_name": event.tool_name,
                "outcome": event.outcome,
                "salesforce_object": event.salesforce_object,
                "record_ids": event.record_ids,
                "risk_level": event.risk_level.value if event.risk_level else None,
                "at": event.created_at.isoformat(),
            }
        )

    return {
        "correlation_id": correlation_id,
        "runs": [
            {
                "id": r.id,
                "state": r.state.value,
                "state_label": describe_state(r.state),
                "terminal": r.state in TERMINAL_RUN_STATES,
                "request": r.user_request[:500],
                "provider": r.llm_provider,
                "model": r.model,
                "created_at": r.created_at.isoformat(),
            }
            for r in runs
        ],
        "systems": {
            system: {"count": len(rows), "events": rows}
            for system, rows in sorted(by_system.items())
        },
        "salesforce": {
            "tool_executions": [
                {
                    "tool": e.tool_name,
                    "state": e.execution_state.value,
                    "object": e.salesforce_object,
                    "record_ids": e.record_ids,
                    "risk": e.risk_level.value,
                    "approval_state": e.approval_state.value,
                    "error": e.error_message,
                    "duration_ms": e.duration_ms,
                    "at": e.created_at.isoformat(),
                }
                for e in executions
            ],
            "deployments": [
                {
                    "id": d.id,
                    "salesforce_deploy_id": d.salesforce_deploy_id,
                    "environment": d.environment.value,
                    "check_only": d.check_only,
                    "status": d.status,
                    "components_failed": d.components_failed,
                    "tests_failed": d.tests_failed,
                    "verified": d.verified,
                }
                for d in deployments
            ],
            "change_sets": [
                {"id": c.id, "name": c.name, "state": c.state.value, "verified": c.verified}
                for c in change_sets
            ],
        },
        "approvals": [
            {
                "id": a.id,
                "tool_name": a.tool_name,
                "state": a.state.value,
                "risk_level": a.risk_level.value,
                "environment": a.environment.value if a.environment else None,
                "approvals_required": a.approvals_required,
                "approved_by": a.approved_by,
                "invalidated_reason": a.invalidated_reason,
                "at": a.created_at.isoformat(),
            }
            for a in approvals
        ],
        "note": (
            "Every entry here has a recorded API call behind it. An operation "
            "the agent described but did not perform does not appear."
        ),
    }


@router.get("/search")
async def search_trail(
    tenant: Tenant,
    db: DbSession,
    reference: str = Query(
        ..., min_length=2, description="A Jira key, repository name, or object name"
    ),
    limit: int = Query(50, ge=1, le=200),
) -> dict[str, Any]:
    """Find the work that touched a given ticket, repository or object.

    The question an auditor actually asks is "what did this system do about
    SF-142", not "show me run 7f3a". This answers it from the audit trail.
    """
    needle = reference.strip()
    rows = (
        await db.execute(
            select(AuditEvent)
            .where(AuditEvent.project_id == tenant.project_id)
            .order_by(AuditEvent.created_at.desc())
            .limit(2000)
        )
    ).scalars().all()

    matches = [
        r
        for r in rows
        if needle.lower() in str(r.arguments or {}).lower()
        or needle.lower() in str(r.result_summary or {}).lower()
        or (r.salesforce_object or "").lower() == needle.lower()
    ][:limit]

    correlations = sorted({r.correlation_id for r in matches if r.correlation_id})
    return {
        "reference": needle,
        "match_count": len(matches),
        "correlation_ids": correlations,
        "matches": [
            {
                "action": r.action,
                "system": _system_for(r.action),
                "outcome": r.outcome,
                "correlation_id": r.correlation_id,
                "agent_run_id": r.agent_run_id,
                "at": r.created_at.isoformat(),
            }
            for r in matches
        ],
        "note": (
            "Searched the audit trail of this project only. Use "
            "/traceability/correlation/{id} for the full picture of one piece of "
            "work."
        ),
    }
