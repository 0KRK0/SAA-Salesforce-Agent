"""Anthropic Claude client wrapper.

The model name and generation settings are configuration, never hardcoded at
call sites, so the platform can be upgraded without touching the agent.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from anthropic import (
    APIConnectionError,
    APIStatusError,
    AsyncAnthropic,
    RateLimitError,
)

from app.config import settings
from app.observability.logging import get_logger

log = get_logger("agent.claude")


class ClaudeNotConfigured(RuntimeError):
    pass


class ClaudeCallError(RuntimeError):
    def __init__(self, message: str, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


@dataclass
class ClaudeResponse:
    content: list[dict[str, Any]]
    stop_reason: str | None
    input_tokens: int
    output_tokens: int
    model: str

    @property
    def tool_uses(self) -> list[dict[str, Any]]:
        return [b for b in self.content if b.get("type") == "tool_use"]

    @property
    def text(self) -> str:
        return "\n".join(
            b.get("text", "") for b in self.content if b.get("type") == "text"
        ).strip()


_client: AsyncAnthropic | None = None


def get_client() -> AsyncAnthropic:
    global _client
    if not settings.claude_configured:
        raise ClaudeNotConfigured(
            "ANTHROPIC_API_KEY is not set. Set it in .env to enable the agent."
        )
    if _client is None:
        _client = AsyncAnthropic(
            api_key=settings.anthropic_api_key,
            timeout=settings.claude_timeout_seconds,
            max_retries=settings.claude_max_retries,
        )
    return _client


async def call_model(
    system: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    *,
    model: str | None = None,
    max_tokens: int | None = None,
    tool_choice: dict[str, Any] | None = None,
) -> ClaudeResponse:
    client = get_client()
    kwargs: dict[str, Any] = {
        "model": model or settings.claude_model,
        "max_tokens": max_tokens or settings.claude_max_tokens,
        "system": system,
        "messages": messages,
        "tools": tools,
    }
    if settings.claude_temperature is not None:
        kwargs["temperature"] = settings.claude_temperature
    if tool_choice:
        kwargs["tool_choice"] = tool_choice

    try:
        resp = await client.messages.create(**kwargs)
    except RateLimitError as exc:
        raise ClaudeCallError(f"Claude rate limit: {exc}", retryable=True) from exc
    except APIConnectionError as exc:
        raise ClaudeCallError(f"Could not reach the Claude API: {exc}", retryable=True) from exc
    except APIStatusError as exc:
        raise ClaudeCallError(
            f"Claude API error {exc.status_code}: {exc.message}",
            retryable=exc.status_code >= 500,
        ) from exc

    content = [b.model_dump() for b in resp.content]
    log.debug(
        "claude.response",
        stop_reason=resp.stop_reason,
        input_tokens=resp.usage.input_tokens,
        output_tokens=resp.usage.output_tokens,
        tool_uses=len([b for b in content if b.get("type") == "tool_use"]),
    )
    return ClaudeResponse(
        content=content,
        stop_reason=resp.stop_reason,
        input_tokens=resp.usage.input_tokens,
        output_tokens=resp.usage.output_tokens,
        model=resp.model,
    )
