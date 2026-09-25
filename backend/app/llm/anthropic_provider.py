"""Anthropic Messages API.

Spoken over HTTP rather than through the SDK so that every provider in this
package shares one retry policy, one error taxonomy and one testing story
(`httpx.MockTransport`). The canonical message shape is Anthropic's, so this
provider is the one that does no translation.
"""

from __future__ import annotations

from typing import Any

import httpx

from app.llm.base import LLMNotConfigured, LLMProvider, LLMResponse

DEFAULT_BASE_URL = "https://api.anthropic.com"
ANTHROPIC_VERSION = "2023-06-01"


class AnthropicProvider(LLMProvider):
    kind = "ANTHROPIC"

    def _headers(self) -> dict[str, str]:
        if not self.config.api_key:
            raise LLMNotConfigured(
                "No Anthropic API key is available for this project.", provider=self.kind
            )
        return {
            "x-api-key": self.config.api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        }

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
    ) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": messages,
        }
        if system:
            payload["system"] = system
        if tools:
            payload["tools"] = tools
        if temperature is not None:
            payload["temperature"] = temperature
        if tool_choice:
            payload["tool_choice"] = tool_choice

        base = (self.config.base_url or DEFAULT_BASE_URL).rstrip("/")
        body = await self._post(
            f"{base}/v1/messages",
            headers=self._headers(),
            payload=payload,
            client=client,
        )
        usage = body.get("usage") or {}
        return LLMResponse(
            content=list(body.get("content") or []),
            stop_reason=body.get("stop_reason"),
            input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
            model=str(body.get("model") or model),
            provider=self.kind,
            cache_read_tokens=int(usage.get("cache_read_input_tokens") or 0),
            cache_write_tokens=int(usage.get("cache_creation_input_tokens") or 0),
        )
