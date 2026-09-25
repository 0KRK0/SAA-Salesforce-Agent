"""create_record — create a single Salesforce record (MEDIUM risk)."""

from __future__ import annotations

from typing import Any

from app.models import RiskLevel
from app.salesforce.errors import SalesforceError
from app.tools.base import Tool, ToolContext, ToolValidationError, ok
from app.tools.registry import registry

DESCRIPTION = """Create one record in the connected Salesforce org.

Before calling this you must know the object's writable and required fields —
call describe_object first. The runtime validates every supplied field against
the live schema (existence, createable, picklist membership) and rejects the
call if a required createable field is missing, so that the user is asked
rather than a bad record being written.

This is a mutation: it is risk-classified and may require explicit human
approval before it executes.
"""

INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "object": {"type": "string", "description": "sObject API name, e.g. 'Account'."},
        "values": {
            "type": "object",
            "description": "Field API name -> value map for the new record.",
            "additionalProperties": True,
        },
        "reason": {
            "type": "string",
            "description": "Short business reason for the change, shown on the approval card.",
        },
    },
    "required": ["object", "values"],
    "additionalProperties": False,
}

OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "success": {"type": "boolean"},
        "object": {"type": "string"},
        "record_id": {"type": "string"},
        "verified": {"type": "boolean"},
        "record": {"type": "object"},
    },
}


async def _schema_check(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    sf = ctx.require_sf()
    obj = str(args.get("object") or "").strip()
    values = args.get("values") or {}
    if not obj:
        raise ToolValidationError("`object` is required.", "MISSING_ARGUMENT")
    if not isinstance(values, dict) or not values:
        raise ToolValidationError(
            "`values` must be a non-empty object of field -> value.", "MISSING_ARGUMENT"
        )

    try:
        desc = await sf.describe(obj)
    except SalesforceError as exc:
        raise ToolValidationError(
            f"Object '{obj}' could not be described: {exc.message}",
            exc.error_type,
            "Confirm the object API name with describe_object.",
        ) from exc

    if not desc.get("createable"):
        raise ToolValidationError(
            f"The connected Salesforce user cannot create {obj} records.",
            "INSUFFICIENT_ACCESS",
            "Report the permission gap to the user.",
        )

    by_name = {f["name"].lower(): f for f in desc.get("fields") or []}
    unknown, not_writable, bad_picklists = [], [], []
    for key, value in values.items():
        f = by_name.get(str(key).lower())
        if f is None:
            unknown.append(key)
            continue
        if not f.get("createable"):
            not_writable.append(key)
            continue
        if f.get("type") in {"picklist", "multipicklist"} and f.get("picklistValues"):
            allowed = {p["value"] for p in f["picklistValues"] if p.get("active", True)}
            candidates = (
                [v.strip() for v in str(value).split(";")]
                if f["type"] == "multipicklist"
                else [str(value)]
            )
            invalid = [c for c in candidates if c and c not in allowed]
            if invalid and f.get("restrictedPicklist"):
                bad_picklists.append(
                    {"field": key, "invalid": invalid, "allowed": sorted(allowed)[:30]}
                )

    if unknown:
        raise ToolValidationError(
            f"Unknown field(s) on {obj}: {', '.join(unknown)}.",
            "INVALID_FIELD",
            f"Call describe_object('{obj}') and use only the field API names it returns.",
            unknown,
        )
    if not_writable:
        raise ToolValidationError(
            f"Field(s) not createable on {obj}: {', '.join(not_writable)}.",
            "INVALID_FIELD_FOR_INSERT_UPDATE",
            "Remove these fields or pick a writable alternative.",
            not_writable,
        )
    if bad_picklists:
        detail = "; ".join(
            f"{b['field']}: {b['invalid']} not in {b['allowed']}" for b in bad_picklists
        )
        raise ToolValidationError(
            f"Invalid restricted picklist value(s): {detail}",
            "FIELD_INTEGRITY_EXCEPTION",
            "Use one of the allowed values, or ask the user which value applies.",
        )

    required_missing = [
        f["name"]
        for f in desc.get("fields") or []
        if f.get("createable")
        and not f.get("nillable", True)
        and not f.get("defaultedOnCreate", False)
        and f["name"].lower() not in {k.lower() for k in values}
    ]
    if required_missing:
        raise ToolValidationError(
            f"Required field(s) missing for {obj}: {', '.join(required_missing)}.",
            "REQUIRED_FIELD_MISSING",
            "Ask the user for these values before creating the record.",
            required_missing,
        )
    return {"object": desc.get("name", obj), "fields_validated": list(values)}


async def _plan(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    obj = args.get("object")
    values = args.get("values") or {}
    return {
        "title": f"Create {obj} record",
        "object": obj,
        "change_type": "record.create",
        "summary": f"Creates one new {obj} record.",
        "details": [{"field": k, "new_value": v} for k, v in values.items()],
        "impact": f"Adds 1 record to {obj}. Reversible by deleting the record.",
        "reason": args.get("reason", ""),
    }


async def _execute(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    sf = ctx.require_sf()
    obj = str(args["object"]).strip()
    values = dict(args["values"])
    try:
        result = await sf.create_record(obj, values)
    except SalesforceError as exc:
        return exc.to_dict() | {"object": obj}
    record_id = result.get("id")
    if not record_id:
        return {
            "success": False,
            "error_type": "CREATE_NO_ID",
            "message": "Salesforce accepted the request but returned no record id.",
            "retryable": False,
            "raw": result,
        }
    return ok(object=obj, record_id=record_id, requested_values=values)


async def _verify(
    ctx: ToolContext, args: dict[str, Any], result: dict[str, Any]
) -> dict[str, Any]:
    if not result.get("success") or not result.get("record_id"):
        return {"verified": False, "reason": "No record id to verify."}
    sf = ctx.require_sf()
    obj = str(args["object"]).strip()
    fields = list((args.get("values") or {}).keys())
    try:
        record = await sf.get_record(obj, result["record_id"], fields=["Id", *fields])
    except SalesforceError as exc:
        return {"verified": False, "reason": exc.message}
    mismatches = [
        {"field": k, "expected": v, "actual": record.get(k)}
        for k, v in (args.get("values") or {}).items()
        if k in record and str(record.get(k)) != str(v)
    ]
    return {
        "verified": not mismatches,
        "record": {k: v for k, v in record.items() if k != "attributes"},
        "mismatches": mismatches,
        "method": "GET sobjects/{object}/{id} after create",
    }


TOOL = registry.register(
    Tool(
        name="create_record",
        description=DESCRIPTION,
        input_schema=INPUT_SCHEMA,
        output_schema=OUTPUT_SCHEMA,
        risk=RiskLevel.MEDIUM,
        requires_approval=True,
        execute=_execute,
        validate=_schema_check,
        plan=_plan,
        verify=_verify,
        mutating=True,
        audit_action="salesforce.create_record",
        tags=["write", "data"],
    )
)
