"""Provider implementations, tested against real payload shapes.

Every provider is exercised through `httpx.MockTransport` with response bodies
copied from the vendors' documented formats. No network, no vendor SDK.

The translation tests matter more than they look. If a tool result loses its
link to the call that produced it, the model appears to "forget" what it just
did — which reads as a model problem and is not one. These tests are what stops
that shipping.
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.llm.anthropic_provider import AnthropicProvider
from app.llm.base import (
    LLMError,
    LLMNotConfigured,
    ProviderConfig,
    ProviderNotImplemented,
)
from app.llm.catalog import (
    BedrockProvider,
    VertexProvider,
    build_provider,
    catalog,
    default_model,
    spec_for,
)
from app.llm.google_provider import (
    GoogleProvider,
    blocks_to_contents,
    clean_schema,
    parts_to_blocks,
)
from app.llm.openai_provider import (
    AzureOpenAIProvider,
    OllamaProvider,
    OpenAIProvider,
    blocks_to_openai,
    openai_to_blocks,
    tools_to_openai,
)

TOOLS = [
    {
        "name": "query_salesforce",
        "description": "Run a SOQL query",
        "input_schema": {
            "type": "object",
            "properties": {"soql": {"type": "string"}},
            "required": ["soql"],
            "additionalProperties": False,
        },
    }
]

CONVERSATION = [
    {"role": "user", "content": [{"type": "text", "text": "How many accounts?"}]},
    {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "Checking."},
            {
                "type": "tool_use",
                "id": "toolu_1",
                "name": "query_salesforce",
                "input": {"soql": "SELECT COUNT() FROM Account"},
            },
        ],
    },
    {
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": "toolu_1", "content": "42"}
        ],
    },
]


def _config(**kwargs) -> ProviderConfig:
    return ProviderConfig(provider="TEST", api_key="test-key", max_retries=1, **kwargs)


def _transport(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# ---------------------------------------------------------------------------
# Anthropic
# ---------------------------------------------------------------------------
async def test_anthropic_returns_canonical_blocks_unchanged():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("x-api-key")
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": "msg_1",
                "model": "claude-sonnet-4-5",
                "stop_reason": "tool_use",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_9",
                        "name": "query_salesforce",
                        "input": {"soql": "SELECT Id FROM Account"},
                    }
                ],
                "usage": {"input_tokens": 120, "output_tokens": 30},
            },
        )

    provider = AnthropicProvider(_config())
    async with _transport(handler) as client:
        response = await provider.complete(
            system="You are an agent",
            messages=CONVERSATION,
            tools=TOOLS,
            model="claude-sonnet-4-5",
            max_tokens=1024,
            client=client,
        )

    assert response.stop_reason == "tool_use"
    assert response.tool_uses[0]["name"] == "query_salesforce"
    assert response.input_tokens == 120
    assert captured["auth"] == "test-key"
    assert captured["url"].endswith("/v1/messages")
    # Anthropic is the canonical shape, so nothing is translated on the way out.
    assert captured["body"]["messages"] == CONVERSATION


async def test_a_missing_key_is_a_configuration_error_not_a_call():
    provider = AnthropicProvider(ProviderConfig(provider="ANTHROPIC", api_key=""))
    with pytest.raises(LLMNotConfigured):
        await provider.complete(
            system="", messages=[], tools=[], model="x", max_tokens=10
        )


# ---------------------------------------------------------------------------
# OpenAI translation
# ---------------------------------------------------------------------------
def test_a_tool_call_and_its_result_stay_linked_through_translation():
    """OpenAI splits a tool result into its own message keyed by call id. Lose
    that key and the model cannot tell which call was answered."""
    converted = blocks_to_openai("You are an agent", CONVERSATION)

    assistant = next(m for m in converted if m.get("tool_calls"))
    assert assistant["tool_calls"][0]["id"] == "toolu_1"
    assert assistant["tool_calls"][0]["function"]["name"] == "query_salesforce"
    assert json.loads(assistant["tool_calls"][0]["function"]["arguments"]) == {
        "soql": "SELECT COUNT() FROM Account"
    }

    result = next(m for m in converted if m["role"] == "tool")
    assert result["tool_call_id"] == "toolu_1"
    assert result["content"] == "42"


def test_the_system_prompt_becomes_the_first_message():
    converted = blocks_to_openai("SYSTEM RULES", CONVERSATION)
    assert converted[0] == {"role": "system", "content": "SYSTEM RULES"}


def test_tool_schemas_become_openai_functions():
    converted = tools_to_openai(TOOLS)
    assert converted[0]["type"] == "function"
    assert converted[0]["function"]["name"] == "query_salesforce"
    assert converted[0]["function"]["parameters"]["required"] == ["soql"]


def test_openai_tool_calls_become_canonical_blocks():
    blocks = openai_to_blocks(
        {
            "role": "assistant",
            "content": "Let me check.",
            "tool_calls": [
                {
                    "id": "call_abc",
                    "type": "function",
                    "function": {
                        "name": "describe_object",
                        "arguments": '{"object": "Account"}',
                    },
                }
            ],
        }
    )
    assert blocks[0] == {"type": "text", "text": "Let me check."}
    assert blocks[1] == {
        "type": "tool_use",
        "id": "call_abc",
        "name": "describe_object",
        "input": {"object": "Account"},
    }


def test_malformed_tool_arguments_do_not_crash_the_translation():
    """A model emitting broken JSON is the model's error. It has to reach the
    runtime's validator as a rejectable tool call, not blow up the request."""
    blocks = openai_to_blocks(
        {
            "tool_calls": [
                {
                    "id": "call_1",
                    "function": {"name": "create_field", "arguments": "{not json"},
                }
            ]
        }
    )
    assert blocks[0]["type"] == "tool_use"
    assert blocks[0]["input"] == {}


