"""Salesforce OAuth 2.0 web-server flow with PKCE + refresh handling.

Tokens never touch the database in plaintext: they are handed to the secret
store, which returns a *reference* bound to the owning company and project.
A reference lifted out of one project's row cannot be resolved as another's.
Tokens are never placed in prompts, tool results, URLs or logs.
"""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Environment, OAuthState, SalesforceConnection
from app.observability.logging import get_logger
from app.salesforce.apps import OAuthClient, resolve_client
from app.salesforce.errors import SalesforceAuthError, from_response
from app.security.crypto import random_token
from app.security.secrets import SecretContext, fingerprint, store_secret

log = get_logger("salesforce.oauth")

SANDBOX_LOGIN = "https://test.salesforce.com"
PROD_LOGIN = "https://login.salesforce.com"


def _pkce_pair() -> tuple[str, str]:
    verifier = random_token(48)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    return verifier, challenge


@dataclass
class TokenBundle:
    access_token: str
    refresh_token: str | None
    instance_url: str
    id_url: str
    issued_at: datetime
    scopes: str
    raw: dict[str, Any]


async def build_authorize_url(
    session: AsyncSession,
    user_id: str,
    *,
    company_id: str,
    project_id: str,
    sandbox: bool = False,
    login_url: str | None = None,
    redirect_after: str = "",
) -> str:
    # Which app authorises this org is a per-company decision, not a
    # deployment-wide one. `resolve_client` raises with an explanation when no
    # app is available, so nothing here has to guess.
    client = await resolve_client(session, company_id=company_id, project_id=project_id)

    base = login_url or (SANDBOX_LOGIN if sandbox else client.login_url)
    verifier, challenge = _pkce_pair()
    state = random_token(24)
    session.add(
        OAuthState(
            state=state,
            provider="salesforce",
            company_id=company_id,
            project_id=project_id,
            user_id=user_id,
            code_verifier=verifier,
            login_url=base,
            redirect_after=redirect_after,
            # Recorded so the exchange presents the same client_id this URL did.
            salesforce_app_id=client.app_id,
            expires_at=datetime.now(UTC) + timedelta(seconds=OAUTH_STATE_TTL_SECONDS),
        )
    )
    await session.flush()
    params = {
        "response_type": "code",
        "client_id": client.client_id,
        "redirect_uri": client.redirect_uri,
        "scope": " ".join(settings.sf_scope_list),
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "prompt": "login",
    }
    return f"{base.rstrip('/')}/services/oauth2/authorize?{urlencode(params)}"


async def _token_request(login_url: str, data: dict[str, str]) -> TokenBundle:
    url = f"{login_url.rstrip('/')}/services/oauth2/token"
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            url, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"}
        )
    if resp.status_code != 200:
        try:
            payload = resp.json()
        except Exception:
            payload = resp.text
        err = from_response(resp.status_code, payload)
        raise SalesforceAuthError(
            error_type=err.error_type or "OAUTH_FAILED",
            message=err.message,
            status_code=resp.status_code,
            likely_cause="The OAuth token endpoint rejected the request.",
            suggested_action=(
                "Verify the Connected App consumer key/secret, the callback URL, and "
                "that the user is allowed to access the app."
            ),
        )
    body = resp.json()
    return TokenBundle(
        access_token=body["access_token"],
        refresh_token=body.get("refresh_token"),
        instance_url=body["instance_url"],
        id_url=body.get("id", ""),
        issued_at=datetime.now(UTC),
        scopes=body.get("scope", ""),
        raw=body,
    )


async def exchange_code(
    login_url: str, code: str, code_verifier: str, client: OAuthClient
) -> TokenBundle:
    """Trade the authorization code for tokens, using the app that issued it.

    `client` is passed in rather than resolved here: Salesforce requires the
    exchange to present the same `client_id` and `redirect_uri` the authorize
    request used, and an administrator editing the company's app between the
    two would otherwise break an in-flight login with an `invalid_grant`.
    """
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": client.client_id,
        "redirect_uri": client.redirect_uri,
        "code_verifier": code_verifier,
    }
    if client.client_secret:
        data["client_secret"] = client.client_secret
    return await _token_request(login_url, data)


async def refresh_access_token(
    login_url: str, refresh_token: str, client: OAuthClient
) -> TokenBundle:
    """Refresh against the app that issued the token, not whichever is current."""
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client.client_id,
    }
    if client.client_secret:
        data["client_secret"] = client.client_secret
    bundle = await _token_request(login_url, data)
    if not bundle.refresh_token:
        bundle.refresh_token = refresh_token
    return bundle


