"""Org knowledge tools: recall what earlier work found, record what matters.

The runtime already injects the most relevant knowledge into the system prompt
at the start of a run. These tools are for the cases that needs more: pulling
the org's deployment and failure history when planning a change, and recording
a durable fact a human has stated.
"""

from __future__ import annotations

from typing import Any

from app.knowledge import store
from app.models import KnowledgeKind, RiskLevel
from app.tools.base import Tool, ToolContext, fail, ok
from app.tools.registry import registry

_KINDS = [k.value for k in KnowledgeKind]


async def _recall_execute(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    if ctx.connection is None:
        return fail("NO_SALESFORCE_CONNECTION", "No Salesforce org is connected.")

    kinds = [KnowledgeKind(k) for k in (args.get("kinds") or []) if k in _KINDS]
    query = str(args.get("query") or "").strip()
    if query:
        entries = await store.recall(
            ctx.db,
            salesforce_connection_id=ctx.connection.id,
            query=query,
            kinds=kinds or None,
            limit=int(args.get("limit") or 25),
        )
    else:
        entries = await store.recent(
            ctx.db,
            salesforce_connection_id=ctx.connection.id,
            kinds=kinds or None,
            limit=int(args.get("limit") or 20),
        )
    return ok(
        count=len(entries),
        entries=entries,
        note=(
            "These are observations from earlier work on this org. They can be stale — "
            "inspect the org before acting on any of them."
        ),
    )


registry.register(
    Tool(
        name="recall_org_knowledge",
        description=(
            "Recall what earlier work found out about this org.\n\n"
            "Most useful before proposing a change: past deployment failures, patterns "
            "that were approved before, and diagnoses already reached are all here. "
            "Pass a `query` to search, or leave it empty for the most recent entries.\n\n"
            "These are hints about where to look, not current facts. Anything you act "
            "on must be re-checked against the org."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "kinds": {"type": "array", "items": {"type": "string", "enum": _KINDS}},
                "limit": {"type": "integer", "default": 20},
            },
            "additionalProperties": False,
        },
        output_schema={"type": "object", "properties": {"success": {"type": "boolean"}}},
        risk=RiskLevel.LOW,
        requires_approval=False,
        execute=_recall_execute,
        audit_action="knowledge.recall",
        tags=["knowledge", "read"],
    )
)


async def _remember_execute(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    if ctx.connection is None:
        return fail("NO_SALESFORCE_CONNECTION", "No Salesforce org is connected.")
    key = str(args.get("key") or "").strip()
    summary = str(args.get("summary") or "").strip()
    if not key or not summary:
        return fail("MISSING_ARGUMENT", "`key` and `summary` are both required.")

    kind = KnowledgeKind(str(args.get("kind") or "PATTERN"))
    row = await store.remember(
        ctx.db,
        company_id=ctx.company_id,
        project_id=ctx.project_id,
        salesforce_connection_id=ctx.connection.id,
        kind=kind,
        key=key,
        summary=summary,
        data=args.get("data"),
        # Sourced from the human's statement in this conversation, which is the
        # only non-org input this store accepts.
        source="user",
    )
    return ok(
        stored=True,
        kind=kind.value,
        key=row.key,
        message=(
            f"Recorded against this org: {summary}. It will be recalled in future "
            "conversations about this org."
        ),
    )


registry.register(
    Tool(
        name="remember_about_org",
        description=(
            "Record a durable fact about this org that a human has told you.\n\n"
            "For conventions and constraints that are not discoverable from metadata: "
            "\"deployments here go through the release manager on Thursdays\", \"the "
            "Tier field is maintained by the billing integration, do not automate it\".\n\n"
            "Only record what the user actually said. Do not record your own "
            "conclusions or anything you inferred — knowledge the agent invents is how "
            "an agent talks itself into a mistake and then repeats it."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": _KINDS, "default": "PATTERN"},
                "key": {
                    "type": "string",
                    "description": "Short identifier, e.g. 'release process'.",
                },
                "summary": {"type": "string", "description": "One or two sentences."},
                "data": {"type": "object"},
            },
            "required": ["key", "summary"],
            "additionalProperties": False,
        },
        output_schema={"type": "object", "properties": {"success": {"type": "boolean"}}},
        risk=RiskLevel.LOW,
        requires_approval=False,
        execute=_remember_execute,
        audit_action="knowledge.remember",
        tags=["knowledge", "write"],
    )
)