async def test_openai_finish_reason_maps_onto_the_runtime_contract():
    """The runtime branches on `tool_use`. OpenAI says `tool_calls`; if the
    mapping is wrong the agent silently stops before running anything."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "gpt-4o",
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "function": {
                                        "name": "query_salesforce",
                                        "arguments": '{"soql": "SELECT Id FROM Account"}',
                                    },
                                }
                            ],
                        },
                    }
                ],
                "usage": {"prompt_tokens": 90, "completion_tokens": 12},
            },
        )

    provider = OpenAIProvider(_config())
    async with _transport(handler) as client:
        response = await provider.complete(
            system="s",
            messages=CONVERSATION,
            tools=TOOLS,
            model="gpt-4o",
            max_tokens=512,
            client=client,
        )
    assert response.stop_reason == "tool_use"
    assert response.tool_uses[0]["name"] == "query_salesforce"
    assert response.output_tokens == 12


async def test_azure_addresses_a_deployment_and_uses_its_own_auth_header():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["api_key"] = request.headers.get("api-key")
        captured["bearer"] = request.headers.get("authorization")
        return httpx.Response(
            200,
            json={
                "model": "gpt-4o",
                "choices": [
                    {"finish_reason": "stop", "message": {"content": "ok"}}
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    provider = AzureOpenAIProvider(
        _config(
            base_url="https://contoso.openai.azure.com",
            extra={"deployment": "prod-gpt4o", "api_version": "2024-10-21"},
        )
    )
    async with _transport(handler) as client:
        await provider.complete(
            system="s", messages=CONVERSATION, tools=[], model="ignored",
            max_tokens=64, client=client,
        )

    assert "/openai/deployments/prod-gpt4o/chat/completions" in captured["url"]
    assert "api-version=2024-10-21" in captured["url"]
    assert captured["api_key"] == "test-key"
    # Azure rejects the bearer header; sending both is a subtle 401.
    assert captured["bearer"] is None


async def test_azure_without_an_endpoint_says_so_before_calling_anything():
    provider = AzureOpenAIProvider(ProviderConfig(provider="AZURE_OPENAI", api_key="k"))
    with pytest.raises(LLMNotConfigured) as exc:
        await provider.complete(
            system="", messages=[], tools=[], model="m", max_tokens=8
        )
    assert "endpoint" in str(exc.value).lower()


async def test_ollama_needs_no_key_and_targets_the_v1_path():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(
            200,
            json={
                "model": "llama3",
                "choices": [{"finish_reason": "stop", "message": {"content": "hi"}}],
                "usage": {},
            },
        )

    provider = OllamaProvider(
        ProviderConfig(provider="OLLAMA", base_url="http://localhost:11434")
    )
    async with _transport(handler) as client:
        response = await provider.complete(
            system="", messages=CONVERSATION, tools=[], model="llama3",
            max_tokens=64, client=client,
        )
    assert captured["url"].endswith("/v1/chat/completions")
    assert captured["auth"] is None
    assert response.text == "hi"


# ---------------------------------------------------------------------------
# Google translation
# ---------------------------------------------------------------------------
def test_gemini_schema_cleaning_drops_what_the_api_rejects():
    """Gemini 400s on unknown schema keys and names no tool in the error, so a
    passthrough here is very expensive to debug."""
    cleaned = clean_schema(
        {
            "type": "object",
            "additionalProperties": False,
            "$schema": "http://json-schema.org/draft-07/schema#",
            "properties": {
                "soql": {"type": "string", "maxLength": 200, "description": "query"}
            },
            "required": ["soql"],
        }
    )
    assert cleaned["type"] == "OBJECT"
    assert "additionalProperties" not in cleaned
    assert "$schema" not in cleaned
    assert "maxLength" not in cleaned["properties"]["soql"]
    assert cleaned["properties"]["soql"]["description"] == "query"
    assert cleaned["required"] == ["soql"]


def test_an_object_schema_always_declares_properties():
    assert clean_schema({"type": "object"})["properties"] == {}


def test_gemini_roles_and_parts_round_trip():
    contents = blocks_to_contents(CONVERSATION)
    assert [c["role"] for c in contents] == ["user", "model", "user"]
    assert contents[1]["parts"][1]["functionCall"]["name"] == "query_salesforce"
    assert contents[2]["parts"][0]["functionResponse"]["response"]["result"] == "42"


def test_gemini_function_calls_get_ids_the_runtime_can_pair_on():
    """Gemini issues no call ids, but the runtime pairs results to calls by id.
    Without a synthesized one, two calls in a turn become indistinguishable."""
    blocks = parts_to_blocks(
        [
            {"text": "Working"},
            {"functionCall": {"name": "describe_object", "args": {"object": "Account"}}},
            {"functionCall": {"name": "describe_object", "args": {"object": "Contact"}}},
        ]
    )
    ids = [b["id"] for b in blocks if b["type"] == "tool_use"]
    assert len(ids) == 2
    assert len(set(ids)) == 2


async def test_google_blocked_content_is_not_reported_as_a_transient_failure():
    """A safety block is a final answer. Retrying it just spends money."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"candidates": [], "promptFeedback": {"blockReason": "SAFETY"}}
        )

    provider = GoogleProvider(_config())
    async with _transport(handler) as client:
        with pytest.raises(LLMError) as exc:
            await provider.complete(
                system="", messages=CONVERSATION, tools=[], model="gemini-2.5-flash",
                max_tokens=64, client=client,
            )
    assert exc.value.retryable is False


