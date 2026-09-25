"""Providers speaking the OpenAI chat-completions shape.

One implementation covers OpenAI, Azure OpenAI, Mistral, Groq, DeepSeek,
Together, Ollama and any other OpenAI-compatible endpoint, because they all
speak the same wire format. What differs is the URL, the auth header and a
couple of quirks, which are data rather than code.

The interesting part is the translation. This platform's canonical shape is
Anthropic content blocks; OpenAI uses flat strings plus a parallel
`tool_calls` array and a separate `tool` role for results. The conversion runs
both ways and is tested against real payload shapes, because a silent mistake
here would show up as the agent "forgetting" a tool result — which looks like a
model problem and is not one.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from app.llm.base import LLMError, LLMNotConfigured, LLMProvider, LLMResponse

DEFAULT_BASE_URL = "https://api.openai.com/v1"

#: OpenAI reports why it stopped with different words than Anthropic. The
#: runtime keys off `tool_use`, so the mapping has to be exact.
_STOP_REASON = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "content_filter": "content_filter",
}


def blocks_to_openai(
    system: str, messages: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Canonical blocks -> OpenAI chat messages.

    An assistant turn holding tool calls becomes one message with `tool_calls`;
    each tool result becomes its own message with role `tool`. Getting this
    wrong drops the link between a call and its result.
    """
    out: list[dict[str, Any]] = []
    if system:
        out.append({"role": "system", "content": system})

    for message in messages:
        role = message.get("role", "user")
        content = message.get("content")

        if isinstance(content, str):
            out.append({"role": role, "content": content})
            continue

        text_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        tool_results: list[dict[str, Any]] = []

        for block in content or []:
            kind = block.get("type")
            if kind == "text":
                text_parts.append(block.get("text", ""))
            elif kind == "tool_use":
                tool_calls.append(
                    {
                        "id": block.get("id", ""),
                        "type": "function",
                        "function": {
                            "name": block.get("name", ""),
                            "arguments": json.dumps(block.get("input") or {}),
                        },
                    }
                )
            elif kind == "tool_result":
                tool_results.append(
                    {
                        "role": "tool",
                        "tool_call_id": block.get("tool_use_id", ""),
                        "content": _result_text(block.get("content")),
                    }
                )

        # Tool results are a user-role concept in the canonical shape but their
        # own role in OpenAI's, so they are emitted before anything else in the
        # turn — the API rejects a tool message that does not directly follow
        # the assistant message that requested it.
        out.extend(tool_results)

        if role == "assistant" and tool_calls:
            out.append(
                {
                    "role": "assistant",
                    "content": "\n".join(p for p in text_parts if p) or None,
                    "tool_calls": tool_calls,
                }
            )
        elif text_parts:
            out.append({"role": role, "content": "\n".join(text_parts)})

    return out


def _result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
            else:
                parts.append(json.dumps(item, default=str))
        return "\n".join(parts)
    return json.dumps(content, default=str)


def tools_to_openai(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool.get("description", ""),
                "parameters": tool.get("input_schema") or {"type": "object"},
            },
        }
        for tool in tools
    ]


def openai_to_blocks(message: dict[str, Any]) -> list[dict[str, Any]]:
    """OpenAI assistant message -> canonical blocks."""
    blocks: list[dict[str, Any]] = []
    text = message.get("content")
    if isinstance(text, str) and text.strip():
        blocks.append({"type": "text", "text": text})
    elif isinstance(text, list):
        for part in text:
            if isinstance(part, dict) and part.get("type") == "text":
                blocks.append({"type": "text", "text": part.get("text", "")})

    for call in message.get("tool_calls") or []:
        function = call.get("function") or {}
        raw = function.get("arguments") or "{}"
        try:
            arguments = json.loads(raw) if isinstance(raw, str) else dict(raw)
        except (ValueError, TypeError):
            # A malformed arguments string is the model's error, not a crash.
            # Surfacing it as a tool_use with empty input lets the runtime's
            # validator reject it with a message the model can act on.
            arguments = {}
        blocks.append(
            {
                "type": "tool_use",
                "id": call.get("id", ""),
                "name": function.get("name", ""),
                "input": arguments,
            }
        )
    return blocks


