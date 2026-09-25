"""describe_object — schema inspection (LOW risk, read-only)."""

from __future__ import annotations

from typing import Any

from app.models import RiskLevel
from app.salesforce.errors import SalesforceError
from app.tools.base import Tool, ToolContext, ToolValidationError, ok
from app.tools.registry import registry

MAX_FIELDS_DEFAULT = 120
MAX_PICKLIST_VALUES = 50

DESCRIPTION = """Inspect the schema of a Salesforce object (sObject).

Use this BEFORE writing SOQL, before creating or updating records, and before
proposing any metadata change, so that field API names, types, required flags
and writability come from the real org instead of assumption.

Returns the object's label, permissions (createable/updateable/deletable),
its fields (API name, label, type, length, required, createable, updateable,
picklist values, reference targets) and its child relationships.

For wide objects, use `field_filter` to search field names/labels, or
`fields` to request specific fields, instead of pulling the whole schema.
"""

INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "object": {
            "type": "string",
            "description": "sObject API name, e.g. 'Account', 'Opportunity', 'My_Object__c'.",
        },
        "field_filter": {
            "type": "string",
            "description": "Case-insensitive substring to filter fields by API name or label.",
        },
        "fields": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Return only these specific field API names.",
        },
        "include_picklist_values": {
            "type": "boolean",
            "description": "Include picklist values (default true).",
        },
        "include_relationships": {
            "type": "boolean",
            "description": "Include child relationships (default false).",
        },
        "max_fields": {
            "type": "integer",
            "description": f"Maximum fields to return (default {MAX_FIELDS_DEFAULT}).",
        },
    },
    "required": ["object"],
    "additionalProperties": False,
}

OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "success": {"type": "boolean"},
        "object": {"type": "string"},
        "label": {"type": "string"},
        "custom": {"type": "boolean"},
        "permissions": {"type": "object"},
        "field_count": {"type": "integer"},
        "returned_field_count": {"type": "integer"},
        "fields": {"type": "array"},
        "child_relationships": {"type": "array"},
        "truncated": {"type": "boolean"},
    },
}


def summarize_field(f: dict[str, Any], include_picklists: bool = True) -> dict[str, Any]:
    out: dict[str, Any] = {
        "name": f.get("name"),
        "label": f.get("label"),
        "type": f.get("type"),
        "required": (not f.get("nillable", True))
        and f.get("createable", False)
        and not f.get("defaultedOnCreate", False),
        "createable": f.get("createable", False),
        "updateable": f.get("updateable", False),
        "unique": f.get("unique", False),
        "custom": f.get("custom", False),
    }
    if f.get("length"):
        out["length"] = f["length"]
    if f.get("type") in {"double", "currency", "percent", "int"}:
        out["precision"] = f.get("precision")
        out["scale"] = f.get("scale")
    if f.get("type") == "reference":
        out["references"] = f.get("referenceTo") or []
        out["relationship_name"] = f.get("relationshipName")
    if include_picklists and f.get("picklistValues"):
        values = [
            {"value": p["value"], "label": p.get("label"), "active": p.get("active", True)}
            for p in f["picklistValues"][:MAX_PICKLIST_VALUES]
        ]
        out["picklist_values"] = values
        out["picklist_truncated"] = len(f["picklistValues"]) > MAX_PICKLIST_VALUES
    if f.get("calculated"):
        out["formula"] = True
    if f.get("inlineHelpText"):
        out["help_text"] = f["inlineHelpText"]
    return out


async def _validate(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    name = str(args.get("object") or "").strip()
    if not name:
        raise ToolValidationError("`object` is required.", "MISSING_ARGUMENT")
    if not name.replace("_", "").isalnum():
        raise ToolValidationError(
            f"'{name}' is not a valid sObject API name.",
            "INVALID_OBJECT_NAME",
            "Use the API name, e.g. Account or Custom_Object__c.",
        )
    return {"object": name}


async def _execute(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    sf = ctx.require_sf()
    name = str(args["object"]).strip()
    try:
        data = await sf.describe(name)
    except SalesforceError as exc:
        if exc.error_type in {"NOT_FOUND", "INVALID_TYPE"} or exc.status_code == 404:
            similar = await _suggest_objects(sf, name)
            return {
                **exc.to_dict(),
                "message": f"Object '{name}' was not found or is not accessible.",
                "similar_objects": similar,
                "suggested_action": (
                    "Use one of similar_objects, or confirm the exact API name with "
                    "the user."
                ),
            }
        raise

    fields = data.get("fields") or []
    include_pick = args.get("include_picklist_values", True)
    wanted = {f.lower() for f in (args.get("fields") or [])}
    flt = str(args.get("field_filter") or "").lower()
    max_fields = int(args.get("max_fields") or MAX_FIELDS_DEFAULT)

    selected = []
    for f in fields:
        fname = str(f.get("name", "")).lower()
        flabel = str(f.get("label", "")).lower()
        if wanted and fname not in wanted:
            continue
        if flt and flt not in fname and flt not in flabel:
            continue
        selected.append(f)

    truncated = len(selected) > max_fields
    summarized = [summarize_field(f, include_pick) for f in selected[:max_fields]]

    result = ok(
        object=data.get("name"),
        label=data.get("label"),
        label_plural=data.get("labelPlural"),
        custom=data.get("custom", False),
        key_prefix=data.get("keyPrefix"),
        permissions={
            "createable": data.get("createable"),
            "updateable": data.get("updateable"),
            "deletable": data.get("deletable"),
            "queryable": data.get("queryable"),
            "layoutable": data.get("layoutable"),
        },
        field_count=len(fields),
        returned_field_count=len(summarized),
        truncated=truncated,
        fields=summarized,
    )
    if truncated:
        result["truncation_note"] = (
            f"{len(selected)} fields matched; {max_fields} returned. Narrow with "
            "`field_filter` or `fields`."
        )
    if args.get("include_relationships"):
        result["child_relationships"] = [
            {
                "child_object": r.get("childSObject"),
                "field": r.get("field"),
                "relationship_name": r.get("relationshipName"),
            }
            for r in (data.get("childRelationships") or [])
            if r.get("relationshipName")
        ][:80]
    return result


async def _suggest_objects(sf: Any, name: str) -> list[str]:
    try:
        listing = await sf.describe_global()
    except Exception:  # pragma: no cover - best effort
        return []
    lowered = name.lower().rstrip("s")
    out = []
    for s in listing.get("sobjects") or []:
        api = str(s.get("name", ""))
        if lowered in api.lower() or lowered in str(s.get("label", "")).lower():
            out.append(api)
        if len(out) >= 10:
            break
    return out


TOOL = registry.register(
    Tool(
        name="describe_object",
        description=DESCRIPTION,
        input_schema=INPUT_SCHEMA,
        output_schema=OUTPUT_SCHEMA,
        risk=RiskLevel.LOW,
        requires_approval=False,
        execute=_execute,
        validate=_validate,
        mutating=False,
        audit_action="salesforce.describe_object",
        tags=["schema", "read"],
    )
)
