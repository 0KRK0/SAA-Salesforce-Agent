"""The provider-independent model contract.

The agent runtime speaks exactly one message shape, and every provider
translates to and from it. That is what makes the platform model-independent
in fact rather than in marketing: swapping Anthropic for Azure OpenAI changes
which class is instantiated and nothing else.

The canonical shape is Anthropic's content-block format, chosen because it
represents tool use natively rather than as a bolt-on. Providers that speak the
OpenAI shape translate in `openai_provider.py`; the translation is a real,
tested conversion, not a passthrough.

Two rules hold for every provider:

  * **A credential never travels upward.** It is resolved from the secret store
    inside the provider and never returned, logged, or placed in an error.
  * **A failure is typed.** `LLMError` carries `retryable` so the runtime can
    decide, rather than parsing a vendor's prose.
"""

from __future__ import annotations

import asyncio
import json
import random
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.observability.logging import get_logger

log = get_logger("llm")

#: Status codes worth trying again. 429 is included because every provider uses
#: it for transient capacity as well as for hard quota, and the retry is cheap.
RETRYABLE_STATUS = frozenset({408, 409, 429, 500, 502, 503, 504})


class LLMError(RuntimeError):
    """A model call failed. `retryable` is a decision, not a guess."""

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        status_code: int | None = None,
        provider: str = "",
        error_type: str = "LLM_ERROR",
    ):
        super().__init__(message)
        self.message = message
        self.retryable = retryable
        self.status_code = status_code
        self.provider = provider
        self.error_type = error_type

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": False,
            "error_type": self.error_type,
            "message": self.message,
            "retryable": self.retryable,
            "provider": self.provider,
        }


class LLMNotConfigured(LLMError):
    """No usable credential for this provider."""

    def __init__(self, message: str, provider: str = ""):
        super().__init__(
            message,
            retryable=False,
            provider=provider,
            error_type="LLM_NOT_CONFIGURED",
        )


class ProviderNotImplemented(LLMError):
    """The provider is declared but has no working implementation here.

    Raised at construction so a deployment that selects it fails immediately,
    rather than at the first customer request.
    """

    def __init__(self, message: str, provider: str = ""):
        super().__init__(
            message,
            retryable=False,
            provider=provider,
            error_type="PROVIDER_NOT_IMPLEMENTED",
        )


# ---------------------------------------------------------------------------
# Canonical types
# ---------------------------------------------------------------------------
@dataclass
class LLMResponse:
    """One model turn, in the canonical block shape."""

    content: list[dict[str, Any]]
    stop_reason: str | None
    input_tokens: int
    output_tokens: int
    model: str
    provider: str = ""
    #: Provider-reported cached-token counts, when it reports them.
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    @property
    def tool_uses(self) -> list[dict[str, Any]]:
        return [b for b in self.content if b.get("type") == "tool_use"]

    @property
    def text(self) -> str:
        return "\n".join(
            b.get("text", "") for b in self.content if b.get("type") == "text"
        ).strip()


@dataclass
class ProviderConfig:
    """Everything a provider needs, resolved. The API key is already plaintext.

    Instances are short-lived and never serialized. `redacted()` exists so a
    config can be logged without the key going with it.
    """

    provider: str
    api_key: str = ""
    base_url: str = ""
    region: str = ""
    #: Non-secret provider settings: Azure deployment name, project id, org id.
    extra: dict[str, Any] = field(default_factory=dict)
    timeout_seconds: float = 120.0
    max_retries: int = 3

    def redacted(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "base_url": self.base_url,
            "region": self.region,
            "extra": self.extra,
            "has_api_key": bool(self.api_key),
        }


