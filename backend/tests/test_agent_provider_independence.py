"""The agent, end to end, over each provider's real wire format.

Every other agent test stubs the gateway. These do not: the runtime calls the
real gateway, which resolves a real credential, builds a real provider, and
speaks the vendor's actual JSON over a mocked transport.

That matters because "model-independent" is a claim that can only be checked
one way — by running the same agent behaviour against different wire formats
and getting the same result. If OpenAI's `tool_calls` shape or Gemini's
`functionCall` shape were translated wrongly, every other test would still pass
and the product would be broken for those customers.
"""

from __future__ import annotations

import json

import httpx
import pytest
import pytest_asyncio

from app.agent.runtime import AgentRuntime
from app.models import AgentRun, LLMCredential, LLMProviderKind, LLMUsage, RunState
from app.salesforce.client import SalesforceClient
from app.security.secrets import SecretContext, store_secret


async def collect(gen) -> list[dict]:
    return [event async for event in gen]


@pytest_asyncio.fixture
async def credential_factory(db, company, project):
    async def _make(provider: LLMProviderKind, base_url: str | None = None):
        row = LLMCredential(
            company_id=company.id,
            project_id=project.id,
            name=f"{provider.value.lower()}-key",
            provider=provider,
            secret_ref=store_secret(
                "sk-test", SecretContext(company.id, project.id, "llm_key")
            ),
            base_url=base_url,
            is_default=True,
        )
        db.add(row)
        await db.commit()
        return row

    return _make


@pytest.fixture
def wire(monkeypatch, fake_sf):
    """Route the gateway's own HTTP through a scripted transport.

    The runtime does not accept an injected client — it should not have to — so
    the transport is swapped underneath `httpx.AsyncClient` inside the provider
    layer. Nothing in the agent, the gateway or the providers is stubbed.
    """

    def _apply(handler):
        # Build the Salesforce transport FIRST. Patching httpx.AsyncClient
        # replaces it process-wide, so a client constructed afterwards would
        # silently get the model transport instead of the Salesforce one.
        http = fake_sf.client()
        monkeypatch.setattr(
            "app.agent.runtime.SalesforceClient",
            lambda connection, db=None, **_: SalesforceClient(connection, db, http=http),
        )

        class _Client(httpx.AsyncClient):
            def __init__(self, *args, **kwargs):
                kwargs["transport"] = httpx.MockTransport(handler)
                super().__init__(*args, **kwargs)

        monkeypatch.setattr("app.llm.base.httpx.AsyncClient", _Client)

    return _apply


def _script(responses: list[dict]):
    """Serve one scripted body per model call, in order."""
    remaining = list(responses)
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        body = remaining.pop(0) if remaining else responses[-1]
        return httpx.Response(200, json=body)

    handler.seen = seen  # type: ignore[attr-defined]
    return handler


# ---------------------------------------------------------------------------
# Anthropic
# ---------------------------------------------------------------------------
async def test_the_agent_runs_a_tool_over_the_anthropic_wire_format(
    db, user, conversation, connection, credential_factory, wire
):
    await credential_factory(LLMProviderKind.ANTHROPIC)
    handler = _script(
        [
            {
                "model": "claude-sonnet-4-5",
                "stop_reason": "tool_use",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "describe_object",
                        "input": {"object": "Account"},
                    }
                ],
                "usage": {"input_tokens": 100, "output_tokens": 20},
            },
            {
                "model": "claude-sonnet-4-5",
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": "Account has 4 fields."}],
                "usage": {"input_tokens": 200, "output_tokens": 15},
            },
        ]
    )
    wire(handler)

    events = await collect(
        AgentRuntime(db, user, conversation, connection).start("Describe Account")
    )
    kinds = [e["type"] for e in events]
    assert "tool.started" in kinds
    assert "run.completed" in kinds

    run = (await db.execute(__import__("sqlalchemy").select(AgentRun))).scalars().one()
    assert run.state is RunState.COMPLETED
    assert run.llm_provider == "ANTHROPIC"
    assert run.input_tokens == 300


# ---------------------------------------------------------------------------
# OpenAI
# ---------------------------------------------------------------------------
async def test_the_same_agent_behaviour_over_the_openai_wire_format(
    db, user, conversation, connection, credential_factory, wire
):
    """Identical agent behaviour, entirely different JSON on the wire."""
    await credential_factory(LLMProviderKind.OPENAI)
    handler = _script(
        [
            {
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
                                    "type": "function",
                                    "function": {
                                        "name": "describe_object",
                                        "arguments": '{"object": "Account"}',
                                    },
                                }
                            ],
                        },
                    }
                ],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20},
            },
            {
                "model": "gpt-4o",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "Account has 4 fields."},
                    }
                ],
                "usage": {"prompt_tokens": 200, "completion_tokens": 15},
            },
        ]
    )
    wire(handler)

    events = await collect(
        AgentRuntime(db, user, conversation, connection).start("Describe Account")
    )
    assert "tool.started" in [e["type"] for e in events]
    assert "run.completed" in [e["type"] for e in events]

    run = (await db.execute(__import__("sqlalchemy").select(AgentRun))).scalars().one()
    assert run.state is RunState.COMPLETED
    assert run.llm_provider == "OPENAI"

    # The second request must carry the tool result back in OpenAI's shape, or
    # the model has no idea what the tool returned.
    second = handler.seen[1]
    tool_messages = [m for m in second["messages"] if m.get("role") == "tool"]
    assert tool_messages
    assert tool_messages[0]["tool_call_id"] == "call_1"
    assert "Account" in tool_messages[0]["content"]