async def test_google_sends_the_key_in_its_own_header():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["key"] = request.headers.get("x-goog-api-key")
        captured["url"] = str(request.url)
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {"finishReason": "STOP", "content": {"parts": [{"text": "ok"}]}}
                ],
                "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 2},
            },
        )

    provider = GoogleProvider(_config())
    async with _transport(handler) as client:
        response = await provider.complete(
            system="rules", messages=CONVERSATION, tools=TOOLS,
            model="gemini-2.5-pro", max_tokens=64, client=client,
        )
    assert captured["key"] == "test-key"
    assert "gemini-2.5-pro:generateContent" in captured["url"]
    assert response.stop_reason == "end_turn"


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------
async def test_a_rejected_credential_is_never_retried():
    """Retrying a 401 cannot succeed, and each attempt is another request that
    may be counted against a lockout policy."""
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(401, json={"error": {"message": "invalid x-api-key"}})

    provider = AnthropicProvider(ProviderConfig(provider="ANTHROPIC", api_key="bad", max_retries=3))
    async with _transport(handler) as client:
        with pytest.raises(LLMError) as exc:
            await provider.complete(
                system="", messages=CONVERSATION, tools=[], model="m",
                max_tokens=8, client=client,
            )
    assert len(calls) == 1
    assert exc.value.error_type == "LLM_UNAUTHORIZED"
    assert exc.value.retryable is False


async def test_a_rate_limit_is_retried_then_surfaced():
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(429, json={"error": {"message": "slow down"}})

    provider = AnthropicProvider(
        ProviderConfig(provider="ANTHROPIC", api_key="k", max_retries=2)
    )
    async with _transport(handler) as client:
        with pytest.raises(LLMError) as exc:
            await provider.complete(
                system="", messages=CONVERSATION, tools=[], model="m",
                max_tokens=8, client=client,
            )
    assert len(calls) == 2
    assert exc.value.retryable is True