class LLMProvider(ABC):
    """One model vendor.

    Implementations own the wire format and nothing else: no risk decisions, no
    approval logic, no tenancy. Those live outside the model by design.
    """

    #: Stable name, matching `app.models.LLMProviderKind`.
    kind: str = ""
    #: Whether this class actually talks to the vendor.
    implemented: bool = True
    #: Whether the provider supports tool use. The agent is useless without it,
    #: so the gateway refuses to route to a provider that does not.
    supports_tools: bool = True

    def __init__(self, config: ProviderConfig) -> None:
        self.config = config

    @abstractmethod
    async def complete(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        model: str,
        max_tokens: int,
        temperature: float | None = None,
        tool_choice: dict[str, Any] | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> LLMResponse: ...

    async def check(
        self, model: str, client: httpx.AsyncClient | None = None
    ) -> dict[str, Any]:
        """A real, minimal call, used by 'Test connection'.

        It is deliberately a live request. A connection test that only validates
        the shape of a key would report "Connected" for a revoked one, which is
        exactly the lie this product must not tell.
        """
        response = await self.complete(
            system="Reply with the single word: ok",
            messages=[{"role": "user", "content": [{"type": "text", "text": "ping"}]}],
            tools=[],
            model=model,
            max_tokens=16,
            client=client,
        )
        return {
            "success": True,
            "provider": self.kind,
            "model": response.model,
            "input_tokens": response.input_tokens,
            "output_tokens": response.output_tokens,
        }

    # -- shared HTTP plumbing ------------------------------------------------
    async def _post(
        self,
        url: str,
        *,
        headers: dict[str, str],
        payload: dict[str, Any],
        client: httpx.AsyncClient | None = None,
    ) -> dict[str, Any]:
        """POST JSON with bounded retries and exponential backoff.

        The request body is never logged: it contains the prompt, which can
        contain customer data, and the headers contain the credential.
        """
        owns = client is None
        http = client or httpx.AsyncClient(timeout=self.config.timeout_seconds)
        attempts = max(1, self.config.max_retries)
        try:
            last: LLMError | None = None
            for attempt in range(attempts):
                try:
                    response = await http.post(url, headers=headers, json=payload)
                except (httpx.ConnectError, httpx.ReadTimeout, httpx.WriteTimeout) as exc:
                    last = LLMError(
                        f"Could not reach the {self.kind} API: {type(exc).__name__}",
                        retryable=True,
                        provider=self.kind,
                        error_type="LLM_UNREACHABLE",
                    )
                except httpx.HTTPError as exc:
                    raise LLMError(
                        f"HTTP error calling {self.kind}: {type(exc).__name__}",
                        retryable=False,
                        provider=self.kind,
                    ) from exc
                else:
                    if response.status_code < 400:
                        return self._decode(response)
                    last = self._error_for(response)
                    if not last.retryable:
                        raise last

                if attempt < attempts - 1:
                    await asyncio.sleep(self._backoff(attempt))
            raise last or LLMError(f"{self.kind} call failed.", provider=self.kind)
        finally:
            if owns:
                await http.aclose()

    @staticmethod
    def _backoff(attempt: int) -> float:
        """Exponential backoff with jitter, so retries do not synchronize."""
        return min(8.0, (2**attempt) * 0.5) * (0.5 + random.random() / 2)  # noqa: S311

    def _decode(self, response: httpx.Response) -> dict[str, Any]:
        try:
            return response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise LLMError(
                f"{self.kind} returned a response that was not JSON.",
                retryable=True,
                provider=self.kind,
                error_type="LLM_BAD_RESPONSE",
            ) from exc

    def _error_for(self, response: httpx.Response) -> LLMError:
        """Turn a vendor error body into a typed error, without echoing secrets.

        Only the vendor's own message is surfaced, truncated. Request bodies and
        headers are never included, because both can carry credentials.
        """
        detail = ""
        try:
            body = response.json()
            if isinstance(body, dict):
                error = body.get("error")
                if isinstance(error, dict):
                    detail = str(error.get("message") or "")
                elif isinstance(error, str):
                    detail = error
                detail = detail or str(body.get("message") or "")
        except ValueError:
            detail = response.text[:200]

        status = response.status_code
        if status in (401, 403):
            return LLMError(
                f"{self.kind} rejected the credential ({status}). "
                "Check the key and its permissions.",
                retryable=False,
                status_code=status,
                provider=self.kind,
                error_type="LLM_UNAUTHORIZED",
            )
        if status == 404:
            return LLMError(
                f"{self.kind} does not recognize that model or endpoint. {detail}"[:400],
                retryable=False,
                status_code=status,
                provider=self.kind,
                error_type="LLM_MODEL_NOT_FOUND",
            )
        if status == 429:
            return LLMError(
                f"{self.kind} rate limit reached. {detail}"[:400],
                retryable=True,
                status_code=status,
                provider=self.kind,
                error_type="LLM_RATE_LIMITED",
            )
        return LLMError(
            f"{self.kind} API error {status}. {detail}"[:400],
            retryable=status in RETRYABLE_STATUS,
            status_code=status,
            provider=self.kind,
            error_type="LLM_ERROR",
        )


class UnimplementedProvider(LLMProvider):
    """Base for a provider that is declared but not built.

    It raises in `__init__`, so selecting it is a startup failure rather than a
    surprise at the first customer request. The message names exactly what is
    missing, so nobody has to read source to find out.
    """

    implemented = False
    reason = "This provider is not implemented in this deployment."

    def __init__(self, config: ProviderConfig) -> None:  # pragma: no cover - raises
        raise ProviderNotImplemented(self.reason, provider=self.kind)

    async def complete(self, **_: Any) -> LLMResponse:  # pragma: no cover - unreachable
        raise ProviderNotImplemented(self.reason, provider=self.kind)
