"""Evidence collection for org debugging.

Given a question about one record or one object, this gathers everything in the
org that could plausibly explain the behaviour — schema, automation, code,
validation, permissions, record types, sharing, ownership, field history — and
returns it as structured *observations*, not conclusions.

Collection is deliberately separated from diagnosis. The collector never
decides anything; it can only be wrong about what it saw, not about what it
means. That separation is what makes the diagnosis auditable: every finding
points back at a specific observation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.observability.logging import get_logger
from app.salesforce.client import SalesforceClient
from app.salesforce.errors import SalesforceError
from app.salesforce.inspect import (
    get_apex_body,
    get_flow_metadata,
    get_flow_versions,
    get_validation_rule_formulas,
    list_apex_triggers,
    list_flows,
    list_record_types,
    list_workflow_and_assignment,
    soql_literal,
    summarize_flow_metadata,
)

log = get_logger("diagnostics.collector")


@dataclass
class Observation:
    """One thing that is true about the org, with where it came from."""

    area: str
    summary: str
    detail: Any = None
    source: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "area": self.area,
            "observed": self.summary,
            "detail": self.detail,
            "source": self.source,
        }


@dataclass
class Evidence:
    object_name: str
    record_id: str | None = None
    observations: list[Observation] = field(default_factory=list)
    unavailable: list[str] = field(default_factory=list)

    def add(self, area: str, summary: str, detail: Any = None, source: str = "") -> None:
        self.observations.append(Observation(area, summary, detail, source))

    def by_area(self) -> dict[str, list[dict[str, Any]]]:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for observation in self.observations:
            grouped.setdefault(observation.area, []).append(observation.to_dict())
        return grouped

    def to_dict(self) -> dict[str, Any]:
        return {
            "object": self.object_name,
            "record_id": self.record_id,
            "observation_count": len(self.observations),
            "observations": self.by_area(),
            "could_not_inspect": self.unavailable,
        }


async def collect(
    sf: SalesforceClient,
    *,
    object_name: str,
    record_id: str | None = None,
    field_name: str | None = None,
    user_id: str | None = None,
    include_history: bool = True,
) -> Evidence:
    """Gather everything that bears on "why did this record behave this way"."""
    evidence = Evidence(object_name=object_name, record_id=record_id)

    await _schema(sf, evidence, object_name, field_name)
    if record_id:
        await _record(sf, evidence, object_name, record_id)
        if include_history:
            await _history(sf, evidence, object_name, record_id)
    await _automation(sf, evidence, object_name)
    await _validation(sf, evidence, object_name)
    await _record_types(sf, evidence, object_name)
    await _assignment(sf, evidence, object_name)
    if field_name:
        await _field_permissions(sf, evidence, object_name, field_name, user_id)
    if user_id:
        await _user(sf, evidence, user_id, object_name)
    return evidence


async def _schema(
    sf: SalesforceClient, evidence: Evidence, object_name: str, field_name: str | None
) -> None:
    try:
        describe = await sf.describe(object_name)
    except SalesforceError as exc:
        evidence.unavailable.append(f"describe({object_name}): {exc.message}")
        return
    evidence.add(
        "schema",
        f"{object_name} is "
        + ("creatable" if describe.get("createable") else "not creatable")
        + ", "
        + ("updateable" if describe.get("updateable") else "not updateable")
        + f", with {len(describe.get('fields') or [])} fields.",
        {
            "createable": describe.get("createable"),
            "updateable": describe.get("updateable"),
            "deletable": describe.get("deletable"),
            "custom": describe.get("custom"),
        },
        source="REST describe",
    )
    if field_name:
        match = next(
            (
                f
                for f in describe.get("fields") or []
                if f["name"].lower() == field_name.lower()
            ),
            None,
        )
        if match is None:
            evidence.add(
                "schema",
                f"{object_name} has no field named '{field_name}'.",
                source="REST describe",
            )
        else:
            evidence.add(
                "schema",
                f"{field_name} is a {match.get('type')} field, "
                + ("updateable" if match.get("updateable") else "NOT updateable")
                + (", calculated (formula)" if match.get("calculated") else "")
                + (", required" if not match.get("nillable") else ""),
                {
                    "type": match.get("type"),
                    "updateable": match.get("updateable"),
                    "createable": match.get("createable"),
                    "calculated": match.get("calculated"),
                    "calculatedFormula": match.get("calculatedFormula"),
                    "nillable": match.get("nillable"),
                    "restrictedPicklist": match.get("restrictedPicklist"),
                    "picklistValues": [
                        p.get("value")
                        for p in (match.get("picklistValues") or [])
                        if p.get("active")
                    ][:25],
                },
                source="REST describe",
            )


async def _record(
    sf: SalesforceClient, evidence: Evidence, object_name: str, record_id: str
) -> None:
    try:
        record = await sf.get_record(object_name, record_id)
    except SalesforceError as exc:
        evidence.unavailable.append(f"record {record_id}: {exc.message}")
        evidence.add(
            "record",
            f"The record {record_id} could not be read: {exc.message}",
            source="REST sobject read",
        )
        return
    interesting = {
        k: v
        for k, v in record.items()
        if k in {"Id", "Name", "OwnerId", "RecordTypeId", "CreatedDate",
                 "LastModifiedDate", "LastModifiedById", "IsDeleted", "StageName",
                 "Status", "IsConverted", "AccountId"}
    }
    evidence.add(
        "record",
        f"{object_name} {record_id} exists and is readable by the connected user.",
        interesting,
        source="REST sobject read",
    )
    owner_id = record.get("OwnerId")
    if owner_id:
        try:
            owner = await sf.query(
                "SELECT Id, Name, IsActive, Profile.Name, UserRole.Name FROM User "
                f"WHERE Id = '{soql_literal(str(owner_id))}' LIMIT 1"
            )
            rows = owner.get("records") or []
            if rows:
                who = rows[0]
                evidence.add(
                    "ownership",
                    f"The record is owned by {who.get('Name')} "
                    + ("(active)" if who.get("IsActive") else "(INACTIVE user)"),
                    {
                        "owner_id": owner_id,
                        "active": who.get("IsActive"),
                        "profile": (who.get("Profile") or {}).get("Name"),
                        "role": (who.get("UserRole") or {}).get("Name"),
                    },
                    source="SOQL on User",
                )
        except SalesforceError:
            pass


async def _history(
    sf: SalesforceClient, evidence: Evidence, object_name: str, record_id: str
) -> None:
    """Field history, when the object has it enabled.

    History is the difference between "something changed this" and "nothing
    ever set it" — the single most useful signal when debugging a field that
    holds an unexpected value.
    """
    history_object = (
        f"{object_name[:-3]}__History" if object_name.endswith("__c")
        else f"{object_name}History"
    )
    try:
        data = await sf.query(
            "SELECT Field, OldValue, NewValue, CreatedDate, CreatedBy.Name "
            f"FROM {history_object} WHERE {_history_parent(object_name)} = "
            f"'{soql_literal(record_id)}' ORDER BY CreatedDate DESC LIMIT 50"
        )
    except SalesforceError as exc:
        evidence.unavailable.append(
            f"{history_object}: {exc.message} "
            "(field history tracking may not be enabled for this object)"
        )
        return
    rows = data.get("records") or []
    if not rows:
        evidence.add(
            "history",
            "Field history tracking is enabled but no tracked field has ever "
            "changed on this record.",
            source=f"SOQL on {history_object}",
        )
        return
    evidence.add(
        "history",
        f"{len(rows)} tracked field change(s) on this record; most recent "
        f"{rows[0].get('Field')} by {(rows[0].get('CreatedBy') or {}).get('Name')} "
        f"at {rows[0].get('CreatedDate')}.",
        [
            {
                "field": r.get("Field"),
                "from": r.get("OldValue"),
                "to": r.get("NewValue"),
                "by": (r.get("CreatedBy") or {}).get("Name"),
                "at": r.get("CreatedDate"),
            }
            for r in rows[:20]
        ],
        source=f"SOQL on {history_object}",
    )


def _history_parent(object_name: str) -> str:
    return "ParentId" if object_name.endswith("__c") else f"{object_name}Id"


async def _automation(
    sf: SalesforceClient, evidence: Evidence, object_name: str
) -> None:
    try:
        flows = await list_flows(sf, object_name=object_name)
    except SalesforceError as exc:
        evidence.unavailable.append(f"flows: {exc.message}")
        flows = []
    active = [f for f in flows if f.get("is_active")]
    if active:
        details = []
        for flow in active[:10]:
            summary: dict[str, Any] = {
                "api_name": flow.get("api_name"),
                "label": flow.get("label"),
                "trigger_type": flow.get("trigger_type"),
            }
            try:
                versions = await get_flow_versions(sf, str(flow.get("api_name")))
                current = next((v for v in versions if v["status"] == "Active"), None)
                if current:
                    summary["logic"] = summarize_flow_metadata(
                        await get_flow_metadata(sf, current["id"])
                    )
            except SalesforceError:
                summary["logic"] = None
            details.append(summary)
        evidence.add(
            "automation",
            f"{len(active)} active flow(s) run on {object_name}.",
            details,
            source="FlowDefinitionView + Tooling Flow metadata",
        )
    else:
        evidence.add(
            "automation",
            f"No active flows are triggered by {object_name}.",
            source="FlowDefinitionView",
        )

    try:
        triggers = await list_apex_triggers(sf, object_name=object_name)
    except SalesforceError as exc:
        evidence.unavailable.append(f"triggers: {exc.message}")
        triggers = []
    if triggers:
        bodies = []
        for trigger in triggers[:5]:
            record = None
            try:
                record = await get_apex_body(sf, "trigger", str(trigger["name"]))
            except SalesforceError:
                pass
            bodies.append(
                {
                    "name": trigger["name"],
                    "events": trigger["events"],
                    "status": trigger["status"],
                    "body": (record or {}).get("body", "")[:4000],
                }
            )
        evidence.add(
            "automation",
            f"{len(triggers)} Apex trigger(s) on {object_name}: "
            + ", ".join(f"{t['name']} ({', '.join(t['events'])})" for t in triggers[:5]),
            bodies,
            source="Tooling ApexTrigger",
        )
    else:
        evidence.add(
            "automation", f"No Apex triggers exist on {object_name}.",
            source="Tooling ApexTrigger",
        )


async def _validation(
    sf: SalesforceClient, evidence: Evidence, object_name: str
) -> None:
    try:
        rules = await get_validation_rule_formulas(sf, object_name)
    except SalesforceError as exc:
        evidence.unavailable.append(f"validation rules: {exc.message}")
        return
    active = [r for r in rules if r.get("active")]
    if not active:
        evidence.add(
            "validation",
            f"{object_name} has no active validation rules.",
            source="Metadata API retrieve",
        )
        return
    evidence.add(
        "validation",
        f"{len(active)} active validation rule(s) on {object_name}.",
        [
            {
                "name": r["name"],
                "formula": r["formula"],
                "error_message": r["error_message"],
                "error_field": r["error_field"],
            }
            for r in active[:15]
        ],
        source="Metadata API retrieve",
    )


async def _record_types(
    sf: SalesforceClient, evidence: Evidence, object_name: str
) -> None:
    try:
        types = await list_record_types(sf, object_name)
    except SalesforceError:
        return
    if types:
        evidence.add(
            "record_types",
            f"{object_name} uses {len(types)} record type(s): "
            + ", ".join(t["developer_name"] for t in types[:8]),
            types[:10],
            source="SOQL on RecordType",
        )


async def _assignment(
    sf: SalesforceClient, evidence: Evidence, object_name: str
) -> None:
    if object_name not in {"Lead", "Case"}:
        return
    result = await list_workflow_and_assignment(sf, object_name)
    rules = result.get("assignment_rules") or []
    if rules:
        active = [r for r in rules if r.get("active")]
        evidence.add(
            "assignment",
            f"{object_name} has {len(rules)} assignment rule set(s), "
            f"{len(active)} active.",
            rules,
            source="Metadata API retrieve",
        )
    else:
        evidence.add(
            "assignment",
            f"No assignment rules were found for {object_name}. Records keep the "
            "owner they are created with unless something else sets it.",
            source="Metadata API retrieve",
        )
    workflows = result.get("workflow_rules") or []
    if workflows:
        evidence.add(
            "automation",
            f"{len(workflows)} legacy workflow rule(s) exist on {object_name}.",
            workflows[:10],
            source="Metadata API retrieve",
        )


async def _field_permissions(
    sf: SalesforceClient,
    evidence: Evidence,
    object_name: str,
    field_name: str,
    user_id: str | None,
) -> None:
    """Who can see and edit this field — the usual answer to "why can't I edit"."""
    try:
        data = await sf.query(
            "SELECT Parent.Profile.Name, Parent.Label, Parent.IsOwnedByProfile, "
            "PermissionsRead, PermissionsEdit FROM FieldPermissions "
            f"WHERE SobjectType = '{soql_literal(object_name)}' "
            f"AND Field = '{soql_literal(f'{object_name}.{field_name}')}' LIMIT 200"
        )
    except SalesforceError as exc:
        evidence.unavailable.append(f"field permissions: {exc.message}")
        return
    rows = data.get("records") or []
    if not rows:
        evidence.add(
            "permissions",
            f"No profile or permission set grants any access to "
            f"{object_name}.{field_name}. Only users with 'View All'/'Modify All' "
            "or the System Administrator profile will see it.",
            source="SOQL on FieldPermissions",
        )
        return
    editable = [r for r in rows if r.get("PermissionsEdit")]
    evidence.add(
        "permissions",
        f"{len(rows)} profile(s)/permission set(s) can read {field_name}; "
        f"{len(editable)} can edit it.",
        [
            {
                "granted_by": (r.get("Parent") or {}).get("Label"),
                "profile": ((r.get("Parent") or {}).get("Profile") or {}).get("Name"),
                "is_profile": (r.get("Parent") or {}).get("IsOwnedByProfile"),
                "read": r.get("PermissionsRead"),
                "edit": r.get("PermissionsEdit"),
            }
            for r in rows[:25]
        ],
        source="SOQL on FieldPermissions",
    )


async def _user(
    sf: SalesforceClient, evidence: Evidence, user_id: str, object_name: str
) -> None:
    try:
        data = await sf.query(
            "SELECT Id, Name, IsActive, Profile.Name, Profile.PermissionsModifyAllData, "
            "Profile.PermissionsViewAllData, UserRole.Name, UserType FROM User "
            f"WHERE Id = '{soql_literal(user_id)}' LIMIT 1"
        )
    except SalesforceError as exc:
        evidence.unavailable.append(f"user {user_id}: {exc.message}")
        return
    rows = data.get("records") or []
    if not rows:
        evidence.add("user", f"No user with id {user_id} exists.", source="SOQL on User")
        return
    user = rows[0]
    profile = user.get("Profile") or {}
    evidence.add(
        "user",
        f"{user.get('Name')} is on the {profile.get('Name')} profile"
        + (" (INACTIVE)" if not user.get("IsActive") else "")
        + (
            " and has Modify All Data."
            if profile.get("PermissionsModifyAllData")
            else "."
        ),
        {
            "active": user.get("IsActive"),
            "profile": profile.get("Name"),
            "modify_all_data": profile.get("PermissionsModifyAllData"),
            "view_all_data": profile.get("PermissionsViewAllData"),
            "role": (user.get("UserRole") or {}).get("Name"),
            "user_type": user.get("UserType"),
        },
        source="SOQL on User",
    )

    try:
        perms = await sf.query(
            "SELECT PermissionsRead, PermissionsCreate, PermissionsEdit, "
            "PermissionsDelete, Parent.Label, Parent.IsOwnedByProfile "
            f"FROM ObjectPermissions WHERE SobjectType = '{soql_literal(object_name)}' "
            "AND ParentId IN (SELECT PermissionSetId FROM PermissionSetAssignment "
            f"WHERE AssigneeId = '{soql_literal(user_id)}') LIMIT 100"
        )
    except SalesforceError:
        return
    rows = perms.get("records") or []
    if not rows:
        evidence.add(
            "permissions",
            f"This user has no object permissions on {object_name} through any "
            "profile or permission set.",
            source="SOQL on ObjectPermissions",
        )
        return
    combined = {
        "read": any(r.get("PermissionsRead") for r in rows),
        "create": any(r.get("PermissionsCreate") for r in rows),
        "edit": any(r.get("PermissionsEdit") for r in rows),
        "delete": any(r.get("PermissionsDelete") for r in rows),
    }
    evidence.add(
        "permissions",
        f"On {object_name} this user has: "
        + ", ".join(k for k, v in combined.items() if v)
        + (" — and nothing else." if not all(combined.values()) else "."),
        {"effective": combined, "granted_by": [
            (r.get("Parent") or {}).get("Label") for r in rows[:20]
        ]},
        source="SOQL on ObjectPermissions",
    )
