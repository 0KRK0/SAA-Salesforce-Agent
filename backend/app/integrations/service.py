"""Resolving a project's integration connection into a live, usable client.

This is the one place tokens become plaintext, and it is deliberately small.
Everything above it works with a client; everything below works with a
reference. A refresh happens here too, because a token that expires mid-run
should be renewed silently rather than surfaced to a user as a failure.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.integrations import oauth as integration_oauth
from app.integrations.base import (
    IntegrationClient,
    IntegrationConfig,
    IntegrationError,
    IntegrationNotConnected,
    OAuthTokens,
)
from app.integrations.bitbucket import BitbucketClient
from app.integrations.github import GitHubClient
from app.integrations.jira import JiraClient
from app.models import IntegrationConnection, IntegrationKind, Repository
from app.observability.logging import get_logger
from app.security.secrets import SecretContext, SecretError, resolve_secret, store_secret

log = get_logger("integrations.service")

CLIENTS: dict[IntegrationKind, type[IntegrationClient]] = {
    IntegrationKind.JIRA: JiraClient,
    IntegrationKind.GITHUB: GitHubClient,
    IntegrationKind.BITBUCKET: BitbucketClient,
}


def _secret_context(connection: IntegrationConnection, purpose: str) -> SecretContext:
    return SecretContext(
        company_id=connection.company_id,
        project_id=connection.project_id,
        purpose=f"integration_{purpose}",
    )


async def connection_for(
    db: AsyncSession, project_id: str, kind: IntegrationKind
) -> IntegrationConnection | None:
    return (
        await db.execute(
            select(IntegrationConnection).where(
                IntegrationConnection.project_id == project_id,
                IntegrationConnection.kind == kind,
                IntegrationConnection.is_active.is_(True),
            )
        )
    ).scalars().first()


async def list_connections(
    db: AsyncSession, project_id: str
) -> list[IntegrationConnection]:
    rows = (
        await db.execute(
            select(IntegrationConnection)
            .where(IntegrationConnection.project_id == project_id)
            .order_by(IntegrationConnection.created_at)
        )
    ).scalars().all()
    return list(rows)


def is_expired(connection: IntegrationConnection, *, skew: int = 0) -> bool:
    if connection.token_expires_at is None:
        # No expiry means it does not expire — GitHub classic tokens. Treating
        # "no expiry" as "expired" would force a pointless reconnect loop.
        return False
    deadline = connection.token_expires_at
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=UTC)
    return datetime.now(UTC).timestamp() + skew >= deadline.timestamp()


async def build_client(
    db: AsyncSession,
    project_id: str,
    kind: IntegrationKind,
    *,
    http: httpx.AsyncClient | None = None,
) -> IntegrationClient:
    """A ready client for this project's connection, refreshing if needed."""
    connection = await connection_for(db, project_id, kind)
    if connection is None:
        raise IntegrationNotConnected(kind.value)

    if is_expired(connection) and connection.refresh_token_ref:
        await refresh_connection(db, connection, http=http)

    if not connection.access_token_ref:
        raise IntegrationNotConnected(kind.value)
    try:
        token = resolve_secret(
            connection.access_token_ref, _secret_context(connection, "access")
        )
    except SecretError as exc:
        raise IntegrationError(
            f"The stored {kind.value.title()} credential could not be read.",
            error_type="SECRET_STORE_UNAVAILABLE",
            provider=kind.value,
            suggested_action=(
                f"Reconnect {kind.value.title()} from the Integrations page."
            ),
        ) from exc

    config = IntegrationConfig(
        provider=kind.value,
        access_token=token,
        base_url=connection.base_url or "",
        account=connection.account or "",
        extra=dict(connection.config or {}),
    )
    return CLIENTS[kind](config, http=http)


async def refresh_connection(
    db: AsyncSession,
    connection: IntegrationConnection,
    *,
    http: httpx.AsyncClient | None = None,
) -> None:
    """Renew an expired token in place.

    A failure here is recorded on the connection and re-raised. Silently
    carrying on with an expired token would produce a 401 further down, where
    the cause is much harder to see.
    """
    try:
        refresh_token = resolve_secret(
            connection.refresh_token_ref or "",
            _secret_context(connection, "refresh"),
        )
        tokens = await integration_oauth.refresh(
            connection.kind, refresh_token, client=http
        )
    except (IntegrationError, SecretError) as exc:
        connection.last_error = (
            f"Could not refresh the {connection.kind.value.title()} token. "
            "Reconnect it from the Integrations page."
        )
        connection.is_active = False
        await db.flush()
        log.warning(
            "integration.refresh_failed",
            project_id=connection.project_id,
            provider=connection.kind.value,
            error=type(exc).__name__,
        )
        raise IntegrationError(
            connection.last_error,
            error_type="REFRESH_FAILED",
            provider=connection.kind.value,
        ) from exc

    store_tokens(connection, tokens)
    connection.last_error = None
    await db.flush()


