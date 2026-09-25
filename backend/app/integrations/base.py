"""Shared contract for non-Salesforce systems the agent can reach.

Three rules hold for every integration here, and they are what make these
safe to hand an agent:

  * **A token never travels upward.** It is resolved from the secret store
    inside the client and is never returned, logged, or placed in an error.
  * **Everything these systems return is untrusted data.** A Jira description,
    a GitHub issue body and a Bitbucket comment are all text a stranger can
    write. They reach the model wrapped in an untrusted-data boundary, exactly
    like Salesforce records, and never as instructions.
  * **A claim requires a call.** "I created the branch" is only ever said after
    an API call returned the branch. Nothing in this package reports success
    from a request that was not made.

Providers are declared in `catalog.py`; anything not implemented raises at
construction rather than failing at a customer's first use.
"""

from __future__ import annotations

import asyncio
import random
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from app.observability.logging import get_logger

log = get_logger("integrations")

RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})


class IntegrationError(RuntimeError):
    """A call to an external system failed, with the reason typed."""

    def __init__(
        self,
        message: str,
        *,
        error_type: str = "INTEGRATION_ERROR",
        retryable: bool = False,
        status_code: int | None = None,
        provider: str = "",
        suggested_action: str = "",
    ):
        super().__init__(message)
        self.message = message
        self.error_type = error_type
        self.retryable = retryable
        self.status_code = status_code
        self.provider = provider
        self.suggested_action = suggested_action

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": False,
            "error_type": self.error_type,
            "message": self.message,
            "retryable": self.retryable,
            "provider": self.provider,
            "suggested_action": self.suggested_action,
        }


class IntegrationNotConnected(IntegrationError):
    def __init__(self, provider: str):
        super().__init__(
            f"No {provider.title()} account is connected to this project.",
            error_type="NOT_CONNECTED",
            provider=provider,
            suggested_action=(
                f"Connect {provider.title()} from the project's Integrations page "
                "before asking the agent to use it."
            ),
        )


class IntegrationNotImplemented(IntegrationError):
    def __init__(self, message: str, provider: str = ""):
        super().__init__(
            message, error_type="NOT_IMPLEMENTED", provider=provider
        )


@dataclass
class OAuthTokens:
    access_token: str
    refresh_token: str | None = None
    expires_at: datetime | None = None
    scopes: str = ""
    #: Provider-specific account identity discovered during the exchange.
    account: str = ""
    display_name: str = ""
    base_url: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    def redacted(self) -> dict[str, Any]:
        """Safe to log. Says that tokens exist, never what they are."""
        return {
            "has_access_token": bool(self.access_token),
            "has_refresh_token": bool(self.refresh_token),
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "scopes": self.scopes,
            "account": self.account,
        }


@dataclass
class IntegrationConfig:
    """Everything a client needs, resolved. Short-lived; never serialized."""

    provider: str
    access_token: str
    base_url: str = ""
    account: str = ""
    extra: dict[str, Any] = field(default_factory=dict)
    timeout_seconds: float = 30.0
    max_retries: int = 3


