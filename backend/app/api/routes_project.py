"""Project settings: policy, environments, retention and MCP tool providers.

These endpoints are the only way to change what the agent is permitted to do in
a project. They are role-gated, audited, and deliberately refuse to make a
project more permissive than the deployment allows — every write here is
clamped by `snapshot_from()` before it has any effect.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.config import settings
from app.models import (
    AuditEvent,
    Environment,
    McpServerConfig,
    ProjectRole,
    RiskLevel,
)
from app.observability.logging import get_logger
from app.security.auth import DbSession, Tenant
from app.security.secrets import SecretContext, store_secret
from app.tenancy import service as tenancy
from app.tenancy.policy import ALL_CATEGORIES, DEFAULT_APPROVER_MATRIX
from app.tools.registry import build_registry

log = get_logger("api.project")

router = APIRouter(prefix="/project", tags=["project"])


class PolicyPatch(BaseModel):
    allow_production_mutations: bool | None = None
    approval_ttl_seconds: int | None = Field(None, ge=60, le=86_400)
    max_agent_steps: int | None = Field(None, ge=1, le=100)
    max_execution_seconds: int | None = Field(None, ge=30, le=86_400)
    max_tool_calls: int | None = Field(None, ge=1, le=1000)
    require_separate_approver: bool | None = None
    max_bulk_records: int | None = Field(None, ge=1)
    disabled_tools: list[str] | None = None
    approver_matrix: dict[str, Any] | None = None
    allowed_environments: list[Environment] | None = None
    allowed_llm_providers: list[str] | None = None
    allow_llm_fallback: bool | None = None
    retain_conversation_days: int | None = Field(None, ge=0, le=3650)
    retain_tool_payload_days: int | None = Field(None, ge=0, le=3650)
    retain_document_days: int | None = Field(None, ge=0, le=3650)


@router.get("/policy")
async def get_policy(tenant: Tenant, db: DbSession) -> dict[str, Any]:
    row = await tenancy.policy_row(db, tenant.project_id, tenant.company_id)
    await db.commit()
    resolved = tenant.policy
    return {
        "project": {
            "id": tenant.project_id,
            "name": tenant.project.name,
            "company_id": tenant.company_id,
            "company_name": tenant.company.name,
            "your_role": tenant.role.value,
            "your_company_role": tenant.company_role.value,
        },
        "stored": {
            "allow_production_mutations": row.allow_production_mutations,
            "approval_ttl_seconds": row.approval_ttl_seconds,
            "max_agent_steps": row.max_agent_steps,
            "max_execution_seconds": row.max_execution_seconds,
            "max_tool_calls": row.max_tool_calls,
            "require_separate_approver": row.require_separate_approver,
            "max_bulk_records": row.max_bulk_records,
            "disabled_tools": row.disabled_tools or [],
            "approver_matrix": row.approver_matrix,
            "allowed_environments": row.allowed_environments or [e.value for e in Environment],
            "allowed_llm_providers": row.allowed_llm_providers or [],
            "allow_llm_fallback": row.allow_llm_fallback,
            "retain_conversation_days": row.retain_conversation_days,
            "retain_tool_payload_days": row.retain_tool_payload_days,
            "retain_document_days": row.retain_document_days,
        },
        # What actually applies after the deployment ceilings are enforced.
        "effective": {
            "allow_production_mutations": resolved.allow_production_mutations,
            "approval_ttl_seconds": resolved.approval_ttl_seconds,
            "max_agent_steps": resolved.max_agent_steps,
            "max_execution_seconds": resolved.max_execution_seconds,
            "max_tool_calls": resolved.max_tool_calls,
            "require_separate_approver": resolved.require_separate_approver,
            "max_bulk_records": resolved.max_bulk_records,
            "disabled_tools": sorted(resolved.disabled_tools),
            "allowed_environments": sorted(resolved.allowed_environments),
            "allowed_llm_providers": sorted(resolved.allowed_llm_providers),
            "allow_llm_fallback": resolved.allow_llm_fallback,
        },
        # The company's own clearance — the level between the deployment and
        # the project. Without it in the response, an administrator ticking the
        # project box and still seeing "no" has no way to tell which of three
        # levels refused.
        "company": {
            "id": tenant.company_id,
            "name": tenant.company.name,
            "allow_production_mutations": bool(
                tenant.company.allow_production_mutations
            ),
            "you_can_change_it": tenant.is_company_admin,
        },
        "deployment_ceilings": {
            "allow_production_mutations": settings.allow_production_mutations,
            "approval_ttl_seconds": settings.approval_ttl_seconds,
            "max_agent_steps": settings.max_agent_steps,
            "max_execution_seconds": settings.max_execution_seconds,
            "max_tool_calls": settings.max_tool_calls,
            "max_bulk_records": settings.max_bulk_records,
            "feature_production_deployment": settings.feature_production_deployment,
            "note": (
                "A project policy may be stricter than these values but never looser."
            ),
        },
        # Which level says no, and what to do about it. Named explicitly so
        # neither the UI nor the model has to guess — and so nobody is sent to
        # an administrator who could not have helped.
        "production": {
            "permitted": resolved.allow_production_mutations,
            "blocked_by": resolved.production_blocked_by or None,
            "levels": [
                {
                    "level": "deployment",
                    "permitted": settings.allow_production_mutations,
                    "changed_by": "whoever operates this installation",
                    "how": "Set ALLOW_PRODUCTION_MUTATIONS=true and restart the backend.",
                },
                {
                    "level": "company",
                    "permitted": bool(tenant.company.allow_production_mutations),
                    "changed_by": "a company administrator",
                    "how": "Settings -> Production access, for every project in the company.",
                },
                {
                    "level": "project",
                    "permitted": bool(row.allow_production_mutations),
                    "changed_by": "a project administrator",
                    "how": "The checkbox on this page, for this project only.",
                },
            ],
            "note": (
                "All three must permit it. Salesforce's own permissions remain the "
                "ceiling above all of them: this platform can only ever reduce what "
                "the connected Salesforce user could already do."
            ),
        },
        "categories": list(ALL_CATEGORIES),
        "environments": [e.value for e in Environment],
        "default_approver_matrix": DEFAULT_APPROVER_MATRIX,
    }


@router.patch("/policy")
async def patch_policy(payload: PolicyPatch, tenant: Tenant, db: DbSession) -> dict[str, Any]:
    tenant.require(ProjectRole.PROJECT_ADMIN, ProjectRole.SECURITY_ADMIN)
    row = await tenancy.policy_row(db, tenant.project_id, tenant.company_id)
    known = set(build_registry().names())

    changes: dict[str, Any] = {}
    for field_name in (
        "allow_production_mutations",
        "approval_ttl_seconds",
        "max_agent_steps",
        "max_execution_seconds",
        "max_tool_calls",
        "require_separate_approver",
        "max_bulk_records",
        "approver_matrix",
        "allow_llm_fallback",
        "retain_conversation_days",
        "retain_tool_payload_days",
        "retain_document_days",
    ):
        value = getattr(payload, field_name)
        if value is not None:
            changes[field_name] = value
            setattr(row, field_name, value)

    if payload.disabled_tools is not None:
        unknown = [t for t in payload.disabled_tools if t not in known]
        if unknown:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown tool name(s): {', '.join(sorted(unknown))}",
            )
        row.disabled_tools = sorted(set(payload.disabled_tools))
        changes["disabled_tools"] = row.disabled_tools

    if payload.allowed_environments is not None:
        if not payload.allowed_environments:
            raise HTTPException(
                status_code=400,
                detail=(
                    "A project must permit at least one environment; otherwise the "
                    "agent can do nothing at all."
                ),
            )
        row.allowed_environments = [e.value for e in payload.allowed_environments]
        changes["allowed_environments"] = row.allowed_environments

    if payload.allowed_llm_providers is not None:
        row.allowed_llm_providers = sorted({p.upper() for p in payload.allowed_llm_providers})
        changes["allowed_llm_providers"] = row.allowed_llm_providers

    db.add(
        AuditEvent(
            company_id=tenant.company_id,
            project_id=tenant.project_id,
            user_id=tenant.user_id,
            action="project.policy_updated",
            arguments=changes,
            outcome="ok",
        )
    )
    await db.commit()
    return await get_policy(tenant, db)


# ---------------------------------------------------------------- MCP servers
class McpServerIn(BaseModel):
    name: str = Field(min_length=1, max_length=80, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    description: str = ""
    transport: str = Field("stdio", pattern="^(stdio|http)$")
    command: str | None = None
    args: list[str] | None = None
    url: str | None = None
    env: dict[str, str] | None = None
    secrets: dict[str, str] | None = None
    enabled: bool = True
    tool_allowlist: list[str] | None = None
    risk_overrides: dict[str, str] | None = None


def _server_out(s: McpServerConfig) -> dict[str, Any]:
    return {
        "id": s.id,
        "name": s.name,
        "description": s.description,
        "transport": s.transport,
        "command": s.command,
        "args": s.args or [],
        "url": s.url,
        "enabled": s.enabled,
        # Whether secrets exist, never what they are.
        "has_secrets": bool(s.secret_ref),
        "tool_allowlist": s.tool_allowlist or [],
        "risk_overrides": s.risk_overrides or {},
        "discovered_tools": s.discovered_tools or [],
        "last_discovery_at": s.last_discovery_at.isoformat() if s.last_discovery_at else None,
        "last_error": s.last_error,
    }


@router.get("/mcp-servers")
async def list_mcp_servers(tenant: Tenant, db: DbSession) -> dict[str, Any]:
    rows = (
        await db.execute(
            select(McpServerConfig).where(
                McpServerConfig.project_id == tenant.project_id
            )
        )
    ).scalars().all()
    return {
        "mcp_enabled": settings.mcp_enabled and settings.feature_mcp,
        "risk_floor": settings.mcp_default_risk,
        "count": len(rows),
        "servers": [_server_out(s) for s in rows],
    }


@router.post("/mcp-servers")
async def create_mcp_server(
    payload: McpServerIn, tenant: Tenant, db: DbSession
) -> dict[str, Any]:
    # Registering an MCP server adds executable capability to the project.
    tenant.require(ProjectRole.PROJECT_ADMIN)
    if payload.transport == "stdio" and not payload.command:
        raise HTTPException(status_code=400, detail="A stdio MCP server needs a command.")
    if payload.transport == "http" and not payload.url:
        raise HTTPException(status_code=400, detail="An http MCP server needs a url.")

    existing = (
        await db.execute(
            select(McpServerConfig).where(
                McpServerConfig.project_id == tenant.project_id,
                McpServerConfig.name == payload.name,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=409, detail=f"'{payload.name}' already exists.")

    server = McpServerConfig(
        company_id=tenant.company_id,
        project_id=tenant.project_id,
        name=payload.name,
        description=payload.description,
        transport=payload.transport,
        command=payload.command,
        args=payload.args,
        url=payload.url,
        env=payload.env,
        secret_ref=(
            store_secret(
                _join_secrets(payload.secrets),
                SecretContext(
                    company_id=tenant.company_id,
                    project_id=tenant.project_id,
                    purpose="mcp_secrets",
                ),
            )
            if payload.secrets
            else None
        ),
        enabled=payload.enabled,
        tool_allowlist=payload.tool_allowlist,
        risk_overrides=payload.risk_overrides,
    )
    db.add(server)
    db.add(
        AuditEvent(
            company_id=tenant.company_id,
            project_id=tenant.project_id,
            user_id=tenant.user_id,
            action="project.mcp_server_added",
            arguments={"name": payload.name, "transport": payload.transport},
            outcome="ok",
        )
    )
    await db.commit()
    return _server_out(server)


@router.delete("/mcp-servers/{server_id}")
async def delete_mcp_server(server_id: str, tenant: Tenant, db: DbSession) -> dict[str, bool]:
    tenant.require(ProjectRole.PROJECT_ADMIN)
    server = await tenancy.owned(db, McpServerConfig, server_id, tenant.project_id)
    if server is None:
        raise HTTPException(status_code=404, detail="MCP server not found")
    db.add(
        AuditEvent(
            company_id=tenant.company_id,
            project_id=tenant.project_id,
            user_id=tenant.user_id,
            action="project.mcp_server_removed",
            arguments={"name": server.name},
            outcome="ok",
        )
    )
    await db.delete(server)
    await db.commit()
    return {"success": True}


@router.post("/mcp-servers/{server_id}/discover")
async def discover_mcp_server(
    server_id: str, tenant: Tenant, db: DbSession
) -> dict[str, Any]:
    """Connect to the server and re-read its tool list.

    This is a real connection attempt. If the server is unreachable the error
    is reported as an error — never as an empty-but-successful discovery.
    """
    from app.mcp.manager import discover_server

    tenant.require(ProjectRole.PROJECT_ADMIN, ProjectRole.DEVELOPER)
    server = await tenancy.owned(db, McpServerConfig, server_id, tenant.project_id)
    if server is None:
        raise HTTPException(status_code=404, detail="MCP server not found")
    result = await discover_server(db, server)
    await db.commit()
    if not result.get("success"):
        raise HTTPException(status_code=502, detail=result)
    return result


# ---------------------------------------------------------------- entitlements
@router.get("/subscription")
async def get_subscription(tenant: Tenant, db: DbSession) -> dict[str, Any]:
    """The company's plan and what it entitles.

    Entitlements here are enforced. Payment collection is not implemented in
    this codebase and no billing provider is contacted — see docs/operations.md.
    """
    subscription = await tenancy.subscription_for(db, tenant.company_id)
    await db.commit()
    projects = await tenancy.projects_in_company(db, tenant.company_id)
    return {
        "plan": subscription.plan.value,
        "status": subscription.status,
        "limits": {
            "max_projects": subscription.max_projects,
            "max_users": subscription.max_users,
            "max_salesforce_connections": subscription.max_salesforce_connections,
            "monthly_run_allowance": subscription.monthly_run_allowance,
        },
        "usage": {"projects": len(projects)},
        "features": subscription.features or {},
        "billing": {
            "provider_connected": False,
            "note": (
                "Entitlements are enforced. Payment collection is not implemented "
                "in this deployment."
            ),
        },
    }


def _join_secrets(secrets: dict[str, str] | None) -> str:
    import json

    return json.dumps(secrets or {}, separators=(",", ":"))


# ---------------------------------------------------------------------------
# Company-level production clearance
#
# The level between the deployment ceiling and a project's own setting.
#
# It is a company decision rather than a project one because the person
# accountable for a customer's production Salesforce org is the one who owns the
# customer account, not whoever happens to administer one team's workspace. And
# it is not an environment variable because in a multi-tenant deployment a
# single global switch is the wrong shape: one customer may be cleared for
# production while another is still in trial and must never reach it.
# ---------------------------------------------------------------------------
class ProductionAccessIn(BaseModel):
    allow_production_mutations: bool
    #: Typed confirmation when enabling. Not theatre: this is the one setting
    #: whose blast radius is a customer's live Salesforce org, and a checkbox
    #: toggled by accident looks identical to one toggled on purpose.
    confirm: str = ""


@router.get("/production-access")
async def get_production_access(tenant: Tenant, db: DbSession) -> dict[str, Any]:
    """This company's production clearance, and every level that gates it."""
    return {
        "company_id": tenant.company_id,
        "company_name": tenant.company.name,
        "allow_production_mutations": bool(tenant.company.allow_production_mutations),
        "you_can_change_it": tenant.is_company_admin,
        "deployment_permits_it": settings.allow_production_mutations,
        "confirmation_phrase": tenant.company.name,
        "note": (
            "Enabling this does not bypass anything else. Production changes still "
            "require the project's own setting, an approval bound to the exact "
            "change, and — for irreversible operations — a second person. "
            "Salesforce's own permissions remain the ceiling: this platform can "
            "only ever reduce what the connected Salesforce user could already do."
        ),
    }


