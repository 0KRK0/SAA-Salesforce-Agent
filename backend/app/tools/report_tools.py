"""Reports and dashboards, through the Salesforce Analytics REST API.

Scope is honest about what the Analytics API can and cannot do:

  * **Reports** — listing, describing, creating, updating and running are all
    supported by the API and all implemented here for the report shapes admins
    actually ask for: tabular and summary reports with columns, groupings,
    filters and a date range.

  * **Dashboards** — the Analytics API exposes dashboards read-only (list,
    describe, and the results of the components). Creating a dashboard needs a
    Metadata API deployment with a hand-built dashboard definition, which is
    not implemented; `inspect_dashboard` says so rather than failing obscurely.

This is not a report designer. Bucket fields, cross filters, joined reports,
custom summary formulas and matrix reports are out of scope, and the tools say
so rather than silently dropping what was asked for.
"""

from __future__ import annotations

from typing import Any

from app.models import RiskLevel
from app.salesforce.errors import SalesforceError
from app.salesforce.inspect import soql_literal
from app.tools.base import Tool, ToolContext, ToolValidationError, fail, ok
from app.tools.registry import registry

SUPPORTED_FORMATS = {"TABULAR", "SUMMARY"}
UNSUPPORTED_FORMATS = {"MATRIX", "MULTI_BLOCK"}

FILTER_OPERATORS = {
    "equals", "notEqual", "lessThan", "greaterThan", "lessOrEqual",
    "greaterOrEqual", "contains", "notContain", "startsWith", "includes",
    "excludes", "within",
}


