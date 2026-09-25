"""Data quality agent: duplicates, completeness, anomalies, merge plans, bulk fixes.

The shape of every tool here is the same, and it is the point of the whole
subsystem: **pull the data server-side, analyze it in Python, return findings.**
Fifty thousand records never enter the model's context. A duplicate scan
returns "312 groups, here are the twenty largest"; a bulk update returns a plan
and then a job id.

Mutations follow the org's blast-radius policy: anything touching more than a
handful of records is HIGH risk, needs approval, and runs through the Bulk API
rather than record-by-record REST calls.
"""

from __future__ import annotations

from typing import Any

from app.analysis.duplicates import build_merge_plan, find_duplicates
from app.analysis.quality import (
    anomalies,
    completeness,
    distributions,
    summarize,
    validity,
)
from app.config import settings
from app.models import DataJob, RiskLevel
from app.salesforce.errors import SalesforceError
from app.salesforce.inspect import list_duplicate_rules, soql_literal
from app.salesforce.soql import SoqlValidationError, validate_soql
from app.tools.base import Tool, ToolContext, ToolValidationError, fail, ok
from app.tools.registry import registry

#: Fields worth pulling for duplicate matching, per object. Querying every
#: field on 20,000 records is slow and unnecessary.
MATCH_FIELDS: dict[str, list[str]] = {
    "account": ["Id", "Name", "Website", "Phone", "BillingCity", "BillingCountry",
                "OwnerId", "CreatedDate", "AnnualRevenue", "Industry"],
    "contact": ["Id", "FirstName", "LastName", "Email", "Phone", "AccountId",
                "Title", "OwnerId", "CreatedDate"],
    "lead": ["Id", "FirstName", "LastName", "Email", "Company", "Phone",
             "Status", "OwnerId", "CreatedDate"],
}


async def _pull(
    ctx: ToolContext,
    object_name: str,
    fields: list[str],
    *,
    where: str = "",
    limit: int,
) -> tuple[list[dict[str, Any]], int | None, bool]:
    """Fetch records for analysis, using the Bulk API when the set is large.

    Returns `(records, total_in_org, used_bulk)`. The REST query path caps out
    quickly; past that, Bulk API 2.0 is the only honest way to analyze a real
    org rather than its first page.
    """
    sf = ctx.require_sf()
    clause = f" WHERE {where}" if where else ""
    try:
        total = await sf.count(object_name, where)
    except SalesforceError:
        total = None

    soql = f"SELECT {', '.join(fields)} FROM {object_name}{clause}"
    if total is not None and total > 2000:
        await ctx.emit(
            "analysis.bulk_extract",
            {"object": object_name, "estimated_records": total},
        )
        data = await sf.bulk_query(f"{soql} LIMIT {limit}", max_records=limit)
        return data.get("records") or [], total, True

    data = await sf.query_all_pages(f"{soql} LIMIT {limit}", max_records=limit)
    return data.get("records") or [], total, False


def _analysis_limit(ctx: ToolContext, requested: Any) -> int:
    ceiling = settings.max_analysis_records
    try:
        wanted = int(requested) if requested else ceiling
    except (TypeError, ValueError):
        wanted = ceiling
    return max(1, min(wanted, ceiling))


# ---------------------------------------------------------------------------
# find_duplicates
# ---------------------------------------------------------------------------
FIND_DUPLICATES_DESCRIPTION = """Find duplicate records and explain why they match.

Analysis happens server-side over the whole result set — up to the org's
analysis limit — so this works on real data volumes. You receive grouped
findings and statistics, never the raw records.

Matching normalizes names (dropping "Inc", "Ltd", punctuation and case),
emails (Gmail dots and +tags) and phone numbers (last 10 digits), then blocks
and scores candidates. Every group reports what it matched on, so a human can
judge it.

Supports Account, Contact and Lead out of the box; other objects fall back to
name matching. This finds and explains — it never merges. Use prepare_merge_plan
for that.
"""


