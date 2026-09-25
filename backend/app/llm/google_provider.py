"""Google Gemini through the Generative Language REST API.

Gemini's shape differs from both of the others: turns are `contents` with
`parts`, the system prompt is `systemInstruction`, tools are
`functionDeclarations`, and results come back as `functionCall` /
`functionResponse` parts. All of it translates cleanly onto the canonical
blocks; the translation is real and tested.

One genuine constraint: Gemini's function-declaration schema is a subset of
JSON Schema. Fields it rejects are stripped rather than passed through, because
a rejected declaration fails the whole request, not just that tool.
"""

from __future__ import annotations

from typing import Any

import httpx

from app.llm.base import LLMError, LLMNotConfigured, LLMProvider, LLMResponse

DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com"

#: JSON Schema keywords Gemini's `functionDeclarations` accepts. Anything else
#: is dropped — silently passing them through produces a 400 that names no tool.
_ALLOWED_SCHEMA_KEYS = frozenset(
    {
        "type",
        "format",
        "description",
        "nullable",
        "enum",
        "properties",
        "required",
        "items",
    }
)

_STOP_REASON = {
    "STOP": "end_turn",
    "MAX_TOKENS": "max_tokens",
    "SAFETY": "content_filter",
    "RECITATION": "content_filter",
}


def clean_schema(schema: Any) -> Any:
    """Reduce a JSON Schema to the subset Gemini accepts."""
    if not isinstance(schema, dict):
        return schema
    out: dict[str, Any] = {}
    for key, value in schema.items():
        if key not in _ALLOWED_SCHEMA_KEYS:
            continue
        if key == "properties" and isinstance(value, dict):
            out[key] = {k: clean_schema(v) for k, v in value.items()}
        elif key == "items":
            out[key] = clean_schema(value)
        elif key == "type" and isinstance(value, str):
            out[key] = value.upper()
        else:
            out[key] = value
    # Gemini requires an object schema to declare its properties, even empty.
    if out.get("type") == "OBJECT" and "properties" not in out:
        out["properties"] = {}
    return out


def blocks_to_contents(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Canonical blocks -> Gemini `contents`."""
    contents: list[dict[str, Any]] = []
    for message in messages:
        role = "model" if message.get("role") == "assistant" else "user"
        content = message.get("content")
        if isinstance(content, str):
            contents.append({"role": role, "parts": [{"text": content}]})
            continue

        parts: list[dict[str, Any]] = []
        for block in content or []:
            kind = block.get("type")
            if kind == "text" and block.get("text"):
                parts.append({"text": block["text"]})
            elif kind == "tool_use":
                parts.append(
                    {
                        "functionCall": {
                            "name": block.get("name", ""),
                            "args": block.get("input") or {},
                        }
                    }
                )
            elif kind == "tool_result":
                parts.append(
                    {
                        "functionResponse": {
                            "name": block.get("name")
                            or block.get("tool_use_id", "tool"),
                            "response": {"result": _as_text(block.get("content"))},
                        }
                    }
                )
        if parts:
            contents.append({"role": role, "parts": parts})
    return contents


def _as_text(content: Any) -> str:
    import json

    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            item.get("text", "") if isinstance(item, dict) else json.dumps(item, default=str)
            for item in content
        )
    return json.dumps(content, default=str)


def parts_to_blocks(parts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Gemini `parts` -> canonical blocks.

    Gemini does not issue ids for function calls, but the runtime pairs a
    result to its call by id, so one is synthesized per call position. It is
    stable within the turn, which is all the pairing needs.
    """
    blocks: list[dict[str, Any]] = []
    for index, part in enumerate(parts or []):
        if part.get("text"):
            blocks.append({"type": "text", "text": part["text"]})
        call = part.get("functionCall")
        if call:
            blocks.append(
                {
                    "type": "tool_use",
                    "id": f"call_{index}_{call.get('name', 'tool')}",
                    "name": call.get("name", ""),
                    "input": call.get("args") or {},
                }
            )
    return blocks


class GoogleProvider(LLMProvider):
    kind = "GOOGLE"

    def _base_url(self) -> str:
        return (self.config.base_url or DEFAULT_BASE_URL).rstrip("/")

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
        if not self.config.api_key:
            raise LLMNotConfigured(
                "No Google API key is available for this project.", provider=self.kind
            )

        payload: dict[str, Any] = {
            "contents": blocks_to_contents(messages),
            "generationConfig": {"maxOutputTokens": max_tokens},
        }
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        if temperature is not None:
            payload["generationConfig"]["temperature"] = temperature
        if tools:
            payload["tools"] = [
                {
                    "functionDeclarations": [
                        {
                            "name": tool["name"],
                            "description": tool.get("description", "")[:1024],
                            "parameters": clean_schema(
                                tool.get("input_schema") or {"type": "object"}
                            ),
                        }
                        for tool in tools
                    ]
                }
            ]
            payload["toolConfig"] = {
                "functionCallingConfig": {"mode": _mode(tool_choice)}
            }

        url = f"{self._base_url()}/v1beta/models/{model}:generateContent"
        body = await self._post(
            url,
            headers={
                "x-goog-api-key": self.config.api_key,
                "content-type": "application/json",
            },
            payload=payload,
            client=client,
        )

        candidates = body.get("candidates") or []
        if not candidates:
            feedback = (body.get("promptFeedback") or {}).get("blockReason")
            raise LLMError(
                f"Google returned no candidates{f' ({feedback})' if feedback else ''}.",
                retryable=not feedback,
                provider=self.kind,
                error_type="LLM_BAD_RESPONSE",
            )
        candidate = candidates[0]
        usage = body.get("usageMetadata") or {}
        content = parts_to_blocks((candidate.get("content") or {}).get("parts") or [])

        # Gemini reports finishReason STOP even when the turn is a function
        # call, unlike every other provider. Taken at face value that reads as
        # "the model is done", and the runtime would end the run without ever
        # executing the tool the model just asked for. The presence of a
        # tool_use block is the authoritative signal.
        stop_reason = _STOP_REASON.get(
            str(candidate.get("finishReason") or ""), candidate.get("finishReason")
        )
        if any(block.get("type") == "tool_use" for block in content):
            stop_reason = "tool_use"

        return LLMResponse(
            content=content,
            stop_reason=stop_reason,
            input_tokens=int(usage.get("promptTokenCount") or 0),
            output_tokens=int(usage.get("candidatesTokenCount") or 0),
            model=model,
            provider=self.kind,
            cache_read_tokens=int(usage.get("cachedContentTokenCount") or 0),
        )


def _mode(choice: dict[str, Any] | None) -> str:
    if not choice:
        return "AUTO"
    kind = choice.get("type")
    if kind in ("any", "tool"):
        return "ANY"
    if kind == "none":
        return "NONE"
    return "AUTO"