class OpenAICompatibleProvider(LLMProvider):
    """OpenAI and every endpoint that speaks its chat-completions API."""

    kind = "OPENAI_COMPATIBLE"
    #: Some compatible endpoints (notably small local models via Ollama) do not
    #: implement tool calling. The gateway refuses to route the agent to a
    #: provider whose tools do not work, rather than letting it fail mid-run.
    supports_tools = True

    def _base_url(self) -> str:
        return (self.config.base_url or DEFAULT_BASE_URL).rstrip("/")

    def _headers(self) -> dict[str, str]:
        if not self.config.api_key and self.kind != "OLLAMA":
            raise LLMNotConfigured(
                f"No API key is available for {self.kind}.", provider=self.kind
            )
        headers = {"content-type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        organization = str(self.config.extra.get("organization") or "")
        if organization:
            headers["OpenAI-Organization"] = organization
        return headers

    def _url(self, model: str) -> str:
        return f"{self._base_url()}/chat/completions"

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
            "messages": blocks_to_openai(system, messages),
            "max_tokens": max_tokens,
        }
        if tools:
            payload["tools"] = tools_to_openai(tools)
            payload["tool_choice"] = _tool_choice(tool_choice)
        if temperature is not None:
            payload["temperature"] = temperature

        body = await self._post(
            self._url(model), headers=self._headers(), payload=payload, client=client
        )
        choices = body.get("choices") or []
        if not choices:
            raise LLMError(
                f"{self.kind} returned no choices.",
                retryable=True,
                provider=self.kind,
                error_type="LLM_BAD_RESPONSE",
            )
        choice = choices[0]
        usage = body.get("usage") or {}
        details = usage.get("prompt_tokens_details") or {}
        return LLMResponse(
            content=openai_to_blocks(choice.get("message") or {}),
            stop_reason=_STOP_REASON.get(
                str(choice.get("finish_reason") or ""), choice.get("finish_reason")
            ),
            input_tokens=int(usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or 0),
            model=str(body.get("model") or model),
            provider=self.kind,
            cache_read_tokens=int(details.get("cached_tokens") or 0),
        )


def _tool_choice(choice: dict[str, Any] | None) -> Any:
    """Translate the canonical tool_choice into OpenAI's."""
    if not choice:
        return "auto"
    kind = choice.get("type")
    if kind == "any":
        return "required"
    if kind == "tool" and choice.get("name"):
        return {"type": "function", "function": {"name": choice["name"]}}
    if kind == "none":
        return "none"
    return "auto"


class OpenAIProvider(OpenAICompatibleProvider):
    kind = "OPENAI"


class MistralProvider(OpenAICompatibleProvider):
    kind = "MISTRAL"

    def _base_url(self) -> str:
        return (self.config.base_url or "https://api.mistral.ai/v1").rstrip("/")


class GroqProvider(OpenAICompatibleProvider):
    kind = "GROQ"

    def _base_url(self) -> str:
        return (self.config.base_url or "https://api.groq.com/openai/v1").rstrip("/")


class DeepSeekProvider(OpenAICompatibleProvider):
    kind = "DEEPSEEK"

    def _base_url(self) -> str:
        return (self.config.base_url or "https://api.deepseek.com/v1").rstrip("/")


class TogetherProvider(OpenAICompatibleProvider):
    kind = "TOGETHER"

    def _base_url(self) -> str:
        return (self.config.base_url or "https://api.together.xyz/v1").rstrip("/")


class OllamaProvider(OpenAICompatibleProvider):
    """A local Ollama server, through its OpenAI-compatible endpoint.

    No API key, and no assumption about tool support: whether tools work depends
    on the model pulled, so `supports_tools` is read from the credential's
    config rather than asserted here.
    """

    kind = "OLLAMA"

    def _base_url(self) -> str:
        base = (self.config.base_url or "http://localhost:11434").rstrip("/")
        return base if base.endswith("/v1") else f"{base}/v1"

    def _headers(self) -> dict[str, str]:
        return {"content-type": "application/json"}


class AzureOpenAIProvider(OpenAICompatibleProvider):
    """Azure OpenAI: same body, different URL shape and auth header.

    Azure addresses a *deployment* rather than a model, so the model name here
    is the deployment name. That is stated in the UI, because using the OpenAI
    model id is the mistake everyone makes first.
    """

    kind = "AZURE_OPENAI"

    def _headers(self) -> dict[str, str]:
        if not self.config.api_key:
            raise LLMNotConfigured(
                "No Azure OpenAI API key is available for this project.",
                provider=self.kind,
            )
        return {"api-key": self.config.api_key, "content-type": "application/json"}

    def _url(self, model: str) -> str:
        endpoint = (self.config.base_url or "").rstrip("/")
        if not endpoint:
            raise LLMNotConfigured(
                "Azure OpenAI needs the resource endpoint "
                "(https://<resource>.openai.azure.com).",
                provider=self.kind,
            )
        version = str(self.config.extra.get("api_version") or "2024-10-21")
        deployment = str(self.config.extra.get("deployment") or model)
        return (
            f"{endpoint}/openai/deployments/{deployment}"
            f"/chat/completions?api-version={version}"
        )
