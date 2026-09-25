"""OAuth for Jira, GitHub and Bitbucket.

One flow, three providers' worth of differences kept as data:

  * **Atlassian** issues a token for an *account*, then requires a second call
    to discover which Jira sites it can reach. Without that `cloud_id` every
    subsequent API call 404s, so it is fetched during the exchange rather than
    left for a confusing first use.
  * **GitHub** returns no `expires_in` for a classic OAuth app token and no
    refresh token — it simply does not expire. Storing a fabricated expiry
    would make the UI claim a re-connect is needed when it is not.
  * **Bitbucket** returns a short-lived access token and a refresh token, so
    refresh is mandatory rather than optional.

State is server-side and single-use, as with the Salesforce flow. Nothing about
which project a callback belongs to comes from the redirect URL, because a URL
is something the user can edit.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.integrations.base import IntegrationError, OAuthTokens, expires_at
from app.models import IntegrationKind, OAuthState
from app.observability.logging import get_logger
from app.security.crypto import random_token

log = get_logger("integrations.oauth")

#: Same reasoning as the Salesforce flow: long enough for a real login with
#: MFA, short enough that a captured link is useless later.
STATE_TTL_SECONDS = 900


class ProviderSpec:
    """Everything that differs between the three, as data."""

    def __init__(
        self,
        kind: IntegrationKind,
        authorize_url: str,
        token_url: str,
        scopes: str,
        *,
        feature: str,
        client_id_setting: str,
        client_secret_setting: str,
        redirect_setting: str,
        extra_authorize: dict[str, str] | None = None,
    ):
        self.kind = kind
        self.authorize_url = authorize_url
        self.token_url = token_url
        self.scopes = scopes
        self.feature = feature
        self.client_id_setting = client_id_setting
        self.client_secret_setting = client_secret_setting
        self.redirect_setting = redirect_setting
        self.extra_authorize = extra_authorize or {}

    @property
    def client_id(self) -> str:
        return str(getattr(settings, self.client_id_setting, "") or "")

    @property
    def client_secret(self) -> str:
        return str(getattr(settings, self.client_secret_setting, "") or "")

    @property
    def redirect_uri(self) -> str:
        return str(getattr(settings, self.redirect_setting, "") or "")

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret and self.redirect_uri)

    @property
    def enabled(self) -> bool:
        return bool(getattr(settings, f"feature_{self.feature}", False))

    def describe(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "enabled": self.enabled,
            "configured": self.configured,
            "scopes": self.scopes.split(),
            "setup_hint": (
                None
                if self.configured
                else (
                    f"Set {self.client_id_setting.upper()}, "
                    f"{self.client_secret_setting.upper()} and "
                    f"{self.redirect_setting.upper()} to enable it."
                )
            ),
        }


PROVIDERS: dict[IntegrationKind, ProviderSpec] = {
    IntegrationKind.JIRA: ProviderSpec(
        IntegrationKind.JIRA,
        authorize_url="https://auth.atlassian.com/authorize",
        token_url="https://auth.atlassian.com/oauth/token",
        scopes=(
            "read:jira-work write:jira-work read:jira-user offline_access"
        ),
        feature="jira",
        client_id_setting="jira_client_id",
        client_secret_setting="jira_client_secret",
        redirect_setting="jira_redirect_uri",
        extra_authorize={"audience": "api.atlassian.com", "prompt": "consent"},
    ),
    IntegrationKind.GITHUB: ProviderSpec(
        IntegrationKind.GITHUB,
        authorize_url="https://github.com/login/oauth/authorize",
        token_url="https://github.com/login/oauth/access_token",
        scopes="repo read:org",
        feature="github",
        client_id_setting="github_client_id",
        client_secret_setting="github_client_secret",
        redirect_setting="github_redirect_uri",
    ),
    IntegrationKind.BITBUCKET: ProviderSpec(
        IntegrationKind.BITBUCKET,
        authorize_url="https://bitbucket.org/site/oauth2/authorize",
        token_url="https://bitbucket.org/site/oauth2/access_token",
        scopes="repository repository:write pullrequest pullrequest:write issue",
        feature="bitbucket",
        client_id_setting="bitbucket_client_id",
        client_secret_setting="bitbucket_client_secret",
        redirect_setting="bitbucket_redirect_uri",
    ),
}


def spec_for(kind: IntegrationKind) -> ProviderSpec:
    spec = PROVIDERS.get(kind)
    if spec is None:
        raise IntegrationError(
            f"{kind.value.title()} is not an integration this platform supports.",
            error_type="NOT_IMPLEMENTED",
            provider=kind.value,
        )
    return spec


def catalog() -> list[dict[str, Any]]:
    return [spec.describe() for spec in PROVIDERS.values()]


# ---------------------------------------------------------------------------
# Authorize
# ---------------------------------------------------------------------------
async def build_authorize_url(
    db: AsyncSession,
    kind: IntegrationKind,
    *,
    company_id: str,
    project_id: str,
    user_id: str,
) -> str:
    from datetime import timedelta

    spec = spec_for(kind)
    if not spec.enabled:
        raise IntegrationError(
            f"{kind.value.title()} integration is disabled in this deployment.",
            error_type="FEATURE_DISABLED",
            provider=kind.value,
        )
    if not spec.configured:
        raise IntegrationError(
            f"{kind.value.title()} OAuth is not configured for this deployment.",
            error_type="CONFIG_MISSING",
            provider=kind.value,
            suggested_action=spec.describe()["setup_hint"] or "",
        )

    state = random_token(24)
    db.add(
        OAuthState(
            state=state,
            provider=kind.value,
            company_id=company_id,
            project_id=project_id,
            user_id=user_id,
            code_verifier="",
            expires_at=datetime.now(UTC) + timedelta(seconds=STATE_TTL_SECONDS),
        )
    )
    await db.flush()

    params = {
        "client_id": spec.client_id,
        "redirect_uri": spec.redirect_uri,
        "response_type": "code",
        "scope": spec.scopes,
        "state": state,
        **spec.extra_authorize,
    }
    return f"{spec.authorize_url}?{urlencode(params)}"


async def consume_state(db: AsyncSession, state: str, kind: IntegrationKind) -> OAuthState:
    """Single-use CSRF state, deleted whether or not it turns out to be valid."""
    row = await db.get(OAuthState, state)
    if row is None:
        raise IntegrationError(
            "This connection link is unknown or has already been used.",
            error_type="INVALID_STATE",
            provider=kind.value,
            suggested_action="Start the connection again from the Integrations page.",
        )
    await db.delete(row)

    if row.provider != kind.value:
        # A state minted for one provider must not complete another's callback.
        raise IntegrationError(
            "This connection link does not belong to this provider.",
            error_type="INVALID_STATE",
            provider=kind.value,
        )

    expiry = row.expires_at
    if expiry is not None and expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=UTC)
    if expiry is not None and expiry <= datetime.now(UTC):
        raise IntegrationError(
            "This connection attempt took too long and has expired.",
            error_type="STATE_EXPIRED",
            provider=kind.value,
            suggested_action="Start the connection again from the Integrations page.",
        )
    return row


# ---------------------------------------------------------------------------
# Exchange and refresh
# ---------------------------------------------------------------------------
async def exchange_code(
    kind: IntegrationKind, code: str, client: httpx.AsyncClient | None = None
) -> OAuthTokens:
    spec = spec_for(kind)
    payload = {
        "grant_type": "authorization_code",
        "client_id": spec.client_id,
        "client_secret": spec.client_secret,
        "code": code,
        "redirect_uri": spec.redirect_uri,
    }
    body = await _token_request(spec, payload, client)
    tokens = _tokens_from(body)
    await _enrich(kind, tokens, client)
    return tokens


async def refresh(
    kind: IntegrationKind, refresh_token: str, client: httpx.AsyncClient | None = None
) -> OAuthTokens:
    spec = spec_for(kind)
    body = await _token_request(
        spec,
        {
            "grant_type": "refresh_token",
            "client_id": spec.client_id,
            "client_secret": spec.client_secret,
            "refresh_token": refresh_token,
        },
        client,
    )
    tokens = _tokens_from(body)
    if not tokens.refresh_token:
        # Not every provider rotates the refresh token. Losing the old one
        # because the response omitted it would break the next refresh.
        tokens.refresh_token = refresh_token
    return tokens


async def _token_request(
    spec: ProviderSpec, payload: dict[str, str], client: httpx.AsyncClient | None
) -> dict[str, Any]:
    owns = client is None
    http = client or httpx.AsyncClient(timeout=30.0)
    try:
        response = await http.post(
            spec.token_url,
            data=payload,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
    finally:
        if owns:
            await http.aclose()

    if response.status_code >= 300:
        raise IntegrationError(
            f"{spec.kind.value.title()} rejected the token request.",
            error_type="OAUTH_FAILED",
            status_code=response.status_code,
            provider=spec.kind.value,
            suggested_action=(
                "Check the OAuth client id, secret and callback URL configured "
                "for this deployment."
            ),
        )
    try:
        body = response.json()
    except ValueError as exc:
        raise IntegrationError(
            f"{spec.kind.value.title()} returned a non-JSON token response.",
            error_type="OAUTH_FAILED",
            provider=spec.kind.value,
        ) from exc

    if body.get("error"):
        # The vendor's error code is safe; `error_description` can echo the
        # request, so only the code is surfaced.
        raise IntegrationError(
            f"{spec.kind.value.title()} rejected the authorization: {body['error']}",
            error_type="OAUTH_FAILED",
            provider=spec.kind.value,
        )
    return body


def _tokens_from(body: dict[str, Any]) -> OAuthTokens:
    return OAuthTokens(
        access_token=str(body.get("access_token") or ""),
        refresh_token=body.get("refresh_token"),
        # GitHub classic tokens carry no expiry and never expire. Inventing one
        # would make the UI demand a re-connect that is not needed.
        expires_at=expires_at(body.get("expires_in")),
        scopes=str(body.get("scope") or ""),
    )


async def _enrich(
    kind: IntegrationKind, tokens: OAuthTokens, client: httpx.AsyncClient | None
) -> None:
    """Discover the account identity the token belongs to.

    For Atlassian this is not optional: without the `cloud_id` of the Jira site,
    every subsequent API call 404s. Finding that out at connection time is much
    kinder than finding it out on the agent's first request.
    """
    if kind is not IntegrationKind.JIRA:
        return

    owns = client is None
    http = client or httpx.AsyncClient(timeout=30.0)
    try:
        response = await http.get(
            "https://api.atlassian.com/oauth/token/accessible-resources",
            headers={
                "Authorization": f"Bearer {tokens.access_token}",
                "Accept": "application/json",
            },
        )
    finally:
        if owns:
            await http.aclose()

    if response.status_code >= 300:
        raise IntegrationError(
            "Connected to Atlassian, but could not list the Jira sites this "
            "account can reach.",
            error_type="OAUTH_FAILED",
            provider=kind.value,
            suggested_action=(
                "Make sure the Atlassian app has the read:jira-work scope and "
                "that you granted it access to a site."
            ),
        )
    sites = response.json() or []
    if not sites:
        raise IntegrationError(
            "This Atlassian account has no Jira site the app can reach.",
            error_type="NO_SITE",
            provider=kind.value,
            suggested_action=(
                "Grant the app access to a Jira site during authorization."
            ),
        )
    site = sites[0]
    tokens.account = str(site.get("id") or "")
    tokens.display_name = str(site.get("name") or "")
    tokens.base_url = str(site.get("url") or "")
    tokens.raw = {"cloud_id": tokens.account, "sites": len(sites)}
