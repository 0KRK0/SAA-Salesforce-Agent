"""Dependency and impact analysis tools.

Answers "what breaks if I change or delete this" before the change is proposed,
using two independent methods and reporting which found what. See
app/salesforce/dependencies.py for why both are needed.
"""

from __future__ import annotations

from typing import Any

from app.models import RiskLevel
from app.salesforce.dependencies import (
    analyze_component,
    analyze_field,
    referencing_objects,
)
from app.salesforce.errors import SalesforceError
from app.tools.base import Tool, ToolContext, fail, ok
from app.tools.registry import registry

DESCRIPTION = """Find everything in the org that depends on a field, flow, Apex class
or object — before you change or delete it.

Answers "can I safely delete Customer_Tier__c?" with an evidence-backed impact
rating and a component-by-component list.

Two independent methods run and are reported separately:
  * the Salesforce Dependency API (authoritative, but not available in every
    org and blind to some reference types);
  * a source scan of flow metadata, Apex bodies, validation-rule formulas and
    report definitions.

Read the result carefully before telling anyone something is safe to delete:
"no dependencies found" and "the Dependency API was unavailable" are different
answers, and the result distinguishes them.

Always run this before proposing a delete, and before changing a field's type.
"""


async def _execute(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    sf = ctx.require_sf()
    component_type = str(args.get("component_type") or "field").lower()

    try:
        if component_type == "field":
            object_name = str(args.get("object") or "").strip()
            field_name = str(args.get("name") or "").strip()
            if not object_name or not field_name:
                return fail(
                    "MISSING_ARGUMENT",
                    "Analyzing a field needs both `object` and `name`.",
                )
            await ctx.emit(
                "analysis.dependencies",
                {"target": f"{object_name}.{field_name}", "type": "field"},
            )
            report = await analyze_field(
                sf,
                object_name,
                field_name,
                scan_source=bool(args.get("scan_source", True)),
            )
            return ok(**report.to_dict())

        if component_type == "object":
            object_name = str(args.get("name") or args.get("object") or "").strip()
            if not object_name:
                return fail("MISSING_ARGUMENT", "`name` is required.")
            report = await analyze_component(sf, object_name, "CustomObject")
            payload = report.to_dict()
            payload["referencing_relationships"] = await referencing_objects(sf, object_name)
            return ok(**payload)

        name = str(args.get("name") or "").strip()
        if not name:
            return fail("MISSING_ARGUMENT", "`name` is required.")
        type_map = {
            "flow": "Flow",
            "apex": "ApexClass",
            "apexclass": "ApexClass",
            "trigger": "ApexTrigger",
        }
        report = await analyze_component(sf, name, type_map.get(component_type, "ApexClass"))
        return ok(**report.to_dict())
    except SalesforceError as exc:
        return exc.to_dict()


registry.register(
    Tool(
        name="analyze_dependencies",
        description=DESCRIPTION,
        input_schema={
            "type": "object",
            "properties": {
                "component_type": {
                    "type": "string",
                    "enum": ["field", "flow", "apex", "trigger", "object"],
                    "default": "field",
                },
                "object": {
                    "type": "string",
                    "description": "Required when component_type is 'field'.",
                },
                "name": {
                    "type": "string",
                    "description": "Field API name, flow API name, class name or object.",
                },
                "scan_source": {
                    "type": "boolean",
                    "default": True,
                    "description": "Also scan flow/Apex/validation source. Slower, more complete.",
                },
            },
            "required": ["name"],
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "properties": {
                "success": {"type": "boolean"},
                "impact": {"type": "string"},
                "dependency_count": {"type": "integer"},
                "recommendation": {"type": "string"},
            },
        },
        risk=RiskLevel.LOW,
        requires_approval=False,
        execute=_execute,
        audit_action="salesforce.analyze_dependencies",
        tags=["metadata", "read", "analysis"],
        long_running=True,
    )
)
