"""Audit, tool-execution and deployment history."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select

from app.models import (
    AuditEvent,
    ChangeSet,
    DataJob,
    Deployment,
    OrgKnowledge,
    ToolExecution,
)
from app.security.auth import DbSession, Tenant
from app.tenancy import service as tenancy

router = APIRouter(tags=["audit"])


@router.get("/audit")
async def list_audit(
    tenant: Tenant,
    db: DbSession,
    conversation_id: str | None = Query(None),
    agent_run_id: str | None = Query(None),
    action: str | None = Query(None),
    limit: int = Query(100, le=500),
) -> dict[str, Any]:
    stmt = select(AuditEvent).where(AuditEvent.project_id == tenant.project_id)
    if conversation_id:
        stmt = stmt.where(AuditEvent.conversation_id == conversation_id)
    if agent_run_id:
        stmt = stmt.where(AuditEvent.agent_run_id == agent_run_id)
    if action:
        stmt = stmt.where(AuditEvent.action == action)
    rows = (
        (await db.execute(stmt.order_by(AuditEvent.created_at.desc()).limit(limit)))
        .scalars()
        .all()
    )
    return {
        "count": len(rows),
        "entries": [
            {
                "id": r.id,
                "action": r.action,
                "tool_name": r.tool_name,
                "arguments": r.arguments,
                "result_summary": r.result_summary,
                "salesforce_object": r.salesforce_object,
                "record_ids": r.record_ids,
                "before_values": r.before_values,
                "after_values": r.after_values,
                "risk_level": r.risk_level.value if r.risk_level else None,
                "approval_state": r.approval_state.value if r.approval_state else None,
                "execution_state": r.execution_state.value if r.execution_state else None,
                "deployment_id": r.deployment_id,
                "outcome": r.outcome,
                "error": r.error,
                "conversation_id": r.conversation_id,
                "agent_run_id": r.agent_run_id,
                "sf_org_id": r.sf_org_id,
                "environment": r.environment.value if r.environment else None,
                "actor_type": r.actor_type,
                "correlation_id": r.correlation_id,
                "created_at": r.created_at.isoformat(),
            }
            for r in rows
        ],
    }


@router.get("/tool-executions")
async def list_tool_executions(
    tenant: Tenant,
    db: DbSession,
    agent_run_id: str | None = Query(None),
    limit: int = Query(100, le=500),
) -> dict[str, Any]:
    stmt = select(ToolExecution).where(
        ToolExecution.project_id == tenant.project_id
    )
    if agent_run_id:
        stmt = stmt.where(ToolExecution.agent_run_id == agent_run_id)
    rows = (
        (await db.execute(stmt.order_by(ToolExecution.created_at.desc()).limit(limit)))
        .scalars()
        .all()
    )
    return {
        "count": len(rows),
        "executions": [
            {
                "id": r.id,
                "agent_run_id": r.agent_run_id,
                "tool_name": r.tool_name,
                "arguments": r.arguments,
                "result": r.result,
                "risk_level": r.risk_level.value,
                "approval_state": r.approval_state.value,
                "execution_state": r.execution_state.value,
                "salesforce_object": r.salesforce_object,
                "record_ids": r.record_ids,
                "error_type": r.error_type,
                "error_message": r.error_message,
                "duration_ms": r.duration_ms,
                "created_at": r.created_at.isoformat(),
            }
            for r in rows
        ],
    }


@router.get("/deployments")
async def list_deployments(
    tenant: Tenant, db: DbSession, limit: int = Query(50, le=200)
) -> dict[str, Any]:
    rows = (
        (
            await db.execute(
                select(Deployment)
                .where(Deployment.project_id == tenant.project_id)
                .order_by(Deployment.created_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return {"count": len(rows), "deployments": [_deployment(r) for r in rows]}


@router.get("/deployments/{deployment_id}")
async def get_deployment(
    deployment_id: str, tenant: Tenant, db: DbSession
) -> dict[str, Any]:
    row = await tenancy.owned(db, Deployment, deployment_id, tenant.project_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Deployment not found")
    return _deployment(row)


def _deployment(r: Deployment) -> dict[str, Any]:
    return {
        "id": r.id,
        "agent_run_id": r.agent_run_id,
        "change_set_id": r.change_set_id,
        "salesforce_deploy_id": r.salesforce_deploy_id,
        "check_only": r.check_only,
        "environment": r.environment.value,
        "status": r.status,
        "components_total": r.components_total,
        "components_failed": r.components_failed,
        "tests_total": r.tests_total,
        "tests_failed": r.tests_failed,
        "package_manifest": r.package_manifest,
        "errors": r.errors,
        "verified": r.verified,
        "created_at": r.created_at.isoformat(),
    }


@router.get("/change-sets")
async def list_change_sets(
    tenant: Tenant, db: DbSession, limit: int = Query(50, le=200)
) -> dict[str, Any]:
    rows = (
        (
            await db.execute(
                select(ChangeSet)
                .where(ChangeSet.project_id == tenant.project_id)
                .order_by(ChangeSet.created_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return {
        "count": len(rows),
        "change_sets": [
            {
                "id": r.id,
                "name": r.name,
                "description": r.description,
                "state": r.state.value,
                "environment": r.environment.value,
                "test_level": r.test_level,
                "components": sum(len(v) for v in (r.package_manifest or {}).values()),
                "verified": r.verified,
                "rollback_available": bool((r.rollback_plan or {}).get("possible")),
                "created_at": r.created_at.isoformat(),
                "updated_at": r.updated_at.isoformat(),
            }
            for r in rows
        ],
    }


@router.get("/change-sets/{change_set_id}")
async def get_change_set(
    change_set_id: str, tenant: Tenant, db: DbSession
) -> dict[str, Any]:
    row = await tenancy.owned(db, ChangeSet, change_set_id, tenant.project_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Change set not found")
    rollback = row.rollback_plan or {}
    return {
        "id": row.id,
        "name": row.name,
        "description": row.description,
        "state": row.state.value,
        "manifest": row.package_manifest,
        "test_level": row.test_level,
        "run_tests": row.run_tests,
        # The diff is what a reviewer reads; the raw source is not returned so
        # the endpoint stays a review surface rather than a source dump.
        "diff": [d for d in (row.diff or []) if d.get("status") != "unchanged"],
        "validation_deploy_id": row.validation_deploy_id,
        "validation_result": row.validation_result,
        "deploy_id": row.deploy_id,
        "deploy_result": row.deploy_result,
        "verified": row.verified,
        "verification": row.verification,
        "rollback": {
            "possible": rollback.get("possible", False),
            "summary": rollback.get("summary", ""),
            "caveats": rollback.get("caveats", []),
            "components_created": rollback.get("components_created", []),
        },
        "created_at": row.created_at.isoformat(),
    }


@router.get("/data-jobs")
async def list_data_jobs(
    tenant: Tenant, db: DbSession, limit: int = Query(50, le=200)
) -> dict[str, Any]:
    rows = (
        (
            await db.execute(
                select(DataJob)
                .where(DataJob.project_id == tenant.project_id)
                .order_by(DataJob.created_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return {
        "count": len(rows),
        "jobs": [
            {
                "id": r.id,
                "operation": r.operation,
                "object": r.sobject,
                "salesforce_job_id": r.sf_job_id,
                "state": r.state,
                "records_total": r.records_total,
                "records_processed": r.records_processed,
                "records_failed": r.records_failed,
                "plan": r.plan,
                "error": r.error,
                "created_at": r.created_at.isoformat(),
            }
            for r in rows
        ],
    }


@router.get("/knowledge")
async def list_knowledge(
    tenant: Tenant,
    db: DbSession,
    salesforce_connection_id: str | None = Query(None),
    limit: int = Query(100, le=500),
) -> dict[str, Any]:
    """What the agent has learned about this project's Salesforce orgs."""
    stmt = select(OrgKnowledge).where(
        OrgKnowledge.project_id == tenant.project_id
    )
    if salesforce_connection_id:
        stmt = stmt.where(OrgKnowledge.salesforce_connection_id == salesforce_connection_id)
    rows = (
        (await db.execute(stmt.order_by(OrgKnowledge.observed_at.desc()).limit(limit)))
        .scalars()
        .all()
    )
    return {
        "count": len(rows),
        "entries": [
            {
                "id": r.id,
                "salesforce_connection_id": r.salesforce_connection_id,
                "kind": r.kind.value,
                "key": r.key,
                "summary": r.summary,
                "source": r.source,
                "hit_count": r.hit_count,
                "observed_at": r.observed_at.isoformat(),
                "expires_at": r.expires_at.isoformat() if r.expires_at else None,
            }
            for r in rows
        ],
    }