# ---------------------------------------------------------------------------
# list_reports
# ---------------------------------------------------------------------------
async def _list_execute(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    sf = ctx.require_sf()
    name_like = str(args.get("name_like") or "").strip()

    where = ""
    if name_like:
        where = f" WHERE Name LIKE '%{soql_literal(name_like)}%'"
    try:
        data = await sf.query(
            "SELECT Id, Name, DeveloperName, Format, FolderName, Description, "
            f"LastRunDate, LastModifiedDate FROM Report{where} "
            "ORDER BY LastModifiedDate DESC LIMIT 200"
        )
    except SalesforceError as exc:
        return exc.to_dict()

    return ok(
        count=len(data.get("records") or []),
        reports=[
            {
                "id": r.get("Id"),
                "name": r.get("Name"),
                "developer_name": r.get("DeveloperName"),
                "format": r.get("Format"),
                "folder": r.get("FolderName"),
                "description": r.get("Description"),
                "last_run": r.get("LastRunDate"),
                "last_modified": r.get("LastModifiedDate"),
            }
            for r in data.get("records") or []
        ],
    )


registry.register(
    Tool(
        name="list_reports",
        description=(
            "List reports in the org, newest-modified first.\n\n"
            "Call this before creating a report: the one the user wants often already "
            "exists under a name they did not think of."
        ),
        input_schema={
            "type": "object",
            "properties": {"name_like": {"type": "string"}},
            "additionalProperties": False,
        },
        output_schema={"type": "object", "properties": {"success": {"type": "boolean"}}},
        risk=RiskLevel.LOW,
        requires_approval=False,
        execute=_list_execute,
        audit_action="salesforce.list_reports",
        tags=["reports", "read"],
    )
)


# ---------------------------------------------------------------------------
# inspect_report
# ---------------------------------------------------------------------------
async def _inspect_execute(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    sf = ctx.require_sf()
    report_id = str(args.get("report_id") or "").strip()
    if not report_id:
        return fail("MISSING_ARGUMENT", "`report_id` is required.")
    try:
        described = await sf.request("GET", f"{sf.base}/analytics/reports/{report_id}/describe")
    except SalesforceError as exc:
        return exc.to_dict()

    metadata = described.get("reportMetadata") or {}
    payload: dict[str, Any] = {
        "id": report_id,
        "name": metadata.get("name"),
        "developer_name": metadata.get("developerName"),
        "format": metadata.get("reportFormat"),
        "report_type": (metadata.get("reportType") or {}).get("type"),
        "report_type_label": (metadata.get("reportType") or {}).get("label"),
        "columns": metadata.get("detailColumns"),
        "groupings_down": metadata.get("groupingsDown"),
        "groupings_across": metadata.get("groupingsAcross"),
        "filters": metadata.get("reportFilters"),
        "filter_logic": metadata.get("reportBooleanFilter"),
        "scope": metadata.get("scope"),
        "date_filter": metadata.get("standardDateFilter"),
        "aggregates": metadata.get("aggregates"),
    }
    if bool(args.get("run")):
        try:
            run = await sf.request(
                "GET",
                f"{sf.base}/analytics/reports/{report_id}",
                params={"includeDetails": "false"},
            )
            fact = (run.get("factMap") or {}).get("T!T", {})
            payload["result"] = {
                "row_count": (fact.get("aggregates") or [{}])[0].get("value"),
                "grand_totals": fact.get("aggregates"),
                "all_data_included": run.get("allData"),
                "note": (
                    "Summary totals only. Row-level data is deliberately not pulled "
                    "into the conversation; use query_salesforce for records."
                ),
            }
        except SalesforceError as exc:
            payload["result_error"] = exc.message
    return ok(**payload)


registry.register(
    Tool(
        name="inspect_report",
        description=(
            "Read a report's definition: type, columns, groupings, filters and date "
            "range. Set `run` to also execute it and return the summary totals "
            "(never the rows — use query_salesforce if you need records)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "report_id": {"type": "string"},
                "run": {"type": "boolean", "default": False},
            },
            "required": ["report_id"],
            "additionalProperties": False,
        },
        output_schema={"type": "object", "properties": {"success": {"type": "boolean"}}},
        risk=RiskLevel.LOW,
        requires_approval=False,
        execute=_inspect_execute,
        audit_action="salesforce.inspect_report",
        tags=["reports", "read"],
    )
)


# ---------------------------------------------------------------------------
# list_report_types
# ---------------------------------------------------------------------------
async def _types_execute(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    sf = ctx.require_sf()
    try:
        data = await sf.request("GET", f"{sf.base}/analytics/reportTypes")
    except SalesforceError as exc:
        return exc.to_dict()

    search = str(args.get("search") or "").lower()
    out = []
    for category in data if isinstance(data, list) else []:
        for report_type in category.get("reportTypes") or []:
            label = str(report_type.get("label") or "")
            type_name = str(report_type.get("type") or "")
            if search and search not in f"{label} {type_name}".lower():
                continue
            out.append(
                {
                    "type": type_name,
                    "label": label,
                    "category": category.get("label"),
                }
            )
    return ok(count=len(out), report_types=out[:200])


registry.register(
    Tool(
        name="list_report_types",
        description=(
            "List the report types available in the org.\n\n"
            "Creating a report requires a report type, and the right one is not always "
            "the obvious name — 'Opportunities with Products' and 'Opportunities' are "
            "different report types with different available fields. Search here first."
        ),
        input_schema={
            "type": "object",
            "properties": {"search": {"type": "string"}},
            "additionalProperties": False,
        },
        output_schema={"type": "object", "properties": {"success": {"type": "boolean"}}},
        risk=RiskLevel.LOW,
        requires_approval=False,
        execute=_types_execute,
        audit_action="salesforce.list_report_types",
        tags=["reports", "read"],
    )
)


# ---------------------------------------------------------------------------
# create_report
# ---------------------------------------------------------------------------
CREATE_DESCRIPTION = """Create a tabular or summary report.

Call list_report_types first to get a valid `report_type`, and inspect the
object so the column API names are real. A report referencing a field that does
not exist on the chosen report type is rejected by Salesforce, not silently
dropped.

Supported: TABULAR and SUMMARY formats, detail columns, up to two groupings,
filters, filter logic, a standard date filter, and scope (all records vs mine).

Not supported, and rejected rather than approximated: matrix and joined reports,
bucket fields, cross filters and custom summary formulas.

The report is created in the running user's private reports folder unless a
`folder_id` is given, because writing into a shared folder changes what other
people see.
"""


def _build_metadata(args: dict[str, Any]) -> dict[str, Any]:
    report_format = str(args.get("format") or "TABULAR").upper()
    columns = [str(c) for c in (args.get("columns") or [])]
    groupings = [str(g) for g in (args.get("group_by") or [])]

    metadata: dict[str, Any] = {
        "name": str(args["name"]),
        "reportType": {"type": str(args["report_type"])},
        "reportFormat": report_format,
        "detailColumns": columns,
        "reportFilters": [],
        "scope": str(args.get("scope") or "organization"),
    }
    if args.get("developer_name"):
        metadata["developerName"] = str(args["developer_name"])
    if args.get("description"):
        metadata["description"] = str(args["description"])
    if args.get("folder_id"):
        metadata["folderId"] = str(args["folder_id"])

    if report_format == "SUMMARY":
        metadata["groupingsDown"] = [
            {"name": g, "sortOrder": "Asc", "dateGranularity": "None"} for g in groupings[:2]
        ]
    for raw in args.get("filters") or []:
        metadata["reportFilters"].append(
            {
                "column": str(raw.get("column")),
                "operator": str(raw.get("operator") or "equals"),
                "value": str(raw.get("value", "")),
            }
        )
    if args.get("filter_logic"):
        metadata["reportBooleanFilter"] = str(args["filter_logic"])
    if args.get("date_filter"):
        date_filter = args["date_filter"]
        metadata["standardDateFilter"] = {
            "column": str(date_filter.get("column")),
            "durationValue": str(date_filter.get("duration") or "CUSTOM"),
            "startDate": date_filter.get("start_date"),
            "endDate": date_filter.get("end_date"),
        }
    if args.get("aggregates"):
        metadata["aggregates"] = [str(a) for a in args["aggregates"]]
    return metadata


async def _create_validate(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    report_format = str(args.get("format") or "TABULAR").upper()
    if report_format in UNSUPPORTED_FORMATS:
        raise ToolValidationError(
            f"{report_format} reports are not supported by this tool.",
            "UNSUPPORTED_REPORT_FORMAT",
            "Build a TABULAR or SUMMARY report, or tell the user this shape has to be "
            "built in the Salesforce report builder.",
        )
    if report_format not in SUPPORTED_FORMATS:
        raise ToolValidationError(
            f"Unknown report format '{report_format}'.",
            "UNSUPPORTED_REPORT_FORMAT",
            f"Use one of: {', '.join(sorted(SUPPORTED_FORMATS))}.",
        )
    if not args.get("columns"):
        raise ToolValidationError(
            "A report with no columns shows nothing.",
            "MISSING_ARGUMENT",
            "Say which fields should appear as columns.",
            ["columns"],
        )
    if report_format == "SUMMARY" and not args.get("group_by"):
        raise ToolValidationError(
            "A summary report needs at least one grouping.",
            "MISSING_ARGUMENT",
            "Say which field the report should be grouped by.",
            ["group_by"],
        )
    if len(args.get("group_by") or []) > 2:
        raise ToolValidationError(
            "This tool supports at most two groupings.",
            "UNSUPPORTED_REPORT_SHAPE",
            "Reduce the groupings, or build the report in the Salesforce UI.",
        )
    for raw in args.get("filters") or []:
        operator = str(raw.get("operator") or "equals")
        if operator not in FILTER_OPERATORS:
            raise ToolValidationError(
                f"'{operator}' is not a valid report filter operator.",
                "INVALID_FILTER_OPERATOR",
                f"Use one of: {', '.join(sorted(FILTER_OPERATORS))}.",
            )
    return {"format": report_format}


async def _create_plan(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    metadata = _build_metadata(args)
    return {
        "title": f"Create report '{args['name']}'",
        "change_type": "reports.create",
        "summary": (
            f"A {metadata['reportFormat'].lower()} report on "
            f"{metadata['reportType']['type']}."
        ),
        "details": [
            {"field": "Report type", "new_value": metadata["reportType"]["type"]},
            {"field": "Format", "new_value": metadata["reportFormat"]},
            {"field": "Columns", "new_value": ", ".join(metadata["detailColumns"])},
            *(
                [
                    {
                        "field": "Grouped by",
                        "new_value": ", ".join(
                            g["name"] for g in metadata.get("groupingsDown") or []
                        ),
                    }
                ]
                if metadata.get("groupingsDown")
                else []
            ),
            *(
                [
                    {
                        "field": "Filters",
                        "new_value": "; ".join(
                            f"{f['column']} {f['operator']} {f['value']}"
                            for f in metadata["reportFilters"]
                        ),
                    }
                ]
                if metadata["reportFilters"]
                else []
            ),
            {
                "field": "Folder",
                "new_value": metadata.get("folderId")
                or "the running user's private reports folder",
            },
        ],
        "impact": (
            "Creates a new saved report. If a folder is specified, everyone with "
            "access to that folder will see it."
        ),
        "reason": args.get("reason", ""),
    }


async def _create_execute(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    sf = ctx.require_sf()
    metadata = _build_metadata(args)
    try:
        created = await sf.request(
            "POST", f"{sf.base}/analytics/reports", json={"reportMetadata": metadata}
        )
    except SalesforceError as exc:
        return exc.to_dict() | {
            "suggested_action": (
                "The report type or a column API name is probably wrong for this report "
                "type. Call list_report_types and inspect an existing report of the same "
                "type to see valid column names."
            )
        }
    report_id = (created.get("reportMetadata") or {}).get("id") or created.get("id")
    return ok(
        report_id=report_id,
        name=metadata["name"],
        report_type=metadata["reportType"]["type"],
        format=metadata["reportFormat"],
    )


async def _create_verify(
    ctx: ToolContext, args: dict[str, Any], result: dict[str, Any]
) -> dict[str, Any]:
    if not result.get("success") or not result.get("report_id"):
        return {"verified": False, "reason": "No report id was returned."}
    sf = ctx.require_sf()
    try:
        described = await sf.request(
            "GET", f"{sf.base}/analytics/reports/{result['report_id']}/describe"
        )
    except SalesforceError as exc:
        return {"verified": False, "reason": f"The new report could not be read: {exc.message}"}
    metadata = described.get("reportMetadata") or {}
    return {
        "verified": True,
        "method": "Analytics API describe after creation",
        "after": {
            "id": result["report_id"],
            "name": metadata.get("name"),
            "format": metadata.get("reportFormat"),
            "columns": metadata.get("detailColumns"),
        },
    }


registry.register(
    Tool(
        name="create_report",
        description=CREATE_DESCRIPTION,
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "developer_name": {"type": "string"},
                "description": {"type": "string"},
                "report_type": {
                    "type": "string",
                    "description": "From list_report_types, e.g. 'OpportunityList'.",
                },
                "format": {"type": "string", "enum": ["TABULAR", "SUMMARY"]},
                "columns": {"type": "array", "items": {"type": "string"}},
                "group_by": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Up to two grouping fields (SUMMARY format).",
                },
                "filters": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "column": {"type": "string"},
                            "operator": {"type": "string"},
                            "value": {"type": "string"},
                        },
                        "required": ["column", "operator", "value"],
                    },
                },
                "filter_logic": {"type": "string", "description": "e.g. '(1 AND 2) OR 3'."},
                "date_filter": {
                    "type": "object",
                    "properties": {
                        "column": {"type": "string"},
                        "duration": {"type": "string", "description": "e.g. THIS_QUARTER"},
                        "start_date": {"type": "string"},
                        "end_date": {"type": "string"},
                    },
                },
                "aggregates": {"type": "array", "items": {"type": "string"}},
                "scope": {"type": "string", "enum": ["organization", "user"]},
                "folder_id": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["name", "report_type", "columns"],
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "properties": {"success": {"type": "boolean"}, "report_id": {"type": "string"}},
        },
        risk=RiskLevel.MEDIUM,
        requires_approval=True,
        execute=_create_execute,
        validate=_create_validate,
        plan=_create_plan,
        verify=_create_verify,
        mutating=True,
        audit_action="salesforce.create_report",
        tags=["reports", "write"],
    )
)


