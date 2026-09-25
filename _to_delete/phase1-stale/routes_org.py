"""Organization settings: agent policy and MCP tool providers.

These endpoints are the only way to change what the agent is permitted to do
in a tenant. They are role-gated, audited, and deliberately refuse to make the
tenant more permissive than the deployment allows.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.config import settings
from app.models import AuditLog, McpServerConfig, Role
from app.security.auth import DbSession, Tenant
from app.security.crypto import encrypt
from app.tenancy import service as tenancy
from app.tenancy.policy import ALL_CATEGORIES, DEFAULT_APPROVER_MATRIX
from app.tools.registry import build_registry

router = APIRouter(prefix="/organization", tags=["organization"])


class PolicyPatch(BaseModel):
    allow_production_mutations: bool | None = None
    approval_ttl_seconds: int | None = Field(None, ge=60, le=86_400)
    max_agent_steps: int | None = Field(None, ge=1, le=100)
    require_separate_approver: bool | None = None
    max_bulk_records: int | None = Field(None, ge=1)
    disabled_tools: list[str] | None = None
    approver_matrix: dict[str, Any] | None = None


@router.get("/policy")
async def get_policy(tenant: Tenant, db: DbSession) -> dict[str, Any]:
    row = await tenancy.policy_row(db, tenant.organization_id)
    await db.commit()
    resolved = tenant.policy
    return {
        "organization": {
            "id": tenant.organization_id,
            "name": tenant.organization.name,
            "your_role": tenant.role.value,
        },
        "stored": {
            "allow_production_mutations": row.allow_production_mutations,
            "approval_ttl_seconds": row.approval_ttl_seconds,
            "max_agent_steps": row.max_agent_steps,
            "require_separate_approver": row.require_separate_approver,
            "max_bulk_records": row.max_bulk_records,
            "disabled_tools": row.disabled_tools or [],
            "approver_matrix": row.approver_matrix,
        },
        # What actually applies after the deployment ceilings are enforced.
        "effective": {
            "allow_production_mutations": resolved.allow_production_mutations,
            "approval_ttl_seconds": resolved.approval_ttl_seconds,
            "max_agent_steps": resolved.max_agent_steps,
            "require_separate_approver": resolved.require_separate_approver,
            "max_bulk_records": resolved.max_bulk_records,
            "disabled_tools": sorted(resolved.disabled_tools),
        },
        "deployment_ceilings": {
            "allow_production_mutations": settings.allow_production_mutations,
            "approval_ttl_seconds": settings.approval_ttl_seconds,
            "max_agent_steps": settings.max_agent_steps,
            "max_bulk_records": settings.max_bulk_records,
            "note": (
                "A tenant policy may be stricter than these values but never looser. "
                "Production metadata changes additionally require "
                "ALLOW_PRODUCTION_MUTATIONS=true for the deployment."
            ),
        },
        "categories": list(ALL_CATEGORIES),
        "default_approver_matrix": DEFAULT_APPROVER_MATRIX,
    }


@router.patch("/policy")
async def patch_policy(payload: PolicyPatch, tenant: Tenant, db: DbSession) -> dict[str, Any]:
    tenant.require(Role.OWNER, Role.ADMIN)
    row = await tenancy.policy_row(db, tenant.organization_id)
    known = set(build_registry().names())

    changes: dict[str, Any] = {}
    for field_name in (
        "allow_production_mutations",
        "approval_ttl_seconds",
        "max_agent_steps",
        "require_separate_approver",
        "max_bulk_records",
        "approver_matrix",
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

    db.add(
        AuditLog(
            organization_id=tenant.organization_id,
            user_id=tenant.user_id,
            action="organization.policy_updated",
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
        "has_secrets": bool(s.secrets_enc),
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
                McpServerConfig.organization_id == tenant.organization_id
            )
        )
    ).scalars().all()
    return {
        "mcp_enabled": settings.mcp_enabled,
        "risk_floor": settings.mcp_default_risk,
        "count": len(rows),
        "servers": [_server_out(s) for s in rows],
    }


@router.post("/mcp-servers")
async def create_mcp_server(
    payload: McpServerIn, tenant: Tenant, db: DbSession
) -> dict[str, Any]:
    # Registering an MCP server adds executable capability to the tenant.
    tenant.require(Role.OWNER, Role.ADMIN)
    if payload.transport == "stdio" and not payload.command:
        raise HTTPException(status_code=400, detail="A stdio MCP server needs a command.")
    if payload.transport == "http" and not payload.url:
        raise HTTPException(status_code=400, detail="An http MCP server needs a url.")

    existing = (
        await db.execute(
            select(McpServerConfig).where(
                McpServerConfig.organization_id == tenant.organization_id,
                McpServerConfig.name == payload.name,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=409, detail=f"'{payload.name}' already exists.")

    server = McpServerConfig(
        organization_id=tenant.organization_id,
        name=payload.name,
        description=payload.description,
        transport=payload.transport,
        command=payload.command,
        args=payload.args,
        url=payload.url,
        env=payload.env,
        secrets_enc=encrypt(_join_secrets(payload.secrets)) if payload.secrets else None,
        enabled=payload.enabled,
        tool_allowlist=payload.tool_allowlist,
        risk_overrides=payload.risk_overrides,
    )
    db.add(server)
    db.add(
        AuditLog(
            organization_id=tenant.organization_id,
            user_id=tenant.user_id,
            action="organization.mcp_server_added",
            arguments={"name": payload.name, "transport": payload.transport},
            outcome="ok",
        )
    )
    await db.commit()
    return _server_out(server)


@router.delete("/mcp-servers/{server_id}")
async def delete_mcp_server(server_id: str, tenant: Tenant, db: DbSession) -> dict[str, bool]:
    tenant.require(Role.OWNER, Role.ADMIN)
    server = await tenancy.owned(db, McpServerConfig, server_id, tenant.organization_id)
    if server is None:
        raise HTTPException(status_code=404, detail="MCP server not found")
    db.add(
        AuditLog(
            organization_id=tenant.organization_id,
            user_id=tenant.user_id,
            action="organization.mcp_server_removed",
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

    tenant.require(Role.OWNER, Role.ADMIN, Role.DEVELOPER)
    server = await tenancy.owned(db, McpServerConfig, server_id, tenant.organization_id)
    if server is None:
        raise HTTPException(status_code=404, detail="MCP server not found")
    result = await discover_server(db, server)
    await db.commit()
    if not result.get("success"):
        raise HTTPException(status_code=502, detail=result)
    return result


def _join_secrets(secrets: dict[str, str] | None) -> str:
    import json

    return json.dumps(secrets or {}, separators=(",", ":"))