def store_tokens(connection: IntegrationConnection, tokens: OAuthTokens) -> None:
    """Write tokens as references. Plaintext never reaches a column."""
    connection.access_token_ref = store_secret(
        tokens.access_token, _secret_context(connection, "access")
    )
    if tokens.refresh_token:
        connection.refresh_token_ref = store_secret(
            tokens.refresh_token, _secret_context(connection, "refresh")
        )
    connection.token_expires_at = tokens.expires_at
    connection.scopes = tokens.scopes
    connection.is_active = True


async def upsert_connection(
    db: AsyncSession,
    *,
    company_id: str,
    project_id: str,
    kind: IntegrationKind,
    tokens: OAuthTokens,
    connected_by: str,
) -> IntegrationConnection:
    """One connection per (project, provider, account).

    Reconnecting the same account replaces its tokens rather than accumulating
    rows, so "reconnect" is a repair and not a duplicate.
    """
    existing = (
        await db.execute(
            select(IntegrationConnection).where(
                IntegrationConnection.project_id == project_id,
                IntegrationConnection.kind == kind,
                IntegrationConnection.account == (tokens.account or ""),
            )
        )
    ).scalar_one_or_none()

    connection = existing or IntegrationConnection(
        company_id=company_id,
        project_id=project_id,
        kind=kind,
        account=tokens.account or "",
    )
    connection.display_name = tokens.display_name or connection.display_name
    connection.base_url = tokens.base_url or connection.base_url
    connection.config = {**(connection.config or {}), **(tokens.raw or {})}
    connection.connected_by = connected_by
    connection.last_validated_at = datetime.now(UTC)
    store_tokens(connection, tokens)
    if existing is None:
        db.add(connection)
    await db.flush()

    log.info(
        "integration.connected",
        company_id=company_id,
        project_id=project_id,
        provider=kind.value,
        # Says a token exists, never what it is.
        **tokens.redacted(),
    )
    return connection


def describe(connection: IntegrationConnection) -> dict[str, Any]:
    """Everything about a connection except the credential."""
    return {
        "id": connection.id,
        "kind": connection.kind.value,
        "account": connection.account,
        "display_name": connection.display_name,
        "base_url": connection.base_url,
        "scopes": connection.scopes.split() if connection.scopes else [],
        "is_active": connection.is_active,
        "has_refresh_token": bool(connection.refresh_token_ref),
        "expires_at": (
            connection.token_expires_at.isoformat()
            if connection.token_expires_at
            else None
        ),
        # A token with no expiry does not expire. Saying "expired" would be wrong.
        "expired": is_expired(connection),
        "last_validated_at": (
            connection.last_validated_at.isoformat()
            if connection.last_validated_at
            else None
        ),
        "last_error": connection.last_error,
        "connected_by": connection.connected_by,
    }


# ---------------------------------------------------------------------------
# Repositories
# ---------------------------------------------------------------------------
async def repository_for(
    db: AsyncSession, project_id: str, full_name: str
) -> Repository | None:
    """A repository the agent is permitted to touch, by name.

    Only repositories explicitly registered against the project are reachable.
    A connected GitHub account usually has access to many repositories the
    agent has no business writing to, and "the token can reach it" is not the
    same as "this project authorized it".
    """
    return (
        await db.execute(
            select(Repository).where(
                Repository.project_id == project_id,
                Repository.full_name == full_name,
                Repository.is_active.is_(True),
            )
        )
    ).scalars().first()


async def list_repositories(db: AsyncSession, project_id: str) -> list[Repository]:
    rows = (
        await db.execute(
            select(Repository)
            .where(Repository.project_id == project_id)
            .order_by(Repository.created_at)
        )
    ).scalars().all()
    return list(rows)


def describe_repository(repo: Repository) -> dict[str, Any]:
    from app.integrations.github import DEFAULT_BRANCH_PATTERNS

    return {
        "id": repo.id,
        "provider": repo.provider.value,
        "full_name": repo.full_name,
        "default_branch": repo.default_branch,
        "allowed_branch_patterns": repo.allowed_branch_patterns
        or list(DEFAULT_BRANCH_PATTERNS),
        "allowed_paths": repo.allowed_paths or [],
        "require_pull_request": repo.require_pull_request,
        "is_active": repo.is_active,
    }


def catalog() -> dict[str, Any]:
    """What this deployment can connect to, and what it needs to do so."""
    return {
        "providers": integration_oauth.catalog(),
        "note": (
            "A provider that is enabled but not configured cannot be connected; "
            "the deployment needs its OAuth client id, secret and callback URL."
        ),
        "features": {
            "jira": settings.feature_jira,
            "github": settings.feature_github,
            "bitbucket": settings.feature_bitbucket,
        },
    }