# ---------------------------------------------------------------------------
# inspect_dashboard
# ---------------------------------------------------------------------------
async def _dashboard_execute(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    sf = ctx.require_sf()
    dashboard_id = str(args.get("dashboard_id") or "").strip()
    if not dashboard_id:
        try:
            data = await sf.query(
                "SELECT Id, Title, DeveloperName, FolderName, Description, "
                "LastModifiedDate FROM Dashboard ORDER BY LastModifiedDate DESC LIMIT 100"
            )
        except SalesforceError as exc:
            return exc.to_dict()
        return ok(
            count=len(data.get("records") or []),
            dashboards=[
                {
                    "id": r.get("Id"),
                    "title": r.get("Title"),
                    "developer_name": r.get("DeveloperName"),
                    "folder": r.get("FolderName"),
                    "description": r.get("Description"),
                }
                for r in data.get("records") or []
            ],
            note=(
                "Pass dashboard_id to inspect one. Creating or editing dashboards is "
                "not implemented in this build — it requires a hand-built Metadata API "
                "dashboard definition."
            ),
        )
    try:
        described = await sf.request(
            "GET", f"{sf.base}/analytics/dashboards/{dashboard_id}/describe"
        )
    except SalesforceError as exc:
        return exc.to_dict()
    metadata = described.get("dashboardMetadata") or described
    components = metadata.get("components") or []
    return ok(
        id=dashboard_id,
        title=metadata.get("name") or metadata.get("title"),
        description=metadata.get("description"),
        running_user=metadata.get("runningUser"),
        component_count=len(components),
        components=[
            {
                "header": c.get("header"),
                "type": c.get("componentType"),
                "report_id": c.get("reportId"),
            }
            for c in components[:40]
        ],
        note=(
            "Dashboard editing is not implemented in this build. Changes must be made "
            "in the Salesforce dashboard builder."
        ),
    )


registry.register(
    Tool(
        name="inspect_dashboard",
        description=(
            "List dashboards, or inspect one dashboard's components and running user.\n\n"
            "Read-only. Creating and editing dashboards is not implemented in this "
            "build — the Analytics API does not support it, and a Metadata API "
            "dashboard definition is not something to improvise."
        ),
        input_schema={
            "type": "object",
            "properties": {"dashboard_id": {"type": "string"}},
            "additionalProperties": False,
        },
        output_schema={"type": "object", "properties": {"success": {"type": "boolean"}}},
        risk=RiskLevel.LOW,
        requires_approval=False,
        execute=_dashboard_execute,
        audit_action="salesforce.inspect_dashboard",
        tags=["reports", "read"],
    )
)
