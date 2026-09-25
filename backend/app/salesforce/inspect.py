"""Read-only org inspection built on the Tooling and Metadata APIs.

Every capability that has to *understand* an org before changing it — the Flow
agent, the Apex agent, dependency analysis, the debugger, the permissions
analyst — reads through this module. Keeping it in one place means those
subsystems agree about what the org contains, and it keeps SOQL against
Tooling objects out of the individual tools.

Nothing here mutates. Everything here returns structured, summarized data
sized for a model context, not raw dumps.
"""

from __future__ import annotations

import re
from typing import Any

from app.observability.logging import get_logger
from app.salesforce.client import SalesforceClient
from app.salesforce.errors import SalesforceError

log = get_logger("salesforce.inspect")


def soql_literal(value: str) -> str:
    """Escape a value for safe inclusion in a SOQL string literal."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


# ---------------------------------------------------------------------- Flow
FLOW_PROCESS_TYPES = {
    "AutoLaunchedFlow": "Record-triggered or auto-launched flow",
    "Flow": "Screen flow",
    "Workflow": "Process Builder process",
    "CustomEvent": "Platform event-triggered flow",
    "InvocableProcess": "Invocable process",
    "Journey": "Journey",
}


async def list_flows(
    sf: SalesforceClient, *, object_name: str | None = None, active_only: bool = False
) -> list[dict[str, Any]]:
    """Every flow definition in the org, with its active version.

    FlowDefinitionView is the queryable view that carries trigger metadata, so
    it — not the Flow object — is what tells us which flows fire on an object.
    """
    where = []
    if active_only:
        where.append("IsActive = true")
    if object_name:
        where.append("TriggerObjectOrEventLabel != null AND ApiName != null")
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    soql = (
        "SELECT ApiName, Label, Description, ProcessType, TriggerType, "
        "TriggerObjectOrEventLabel, TriggerObjectOrEventId, IsActive, "
        "ActiveVersionId, LatestVersionId, VersionNumber, LastModifiedDate, "
        "LastModifiedBy.Name, IsTemplate, Builder "
        f"FROM FlowDefinitionView{clause} ORDER BY Label LIMIT 500"
    )
    try:
        data = await sf.query(soql)
    except SalesforceError:
        # FlowDefinitionView is unavailable on very old API versions; fall back
        # to the Tooling Flow object, which has less trigger detail.
        return await _list_flows_tooling(sf)

    flows = []
    for row in data.get("records") or []:
        trigger_object = row.get("TriggerObjectOrEventLabel") or ""
        if object_name and trigger_object.lower() not in {
            object_name.lower(),
            _plural_guess(object_name).lower(),
        }:
            continue
        flows.append(
            {
                "api_name": row.get("ApiName"),
                "label": row.get("Label"),
                "description": row.get("Description"),
                "process_type": row.get("ProcessType"),
                "process_type_label": FLOW_PROCESS_TYPES.get(
                    str(row.get("ProcessType")), row.get("ProcessType")
                ),
                "trigger_type": row.get("TriggerType"),
                "trigger_object": trigger_object,
                "is_active": bool(row.get("IsActive")),
                "version": row.get("VersionNumber"),
                "active_version_id": row.get("ActiveVersionId"),
                "latest_version_id": row.get("LatestVersionId"),
                "last_modified": row.get("LastModifiedDate"),
                "last_modified_by": (row.get("LastModifiedBy") or {}).get("Name"),
                "builder": row.get("Builder"),
            }
        )
    return flows


async def _list_flows_tooling(sf: SalesforceClient) -> list[dict[str, Any]]:
    data = await sf.tooling_query(
        "SELECT Id, DeveloperName, MasterLabel, ProcessType, Status, "
        "VersionNumber, Description FROM Flow ORDER BY DeveloperName LIMIT 500"
    )
    return [
        {
            "api_name": r.get("DeveloperName"),
            "label": r.get("MasterLabel"),
            "description": r.get("Description"),
            "process_type": r.get("ProcessType"),
            "is_active": r.get("Status") == "Active",
            "version": r.get("VersionNumber"),
            "source": "tooling",
        }
        for r in data.get("records") or []
    ]


async def get_flow_versions(sf: SalesforceClient, api_name: str) -> list[dict[str, Any]]:
    """All versions of one flow, newest first."""
    data = await sf.tooling_query(
        "SELECT Id, VersionNumber, Status, MasterLabel, ProcessType, "
        "Description, LastModifiedDate FROM Flow WHERE DefinitionId IN "
        "(SELECT Id FROM FlowDefinition WHERE DeveloperName = "
        f"'{soql_literal(api_name)}') ORDER BY VersionNumber DESC LIMIT 50"
    )
    return [
        {
            "id": r.get("Id"),
            "version": r.get("VersionNumber"),
            "status": r.get("Status"),
            "label": r.get("MasterLabel"),
            "process_type": r.get("ProcessType"),
            "description": r.get("Description"),
            "last_modified": r.get("LastModifiedDate"),
        }
        for r in data.get("records") or []
    ]


async def get_flow_metadata(sf: SalesforceClient, flow_version_id: str) -> dict[str, Any]:
    """Full Flow metadata for one version, via the Tooling API sobject read."""
    return await sf.request(
        "GET", f"{sf.tooling_base}/sobjects/Flow/{flow_version_id}"
    )


def summarize_flow_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    """Reduce Flow metadata to what a human (or a model) needs to reason.

    Flow metadata is verbose and deeply nested; handing it to the model whole
    burns the context budget and buries the logic. This keeps the shape of the
    automation and drops the coordinates.
    """
    md = metadata.get("Metadata") or metadata
    start = md.get("start") or {}
    filters = [
        {
            "field": f.get("field"),
            "operator": f.get("operator"),
            "value": _flow_value(f.get("value")),
        }
        for f in (start.get("filters") or [])
    ]
    decisions = [
        {
            "name": d.get("name"),
            "label": d.get("label"),
            "rules": [
                {
                    "name": r.get("name"),
                    "label": r.get("label"),
                    "logic": r.get("conditionLogic"),
                    "conditions": [
                        {
                            "field": c.get("leftValueReference"),
                            "operator": c.get("operator"),
                            "value": _flow_value(c.get("rightValue")),
                        }
                        for c in (r.get("conditions") or [])
                    ],
                }
                for r in (d.get("rules") or [])
            ],
        }
        for d in (md.get("decisions") or [])
    ]
    assignments = [
        {
            "name": a.get("name"),
            "label": a.get("label"),
            "items": [
                {
                    "field": i.get("assignToReference"),
                    "operator": i.get("operator"),
                    "value": _flow_value(i.get("value")),
                }
                for i in (a.get("assignmentItems") or [])
            ],
        }
        for a in (md.get("assignments") or [])
    ]
    return {
        "label": md.get("label"),
        "description": md.get("description"),
        "process_type": md.get("processType"),
        "status": md.get("status"),
        "trigger": {
            "object": start.get("object"),
            "trigger_type": start.get("triggerType"),
            "record_trigger_type": start.get("recordTriggerType"),
            "filter_logic": start.get("filterLogic"),
            "filters": filters,
            "run_async": bool(start.get("scheduledPaths")),
        },
        "elements": {
            "decisions": decisions,
            "assignments": assignments,
            "record_updates": [
                {"name": u.get("name"), "object": u.get("object")}
                for u in (md.get("recordUpdates") or [])
            ],
            "record_creates": [
                {"name": c.get("name"), "object": c.get("object")}
                for c in (md.get("recordCreates") or [])
            ],
            "record_lookups": [
                {"name": lk.get("name"), "object": lk.get("object")}
                for lk in (md.get("recordLookups") or [])
            ],
            "action_calls": [
                {"name": a.get("name"), "type": a.get("actionType"), "action": a.get("actionName")}
                for a in (md.get("actionCalls") or [])
            ],
            "subflows": [
                {"name": s.get("name"), "flow": s.get("flowName")}
                for s in (md.get("subflows") or [])
            ],
        },
        "formulas": [
            {"name": f.get("name"), "expression": f.get("expression")}
            for f in (md.get("formulas") or [])
        ],
    }


def _flow_value(value: Any) -> Any:
    """Flow stores literals as {stringValue: ...} / {numberValue: ...}."""
    if not isinstance(value, dict):
        return value
    for key in (
        "stringValue", "numberValue", "booleanValue", "dateValue",
        "dateTimeValue", "elementReference",
    ):
        if key in value and value[key] is not None:
            return value[key]
    return value


def _plural_guess(name: str) -> str:
    if name.endswith("y"):
        return name[:-1] + "ies"
    if name.endswith(("s", "x", "z", "ch", "sh")):
        return name + "es"
    return name + "s"


# ---------------------------------------------------------------------- Apex
async def list_apex_classes(
    sf: SalesforceClient, *, name_like: str | None = None, limit: int = 200
) -> list[dict[str, Any]]:
    where = "WHERE NamespacePrefix = null"
    if name_like:
        where += f" AND Name LIKE '%{soql_literal(name_like)}%'"
    data = await sf.tooling_query(
        "SELECT Id, Name, ApiVersion, Status, LengthWithoutComments, "
        f"LastModifiedDate FROM ApexClass {where} ORDER BY Name LIMIT {int(limit)}"
    )
    return [
        {
            "id": r.get("Id"),
            "name": r.get("Name"),
            "api_version": r.get("ApiVersion"),
            "status": r.get("Status"),
            "length": r.get("LengthWithoutComments"),
            "last_modified": r.get("LastModifiedDate"),
            "is_test": str(r.get("Name") or "").lower().endswith("test")
            or str(r.get("Name") or "").lower().startswith("test"),
        }
        for r in data.get("records") or []
    ]


async def list_apex_triggers(
    sf: SalesforceClient, *, object_name: str | None = None
) -> list[dict[str, Any]]:
    where = "WHERE NamespacePrefix = null"
    if object_name:
        where += f" AND TableEnumOrId = '{soql_literal(object_name)}'"
    data = await sf.tooling_query(
        "SELECT Id, Name, TableEnumOrId, Status, ApiVersion, UsageBeforeInsert, "
        "UsageAfterInsert, UsageBeforeUpdate, UsageAfterUpdate, UsageBeforeDelete, "
        "UsageAfterDelete, UsageAfterUndelete, LengthWithoutComments, LastModifiedDate "
        f"FROM ApexTrigger {where} ORDER BY Name LIMIT 200"
    )
    out = []
    for r in data.get("records") or []:
        events = [
            label
            for key, label in (
                ("UsageBeforeInsert", "before insert"),
                ("UsageAfterInsert", "after insert"),
                ("UsageBeforeUpdate", "before update"),
                ("UsageAfterUpdate", "after update"),
                ("UsageBeforeDelete", "before delete"),
                ("UsageAfterDelete", "after delete"),
                ("UsageAfterUndelete", "after undelete"),
            )
            if r.get(key)
        ]
        out.append(
            {
                "id": r.get("Id"),
                "name": r.get("Name"),
                "object": r.get("TableEnumOrId"),
                "status": r.get("Status"),
                "events": events,
                "api_version": r.get("ApiVersion"),
                "length": r.get("LengthWithoutComments"),
                "last_modified": r.get("LastModifiedDate"),
            }
        )
    return out


async def get_apex_body(
    sf: SalesforceClient, kind: str, name: str
) -> dict[str, Any] | None:
    """Source of one ApexClass or ApexTrigger by name."""
    sobject = "ApexClass" if kind.lower() == "class" else "ApexTrigger"
    data = await sf.tooling_query(
        f"SELECT Id, Name, Body, ApiVersion, Status FROM {sobject} "
        f"WHERE Name = '{soql_literal(name)}' AND NamespacePrefix = null LIMIT 1"
    )
    records = data.get("records") or []
    if not records:
        return None
    record = records[0]
    return {
        "id": record.get("Id"),
        "name": record.get("Name"),
        "body": record.get("Body"),
        "api_version": record.get("ApiVersion"),
        "status": record.get("Status"),
    }


async def apex_code_coverage(sf: SalesforceClient, class_names: list[str]) -> dict[str, Any]:
    """Aggregate code coverage as Salesforce currently reports it.

    Coverage is only meaningful after tests have run in the org; when it is
    absent this returns empty rather than implying zero.
    """
    if not class_names:
        return {"coverage": [], "note": "No classes requested."}
    quoted = ", ".join(f"'{soql_literal(n)}'" for n in class_names[:50])
    data = await sf.tooling_query(
        "SELECT ApexClassOrTrigger.Name, NumLinesCovered, NumLinesUncovered "
        f"FROM ApexCodeCoverageAggregate WHERE ApexClassOrTrigger.Name IN ({quoted})"
    )
    rows = []
    for r in data.get("records") or []:
        covered = r.get("NumLinesCovered") or 0
        uncovered = r.get("NumLinesUncovered") or 0
        total = covered + uncovered
        rows.append(
            {
                "name": (r.get("ApexClassOrTrigger") or {}).get("Name"),
                "lines_covered": covered,
                "lines_uncovered": uncovered,
                "percent": round(covered / total * 100, 1) if total else None,
            }
        )
    return {
        "coverage": rows,
        "note": (
            "Coverage reflects the last test run recorded in this org. It is empty "
            "until tests have been executed."
            if not rows
            else ""
        ),
    }


# ------------------------------------------------------------ other automation
async def list_validation_rules(
    sf: SalesforceClient, object_name: str
) -> list[dict[str, Any]]:
    data = await sf.tooling_query(
        "SELECT Id, ValidationName, Active, Description, ErrorMessage, "
        "ErrorDisplayField, EntityDefinition.QualifiedApiName "
        "FROM ValidationRule WHERE EntityDefinition.QualifiedApiName = "
        f"'{soql_literal(object_name)}' LIMIT 200"
    )
    return [
        {
            "name": r.get("ValidationName"),
            "active": bool(r.get("Active")),
            "description": r.get("Description"),
            "error_message": r.get("ErrorMessage"),
            "error_field": r.get("ErrorDisplayField"),
        }
        for r in data.get("records") or []
    ]


async def get_validation_rule_formulas(
    sf: SalesforceClient, object_name: str
) -> list[dict[str, Any]]:
    """Validation rules including their formulas, read via the Metadata API.

    The Tooling ValidationRule object does not expose `errorConditionFormula`
    in a queryable form on every release, so the formula comes from a retrieve.
    """
    from app.salesforce.metadata import MetadataClient, MetadataReader

    reader = MetadataReader(
        MetadataClient(sf.instance_url, sf.access_token, sf.api_version)
    )
    result = await reader.retrieve({"CustomObject": [object_name]})
    source = result.files.get(f"objects/{object_name}.object", "")
    rules = []
    for block in re.findall(r"<validationRules>(.*?)</validationRules>", source, re.S):
        rules.append(
            {
                "name": _tag(block, "fullName"),
                "active": _tag(block, "active") == "true",
                "formula": _tag(block, "errorConditionFormula"),
                "error_message": _tag(block, "errorMessage"),
                "error_field": _tag(block, "errorDisplayField"),
                "description": _tag(block, "description"),
            }
        )
    return rules


def _tag(xml: str, tag: str) -> str:
    match = re.search(rf"<{tag}>(.*?)</{tag}>", xml, re.S)
    if not match:
        return ""
    from xml.sax.saxutils import unescape

    return unescape(match.group(1)).strip()


async def list_workflow_and_assignment(
    sf: SalesforceClient, object_name: str
) -> dict[str, Any]:
    """Legacy automation that still routes records: workflow and assignment rules."""
    from app.salesforce.metadata import MetadataClient, MetadataReader

    reader = MetadataReader(
        MetadataClient(sf.instance_url, sf.access_token, sf.api_version)
    )
    out: dict[str, Any] = {"workflow_rules": [], "assignment_rules": [], "notes": []}
    try:
        wf = await reader.retrieve({"Workflow": [object_name]})
        source = wf.files.get(f"workflows/{object_name}.workflow", "")
        for block in re.findall(r"<rules>(.*?)</rules>", source, re.S):
            out["workflow_rules"].append(
                {
                    "name": _tag(block, "fullName"),
                    "active": _tag(block, "active") == "true",
                    "trigger_type": _tag(block, "triggerType"),
                    "formula": _tag(block, "formula"),
                    "description": _tag(block, "description"),
                }
            )
    except SalesforceError as exc:
        out["notes"].append(f"No workflow metadata retrieved for {object_name}: {exc.message}")

    try:
        ar = await reader.retrieve({"AssignmentRules": [object_name]})
        source = ar.files.get(f"assignmentRules/{object_name}.assignmentRules", "")
        for block in re.findall(r"<assignmentRule>(.*?)</assignmentRule>", source, re.S):
            out["assignment_rules"].append(
                {
                    "name": _tag(block, "fullName"),
                    "active": _tag(block, "active") == "true",
                    "rule_entries": len(re.findall(r"<ruleEntry>", block)),
                }
            )
    except SalesforceError:
        out["notes"].append(
            f"{object_name} has no assignment rules, or they are not retrievable."
        )
    return out


async def list_record_types(sf: SalesforceClient, object_name: str) -> list[dict[str, Any]]:
    data = await sf.query(
        "SELECT Id, Name, DeveloperName, IsActive, Description FROM RecordType "
        f"WHERE SobjectType = '{soql_literal(object_name)}' LIMIT 100"
    )
    return [
        {
            "id": r.get("Id"),
            "name": r.get("Name"),
            "developer_name": r.get("DeveloperName"),
            "active": bool(r.get("IsActive")),
            "description": r.get("Description"),
        }
        for r in data.get("records") or []
    ]


async def list_duplicate_rules(sf: SalesforceClient, object_name: str) -> list[dict[str, Any]]:
    data = await sf.query(
        "SELECT Id, DeveloperName, MasterLabel, IsActive, SobjectType "
        f"FROM DuplicateRule WHERE SobjectType = '{soql_literal(object_name)}' LIMIT 50"
    )
    return [
        {
            "developer_name": r.get("DeveloperName"),
            "label": r.get("MasterLabel"),
            "active": bool(r.get("IsActive")),
        }
        for r in data.get("records") or []
    ]


# ---------------------------------------------------------------- dependencies
async def metadata_dependencies(
    sf: SalesforceClient, *, component_ids: list[str] | None = None,
    component_name: str | None = None, limit: int = 200,
) -> dict[str, Any]:
    """Query MetadataComponentDependency (Tooling API).

    This is Salesforce's own dependency graph — the authoritative answer to
    "what breaks if I delete this". It requires the Dependency API to be
    available in the org; when it is not, the caller falls back to a source
    scan and says which method produced the answer.
    """
    clauses = []
    if component_ids:
        quoted = ", ".join(f"'{soql_literal(c)}'" for c in component_ids[:20])
        clauses.append(f"RefMetadataComponentId IN ({quoted})")
    if component_name:
        clauses.append(f"RefMetadataComponentName = '{soql_literal(component_name)}'")
    if not clauses:
        return {"available": False, "reason": "No component specified.", "dependencies": []}

    soql = (
        "SELECT MetadataComponentId, MetadataComponentName, MetadataComponentType, "
        "RefMetadataComponentId, RefMetadataComponentName, RefMetadataComponentType "
        f"FROM MetadataComponentDependency WHERE {' AND '.join(clauses)} "
        f"LIMIT {int(limit)}"
    )
    try:
        data = await sf.tooling_query(soql)
    except SalesforceError as exc:
        return {
            "available": False,
            "reason": (
                "The Salesforce Dependency API did not answer: "
                f"{exc.error_type} — {exc.message}"
            ),
            "dependencies": [],
        }
    return {
        "available": True,
        "dependencies": [
            {
                "component": r.get("MetadataComponentName"),
                "component_type": r.get("MetadataComponentType"),
                "component_id": r.get("MetadataComponentId"),
                "depends_on": r.get("RefMetadataComponentName"),
                "depends_on_type": r.get("RefMetadataComponentType"),
            }
            for r in data.get("records") or []
        ],
    }


async def entity_definition_id(sf: SalesforceClient, object_name: str) -> str | None:
    data = await sf.tooling_query(
        "SELECT Id, DurableId, QualifiedApiName FROM EntityDefinition "
        f"WHERE QualifiedApiName = '{soql_literal(object_name)}' LIMIT 1"
    )
    records = data.get("records") or []
    return records[0].get("DurableId") if records else None


async def field_definition_id(
    sf: SalesforceClient, object_name: str, field_name: str
) -> str | None:
    """The 15/18-char CustomField id, needed for dependency lookups."""
    data = await sf.tooling_query(
        "SELECT Id, DeveloperName, TableEnumOrId FROM CustomField "
        f"WHERE DeveloperName = '{soql_literal(field_name.removesuffix('__c'))}' LIMIT 5"
    )
    entity = await entity_definition_id(sf, object_name)
    for record in data.get("records") or []:
        if entity and str(record.get("TableEnumOrId")) in {entity, object_name}:
            return record.get("Id")
    records = data.get("records") or []
    return records[0].get("Id") if records else None
