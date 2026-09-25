"""What this deployment is actually doing, and what it is keeping.

Three surfaces, all deliberately boring:

  * **Metrics** — run counts, failure rate, queue depth, spend. Enough to
    answer "is it healthy" and "what is it costing" without a monitoring stack,
    and honest about being a point-in-time read rather than a time series.
  * **Retention** — what this project keeps, for how long, and a way to run the
    sweep now. Stated in sentences, because a compliance answer that reads as a
    settings dump is not an answer.
  * **Entitlements** — the plan's run allowance and how much of it is used.

None of it invents a number. A rate over zero runs is reported as "no data",
not as 0% or 100%, because both of those read as facts.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Query
from sqlalchemy import func, select

from app.config import settings
from app.execution import queue as run_queue
from app.models import (
    TERMINAL_RUN_STATES,
    AgentRun,
    Approval,
    ApprovalState,
    AuditEvent,
    LLMUsage,
    ProjectRole,
    RunState,
    ToolExecution,
)
from app.observability.redaction import describe as describe_redaction
from app.retention import describe as describe_retention
from app.retention import sweep_project
from app.security.auth import DbSession, Tenant
from app.security.ratelimit import describe as describe_rate_limits
from app.tenancy import service as tenancy

router = APIRouter(prefix="/operations", tags=["operations"])


def _rate(numerator: int, denominator: int) -> float | None:
    """A rate, or None when there is nothing to divide.

    Reporting 0% for zero runs reads as "nothing is failing", and 100% reads as
    "everything is". Both are claims the data does not support.
    """
    if denominator <= 0:
        return None
    return round(numerator / denominator, 4)


@router.get("/metrics")
async def metrics(
    tenant: Tenant, db: DbSession, days: int = Query(7, ge=1, le=90)
) -> dict[str, Any]:
    """Health and cost for this project, over a window."""
    since = datetime.now(UTC) - timedelta(days=days)

    states = dict(
        (
            await db.execute(
                select(AgentRun.state, func.count(AgentRun.id))
                .where(
                    AgentRun.project_id == tenant.project_id,
                    AgentRun.created_at >= since,
                )
                .group_by(AgentRun.state)
            )
        ).all()
    )
    total = sum(states.values())
    failed = states.get(RunState.FAILED, 0)
    cancelled = states.get(RunState.CANCELLED, 0)
    expired = states.get(RunState.EXPIRED, 0)
    completed = states.get(RunState.COMPLETED, 0)
    in_flight = sum(
        count for state, count in states.items() if state not in TERMINAL_RUN_STATES
    )

    approvals = dict(
        (
            await db.execute(
                select(Approval.state, func.count(Approval.id))
                .where(
                    Approval.project_id == tenant.project_id,
                    Approval.created_at >= since,
                )
                .group_by(Approval.state)
            )
        ).all()
    )

    tools = (
        await db.execute(
            select(
                ToolExecution.tool_name,
                func.count(ToolExecution.id),
                func.avg(ToolExecution.duration_ms),
            )
            .where(
                ToolExecution.project_id == tenant.project_id,
                ToolExecution.created_at >= since,
            )
            .group_by(ToolExecution.tool_name)
            .order_by(func.count(ToolExecution.id).desc())
            .limit(15)
        )
    ).all()

    spend = (
        await db.execute(
            select(
                func.count(LLMUsage.id),
                func.sum(LLMUsage.input_tokens),
                func.sum(LLMUsage.output_tokens),
                func.sum(LLMUsage.estimated_cost_usd),
            ).where(
                LLMUsage.project_id == tenant.project_id,
                LLMUsage.created_at >= since,
            )
        )
    ).one()

    queued = (
        await db.execute(
            select(func.count(AgentRun.id)).where(
                AgentRun.project_id == tenant.project_id,
                AgentRun.state == RunState.QUEUED,
            )
        )
    ).scalar() or 0

    return {
        "window_days": days,
        "runs": {
            "total": total,
            "completed": completed,
            "failed": failed,
            "cancelled": cancelled,
            "expired": expired,
            "in_flight": in_flight,
            "queued": queued,
            # None rather than a number when there is nothing to divide.
            "failure_rate": _rate(failed, total),
            "completion_rate": _rate(completed, total),
            "by_state": {state.value: count for state, count in states.items()},
        },
        "approvals": {
            "total": sum(approvals.values()),
            "pending": approvals.get(ApprovalState.PENDING, 0),
            "approved": approvals.get(ApprovalState.APPROVED, 0),
            "rejected": approvals.get(ApprovalState.REJECTED, 0),
            "expired": approvals.get(ApprovalState.EXPIRED, 0),
            "rejection_rate": _rate(
                approvals.get(ApprovalState.REJECTED, 0), sum(approvals.values())
            ),
        },
        "tools": [
            {
                "name": name,
                "calls": count,
                "avg_duration_ms": round(float(avg or 0), 1),
            }
            for name, count, avg in tools
        ],
        "spend": {
            "model_calls": int(spend[0] or 0),
            "input_tokens": int(spend[1] or 0),
            "output_tokens": int(spend[2] or 0),
            "estimated_cost_usd": round(float(spend[3] or 0), 4),
            "note": (
                "Token counts are exact. Costs are estimates from published "
                "list prices — see /ai/pricing."
            ),
        },
        "note": (
            "A point-in-time read over the window, not a time series. For "
            "trends, ship the structured logs to a monitoring system."
        ),
    }


@router.get("/entitlements")
async def entitlements(tenant: Tenant, db: DbSession) -> dict[str, Any]:
    """The plan's run allowance and how much of the period is used."""
    subscription = await tenancy.subscription_for(db, tenant.company_id)
    await db.commit()

    period_start = subscription.current_period_start
    if period_start.tzinfo is None:
        period_start = period_start.replace(tzinfo=UTC)

    used = (
        await db.execute(
            select(func.count(AgentRun.id)).where(
                AgentRun.company_id == tenant.company_id,
                AgentRun.created_at >= period_start,
            )
        )
    ).scalar() or 0

    allowance = int(subscription.monthly_run_allowance or 0)
    remaining = max(0, allowance - used) if allowance else None
    return {
        "plan": subscription.plan.value,
        "period_start": period_start.isoformat(),
        "runs": {
            "allowance": allowance,
            "used": used,
            "remaining": remaining,
            "exhausted": bool(allowance and used >= allowance),
        },
        "limits": {
            "max_projects": subscription.max_projects,
            "max_users": subscription.max_users,
            "max_salesforce_connections": subscription.max_salesforce_connections,
        },
        "billing": {
            "provider_connected": False,
            "note": (
                "Entitlements are enforced. Payment collection is not "
                "implemented in this deployment; no billing provider is called."
            ),
        },
    }


