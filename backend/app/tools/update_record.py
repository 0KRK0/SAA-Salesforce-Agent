"""update_record — update one existing Salesforce record (MEDIUM risk).

Captures before-values so the approval card shows a real diff and the audit
log records old -> new.
"""

from __future__ import annotations

from typing import Any

from app.models import RiskLevel
from app.salesforce.errors import SalesforceError
from app.tools.base import Tool, ToolContext, ToolValidationError, ok
from app.tools.registry import registry

DESCRIPTION = """Update fields on one existing Salesforce record.

Supply the record id and the fields to change. The runtime reads the record's
current values first, validates every field against the live schema
(existence, updateable, picklist membership), and shows the user a
before/after diff on the approval card.

Call describe_object and/or query_salesforce first if you need to confirm the
record id or the field API names.

This is a mutation: it is risk-classified and may require explicit human
approval before it executes.
"""

INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "object": {"type": "string", "description": "sObject API name."},
        "record_id": {"type": "string", "description": "15- or 18-character record id."},
        "values": {
            "type": "object",
            "description": "Field API name -> new value.",
            "additionalProperties": True,
        },
        "reason": {"type": "string", "description": "Short business reason for the change."},
    },
    "required": ["object", "record_id", "values"],
    "additionalProperties": False,
}

OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "success": {"type": "boolean"},
        "object": {"type": "string"},
        "record_id": {"type": "string"},
        "before": {"type": "object"},
        "after": {"type": "object"},
        "verified": {"type": "boolean"},
    },
}


def _valid_id(value: str) -> bool:
    return len(value) in (15, 18) and value.isalnum()


async def _validate(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    sf = ctx.require_sf()
    obj = str(args.get("object") or "").strip()
    rid = str(args.get("record_id") or "").strip()
    values = args.get("values") or {}
    if not obj or not rid:
        raise ToolValidationError("`object` and `record_id` are required.", "MISSING_ARGUMENT")
    if not _valid_id(rid):
        raise ToolValidationError(
            f"'{rid}' is not a valid Salesforce record id.",
            "INVALID_ID",
            "Query the record first to obtain its Id.",
        )
    if not isinstance(values, dict) or not values:
        raise ToolValidationError("`values` must be a non-empty object.", "MISSING_ARGUMENT")

    desc = await sf.describe(obj)
    by_name = {f["name"].lower(): f for f in desc.get("fields") or []}
    unknown = [k for k in values if str(k).lower() not in by_name]
    if unknown:
        raise ToolValidationError(
            f"Unknown field(s) on {obj}: {', '.join(unknown)}.",
            "INVALID_FIELD",
            f"Call describe_object('{obj}') for valid field API names.",
            unknown,
        )
    read_only = [
        k for k in values if not by_name[str(k).lower()].get("updateable", False)
    ]
    if read_only:
        raise ToolValidationError(
            f"Field(s) not updateable on {obj}: {', '.join(read_only)}.",
            "INVALID_FIELD_FOR_INSERT_UPDATE",
            "These are read-only/formula/system fields; choose a writable field.",
            read_only,
        )
    for key, value in values.items():
        f = by_name[str(key).lower()]
        if (
            f.get("type") in {"picklist", "multipicklist"}
            and f.get("restrictedPicklist")
            and f.get("picklistValues")
        ):
            allowed = {p["value"] for p in f["picklistValues"] if p.get("active", True)}
            candidates = (
                [v.strip() for v in str(value).split(";")]
                if f["type"] == "multipicklist"
                else [str(value)]
            )
            invalid = [c for c in candidates if c and c not in allowed]
            if invalid:
                raise ToolValidationError(
                    f"Invalid value(s) {invalid} for restricted picklist {key}.",
                    "FIELD_INTEGRITY_EXCEPTION",
                    f"Allowed values: {sorted(allowed)[:30]}",
                )

    # Verify the record exists and is visible before proposing a change.
    try:
        await sf.get_record(obj, rid, fields=["Id"])
    except SalesforceError as exc:
        raise ToolValidationError(
            f"Record {rid} on {obj} is not accessible: {exc.message}",
            exc.error_type,
            exc.suggested_action,
        ) from exc
    return {"object": desc.get("name", obj), "record_id": rid}


async def _before_values(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    sf = ctx.require_sf()
    fields = ["Id", *list((args.get("values") or {}).keys())]
    try:
        record = await sf.get_record(str(args["object"]), str(args["record_id"]), fields=fields)
        return {k: v for k, v in record.items() if k != "attributes"}
    except SalesforceError:
        return {}


async def _plan(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    before = await _before_values(ctx, args)
    values = args.get("values") or {}
    return {
        "title": f"Update {args.get('object')} record",
        "object": args.get("object"),
        "record_id": args.get("record_id"),
        "change_type": "record.update",
        "summary": f"Changes {len(values)} field(s) on 1 record.",
        "details": [
            {"field": k, "old_value": before.get(k), "new_value": v}
            for k, v in values.items()
        ],
        "impact": "Modifies existing data. Old values are recorded in the audit log.",
        "reason": args.get("reason", ""),
        "before": before,
    }


async def _execute(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    sf = ctx.require_sf()
    obj, rid = str(args["object"]), str(args["record_id"])
    before = await _before_values(ctx, args)
    try:
        await sf.update_record(obj, rid, dict(args["values"]))
    except SalesforceError as exc:
        return exc.to_dict() | {"object": obj, "record_id": rid, "before": before}
    return ok(object=obj, record_id=rid, before=before, requested_values=args["values"])


async def _verify(
    ctx: ToolContext, args: dict[str, Any], result: dict[str, Any]
) -> dict[str, Any]:
    if not result.get("success"):
        return {"verified": False, "reason": "Update did not succeed."}
    sf = ctx.require_sf()
    values = args.get("values") or {}
    try:
        record = await sf.get_record(
            str(args["object"]), str(args["record_id"]), fields=["Id", *values.keys()]
        )
    except SalesforceError as exc:
        return {"verified": False, "reason": exc.message}
    after = {k: v for k, v in record.items() if k != "attributes"}
    mismatches = [
        {"field": k, "expected": v, "actual": after.get(k)}
        for k, v in values.items()
        if str(after.get(k)) != str(v)
    ]
    return {
        "verified": not mismatches,
        "after": after,
        "mismatches": mismatches,
        "method": "re-read record after update",
    }


TOOL = registry.register(
    Tool(
        name="update_record",
        description=DESCRIPTION,
        input_schema=INPUT_SCHEMA,
        output_schema=OUTPUT_SCHEMA,
        risk=RiskLevel.MEDIUM,
        requires_approval=True,
        execute=_execute,
        validate=_validate,
        plan=_plan,
        verify=_verify,
        mutating=True,
        audit_action="salesforce.update_record",
        tags=["write", "data"],
    )
)
