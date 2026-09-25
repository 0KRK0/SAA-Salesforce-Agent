"""Metadata dependency and impact analysis.

"What breaks if I delete this field?" is the question that separates an admin
tool from an assistant, and it is answered here with two independent methods
that are always reported separately:

  1. **The Salesforce Dependency API** (MetadataComponentDependency). This is
     the org's own answer and it is authoritative — but it is not available in
     every org, and it does not see everything (formulas in some contexts,
     references inside managed packages, hardcoded strings in Apex).

  2. **A source scan.** Flow metadata, Apex bodies, validation-rule formulas
     and report definitions are retrieved and searched for the component's API
     name. This catches what the Dependency API misses, at the cost of some
     false positives from comments and similarly-named fields.

The result always says which method found what, and never claims a component is
safe to delete on the basis of one silent method. "No dependencies found" and
"the Dependency API is unavailable" are different answers, and conflating them
is how someone deletes a field that three flows use.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.observability.logging import get_logger
from app.salesforce.client import SalesforceClient
from app.salesforce.errors import SalesforceError
from app.salesforce.inspect import (
    field_definition_id,
    get_apex_body,
    get_flow_metadata,
    get_flow_versions,
    list_apex_classes,
    list_apex_triggers,
    list_flows,
    metadata_dependencies,
    soql_literal,
)

log = get_logger("salesforce.dependencies")

IMPACT_NONE = "NONE"
IMPACT_LOW = "LOW"
IMPACT_MEDIUM = "MEDIUM"
IMPACT_HIGH = "HIGH"


@dataclass
class Dependency:
    component: str
    component_type: str
    method: str  # 'dependency_api' | 'source_scan'
    detail: str = ""
    confidence: str = "high"

    def to_dict(self) -> dict[str, Any]:
        return {
            "component": self.component,
            "type": self.component_type,
            "found_by": self.method,
            "detail": self.detail,
            "confidence": self.confidence,
        }


@dataclass
class DependencyReport:
    target: str
    target_type: str
    dependencies: list[Dependency] = field(default_factory=list)
    dependency_api_available: bool = False
    dependency_api_note: str = ""
    scanned: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    @property
    def impact(self) -> str:
        if not self.dependencies:
            return IMPACT_NONE
        types = {d.component_type for d in self.dependencies}
        count = len(self.dependencies)
        # Code and automation breaking is worse than a report needing an edit.
        if types & {"ApexClass", "ApexTrigger", "Flow", "ValidationRule"}:
            return IMPACT_HIGH if count > 2 else IMPACT_MEDIUM
        return IMPACT_MEDIUM if count > 3 else IMPACT_LOW

    def recommendation(self) -> str:
        if not self.dependencies:
            if not self.dependency_api_available:
                return (
                    "No references were found by the source scan, but the Salesforce "
                    "Dependency API was unavailable, so this is not a complete answer. "
                    "Check Setup > Object Manager > Field > Where is this used? before "
                    "deleting."
                )
            return (
                "No dependencies were found by either method. Deletion still cannot be "
                "undone, so confirm with the field's owner first."
            )
        blockers = [
            d for d in self.dependencies
            if d.component_type in {"ApexClass", "ApexTrigger", "Flow", "ValidationRule"}
        ]
        if blockers:
            return (
                f"Do not delete yet: {len(blockers)} automation or code component(s) "
                "reference this. Remove or update those references first, in a change "
                "set that deploys together."
            )
        return (
            f"{len(self.dependencies)} component(s) reference this. Update them first; "
            "none of them will block the delete, but each will silently lose the field."
        )

    def to_dict(self) -> dict[str, Any]:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for dependency in self.dependencies:
            grouped.setdefault(dependency.component_type, []).append(dependency.to_dict())
        return {
            "target": self.target,
            "target_type": self.target_type,
            "impact": self.impact,
            "dependency_count": len(self.dependencies),
            "by_type": {k: len(v) for k, v in grouped.items()},
            "dependencies": grouped,
            "methods": {
                "dependency_api": {
                    "available": self.dependency_api_available,
                    "note": self.dependency_api_note,
                },
                "source_scan": {"scanned": self.scanned},
            },
            "recommendation": self.recommendation(),
            "errors": self.errors,
        }


def _reference_pattern(api_name: str) -> re.Pattern[str]:
    """Match the API name as a whole token, not as a substring.

    Without the boundaries, `Tier__c` matches `Customer_Tier__c` and the report
    fills with false positives that make it useless.
    """
    return re.compile(rf"(?<![A-Za-z0-9_]){re.escape(api_name)}(?![A-Za-z0-9_])")


async def analyze_field(
    sf: SalesforceClient,
    object_name: str,
    field_name: str,
    *,
    scan_source: bool = True,
    max_apex: int = 150,
) -> DependencyReport:
    """Everything in the org that references one field."""
    report = DependencyReport(target=f"{object_name}.{field_name}", target_type="CustomField")
    pattern = _reference_pattern(field_name)

    # --- method 1: the org's own dependency graph ---
    try:
        component_id = await field_definition_id(sf, object_name, field_name)
    except SalesforceError as exc:
        component_id = None
        report.errors.append(f"Could not resolve the field's component id: {exc.message}")

    if component_id:
        result = await metadata_dependencies(sf, component_ids=[component_id])
        report.dependency_api_available = bool(result.get("available"))
        report.dependency_api_note = str(result.get("reason") or "")
        for row in result.get("dependencies") or []:
            report.dependencies.append(
                Dependency(
                    component=str(row.get("component")),
                    component_type=str(row.get("component_type")),
                    method="dependency_api",
                    detail="Reported by the Salesforce Dependency API.",
                )
            )
    else:
        report.dependency_api_note = (
            "The field's component id could not be resolved, so the Dependency API "
            "was not queried. It may be a standard field."
        )

    if not scan_source:
        return report

    seen = {(d.component, d.component_type) for d in report.dependencies}

    # --- method 2: source scan ---
    await _scan_flows(sf, pattern, report, seen)
    await _scan_apex(sf, pattern, report, seen, max_apex=max_apex)
    await _scan_validation_rules(sf, object_name, pattern, report, seen)
    await _scan_reports(sf, object_name, field_name, report, seen)
    return report


async def _scan_flows(
    sf: SalesforceClient,
    pattern: re.Pattern[str],
    report: DependencyReport,
    seen: set[tuple[str, str]],
) -> None:
    try:
        flows = await list_flows(sf)
    except SalesforceError as exc:
        report.errors.append(f"Flows could not be listed: {exc.message}")
        return
    checked = 0
    for flow in flows[:200]:
        api_name = flow.get("api_name")
        if not api_name:
            continue
        try:
            versions = await get_flow_versions(sf, api_name)
            target = next((v for v in versions if v["status"] == "Active"), None)
            if target is None:
                # An inactive flow is not currently breaking, but it will break
                # the moment someone activates it — worth reporting, flagged.
                target = versions[0] if versions else None
            if target is None:
                continue
            metadata = await get_flow_metadata(sf, target["id"])
        except SalesforceError:
            continue
        checked += 1
        blob = str(metadata.get("Metadata") or metadata)
        if pattern.search(blob):
            key = (str(api_name), "Flow")
            if key in seen:
                continue
            seen.add(key)
            report.dependencies.append(
                Dependency(
                    component=str(api_name),
                    component_type="Flow",
                    method="source_scan",
                    detail=(
                        f"Referenced in flow version {target['version']} "
                        f"({target['status']})."
                    ),
                    confidence="high" if target["status"] == "Active" else "medium",
                )
            )
    report.scanned["flows"] = checked


async def _scan_apex(
    sf: SalesforceClient,
    pattern: re.Pattern[str],
    report: DependencyReport,
    seen: set[tuple[str, str]],
    *,
    max_apex: int,
) -> None:
    checked = 0
    try:
        units = [("class", c["name"]) for c in await list_apex_classes(sf, limit=max_apex)]
        units += [("trigger", t["name"]) for t in await list_apex_triggers(sf)]
    except SalesforceError as exc:
        report.errors.append(f"Apex could not be listed: {exc.message}")
        return

    for kind, name in units[:max_apex]:
        try:
            record = await get_apex_body(sf, kind, name)
        except SalesforceError:
            continue
        if record is None:
            continue
        checked += 1
        body = record.get("body") or ""
        if not pattern.search(body):
            continue
        component_type = "ApexClass" if kind == "class" else "ApexTrigger"
        key = (name, component_type)
        if key in seen:
            continue
        seen.add(key)
        # A hit inside a comment is a false positive; say so instead of
        # dropping it, because the reader can judge and we cannot.
        in_comment_only = _only_in_comments(body, pattern)
        report.dependencies.append(
            Dependency(
                component=name,
                component_type=component_type,
                method="source_scan",
                detail=(
                    "Appears only inside comments — likely not a real dependency."
                    if in_comment_only
                    else "Referenced in the Apex source."
                ),
                confidence="low" if in_comment_only else "high",
            )
        )
    report.scanned["apex"] = checked


def _only_in_comments(body: str, pattern: re.Pattern[str]) -> bool:
    stripped = re.sub(r"//[^\n]*", "", re.sub(r"/\*.*?\*/", "", body, flags=re.DOTALL))
    return not pattern.search(stripped)


async def _scan_validation_rules(
    sf: SalesforceClient,
    object_name: str,
    pattern: re.Pattern[str],
    report: DependencyReport,
    seen: set[tuple[str, str]],
) -> None:
    from app.salesforce.inspect import get_validation_rule_formulas

    try:
        rules = await get_validation_rule_formulas(sf, object_name)
    except SalesforceError as exc:
        report.errors.append(f"Validation rules could not be retrieved: {exc.message}")
        return
    for rule in rules:
        haystack = f"{rule.get('formula', '')} {rule.get('error_field', '')}"
        if not pattern.search(haystack):
            continue
        key = (str(rule.get("name")), "ValidationRule")
        if key in seen:
            continue
        seen.add(key)
        report.dependencies.append(
            Dependency(
                component=str(rule.get("name")),
                component_type="ValidationRule",
                method="source_scan",
                detail=(
                    "Used in the rule's formula."
                    + ("" if rule.get("active") else " (rule is inactive)")
                ),
                confidence="high" if rule.get("active") else "medium",
            )
        )
    report.scanned["validation_rules"] = len(rules)


async def _scan_reports(
    sf: SalesforceClient,
    object_name: str,
    field_name: str,
    report: DependencyReport,
    seen: set[tuple[str, str]],
) -> None:
    """Reports referencing the field, via the report metadata description."""
    try:
        data = await sf.query(
            "SELECT Id, Name, DeveloperName, Format FROM Report "
            "WHERE Name != null LIMIT 200"
        )
    except SalesforceError as exc:
        report.errors.append(f"Reports could not be listed: {exc.message}")
        return
    reports = data.get("records") or []
    checked = 0
    for row in reports:
        try:
            described = await sf.request(
                "GET", f"{sf.base}/analytics/reports/{row['Id']}/describe"
            )
        except SalesforceError:
            continue
        checked += 1
        metadata = described.get("reportMetadata") or {}
        columns = set(metadata.get("detailColumns") or [])
        groupings = {
            g.get("name") for g in (metadata.get("groupingsDown") or [])
            + (metadata.get("groupingsAcross") or [])
        }
        filters = {f.get("column") for f in (metadata.get("reportFilters") or [])}
        referenced = {c.split(".")[-1] for c in (columns | groupings | filters) if c}
        if field_name in referenced:
            key = (str(row.get("Name")), "Report")
            if key in seen:
                continue
            seen.add(key)
            report.dependencies.append(
                Dependency(
                    component=str(row.get("Name")),
                    component_type="Report",
                    method="source_scan",
                    detail="Used as a column, grouping or filter.",
                )
            )
    report.scanned["reports"] = checked


async def analyze_component(
    sf: SalesforceClient, component_name: str, component_type: str
) -> DependencyReport:
    """Dependencies of a non-field component (a flow, an Apex class, an object)."""
    report = DependencyReport(target=component_name, target_type=component_type)
    result = await metadata_dependencies(sf, component_name=component_name)
    report.dependency_api_available = bool(result.get("available"))
    report.dependency_api_note = str(result.get("reason") or "")
    for row in result.get("dependencies") or []:
        report.dependencies.append(
            Dependency(
                component=str(row.get("component")),
                component_type=str(row.get("component_type")),
                method="dependency_api",
                detail="Reported by the Salesforce Dependency API.",
            )
        )

    pattern = _reference_pattern(component_name)
    seen = {(d.component, d.component_type) for d in report.dependencies}
    await _scan_apex(sf, pattern, report, seen, max_apex=150)
    await _scan_flows(sf, pattern, report, seen)
    return report


async def referencing_objects(sf: SalesforceClient, object_name: str) -> list[dict[str, Any]]:
    """Objects with a lookup or master-detail pointing at this one."""
    try:
        data = await sf.tooling_query(
            "SELECT EntityDefinition.QualifiedApiName, QualifiedApiName, DataType "
            "FROM FieldDefinition WHERE DataType LIKE '%"
            f"{soql_literal(object_name)}%' LIMIT 200"
        )
    except SalesforceError:
        return []
    return [
        {
            "object": (r.get("EntityDefinition") or {}).get("QualifiedApiName"),
            "field": r.get("QualifiedApiName"),
            "type": r.get("DataType"),
        }
        for r in data.get("records") or []
    ]