class IntegrationClient(ABC):
    """One external system, for one project's connection."""

    provider: str = ""
    implemented: bool = True

    def __init__(
        self, config: IntegrationConfig, http: httpx.AsyncClient | None = None
    ) -> None:
        self.config = config
        self._http = http
        self._owns_http = http is None

    async def __aenter__(self) -> IntegrationClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=self.config.timeout_seconds)
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._owns_http and self._http is not None:
            await self._http.aclose()
            self._http = None

    # -- subclass surface ----------------------------------------------------
    @abstractmethod
    def api_base(self) -> str: ...

    def headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.config.access_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    @abstractmethod
    async def whoami(self) -> dict[str, Any]:
        """A real call proving the connection works. Used by 'Test connection'."""

    # -- shared plumbing -----------------------------------------------------
    async def request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        absolute: bool = False,
    ) -> Any:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=self.config.timeout_seconds)
        url = path if absolute else f"{self.api_base().rstrip('/')}{path}"
        merged = {**self.headers(), **(headers or {})}

        attempts = max(1, self.config.max_retries)
        last: IntegrationError | None = None
        for attempt in range(attempts):
            try:
                response = await self._http.request(
                    method, url, json=json, params=params, headers=merged
                )
            except (httpx.ConnectError, httpx.ReadTimeout, httpx.WriteTimeout) as exc:
                last = IntegrationError(
                    f"Could not reach {self.provider.title()}: {type(exc).__name__}",
                    error_type="UNREACHABLE",
                    retryable=True,
                    provider=self.provider,
                )
            except httpx.HTTPError as exc:
                raise IntegrationError(
                    f"HTTP error calling {self.provider.title()}: {type(exc).__name__}",
                    provider=self.provider,
                ) from exc
            else:
                if response.status_code < 400:
                    return self._decode(response)
                last = self._error_for(response)
                if not last.retryable:
                    raise last

            if attempt < attempts - 1:
                await asyncio.sleep(
                    min(8.0, (2**attempt) * 0.5) * (0.5 + random.random() / 2)  # noqa: S311
                )
        raise last or IntegrationError(
            f"{self.provider.title()} call failed.", provider=self.provider
        )

    def _decode(self, response: httpx.Response) -> Any:
        if response.status_code == 204 or not response.content:
            return {}
        try:
            return response.json()
        except ValueError:
            return {"raw": response.text[:2000]}

    def _error_for(self, response: httpx.Response) -> IntegrationError:
        """A typed error carrying the vendor's message and nothing of ours.

        Request bodies and headers are never included: both can carry the
        token, and an error message is the least guarded thing in a system.
        """
        detail = _vendor_message(response)
        status = response.status_code
        name = self.provider.title()

        if status == 401:
            return IntegrationError(
                f"{name} rejected the stored credential.",
                error_type="UNAUTHORIZED",
                status_code=status,
                provider=self.provider,
                suggested_action=(
                    f"Reconnect {name} from the project's Integrations page. The "
                    "token may have been revoked or expired."
                ),
            )
        if status == 403:
            return IntegrationError(
                f"{name} refused this action: {detail}"[:400],
                error_type="FORBIDDEN",
                status_code=status,
                provider=self.provider,
                suggested_action=(
                    f"The connected {name} account does not have permission for "
                    "this. Grant it there, or ask someone who has it."
                ),
            )
        if status == 404:
            return IntegrationError(
                f"{name} could not find that. {detail}"[:400],
                error_type="NOT_FOUND",
                status_code=status,
                provider=self.provider,
                suggested_action="Check the identifier and try again.",
            )
        if status == 409:
            return IntegrationError(
                f"{name} reports a conflict: {detail}"[:400],
                error_type="CONFLICT",
                status_code=status,
                provider=self.provider,
                suggested_action=(
                    "Something changed underneath this operation. Re-read the "
                    "current state before retrying."
                ),
            )
        if status == 422:
            return IntegrationError(
                f"{name} rejected the request: {detail}"[:400],
                error_type="VALIDATION_ERROR",
                status_code=status,
                provider=self.provider,
            )
        if status == 429:
            return IntegrationError(
                f"{name} rate limit reached.",
                error_type="RATE_LIMITED",
                retryable=True,
                status_code=status,
                provider=self.provider,
            )
        return IntegrationError(
            f"{name} API error {status}. {detail}"[:400],
            retryable=status in RETRYABLE_STATUS,
            status_code=status,
            provider=self.provider,
        )


def _vendor_message(response: httpx.Response) -> str:
    """Best-effort extraction of a vendor's own error text."""
    try:
        body = response.json()
    except ValueError:
        return response.text[:200]
    if isinstance(body, dict):
        for key in ("message", "error_description", "error"):
            value = body.get(key)
            if isinstance(value, str) and value:
                return value
        # Jira and Bitbucket nest theirs.
        for key in ("errorMessages", "errors"):
            value = body.get(key)
            if isinstance(value, list) and value:
                return "; ".join(str(v) for v in value)
            if isinstance(value, dict) and value:
                return "; ".join(f"{k}: {v}" for k, v in value.items())
        nested = body.get("error")
        if isinstance(nested, dict):
            return str(nested.get("message") or nested)
    if isinstance(body, list) and body:
        return str(body[0])
    return ""


def expires_at(seconds: Any) -> datetime | None:
    """Turn a provider's `expires_in` into a real deadline."""
    try:
        value = int(seconds)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    # A minute of slack: a token that expires while a request is in flight is
    # indistinguishable from one that was revoked, and the second is alarming.
    return datetime.now(UTC) + timedelta(seconds=max(0, value - 60))
