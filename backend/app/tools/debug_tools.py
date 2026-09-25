"""Org debugger: evidence-backed answers to "why did Salesforce do that?"

Handles the questions admins actually get stuck on:
  "Why isn't this Opportunity being assigned?"
  "Why didn't this Flow run?"
  "Why can't this user edit this field?"
  "Why does this field keep getting overwritten?"

The tool collects evidence from the org first and diagnoses second. Findings
cite the observation that supports them and carry an explicit confidence level,
so the answer can be checked rather than believed.
"""

from __future__ import annotations

from typing import Any

from app.diagnostics.collector import collect
from app.diagnostics.diagnose import diagnose
from app.models import RiskLevel
from app.salesforce.errors import SalesforceError
from app.tools.base import Tool, ToolContext, fail, ok
from app.tools.registry import registry

DESCRIPTION = """Investigate why a record or object behaves the way it does.

Give it the object, the question, and — whenever you have one — a specific
record id. A record id turns a survey of the configuration into an
investigation of an actual case, including field history and ownership.

It inspects: object and field metadata, active flows and their real logic, Apex
triggers and their source, validation rules and their formulas, record types,
assignment rules, field-level security, object permissions, ownership and field
history. It then returns:

  * observed facts — what the org actually shows, by area;
  * findings — candidate causes, each citing its evidence, ranked by confidence;
  * areas it could not inspect, so a gap is never mistaken for an all-clear.

Read the findings and the observations together. Where the evidence supports a
conclusion, say so and cite it. Where it does not, say that instead of guessing
— a confident wrong answer sends someone looking in the wrong place.

Pass `field` when the question is about one field, and `user_id` when it is
about what a particular person can do.
"""


async def _execute(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    sf = ctx.require_sf()
    object_name = str(args.get("object") or "").strip()
    if not object_name:
        return fail(
            "MISSING_ARGUMENT",
            "`object` is required — say which object the behaviour is about.",
        )

    question = str(args.get("question") or "")
    await ctx.emit(
        "debug.collecting",
        {
            "object": object_name,
            "record_id": args.get("record_id"),
            "field": args.get("field"),
        },
    )
    try:
        evidence = await collect(
            sf,
            object_name=object_name,
            record_id=str(args.get("record_id") or "") or None,
            field_name=str(args.get("field") or "") or None,
            user_id=str(args.get("user_id") or "") or None,
            include_history=bool(args.get("include_history", True)),
        )
    except SalesforceError as exc:
        return exc.to_dict()

    await ctx.emit(
        "debug.diagnosing", {"observations": len(evidence.observations)}
    )
    result = diagnose(evidence, question)
    return ok(
        **result,
        note=(
            "These findings come from configuration the org actually reports. Where "
            "confidence is medium or low, say so to the user rather than presenting a "
            "candidate as the answer."
        ),
    )


registry.register(
    Tool(
        name="debug_org_behaviour",
        description=DESCRIPTION,
        input_schema={
            "type": "object",
            "properties": {
                "object": {"type": "string", "description": "Object API name."},
                "question": {
                    "type": "string",
                    "description": "The user's question, in their words.",
                },
                "record_id": {
                    "type": "string",
                    "description": "A specific record exhibiting the behaviour.",
                },
                "field": {
                    "type": "string",
                    "description": "Field API name, when the question is about one field.",
                },
                "user_id": {
                    "type": "string",
                    "description": "Salesforce user id, when the question is about access.",
                },
                "include_history": {"type": "boolean", "default": True},
            },
            "required": ["object"],
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "properties": {
                "success": {"type": "boolean"},
                "findings": {"type": "array"},
                "overall_confidence": {"type": "string"},
                "verdict": {"type": "string"},
            },
        },
        risk=RiskLevel.LOW,
        requires_approval=False,
        execute=_execute,
        audit_action="salesforce.debug_org_behaviour",
        tags=["diagnostics", "read"],
        long_running=True,
    )
)