async def test_the_untrusted_boundary_survives_translation_to_openai(
    db, user, conversation, connection, credential_factory, wire
):
    """Salesforce data is wrapped in an untrusted-data boundary. If translation
    dropped it, org content would reach the model as instructions."""
    from app.agent.context import UNTRUSTED_HEADER

    await credential_factory(LLMProviderKind.OPENAI)
    handler = _script(
        [
            {
                "model": "gpt-4o",
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "function": {
                                        "name": "describe_object",
                                        "arguments": '{"object": "Account"}',
                                    },
                                }
                            ]
                        },
                    }
                ],
                "usage": {},
            },
            {
                "model": "gpt-4o",
                "choices": [{"finish_reason": "stop", "message": {"content": "done"}}],
                "usage": {},
            },
        ]
    )
    wire(handler)
    await collect(
        AgentRuntime(db, user, conversation, connection).start("Describe Account")
    )

    tool_message = next(
        m for m in handler.seen[1]["messages"] if m.get("role") == "tool"
    )
    assert UNTRUSTED_HEADER in tool_message["content"]


# ---------------------------------------------------------------------------
# Google
# ---------------------------------------------------------------------------
async def test_the_same_agent_behaviour_over_the_gemini_wire_format(
    db, user, conversation, connection, credential_factory, wire
):
    await credential_factory(LLMProviderKind.GOOGLE)
    handler = _script(
        [
            {
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
                "usageMetadata": {"promptTokenCount": 90, "candidatesTokenCount": 10},
            },
            {
                "candidates": [
                    {
                        "finishReason": "STOP",
                        "content": {"parts": [{"text": "Account has 4 fields."}]},
                    }
                ],
                "usageMetadata": {"promptTokenCount": 150, "candidatesTokenCount": 8},
            },
        ]
    )
    wire(handler)

    events = await collect(
        AgentRuntime(db, user, conversation, connection).start("Describe Account")
    )
    assert "tool.started" in [e["type"] for e in events]

    run = (await db.execute(__import__("sqlalchemy").select(AgentRun))).scalars().one()
    assert run.llm_provider == "GOOGLE"

    # Gemini rejects unknown JSON Schema keywords and names no tool when it
    # does, so the declarations must already be cleaned by the time they ship.
    declarations = handler.seen[0]["tools"][0]["functionDeclarations"]
    for declaration in declarations:
        assert "additionalProperties" not in json.dumps(declaration)
        assert "$schema" not in json.dumps(declaration)


# ---------------------------------------------------------------------------
# Accounting and failure
# ---------------------------------------------------------------------------
async def test_every_model_call_in_a_run_is_accounted_for(
    db, user, conversation, connection, credential_factory, wire
):
    from sqlalchemy import select

    await credential_factory(LLMProviderKind.ANTHROPIC)
    handler = _script(
        [
            {
                "model": "claude-sonnet-4-5",
                "stop_reason": "tool_use",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "describe_object",
                        "input": {"object": "Account"},
                    }
                ],
                "usage": {"input_tokens": 100, "output_tokens": 20},
            },
            {
                "model": "claude-sonnet-4-5",
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": "Done."}],
                "usage": {"input_tokens": 200, "output_tokens": 15},
            },
        ]
    )
    wire(handler)
    await collect(
        AgentRuntime(db, user, conversation, connection).start("Describe Account")
    )

    rows = (await db.execute(select(LLMUsage))).scalars().all()
    assert len(rows) == 2
    assert all(r.project_id == conversation.project_id for r in rows)
    assert all(r.agent_run_id for r in rows)
    assert sum(r.input_tokens for r in rows) == 300


async def test_a_project_with_no_model_gets_a_run_failure_that_says_what_to_do(
    db, user, conversation, connection, wire, monkeypatch
):
    """The most common first-run failure. It must name the fix, not stack-trace."""
    from app.config import settings

    monkeypatch.setattr(settings, "feature_platform_managed_ai", False)
    wire(lambda request: httpx.Response(500))

    events = await collect(
        AgentRuntime(db, user, conversation, connection).start("Describe Account")
    )
    errors = [e for e in events if e["type"] == "error"]
    assert errors
    assert "api key" in errors[0]["data"]["message"].lower()

    run = (await db.execute(__import__("sqlalchemy").select(AgentRun))).scalars().one()
    assert run.state is RunState.FAILED
    assert run.error_code == "NO_MODEL_AVAILABLE"
