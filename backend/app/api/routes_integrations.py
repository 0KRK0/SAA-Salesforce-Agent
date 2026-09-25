"""Connecting Jira, GitHub and Bitbucket to a project.

Tokens are never returned by any endpoint here — only whether one exists, when
it expires, and what went wrong last. As with Salesforce, which project a
callback belongs to comes from a server-side state row, never from the redirect
URL, because a URL is something the user can edit.

Registering a repository is a separate, deliberate step from connecting an
account. A GitHub token usually reaches many repositories a project never
authorized, and "the token can see it" is not "this project approved it".
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from app.config import settings
from app.integrations import oauth as integration_oauth
from app.integrations import service as integrations
from app.integrations.base import IntegrationError
from app.integrations.github import DEFAULT_BRANCH_PATTERNS
from app.models import AuditEvent, IntegrationKind, ProjectRole, Repository
from app.observability.logging import get_logger
from app.security.auth import DbSession, Tenant
from app.tenancy import service as tenancy

router = APIRouter(prefix="/integrations", tags=["integrations"])
log = get_logger("api.integrations")


@router.get("")
async def list_integrations(tenant: Tenant, db: DbSession) -> dict[str, Any]:
    """What is connected, and what this deployment can connect to."""
    connections = await integrations.list_connections(db, tenant.project_id)
    repositories = await integrations.list_repositories(db, tenant.project_id)
    return {
        "connections": [integrations.describe(c) for c in connections],
        "repositories": [
            integrations.describe_repository(r) for r in repositories
        ],
        "available": integrations.catalog(),
        "default_branch_patterns": list(DEFAULT_BRANCH_PATTERNS),
    }


@router.post("/{kind}/connect")
async def connect(kind: IntegrationKind, tenant: Tenant, db: DbSession) -> dict[str, str]:
    """Begin an OAuth connection. Returns the URL to send the user to."""
    # Connecting an account gives the whole project reach into it.
    tenant.require(
        ProjectRole.PROJECT_ADMIN, ProjectRole.RELEASE_MANAGER, ProjectRole.DEVELOPER
    )
    try:
        url = await integration_oauth.build_authorize_url(
            db,
            kind,
            company_id=tenant.company_id,
            project_id=tenant.project_id,
            user_id=tenant.user_id,
        )
    except IntegrationError as exc:
        raise HTTPException(status_code=400, detail=exc.to_dict()) from exc
    await db.commit()
    return {"authorize_url": url}


@router.get("/{kind}/callback")
async def callback(
    kind: IntegrationKind,
    db: DbSession,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> RedirectResponse:
    front = settings.frontend_base_url.rstrip("/")
    if error:
        log.warning("integration.oauth_denied", provider=kind.value, error=error)
        return RedirectResponse(f"{front}/integrations?error={error}")
    if not code or not state:
        return RedirectResponse(f"{front}/integrations?error=missing_code")

    try:
        state_row = await integration_oauth.consume_state(db, state, kind)
        tokens = await integration_oauth.exchange_code(kind, code)
        connection = await integrations.upsert_connection(
            db,
            company_id=state_row.company_id,
            project_id=state_row.project_id,
            kind=kind,
            tokens=tokens,
            connected_by=state_row.user_id,
        )
        db.add(
            AuditEvent(
                company_id=state_row.company_id,
                project_id=state_row.project_id,
                user_id=state_row.user_id,
                action=f"integration.{kind.value.lower()}_connected",
                # Says a token exists, never what it is.
                arguments=tokens.redacted(),
                outcome="ok",
            )
        )
        await db.commit()
    except IntegrationError as exc:
        await db.rollback()
        log.error(
            "integration.oauth_failed", provider=kind.value, error=exc.error_type
        )
        return RedirectResponse(f"{front}/integrations?error={exc.error_type}")

    return RedirectResponse(f"{front}/integrations?connected={connection.id}")


@router.post("/{kind}/test")
async def test_connection(
    kind: IntegrationKind, tenant: Tenant, db: DbSession
) -> dict[str, Any]:
    """Make a real call and report what happened.

    Live on purpose, as with model providers. A test that only checked for a
    stored token would report "Connected" for one that has been revoked.
    """
    connection = await integrations.connection_for(db, tenant.project_id, kind)
    if connection is None:
        raise HTTPException(
            status_code=404, detail=f"{kind.value.title()} is not connected."
        )
    try:
        client = await integrations.build_client(db, tenant.project_id, kind)
        async with client as api:
            result = await api.whoami()
        connection.last_error = None
        from datetime import UTC, datetime

        connection.last_validated_at = datetime.now(UTC)
        await db.commit()
        return result
    except IntegrationError as exc:
        connection.last_error = exc.message
        await db.commit()
        return exc.to_dict()


@router.delete("/{kind}")
async def disconnect(
    kind: IntegrationKind, tenant: Tenant, db: DbSession
) -> dict[str, bool]:
    tenant.require(ProjectRole.PROJECT_ADMIN)
    connection = await integrations.connection_for(db, tenant.project_id, kind)
    if connection is None:
        raise HTTPException(status_code=404, detail="Not connected.")
    db.add(
        AuditEvent(
            company_id=tenant.company_id,
            project_id=tenant.project_id,
            user_id=tenant.user_id,
            action=f"integration.{kind.value.lower()}_disconnected",
            arguments={"account": connection.account},
            outcome="ok",
        )
    )
    await db.delete(connection)
    await db.commit()
    return {"success": True}


# ---------------------------------------------------------------------------
# Repositories
# ---------------------------------------------------------------------------
@router.get("/{kind}/available-repositories")
async def available_repositories(
    kind: IntegrationKind, tenant: Tenant, db: DbSession
) -> dict[str, Any]:
    """What the connected account can see, for choosing what to register.

    Listing is not authorizing: nothing here is reachable by the agent until it
    is registered against the project.
    """
    if kind not in (IntegrationKind.GITHUB, IntegrationKind.BITBUCKET):
        raise HTTPException(
            status_code=400, detail=f"{kind.value.title()} has no repositories."
        )
    try:
        client = await integrations.build_client(db, tenant.project_id, kind)
        async with client as api:
            repos = await api.list_repositories()
    except IntegrationError as exc:
        raise HTTPException(status_code=400, detail=exc.to_dict()) from exc

    registered = {
        r.full_name for r in await integrations.list_repositories(db, tenant.project_id)
    }
    return {
        "count": len(repos),
        "repositories": [
            {**r, "registered": r["full_name"] in registered} for r in repos
        ],
        "note": (
            "Listing is not authorizing. The agent can only reach repositories "
            "registered to this project."
        ),
    }


class RepositoryIn(BaseModel):
    provider: IntegrationKind
    full_name: str = Field(min_length=1, max_length=300)
    default_branch: str = "main"
    #: Branch names the agent may create or push to. Empty uses the built-in
    #: patterns, which never include a default branch.
    allowed_branch_patterns: list[str] | None = None
    #: Path prefixes the agent may write. Empty means the whole repository.
    allowed_paths: list[str] | None = None
    require_pull_request: bool = True


@router.post("/repositories", status_code=201)
async def register_repository(
    payload: RepositoryIn, tenant: Tenant, db: DbSession
) -> dict[str, Any]:
    """Authorize one repository for this project, with its rules."""
    tenant.require(ProjectRole.PROJECT_ADMIN, ProjectRole.RELEASE_MANAGER)

    connection = await integrations.connection_for(
        db, tenant.project_id, payload.provider
    )
    if connection is None:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Connect {payload.provider.value.title()} before registering a "
                "repository."
            ),
        )

    # Confirm the repository exists and the connected account can reach it,
    # rather than storing a name that will fail on first use.
    try:
        client = await integrations.build_client(db, tenant.project_id, payload.provider)
        async with client as api:
            remote = await api.get_repository(payload.full_name)
    except IntegrationError as exc:
        raise HTTPException(status_code=400, detail=exc.to_dict()) from exc

    existing = await integrations.repository_for(
        db, tenant.project_id, payload.full_name
    )
    repo = existing or Repository(
        company_id=tenant.company_id,
        project_id=tenant.project_id,
        integration_id=connection.id,
        provider=payload.provider,
        full_name=payload.full_name,
    )
    repo.integration_id = connection.id
    # The remote's own default branch wins: a stale value here is how a "safe"
    # branch pattern quietly stops protecting the branch it was meant to.
    repo.default_branch = remote.get("default_branch") or payload.default_branch
    repo.allowed_branch_patterns = payload.allowed_branch_patterns or list(
        DEFAULT_BRANCH_PATTERNS
    )
    repo.allowed_paths = payload.allowed_paths
    repo.require_pull_request = payload.require_pull_request
    repo.is_active = True
    if existing is None:
        db.add(repo)

    db.add(
        AuditEvent(
            company_id=tenant.company_id,
            project_id=tenant.project_id,
            user_id=tenant.user_id,
            action="integration.repository_registered",
            arguments={
                "provider": payload.provider.value,
                "full_name": payload.full_name,
                "branch_patterns": repo.allowed_branch_patterns,
                "allowed_paths": repo.allowed_paths,
                "require_pull_request": repo.require_pull_request,
            },
            outcome="ok",
        )
    )
    await db.commit()
    return integrations.describe_repository(repo)


@router.delete("/repositories/{repository_id}")
async def unregister_repository(
    repository_id: str, tenant: Tenant, db: DbSession
) -> dict[str, bool]:
    tenant.require(ProjectRole.PROJECT_ADMIN, ProjectRole.RELEASE_MANAGER)
    repo = await tenancy.owned(db, Repository, repository_id, tenant.project_id)
    if repo is None:
        raise HTTPException(status_code=404, detail="Repository not found")
    db.add(
        AuditEvent(
            company_id=tenant.company_id,
            project_id=tenant.project_id,
            user_id=tenant.user_id,
            action="integration.repository_unregistered",
            arguments={"full_name": repo.full_name},
            outcome="ok",
        )
    )
    await db.delete(repo)
    await db.commit()
    return {"success": True}