async def fetch_identity(bundle: TokenBundle) -> dict[str, Any]:
    if not bundle.id_url:
        return {}
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(
            bundle.id_url, headers={"Authorization": f"Bearer {bundle.access_token}"}
        )
    if resp.status_code != 200:
        return {}
    return resp.json()


#: How long an authorization round-trip may take. Long enough for a real login
#: with MFA, short enough that a captured link is useless later.
OAUTH_STATE_TTL_SECONDS = 900


async def consume_state(session: AsyncSession, state: str) -> OAuthState:
    """Single-use CSRF state. Deleted whether or not it turns out to be valid."""
    row = await session.get(OAuthState, state)
    if row is None:
        raise SalesforceAuthError(
            error_type="INVALID_OAUTH_STATE",
            message="OAuth state is unknown or already used.",
            likely_cause="CSRF protection rejected the callback.",
            suggested_action="Restart the Salesforce connection flow.",
        )
    await session.delete(row)

    issued = row.created_at
    if issued is not None and issued.tzinfo is None:
        issued = issued.replace(tzinfo=UTC)
    expires = row.expires_at
    if expires is not None and expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    now = datetime.now(UTC)
    stale = (expires is not None and expires <= now) or (
        expires is None
        and issued is not None
        and (now - issued).total_seconds() > OAUTH_STATE_TTL_SECONDS
    )
    if stale:
        raise SalesforceAuthError(
            error_type="OAUTH_STATE_EXPIRED",
            message="This connection attempt took too long and has expired.",
            likely_cause="More than 15 minutes passed between starting and finishing login.",
            suggested_action="Start the Salesforce connection flow again.",
        )
    return row


async def upsert_connection(
    session: AsyncSession,
    user_id: str,
    bundle: TokenBundle,
    identity: dict[str, Any],
    login_url: str,
    *,
    company_id: str,
    project_id: str,
    environment: Environment | None = None,
    label: str = "",
    salesforce_app_id: str | None = None,
) -> SalesforceConnection:
    org_id = str(identity.get("organization_id") or "")[:18]
    if not org_id:
        # Fall back to the id URL shape: .../id/{orgId}/{userId}
        parts = bundle.id_url.rstrip("/").split("/")
        org_id = parts[-2] if len(parts) >= 2 else "unknown"
    sf_user_id = str(identity.get("user_id") or "")[:18]
    is_sandbox = login_url.rstrip("/") == SANDBOX_LOGIN or ".sandbox." in bundle.instance_url

    # A Salesforce org connects once per *project*, not once per user: two
    # colleagues on the same project share one connection, and another project
    # connecting the same org gets its own isolated row with its own tokens.
    existing = (
        await session.execute(
            select(SalesforceConnection).where(
                SalesforceConnection.project_id == project_id,
                SalesforceConnection.sf_org_id == org_id,
            )
        )
    ).scalar_one_or_none()

    conn = existing or SalesforceConnection(
        company_id=company_id,
        project_id=project_id,
        connected_by=user_id,
        sf_org_id=org_id,
    )
    secret_ctx = SecretContext(
        company_id=company_id, project_id=project_id, purpose="salesforce_token"
    )
    conn.sf_user_id = sf_user_id
    conn.username = str(identity.get("username") or "")
    conn.instance_url = bundle.instance_url.rstrip("/")
    conn.login_url = login_url.rstrip("/")
    conn.is_sandbox = is_sandbox
    conn.org_type = "sandbox" if is_sandbox else "production"
    # An explicit environment always wins over the sandbox flag: an org can be a
    # sandbox and still be the team's UAT environment, and policy keys off this.
    conn.environment = environment or (
        Environment.SANDBOX if is_sandbox else Environment.PRODUCTION
    )
    if label:
        conn.label = label
    elif not conn.label:
        conn.label = conn.username or conn.environment.value.title()
    conn.api_version = settings.salesforce_api_version
    # Which app authorised this connection, so a refresh is presented to the
    # same one. Reconnecting through a different app updates it here.
    conn.salesforce_app_id = salesforce_app_id
    conn.access_token_ref = store_secret(bundle.access_token, secret_ctx)
    conn.refresh_token_ref = (
        store_secret(bundle.refresh_token, secret_ctx) if bundle.refresh_token else None
    )
    conn.token_issued_at = bundle.issued_at
    conn.token_fingerprint = fingerprint(bundle.access_token)
    conn.scopes = bundle.scopes
    conn.is_active = True
    conn.last_validated_at = datetime.now(UTC)
    if existing is None:
        session.add(conn)
    await session.flush()
    log.info(
        "salesforce.connected",
        company_id=company_id,
        project_id=project_id,
        salesforce_connection_id=conn.id,
        sf_org_id=org_id,
        sandbox=is_sandbox,
    )
    return conn