@router.get("/retention")
async def retention_policy(tenant: Tenant, db: DbSession) -> dict[str, Any]:
    """What this project keeps and for how long, in sentences."""
    row = await tenancy.policy_row(db, tenant.project_id, tenant.company_id)
    await db.commit()
    return describe_retention(row)


@router.post("/retention/sweep")
async def run_sweep(
    tenant: Tenant, db: DbSession, dry_run: bool = Query(True)
) -> dict[str, Any]:
    """Apply retention to this project now.

    Defaults to a dry run. Deleting a customer's history is not something an
    endpoint should do because someone was curious what the button did.
    """
    tenant.require(ProjectRole.PROJECT_ADMIN, ProjectRole.SECURITY_ADMIN)
    row = await tenancy.policy_row(db, tenant.project_id, tenant.company_id)
    result = await sweep_project(db, tenant.project, row, dry_run=dry_run)

    if not dry_run:
        db.add(
            AuditEvent(
                company_id=tenant.company_id,
                project_id=tenant.project_id,
                user_id=tenant.user_id,
                action="retention.swept",
                arguments=result.to_dict(),
                outcome="ok",
            )
        )
    await db.commit()
    return {
        "dry_run": dry_run,
        **result.to_dict(),
        "message": (
            "Nothing was deleted — this was a dry run. Pass dry_run=false to "
            "apply it."
            if dry_run
            else "Retention applied."
        ),
    }


@router.get("/posture")
async def posture(tenant: Tenant, db: DbSession) -> dict[str, Any]:
    """What this deployment's controls actually are.

    The answer to a security questionnaire, generated from configuration rather
    than written down once and left to drift. Every entry is a fact about this
    running process.
    """
    from app.security.secrets import describe_backend

    row = await tenancy.policy_row(db, tenant.project_id, tenant.company_id)
    await db.commit()
    policy = tenant.policy

    return {
        "secrets": describe_backend(),
        "redaction": describe_redaction(),
        "rate_limiting": describe_rate_limits(),
        "retention": describe_retention(row),
        "execution": {
            "server_owned_runs": True,
            "worker_enabled": settings.run_worker_enabled,
            "max_execution_seconds": policy.max_execution_seconds,
            "max_agent_steps": policy.max_agent_steps,
            "max_tool_calls": policy.max_tool_calls,
            "heartbeat_timeout_seconds": settings.run_heartbeat_timeout_seconds,
            "worker_id": run_queue.worker_id(),
        },
        "approvals": {
            "separation_of_duties": policy.require_separate_approver,
            "separation_always_for_critical": True,
            "approval_ttl_seconds": policy.approval_ttl_seconds,
            "bound_to": [
                "project",
                "actor",
                "exact tool",
                "arguments hash",
                "org state fingerprint",
                "environment",
                "expiry",
            ],
        },
        "salesforce": {
            "allow_production_mutations": policy.allow_production_mutations,
            "allowed_environments": sorted(policy.allowed_environments),
            "permission_ceiling": (
                "The connected Salesforce user's own permissions. This platform "
                "can reduce what the agent may do; it can never elevate it."
            ),
        },
        "ai": {
            "allowed_providers": sorted(policy.allowed_llm_providers) or ["any"],
            "fallback_enabled": policy.allow_llm_fallback,
            "platform_managed_key_available": settings.ai_available_without_byok,
        },
        "features": settings.feature_flags,
        "not_implemented": [
            "SAML single sign-on",
            "AWS Bedrock and Google Vertex model providers",
            "HashiCorp Vault, Azure Key Vault and GCP KMS secret backends",
            "Payment collection",
        ],
        "compliance_note": (
            "This describes the controls this deployment runs. It is not a "
            "certification claim: no SOC 2, ISO 27001 or similar attestation is "
            "asserted anywhere in this product."
        ),
    }