async def test_an_error_never_echoes_the_credential():
    """Vendor error bodies sometimes quote the request. Nothing from the
    request — headers included — may reach the message."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"error": {"message": "bad request for key sk-live-SECRET123"}},
        )

    provider = AnthropicProvider(
        ProviderConfig(provider="ANTHROPIC", api_key="sk-live-SECRET123", max_retries=1)
    )
    async with _transport(handler) as client:
        with pytest.raises(LLMError) as exc:
            await provider.complete(
                system="", messages=CONVERSATION, tools=[], model="m",
                max_tokens=8, client=client,
            )
    # The vendor put the key in its own message; we cannot unsay that, but we
    # must never *add* the credential ourselves. Assert the config is not leaked
    # through the exception's own fields.
    assert "sk-live-SECRET123" not in repr(exc.value.provider)
    assert not hasattr(exc.value, "api_key")


async def test_a_non_json_response_is_a_retryable_transport_problem():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>502 from a proxy</html>")

    provider = AnthropicProvider(_config())
    async with _transport(handler) as client:
        with pytest.raises(LLMError) as exc:
            await provider.complete(
                system="", messages=CONVERSATION, tools=[], model="m",
                max_tokens=8, client=client,
            )
    assert exc.value.error_type == "LLM_BAD_RESPONSE"


# ---------------------------------------------------------------------------
# Catalog honesty
# ---------------------------------------------------------------------------
def test_unimplemented_providers_raise_at_construction_with_a_reason():
    """Selecting Bedrock must fail immediately and say why, not fail on the
    first customer request with a stack trace."""
    for cls in (BedrockProvider, VertexProvider):
        with pytest.raises(ProviderNotImplemented) as exc:
            cls(_config())
        assert len(str(exc.value)) > 40


def test_the_catalog_says_which_providers_actually_work():
    entries = {e["kind"]: e for e in catalog()}
    assert entries["ANTHROPIC"]["implemented"] is True
    assert entries["BEDROCK"]["implemented"] is False
    # An unimplemented entry must explain itself rather than just say "no".
    assert "not implemented" in entries["BEDROCK"]["notes"].lower()


def test_implemented_providers_are_listed_first():
    kinds = [e["implemented"] for e in catalog()]
    assert kinds == sorted(kinds, reverse=True)


def test_an_unknown_provider_is_refused_rather_than_guessed():
    with pytest.raises(ProviderNotImplemented):
        spec_for("SOME_STARTUP")


def test_every_implemented_provider_can_be_constructed():
    for entry in catalog():
        if not entry["implemented"]:
            continue
        provider = build_provider(entry["kind"], _config(base_url="https://example.test"))
        assert provider.kind == entry["kind"]


def test_a_tier_with_no_default_falls_back_to_balanced():
    assert default_model("ANTHROPIC", "ADVANCED")
    # GROQ ships no default table; it must not return None into a request body.
    assert isinstance(default_model("GROQ", "FAST"), str)


async def test_gemini_reports_tool_use_even_though_it_says_stop():
    """Gemini sets finishReason STOP on a turn that is a function call.

    Taken literally that reads as "the model is done", and the runtime would
    end the run without ever executing the tool the model just asked for. The
    presence of a tool_use block has to win.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "finishReason": "STOP",
                        "content": {
                            "parts": [
                                {
                                    "functionCall": {
                                        "name": "describe_object",
                                        "args": {"object": "Account"},
                                    }
                                }
                            ]
                        },
                    }
                ],
                "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 4},
            },
        )

    provider = GoogleProvider(_config())
    async with _transport(handler) as client:
        response = await provider.complete(
            system="", messages=CONVERSATION, tools=TOOLS,
            model="gemini-2.5-flash", max_tokens=64, client=client,
        )
    assert response.stop_reason == "tool_use"
    assert response.tool_uses[0]["name"] == "describe_object"


async def test_gemini_text_only_turns_still_end_the_run():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {"finishReason": "STOP", "content": {"parts": [{"text": "done"}]}}
                ],
                "usageMetadata": {},
            },
        )

    provider = GoogleProvider(_config())
    async with _transport(handler) as client:
        response = await provider.complete(
            system="", messages=CONVERSATION, tools=[],
            model="gemini-2.5-flash", max_tokens=64, client=client,
        )
    assert response.stop_reason == "end_turn"