@router.put("/production-access")
async def set_production_access(
    payload: ProductionAccessIn, tenant: Tenant, db: DbSession
) -> dict[str, Any]:
    """Clear this company for production changes, or withdraw that clearance."""
    tenant.require_company_admin()

    if payload.allow_production_mutations:
        if not settings.allow_production_mutations:
            raise HTTPException(
                status_code=400,
                detail=(
                    "This deployment has production changes switched off entirely "
                    "(ALLOW_PRODUCTION_MUTATIONS is false). Enabling it here would "
                    "record a permission that nothing honours. Whoever operates this "
                    "installation has to change it first."
                ),
            )
        if payload.confirm.strip() != tenant.company.name:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Type the company name exactly — '{tenant.company.name}' — to "
                    "confirm. This clears the agent to change your live Salesforce "
                    "orgs."
                ),
            )

    was = bool(tenant.company.allow_production_mutations)
    tenant.company.allow_production_mutations = payload.allow_production_mutations

    db.add(
        AuditEvent(
            company_id=tenant.company_id,
            user_id=tenant.user_id,
            action="company.production_access_changed",
            arguments={
                "from": was,
                "to": payload.allow_production_mutations,
                "company_name": tenant.company.name,
            },
            risk_level=RiskLevel.HIGH,
            outcome="ok",
        )
    )
    await db.commit()

    log.warning(
        "company.production_access_changed",
        company_id=tenant.company_id,
        enabled=payload.allow_production_mutations,
        actor=tenant.user_id,
    )
    return {
        "success": True,
        "allow_production_mutations": payload.allow_production_mutations,
        "message": (
            "This company is cleared for production changes. Each project must "
            "still enable it, and every change still needs approval."
            if payload.allow_production_mutations
            else "Production clearance withdrawn. Every project is blocked from "
            "production changes immediately, whatever their own setting says."
        ),
    }