async def _find_duplicates_execute(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    sf = ctx.require_sf()
    object_name = str(args.get("object") or "").strip()
    if not object_name:
        return fail("MISSING_ARGUMENT", "`object` is required.")

    try:
        describe = await sf.describe(object_name)
    except SalesforceError as exc:
        return exc.to_dict()

    available = {f["name"] for f in describe.get("fields") or []}
    requested = args.get("fields") or MATCH_FIELDS.get(object_name.lower())
    if requested:
        fields = [f for f in requested if f in available]
    else:
        fields = [f for f in ("Id", "Name", "CreatedDate") if f in available]
    if "Id" not in fields:
        fields.insert(0, "Id")
    if len(fields) < 2:
        return fail(
            "NO_MATCHABLE_FIELDS",
            f"{object_name} has none of the fields duplicate matching needs.",
            suggested_action="Pass `fields` explicitly with the columns to match on.",
        )

    where = str(args.get("where") or "").strip()
    if where and not _safe_filter(where):
        raise ToolValidationError(
            "The `where` filter contains something that is not a filter expression.",
            "UNSAFE_FILTER",
            "Pass a plain SOQL WHERE clause, e.g. \"BillingCountry = 'US'\".",
        )

    limit = _analysis_limit(ctx, args.get("limit"))
    records, total, used_bulk = await _pull(
        ctx, object_name, fields, where=where, limit=limit
    )
    groups, stats = find_duplicates(
        records,
        object_name,
        fields=set(fields),
        min_confidence=float(args.get("min_confidence") or 0.5),
    )
    rules = await list_duplicate_rules(sf, object_name)
    top = int(args.get("max_groups") or 20)

    return ok(
        object=object_name,
        analysis=stats
        | {
            "records_in_org": total,
            "extract_method": "Bulk API 2.0" if used_bulk else "REST query",
            "sampled": bool(total and len(records) < total),
        },
        duplicate_groups=[g.to_dict() for g in groups[:top]],
        groups_omitted=max(0, len(groups) - top),
        salesforce_duplicate_rules=rules,
        note=(
            "These are candidate duplicates found by this analysis, not Salesforce's own "
            "duplicate rules. Nothing was changed."
        ),
    )


def _safe_filter(where: str) -> bool:
    """A WHERE fragment must not smuggle in a second statement or a subquery DML."""
    lowered = where.lower()
    banned = ("delete", "update ", "insert", "upsert", ";", "--")
    return not any(b in lowered for b in banned)


registry.register(
    Tool(
        name="find_duplicates",
        description=FIND_DUPLICATES_DESCRIPTION,
        input_schema={
            "type": "object",
            "properties": {
                "object": {"type": "string"},
                "fields": {"type": "array", "items": {"type": "string"}},
                "where": {"type": "string", "description": "Optional SOQL WHERE clause."},
                "min_confidence": {"type": "number", "default": 0.5},
                "limit": {"type": "integer", "description": "Records to analyze."},
                "max_groups": {"type": "integer", "default": 20},
            },
            "required": ["object"],
            "additionalProperties": False,
        },
        output_schema={"type": "object", "properties": {"success": {"type": "boolean"}}},
        risk=RiskLevel.LOW,
        requires_approval=False,
        execute=_find_duplicates_execute,
        audit_action="salesforce.find_duplicates",
        tags=["data", "quality", "read"],
        long_running=True,
    )
)


# ---------------------------------------------------------------------------
# analyze_data_quality
# ---------------------------------------------------------------------------
ANALYZE_DESCRIPTION = """Assess the data quality of one object: field completeness,
invalid values, picklist sprawl and numeric outliers.

Answers questions like "which Accounts are missing Industry", "do we have
Contacts with broken email addresses", "is our Stage picklist being used
consistently". Analysis is server-side; you get ranked findings with a handful
of examples, not the records.

Validity checks only flag values that are definitely wrong for their field type
(an email with no @, letters in a phone number, a date centuries out), so a
finding here is worth acting on.
"""


async def _analyze_execute(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    sf = ctx.require_sf()
    object_name = str(args.get("object") or "").strip()
    if not object_name:
        return fail("MISSING_ARGUMENT", "`object` is required.")
    try:
        describe = await sf.describe(object_name)
    except SalesforceError as exc:
        return exc.to_dict()

    all_fields = [f for f in describe.get("fields") or [] if f.get("name")]
    requested = args.get("fields")
    if requested:
        chosen = [f for f in all_fields if f["name"] in set(requested)]
    else:
        # Default to the fields an admin cares about: creatable, non-system,
        # excluding long text that would bloat the extract.
        chosen = [
            f
            for f in all_fields
            if f.get("createable")
            and f.get("type") not in {"textarea", "base64", "address", "location"}
        ][:40]
    names = ["Id"] + [f["name"] for f in chosen if f["name"] != "Id"]

    where = str(args.get("where") or "").strip()
    if where and not _safe_filter(where):
        raise ToolValidationError(
            "The `where` filter contains something that is not a filter expression.",
            "UNSAFE_FILTER",
        )
    limit = _analysis_limit(ctx, args.get("limit"))
    records, total, used_bulk = await _pull(
        ctx, object_name, names, where=where, limit=limit
    )
    if not records:
        return ok(
            object=object_name,
            records_analyzed=0,
            message=f"No {object_name} records matched, so there is nothing to analyze.",
        )

    field_types = {f["name"]: f.get("type", "") for f in chosen}
    completeness_rows = completeness(records, [n for n in names if n != "Id"])
    validity_rows = validity(records, field_types)
    picklists = [f["name"] for f in chosen if f.get("type") == "picklist"]
    numerics = [
        f["name"]
        for f in chosen
        if f.get("type") in {"double", "currency", "int", "percent"}
    ]
    anomaly_rows = anomalies(records, numerics)

    return ok(
        **summarize(
            object_name=object_name,
            analyzed=len(records),
            total_in_org=total,
            completeness_rows=completeness_rows,
            validity_rows=validity_rows,
            anomaly_rows=anomaly_rows,
        ),
        extract_method="Bulk API 2.0" if used_bulk else "REST query",
        completeness=completeness_rows[:25],
        invalid_values=validity_rows[:15],
        picklist_usage=distributions(records, picklists)[:10],
        outliers=anomaly_rows[:10],
    )


registry.register(
    Tool(
        name="analyze_data_quality",
        description=ANALYZE_DESCRIPTION,
        input_schema={
            "type": "object",
            "properties": {
                "object": {"type": "string"},
                "fields": {"type": "array", "items": {"type": "string"}},
                "where": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": ["object"],
            "additionalProperties": False,
        },
        output_schema={"type": "object", "properties": {"success": {"type": "boolean"}}},
        risk=RiskLevel.LOW,
        requires_approval=False,
        execute=_analyze_execute,
        audit_action="salesforce.analyze_data_quality",
        tags=["data", "quality", "read"],
        long_running=True,
    )
)


# ---------------------------------------------------------------------------
# prepare_merge_plan
# ---------------------------------------------------------------------------
MERGE_PLAN_DESCRIPTION = """Build a merge plan for a group of duplicate records.

Produces, for each group: which record survives and why, which records are
deleted, which field values conflict, and — the part people miss — which values
exist only on a losing record and would be destroyed.

This produces a plan. It does not merge. Salesforce merges are irreversible, so
executing one is a separate, explicitly approved action; give the plan to the
user and let them decide.
"""


async def _merge_plan_execute(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    sf = ctx.require_sf()
    object_name = str(args.get("object") or "").strip()
    record_ids = [str(r) for r in (args.get("record_ids") or []) if r]
    if not object_name or len(record_ids) < 2:
        return fail(
            "MISSING_ARGUMENT",
            "`object` and at least two `record_ids` are required.",
        )
    if len(record_ids) > 20:
        return fail(
            "TOO_MANY_RECORDS",
            "A merge group of more than 20 records is almost always a matching error.",
            suggested_action="Re-run find_duplicates with a higher min_confidence.",
        )

    try:
        describe = await sf.describe(object_name)
    except SalesforceError as exc:
        return exc.to_dict()
    fields = [
        f["name"]
        for f in describe.get("fields") or []
        if f.get("type") not in {"base64", "address", "location"}
    ][:80]
    if "Id" not in fields:
        fields.insert(0, "Id")

    quoted = ", ".join(f"'{soql_literal(r)}'" for r in record_ids)
    data = await sf.query(
        f"SELECT {', '.join(fields)} FROM {object_name} WHERE Id IN ({quoted})"
    )
    records = [
        {k: v for k, v in r.items() if k != "attributes"} for r in data.get("records") or []
    ]
    found = {str(r.get("Id"))[:15] for r in records}
    missing = [r for r in record_ids if r[:15] not in found]
    if len(records) < 2:
        return fail(
            "RECORDS_NOT_FOUND",
            f"Only {len(records)} of the {len(record_ids)} records exist or are visible.",
            missing=missing,
        )

    from app.analysis.duplicates import DuplicateGroup

    group = DuplicateGroup(
        record_ids=[str(r.get("Id")) for r in records],
        matched_on=list(args.get("matched_on") or ["supplied by the user"]),
        confidence=float(args.get("confidence") or 1.0),
        sample=records,
    )
    plan = build_merge_plan(group, strategy=str(args.get("strategy") or "most_complete"))
    return ok(
        object=object_name,
        merge_plan=plan,
        records_not_found=missing,
        next_step=(
            "Show this plan to the user. Merging is irreversible and is not something "
            "this agent performs automatically; a Salesforce admin executes the merge "
            "once the plan is agreed."
        ),
    )


registry.register(
    Tool(
        name="prepare_merge_plan",
        description=MERGE_PLAN_DESCRIPTION,
        input_schema={
            "type": "object",
            "properties": {
                "object": {"type": "string"},
                "record_ids": {"type": "array", "items": {"type": "string"}},
                "strategy": {
                    "type": "string",
                    "enum": ["most_complete", "oldest", "newest", "most_activity"],
                    "default": "most_complete",
                },
                "matched_on": {"type": "array", "items": {"type": "string"}},
                "confidence": {"type": "number"},
            },
            "required": ["object", "record_ids"],
            "additionalProperties": False,
        },
        output_schema={"type": "object", "properties": {"success": {"type": "boolean"}}},
        risk=RiskLevel.LOW,
        requires_approval=False,
        execute=_merge_plan_execute,
        audit_action="salesforce.prepare_merge_plan",
        tags=["data", "quality", "read"],
    )
)


# ---------------------------------------------------------------------------
# bulk_update
# ---------------------------------------------------------------------------
BULK_UPDATE_DESCRIPTION = """Apply the same field values to every record matching a
filter, using the Bulk API.

This is how "set Industry to Technology for all Accounts whose website ends in
.io" is done safely. The tool:

  1. counts the matching records first, and tells you the count;
  2. refuses outright if the count exceeds the organization's bulk limit;
  3. requires human approval, with the count and the exact change on the card;
  4. runs a real Bulk API 2.0 ingest job and reports Salesforce's own
     processed/failed numbers, with a sample of the failures.

Records are never loaded into the conversation. Give a `where` clause, not a
list of ids — and make the filter specific: this changes every record it
matches.
"""


async def _bulk_validate(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    sf = ctx.require_sf()
    object_name = str(args.get("object") or "").strip()
    where = str(args.get("where") or "").strip()
    values = args.get("values") or {}
    if not object_name or not where or not values:
        raise ToolValidationError(
            "`object`, `where` and `values` are all required.",
            "MISSING_ARGUMENT",
            "A bulk update with no filter would change every record of that type; "
            "say which records this applies to.",
        )
    if not _safe_filter(where):
        raise ToolValidationError(
            "The `where` filter contains something that is not a filter expression.",
            "UNSAFE_FILTER",
        )
    # Reuse the read-path SOQL validator so the filter cannot smuggle in DML,
    # a second statement or a comment-obfuscated payload.
    try:
        validate_soql(f"SELECT Id FROM {object_name} WHERE {where} LIMIT 1")
    except SoqlValidationError as exc:
        raise ToolValidationError(
            f"The filter is not a safe SOQL condition: {exc.message}",
            "UNSAFE_FILTER",
            exc.suggested_action,
        ) from exc

    try:
        describe = await sf.describe(object_name)
    except SalesforceError as exc:
        raise ToolValidationError(
            f"Object '{object_name}' could not be described: {exc.message}",
            exc.error_type,
        ) from exc

    by_name = {f["name"].lower(): f for f in describe.get("fields") or []}
    for field_name in values:
        meta = by_name.get(str(field_name).lower())
        if meta is None:
            raise ToolValidationError(
                f"{object_name} has no field '{field_name}'.",
                "FIELD_NOT_FOUND",
                "Call describe_object and use real field API names.",
            )
        if not meta.get("updateable"):
            raise ToolValidationError(
                f"{object_name}.{meta['name']} is not updateable.",
                "FIELD_NOT_WRITABLE",
                "Choose a writable field.",
            )

    count = await sf.count(object_name, where)
    limit = getattr(ctx.policy, "max_bulk_records", settings.max_bulk_records)
    if count == 0:
        raise ToolValidationError(
            f"No {object_name} records match that filter, so there is nothing to update.",
            "NO_MATCHING_RECORDS",
            "Report this to the user and check the filter with query_salesforce.",
        )
    if count > limit:
        raise ToolValidationError(
            f"{count:,} records match, above this organization's bulk limit of "
            f"{limit:,}.",
            "BULK_LIMIT_EXCEEDED",
            "Narrow the filter, or ask an admin to raise the limit in the "
            "organization's agent policy.",
        )
    return {"matching_records": count}


async def _bulk_plan(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    sf = ctx.require_sf()
    object_name = str(args["object"])
    where = str(args["where"])
    count = await sf.count(object_name, where)
    sample = await sf.query(
        f"SELECT Id FROM {object_name} WHERE {where} LIMIT 5"
    )
    return {
        "title": f"Bulk update {count:,} {object_name} record(s)",
        "object": object_name,
        "change_type": "data.bulk_update",
        "summary": f"Sets {', '.join(args['values'])} on every matching record.",
        "details": [
            {"field": "Filter", "new_value": where},
            {"field": "Records affected", "new_value": f"{count:,}"},
            *[
                {"field": k, "new_value": v}
                for k, v in (args.get("values") or {}).items()
            ],
        ],
        "sample_record_ids": [r.get("Id") for r in sample.get("records") or []],
        "impact": (
            f"Updates {count:,} records in one Bulk API job. Field history is written "
            "for tracked fields, and any automation on this object fires for every "
            "record. There is no undo."
        ),
        "reason": args.get("reason", ""),
    }


async def _bulk_fingerprint(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """The record count is the fingerprint.

    If the matching set moved while a human was reviewing, the approved blast
    radius is not the actual blast radius, and the approval no longer applies.
    """
    sf = ctx.require_sf()
    return {
        "matching_records": await sf.count(str(args["object"]), str(args["where"]))
    }


async def _bulk_execute(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    sf = ctx.require_sf()
    conn = ctx.connection
    assert conn is not None
    object_name = str(args["object"])
    where = str(args["where"])
    values = dict(args["values"])
    limit = getattr(ctx.policy, "max_bulk_records", settings.max_bulk_records)

    ids_result = await sf.bulk_query(
        f"SELECT Id FROM {object_name} WHERE {where}", max_records=limit
    )
    ids = [r["Id"] for r in ids_result.get("records") or [] if r.get("Id")]
    if not ids:
        return fail(
            "NO_MATCHING_RECORDS",
            f"No {object_name} records matched at execution time; nothing was changed.",
        )
    if ids_result.get("truncated"):
        return fail(
            "BULK_LIMIT_EXCEEDED",
            f"More than {limit:,} records match; the job was not started.",
            suggested_action="Narrow the filter and propose the change again.",
        )

    job = DataJob(
        company_id=ctx.company_id,
        project_id=ctx.project_id,
        user_id=ctx.user.id,
        salesforce_connection_id=conn.id,
        agent_run_id=ctx.agent_run_id,
        operation="update",
        sobject=object_name,
        records_total=len(ids),
        plan={"where": where, "values": values},
        state="Uploading",
    )
    ctx.db.add(job)
    await ctx.db.flush()

    await ctx.emit(
        "bulk.started",
        {"job": job.id, "object": object_name, "records": len(ids)},
    )

    async def _progress(update: dict[str, Any]) -> None:
        await ctx.emit("bulk.progress", {"job": job.id, **update})

    rows = [{"Id": record_id, **{k: str(v) for k, v in values.items()}} for record_id in ids]
    try:
        result = await sf.bulk_ingest(
            object_name, "update", rows, on_progress=_progress
        )
    except SalesforceError as exc:
        job.state = "Failed"
        job.error = exc.message
        await ctx.db.flush()
        return exc.to_dict() | {"job": job.id}

    job.sf_job_id = result.get("job_id")
    job.state = str(result.get("state") or "Unknown")
    job.records_processed = int(result.get("records_processed") or 0)
    job.records_failed = int(result.get("records_failed") or 0)
    job.failures_sample = result.get("failures_sample")
    await ctx.db.flush()

    await ctx.emit(
        "bulk.finished",
        {
            "job": job.id,
            "state": job.state,
            "processed": job.records_processed,
            "failed": job.records_failed,
        },
    )

    if result.get("timed_out"):
        return fail(
            "BULK_JOB_TIMEOUT",
            result.get("note") or "The bulk job did not finish in time.",
            retryable=False,
            suggested_action=(
                "The job is still running in Salesforce. Tell the user it is in "
                "progress; do not report a final count."
            ),
            job=job.id,
            salesforce_job_id=job.sf_job_id,
            state=job.state,
        )
    if job.records_failed:
        return {
            "success": False,
            "error_type": "BULK_PARTIAL_FAILURE",
            "message": (
                f"{job.records_processed - job.records_failed:,} record(s) updated, "
                f"{job.records_failed:,} failed."
            ),
            "retryable": False,
            "suggested_action": (
                "Read the failure sample to find the cause (validation rule, required "
                "field, permission), then propose a corrected update for the failures."
            ),
            "job": job.id,
            "salesforce_job_id": job.sf_job_id,
            "records_processed": job.records_processed,
            "records_failed": job.records_failed,
            "failures_sample": job.failures_sample,
            "partial": True,
        }
    return ok(
        object=object_name,
        job=job.id,
        salesforce_job_id=job.sf_job_id,
        state=job.state,
        records_processed=job.records_processed,
        records_failed=0,
        values=values,
    )


async def _bulk_verify(
    ctx: ToolContext, args: dict[str, Any], result: dict[str, Any]
) -> dict[str, Any]:
    """Confirm against the org that the records now hold the new values."""
    if not result.get("success"):
        return {"verified": False, "reason": "The bulk job did not complete cleanly."}
    sf = ctx.require_sf()
    object_name = str(args["object"])
    values = dict(args["values"])
    conditions = " AND ".join(
        f"{field} = {_soql_value(value)}" for field, value in values.items()
    )
    try:
        remaining = await sf.count(object_name, f"({args['where']}) AND NOT ({conditions})")
    except SalesforceError as exc:
        return {
            "verified": False,
            "reason": f"Could not verify the update: {exc.message}",
        }
    if remaining:
        return {
            "verified": False,
            "reason": (
                f"{remaining:,} record(s) still match the filter without the new values. "
                "The update was not fully applied."
            ),
        }
    return {
        "verified": True,
        "method": "post-update SOQL count of records still missing the new values",
        "after": {"records_without_new_values": 0},
    }


def _soql_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    return f"'{soql_literal(str(value))}'"


registry.register(
    Tool(
        name="bulk_update",
        description=BULK_UPDATE_DESCRIPTION,
        input_schema={
            "type": "object",
            "properties": {
                "object": {"type": "string"},
                "where": {
                    "type": "string",
                    "description": "SOQL WHERE clause selecting the records to update.",
                },
                "values": {
                    "type": "object",
                    "description": "Field API name -> new value, applied to every match.",
                },
                "reason": {"type": "string"},
            },
            "required": ["object", "where", "values"],
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "properties": {
                "success": {"type": "boolean"},
                "records_processed": {"type": "integer"},
                "records_failed": {"type": "integer"},
            },
        },
        risk=RiskLevel.HIGH,
        requires_approval=True,
        execute=_bulk_execute,
        validate=_bulk_validate,
        plan=_bulk_plan,
        verify=_bulk_verify,
        fingerprint=_bulk_fingerprint,
        mutating=True,
        audit_action="salesforce.bulk_update",
        tags=["data", "bulk", "write"],
        long_running=True,
    )
)
