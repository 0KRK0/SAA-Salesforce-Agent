"""Flow agent: inspect, design, deploy and activate record-triggered flows.

The workflow this implements end to end:

    understand → inspect object → inspect existing flows → detect overlap
    → design → generate metadata → validate (check-only deploy)
    → propose (human-readable plan) → approval → deploy → verify → report

Two things it will not do, because both produce silent breakage:

  * Invent a field. Every field named in a condition or assignment is checked
    against the org's describe, and its Salesforce type drives how the value is
    encoded in the Flow metadata.
  * Claim a flow exists or is active without reading it back from the org.
"""

from __future__ import annotations

from typing import Any

from app.models import RiskLevel
from app.salesforce.errors import SalesforceError
from app.salesforce.flow import (
    AFTER_SAVE,
    BEFORE_SAVE,
    OPERATORS,
    RECORD_TRIGGER_TYPES,
    Condition,
    FieldAssignment,
    FlowBuildError,
    FlowSpec,
    build_flow_definition_xml,
    build_flow_xml,
    describe_flow_plan,
    normalize_flow_api_name,
)
from app.salesforce.inspect import (
    get_flow_metadata,
    get_flow_versions,
    list_flows,
    summarize_flow_metadata,
)
from app.tools._deploy import deploy_package
from app.tools.base import Tool, ToolContext, ToolValidationError, fail, ok
from app.tools.registry import registry

# ---------------------------------------------------------------------------
# list_flows
# ---------------------------------------------------------------------------
LIST_DESCRIPTION = """List the flows in the Salesforce org.

Use this before creating any flow: it is how you find out whether automation
for this behaviour already exists. Creating a second flow that fights an
existing one is a real and common way to break an org.

Filter by `object` to see only flows triggered by that object.
"""


async def _list_execute(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    sf = ctx.require_sf()
    flows = await list_flows(
        sf,
        object_name=args.get("object") or None,
        active_only=bool(args.get("active_only")),
    )
    active = [f for f in flows if f.get("is_active")]
    return ok(
        count=len(flows),
        active_count=len(active),
        object=args.get("object"),
        flows=flows[: int(args.get("limit") or 100)],
    )


registry.register(
    Tool(
        name="list_flows",
        description=LIST_DESCRIPTION,
        input_schema={
            "type": "object",
            "properties": {
                "object": {"type": "string", "description": "Filter by triggering object."},
                "active_only": {"type": "boolean"},
                "limit": {"type": "integer", "default": 100},
            },
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "properties": {
                "success": {"type": "boolean"},
                "count": {"type": "integer"},
                "flows": {"type": "array"},
            },
        },
        risk=RiskLevel.LOW,
        requires_approval=False,
        execute=_list_execute,
        audit_action="salesforce.list_flows",
        tags=["flow", "automation", "read"],
    )
)


# ---------------------------------------------------------------------------
# inspect_flow
# ---------------------------------------------------------------------------
INSPECT_DESCRIPTION = """Read one flow's actual logic: trigger, entry conditions,
decisions, assignments and record operations.

Returns a summarized view of the real Flow metadata, not a description of what
the flow is named. Use it to answer "what does this automation actually do",
to check for conflicts before adding automation, and when debugging why a
record was or was not updated.
"""


async def _inspect_execute(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    sf = ctx.require_sf()
    api_name = str(args.get("api_name") or "").strip()
    if not api_name:
        return fail("MISSING_ARGUMENT", "`api_name` is required.")

    versions = await get_flow_versions(sf, api_name)
    if not versions:
        return fail(
            "FLOW_NOT_FOUND",
            f"No flow named '{api_name}' exists in this org.",
            suggested_action="Call list_flows to see the flows that do exist.",
        )

    wanted = args.get("version")
    if wanted:
        target = next((v for v in versions if v["version"] == int(wanted)), None)
        if target is None:
            return fail(
                "FLOW_VERSION_NOT_FOUND",
                f"Flow '{api_name}' has no version {wanted}.",
                versions=[v["version"] for v in versions],
            )
    else:
        target = next((v for v in versions if v["status"] == "Active"), versions[0])

    metadata = await get_flow_metadata(sf, target["id"])
    return ok(
        api_name=api_name,
        version=target["version"],
        status=target["status"],
        versions=[
            {"version": v["version"], "status": v["status"], "modified": v["last_modified"]}
            for v in versions
        ],
        flow=summarize_flow_metadata(metadata),
    )


registry.register(
    Tool(
        name="inspect_flow",
        description=INSPECT_DESCRIPTION,
        input_schema={
            "type": "object",
            "properties": {
                "api_name": {"type": "string"},
                "version": {"type": "integer", "description": "Defaults to the active version."},
            },
            "required": ["api_name"],
            "additionalProperties": False,
        },
        output_schema={"type": "object", "properties": {"success": {"type": "boolean"}}},
        risk=RiskLevel.LOW,
        requires_approval=False,
        execute=_inspect_execute,
        audit_action="salesforce.inspect_flow",
        tags=["flow", "automation", "read"],
    )
)


# ---------------------------------------------------------------------------
# create_flow / update_flow — shared machinery
# ---------------------------------------------------------------------------
CREATE_DESCRIPTION = f"""Create a record-triggered flow that sets fields when
conditions are met.

Call describe_object and list_flows first. This tool refuses to guess: every
field you name is verified against the org, and its Salesforce type decides how
the value is encoded.

Choose the trigger deliberately:
  before_save — set fields on the record being saved. No extra DML. This is the
    right choice for "when X, set field Y on the same record".
  after_save  — runs after the save; can update the record (costs a second DML)
    and is required if you need the record's Id or related records.

Entry conditions: either `entry_conditions` (a list of field comparisons) or
`entry_formula` (a Flow formula string) — not both. Use a formula for anything
involving dates or arithmetic, e.g.
  AND({{!$Record.CloseDate}} <= TODAY() + 14, {{!$Record.Probability}} < 40)

Supported operators: {', '.join(sorted(set(OPERATORS.values())))}.

Scope: record-triggered flows that evaluate conditions and set fields. Screen
flows, loops, scheduled paths and invocable actions are not supported and this
tool will say so rather than emit a flow that ignores them.

Flows are deployed as Draft unless `activate` is true. Activating automation is
a behaviour change on live records and always requires approval.
"""

INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "api_name": {"type": "string", "description": "Flow API name (no spaces)."},
        "label": {"type": "string", "description": "Human-readable flow label."},
        "object": {"type": "string", "description": "Triggering object API name."},
        "trigger": {"type": "string", "enum": [BEFORE_SAVE, AFTER_SAVE], "default": BEFORE_SAVE},
        "record_trigger_type": {
            "type": "string",
            "enum": sorted(RECORD_TRIGGER_TYPES),
            "default": "CreateAndUpdate",
        },
        "description": {"type": "string"},
        "entry_conditions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "field": {"type": "string"},
                    "operator": {"type": "string"},
                    "value": {},
                },
                "required": ["field", "operator"],
            },
        },
        "entry_logic": {"type": "string", "enum": ["and", "or"], "default": "and"},
        "entry_formula": {
            "type": "string",
            "description": "Flow formula for the entry condition, e.g. relative dates.",
        },
        "assignments": {
            "type": "array",
            "description": "Fields to set when the flow runs.",
            "items": {
                "type": "object",
                "properties": {"field": {"type": "string"}, "value": {}},
                "required": ["field", "value"],
            },
        },
        "activate": {"type": "boolean", "default": False},
        "check_only": {"type": "boolean", "description": "Validate without saving."},
        "reason": {"type": "string"},
    },
    "required": ["api_name", "object", "assignments"],
    "additionalProperties": False,
}

OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "success": {"type": "boolean"},
        "api_name": {"type": "string"},
        "deploy_id": {"type": "string"},
        "status": {"type": "string"},
        "verified": {"type": "boolean"},
    },
}


async def _field_types(ctx: ToolContext, object_name: str) -> dict[str, dict[str, Any]]:
    sf = ctx.require_sf()
    try:
        describe = await sf.describe(object_name)
    except SalesforceError as exc:
        raise ToolValidationError(
            f"Object '{object_name}' could not be described: {exc.message}",
            exc.error_type,
            "Confirm the object API name with describe_object.",
        ) from exc
    return {f["name"].lower(): f for f in describe.get("fields") or []}


def _resolve_field(
    fields: dict[str, dict[str, Any]], name: str, object_name: str, *, writable: bool
) -> dict[str, Any]:
    found = fields.get(str(name).lower())
    if found is None:
        near = [f["name"] for f in fields.values() if str(name).lower() in f["name"].lower()][:5]
        raise ToolValidationError(
            f"{object_name} has no field '{name}'.",
            "FIELD_NOT_FOUND",
            (
                f"Did you mean: {', '.join(near)}?"
                if near
                else "Call describe_object and use a real field API name."
            ),
        )
    if writable and not found.get("updateable", False):
        raise ToolValidationError(
            f"{object_name}.{found['name']} is not writable "
            f"(type {found.get('type')}); a flow cannot set it.",
            "FIELD_NOT_WRITABLE",
            "Pick a writable field, or tell the user why this one cannot be automated.",
        )
    return found


async def _build_spec(ctx: ToolContext, args: dict[str, Any]) -> FlowSpec:
    sf = ctx.require_sf()
    object_name = str(args.get("object") or "").strip()
    if not object_name:
        raise ToolValidationError("`object` is required.", "MISSING_ARGUMENT")
    fields = await _field_types(ctx, object_name)

    conditions = []
    for raw in args.get("entry_conditions") or []:
        meta = _resolve_field(fields, raw.get("field", ""), object_name, writable=False)
        conditions.append(
            Condition(
                field=meta["name"],
                operator=str(raw.get("operator") or "equals"),
                value=raw.get("value"),
                field_type=str(meta.get("type") or "string"),
            )
        )

    assignments = []
    for raw in args.get("assignments") or []:
        meta = _resolve_field(fields, raw.get("field", ""), object_name, writable=True)
        assignments.append(
            FieldAssignment(
                field=meta["name"],
                value=raw.get("value"),
                field_type=str(meta.get("type") or "string"),
            )
        )

    api_name = normalize_flow_api_name(str(args.get("api_name") or ""))
    return FlowSpec(
        api_name=api_name,
        label=str(args.get("label") or api_name.replace("_", " ")),
        object_name=object_name,
        trigger=str(args.get("trigger") or BEFORE_SAVE),
        record_trigger_type=str(args.get("record_trigger_type") or "CreateAndUpdate"),
        description=str(args.get("description") or ""),
        entry_conditions=conditions,
        entry_logic=str(args.get("entry_logic") or "and"),
        entry_formula=str(args.get("entry_formula") or ""),
        assignments=assignments,
        active=bool(args.get("activate")),
        api_version=sf.api_version,
    )


async def _existing_flow(ctx: ToolContext, api_name: str) -> list[dict[str, Any]]:
    sf = ctx.require_sf()
    try:
        return await get_flow_versions(sf, api_name)
    except SalesforceError:
        return []


async def _create_validate(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    try:
        spec = await _build_spec(ctx, args)
        xml = build_flow_xml(spec)
    except FlowBuildError as exc:
        raise ToolValidationError(
            exc.message, "FLOW_BUILD_ERROR", exc.suggested_action, exc.missing
        ) from exc

    versions = await _existing_flow(ctx, spec.api_name)
    if versions:
        raise ToolValidationError(
            f"A flow named '{spec.api_name}' already exists "
            f"(version {versions[0]['version']}, {versions[0]['status']}).",
            "FLOW_ALREADY_EXISTS",
            "Use update_flow to add a new version, or choose a different API name. "
            "Do not create a parallel flow that duplicates existing automation.",
        )
    return {"api_name": spec.api_name, "xml_length": len(xml)}


async def _create_plan(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    spec = await _build_spec(ctx, args)
    readable = describe_flow_plan(spec)
    overlapping = await _overlapping_flows(ctx, spec.object_name)
    return {
        "title": f"Create flow {spec.api_name} on {spec.object_name}",
        "object": spec.object_name,
        "change_type": "automation.create_flow",
        "summary": readable["trigger"],
        "details": [
            {"field": "Flow API name", "new_value": spec.api_name},
            {"field": "Label", "new_value": spec.label},
            {"field": "Runs", "new_value": readable["trigger"]},
            {"field": "When", "new_value": readable["condition"]},
            {"field": "Then", "new_value": "; ".join(readable["actions"])},
            {"field": "Status on deploy", "new_value": spec.status},
        ],
        "impact": (
            f"Adds automation to {spec.object_name}. "
            + (
                "It will run against live records as soon as it is deployed."
                if spec.active
                else "It is deployed inactive; activating it is a separate approved step."
            )
        ),
        "existing_automation": overlapping,
        "notes": readable["notes"],
        "reason": args.get("reason", ""),
    }


async def _overlapping_flows(ctx: ToolContext, object_name: str) -> list[dict[str, Any]]:
    """Active automation already running on the same object.

    Shown on the approval card because "is something else already doing this?"
    is the question a reviewer most needs answered and least easily checks.
    """
    try:
        flows = await list_flows(ctx.require_sf(), object_name=object_name, active_only=True)
    except SalesforceError:
        return []
    return [
        {"api_name": f["api_name"], "label": f["label"], "trigger": f.get("trigger_type")}
        for f in flows[:10]
    ]


async def _create_fingerprint(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    api_name = normalize_flow_api_name(str(args.get("api_name") or ""))
    versions = await _existing_flow(ctx, api_name)
    return {
        "flow_exists": bool(versions),
        "latest_version": versions[0]["version"] if versions else None,
        "active_version": next(
            (v["version"] for v in versions if v["status"] == "Active"), None
        ),
    }


async def _deploy_flow(
    ctx: ToolContext, spec: FlowSpec, *, check_only: bool
) -> dict[str, Any]:
    files = {f"flows/{spec.api_name}.flow": build_flow_xml(spec)}
    types: dict[str, list[str]] = {"Flow": [spec.api_name]}
    if spec.active:
        # Activation is expressed through FlowDefinition; deploying both in one
        # package is what makes "create and activate" a single atomic change.
        files[f"flowDefinitions/{spec.api_name}.flowDefinition"] = build_flow_definition_xml(1)
        types["FlowDefinition"] = [spec.api_name]

    result, deployment, error = await deploy_package(
        ctx,
        files=files,
        types=types,
        check_only=check_only,
        label=f"flow {spec.api_name}",
    )
    if error is not None:
        return error | {"api_name": spec.api_name}
    assert result is not None
    return ok(
        api_name=spec.api_name,
        object=spec.object_name,
        deploy_id=result.id,
        deployment_id=deployment.id,
        status=result.status,
        check_only=check_only,
        activated=spec.active,
        components=result.to_dict()["components"],
    )


async def _create_execute(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    spec = await _build_spec(ctx, args)
    # Re-check immediately before deploying: the org may have moved.
    if await _existing_flow(ctx, spec.api_name):
        return fail(
            "FLOW_ALREADY_EXISTS",
            f"'{spec.api_name}' now exists in the org; nothing was deployed.",
            suggested_action="Inspect it and propose update_flow if a change is still wanted.",
            api_name=spec.api_name,
        )
    return await _deploy_flow(ctx, spec, check_only=bool(args.get("check_only")))


async def _create_verify(
    ctx: ToolContext, args: dict[str, Any], result: dict[str, Any]
) -> dict[str, Any]:
    if not result.get("success"):
        return {"verified": False, "reason": "Deployment did not succeed."}
    if result.get("check_only"):
        return {
            "verified": True,
            "reason": "Validation-only deployment: nothing was saved to the org.",
        }
    api_name = str(result.get("api_name"))
    versions = await _existing_flow(ctx, api_name)
    if not versions:
        return {
            "verified": False,
            "reason": (
                f"The deployment reported success but no flow named '{api_name}' is "
                "present in the org. Do not report this as complete."
            ),
        }
    latest = versions[0]
    if result.get("activated") and latest["status"] != "Active":
        return {
            "verified": False,
            "reason": (
                f"Flow '{api_name}' exists at version {latest['version']} but its status "
                f"is {latest['status']}, not Active."
            ),
        }
    return {
        "verified": True,
        "method": "Tooling API Flow version read after deployment",
        "after": {
            "api_name": api_name,
            "version": latest["version"],
            "status": latest["status"],
        },
    }


registry.register(
    Tool(
        name="create_flow",
        description=CREATE_DESCRIPTION,
        input_schema=INPUT_SCHEMA,
        output_schema=OUTPUT_SCHEMA,
        risk=RiskLevel.MEDIUM,
        requires_approval=True,
        execute=_create_execute,
        validate=_create_validate,
        plan=_create_plan,
        verify=_create_verify,
        fingerprint=_create_fingerprint,
        mutating=True,
        audit_action="salesforce.create_flow",
        tags=["flow", "automation", "metadata", "deploy"],
        long_running=True,
    )
)


# ---------------------------------------------------------------------------
# update_flow
# ---------------------------------------------------------------------------
UPDATE_DESCRIPTION = """Deploy a new version of an existing flow.

Salesforce flows are versioned: this does not edit the running version, it
deploys a new one. Inspect the current flow first and describe the change in
`reason` — a reviewer approving this needs to know what is different, and the
approval card shows the current active version alongside the proposal.
"""


async def _update_validate(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    try:
        spec = await _build_spec(ctx, args)
        build_flow_xml(spec)
    except FlowBuildError as exc:
        raise ToolValidationError(
            exc.message, "FLOW_BUILD_ERROR", exc.suggested_action, exc.missing
        ) from exc
    if not await _existing_flow(ctx, spec.api_name):
        raise ToolValidationError(
            f"No flow named '{spec.api_name}' exists, so there is nothing to update.",
            "FLOW_NOT_FOUND",
            "Use create_flow, or call list_flows to find the right API name.",
        )
    return {"api_name": spec.api_name}


async def _update_plan(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    spec = await _build_spec(ctx, args)
    readable = describe_flow_plan(spec)
    versions = await _existing_flow(ctx, spec.api_name)
    current = next((v for v in versions if v["status"] == "Active"), None)
    current_summary: dict[str, Any] | None = None
    if current:
        try:
            current_summary = summarize_flow_metadata(
                await get_flow_metadata(ctx.require_sf(), current["id"])
            )
        except SalesforceError:
            current_summary = None
    return {
        "title": f"Deploy a new version of {spec.api_name}",
        "object": spec.object_name,
        "change_type": "automation.update_flow",
        "summary": readable["trigger"],
        "details": [
            {"field": "Runs", "new_value": readable["trigger"]},
            {"field": "When", "new_value": readable["condition"]},
            {"field": "Then", "new_value": "; ".join(readable["actions"])},
            {
                "field": "Currently active version",
                "old_value": current["version"] if current else "none",
                "new_value": f"new version, {spec.status}",
            },
        ],
        "current_behaviour": current_summary,
        "impact": (
            "Deploys a new flow version. "
            + (
                "It becomes the active version immediately."
                if spec.active
                else "The currently active version keeps running until this one is activated."
            )
        ),
        "notes": readable["notes"],
        "reason": args.get("reason", ""),
    }


async def _update_execute(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    spec = await _build_spec(ctx, args)
    versions = await _existing_flow(ctx, spec.api_name)
    if not versions:
        return fail(
            "FLOW_NOT_FOUND",
            f"'{spec.api_name}' no longer exists in the org; nothing was deployed.",
        )
    if spec.active:
        next_version = max(int(v["version"]) for v in versions) + 1
        files = {
            f"flows/{spec.api_name}.flow": build_flow_xml(spec),
            f"flowDefinitions/{spec.api_name}.flowDefinition": build_flow_definition_xml(
                next_version
            ),
        }
        types = {"Flow": [spec.api_name], "FlowDefinition": [spec.api_name]}
        result, deployment, error = await deploy_package(
            ctx,
            files=files,
            types=types,
            check_only=bool(args.get("check_only")),
            label=f"flow {spec.api_name} v{next_version}",
        )
        if error is not None:
            return error | {"api_name": spec.api_name}
        assert result is not None
        return ok(
            api_name=spec.api_name,
            object=spec.object_name,
            deploy_id=result.id,
            deployment_id=deployment.id,
            status=result.status,
            check_only=bool(args.get("check_only")),
            activated=True,
            new_version=next_version,
        )
    return await _deploy_flow(ctx, spec, check_only=bool(args.get("check_only")))


registry.register(
    Tool(
        name="update_flow",
        description=UPDATE_DESCRIPTION,
        input_schema=INPUT_SCHEMA,
        output_schema=OUTPUT_SCHEMA,
        risk=RiskLevel.MEDIUM,
        requires_approval=True,
        execute=_update_execute,
        validate=_update_validate,
        plan=_update_plan,
        verify=_create_verify,
        fingerprint=_create_fingerprint,
        mutating=True,
        audit_action="salesforce.update_flow",
        tags=["flow", "automation", "metadata", "deploy"],
        long_running=True,
    )
)


# ---------------------------------------------------------------------------
# activate_flow / deactivate_flow
# ---------------------------------------------------------------------------
ACTIVATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "api_name": {"type": "string"},
        "version": {
            "type": "integer",
            "description": "Version to activate. Defaults to the latest version.",
        },
        "reason": {"type": "string"},
    },
    "required": ["api_name"],
    "additionalProperties": False,
}


def _activation_tool(*, activate: bool) -> Tool:
    verb = "Activate" if activate else "Deactivate"

    async def _validate(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
        api_name = normalize_flow_api_name(str(args.get("api_name") or ""))
        versions = await _existing_flow(ctx, api_name)
        if not versions:
            raise ToolValidationError(
                f"No flow named '{api_name}' exists in this org.",
                "FLOW_NOT_FOUND",
                "Call list_flows to find the right API name.",
            )
        active = next((v for v in versions if v["status"] == "Active"), None)
        if activate and args.get("version"):
            if not any(int(v["version"]) == int(args["version"]) for v in versions):
                raise ToolValidationError(
                    f"Flow '{api_name}' has no version {args['version']}.",
                    "FLOW_VERSION_NOT_FOUND",
                    f"Available versions: "
                    f"{', '.join(str(v['version']) for v in versions)}.",
                )
        if not activate and active is None:
            raise ToolValidationError(
                f"Flow '{api_name}' has no active version; there is nothing to deactivate.",
                "FLOW_NOT_ACTIVE",
                "Tell the user the flow is already inactive.",
            )
        return {"api_name": api_name}

    async def _plan(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
        api_name = normalize_flow_api_name(str(args.get("api_name") or ""))
        versions = await _existing_flow(ctx, api_name)
        active = next((v for v in versions if v["status"] == "Active"), None)
        target = args.get("version") or (versions[0]["version"] if versions else None)
        return {
            "title": f"{verb} flow {api_name}",
            "change_type": f"automation.{'activate' if activate else 'deactivate'}_flow",
            "summary": (
                f"{verb}s automation on live records."
                if activate
                else "Stops this automation from running on any further records."
            ),
            "details": [
                {"field": "Flow", "new_value": api_name},
                {
                    "field": "Currently active version",
                    "old_value": active["version"] if active else "none",
                    "new_value": target if activate else "none",
                },
            ],
            "impact": (
                "From the moment this deploys, the flow runs on every qualifying "
                "record change. Existing records are not retroactively processed."
                if activate
                else "The automation stops immediately. Records that depended on it "
                "will no longer be updated."
            ),
            "reason": args.get("reason", ""),
        }

    async def _fingerprint(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
        api_name = normalize_flow_api_name(str(args.get("api_name") or ""))
        versions = await _existing_flow(ctx, api_name)
        return {
            "versions": [int(v["version"]) for v in versions],
            "active_version": next(
                (int(v["version"]) for v in versions if v["status"] == "Active"), None
            ),
        }

    async def _execute(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
        api_name = normalize_flow_api_name(str(args.get("api_name") or ""))
        versions = await _existing_flow(ctx, api_name)
        if not versions:
            return fail("FLOW_NOT_FOUND", f"'{api_name}' no longer exists in the org.")
        target = int(args["version"]) if args.get("version") else int(versions[0]["version"])
        files = {
            f"flowDefinitions/{api_name}.flowDefinition": build_flow_definition_xml(
                target if activate else None
            )
        }
        result, deployment, error = await deploy_package(
            ctx,
            files=files,
            types={"FlowDefinition": [api_name]},
            label=f"{'activate' if activate else 'deactivate'} {api_name}",
        )
        if error is not None:
            return error | {"api_name": api_name}
        assert result is not None
        return ok(
            api_name=api_name,
            deploy_id=result.id,
            deployment_id=deployment.id,
            status=result.status,
            activated=activate,
            version=target if activate else None,
        )

    async def _verify(
        ctx: ToolContext, args: dict[str, Any], result: dict[str, Any]
    ) -> dict[str, Any]:
        if not result.get("success"):
            return {"verified": False, "reason": "Deployment did not succeed."}
        api_name = str(result.get("api_name"))
        versions = await _existing_flow(ctx, api_name)
        active = next((v for v in versions if v["status"] == "Active"), None)
        if activate:
            if active is None or int(active["version"]) != int(result.get("version") or -1):
                return {
                    "verified": False,
                    "reason": (
                        f"'{api_name}' is not showing version {result.get('version')} as "
                        f"active (currently: {active['version'] if active else 'none'})."
                    ),
                }
            return {
                "verified": True,
                "method": "Flow version status read after deployment",
                "after": {"active_version": active["version"]},
            }
        if active is not None:
            return {
                "verified": False,
                "reason": f"'{api_name}' still has version {active['version']} active.",
            }
        return {
            "verified": True,
            "method": "Flow version status read after deployment",
            "after": {"active_version": None},
        }

    return Tool(
        name="activate_flow" if activate else "deactivate_flow",
        description=(
            f"{verb} a flow version.\n\n"
            + (
                "Activating automation changes what happens to live records from that "
                "moment on. Inspect the flow first and be sure the org wants it running."
                if activate
                else "Deactivating stops the automation. Anything that depended on it "
                "silently stops happening, so confirm that is the intent."
            )
        ),
        input_schema=ACTIVATION_SCHEMA,
        output_schema=OUTPUT_SCHEMA,
        risk=RiskLevel.HIGH,
        requires_approval=True,
        execute=_execute,
        validate=_validate,
        plan=_plan,
        verify=_verify,
        fingerprint=_fingerprint,
        mutating=True,
        audit_action=f"salesforce.{'activate' if activate else 'deactivate'}_flow",
        tags=["flow", "automation", "metadata", "deploy"],
        long_running=True,
    )


registry.register(_activation_tool(activate=True))
registry.register(_activation_tool(activate=False))


# ---------------------------------------------------------------------------
# validate_flow
# ---------------------------------------------------------------------------
async def _validate_only_execute(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """Check-only deploy: proves the metadata is valid without changing the org."""
    try:
        spec = await _build_spec(ctx, args)
    except FlowBuildError as exc:
        return fail("FLOW_BUILD_ERROR", exc.message, suggested_action=exc.suggested_action)
    spec.active = False
    result = await _deploy_flow(ctx, spec, check_only=True)
    if result.get("success"):
        result["message"] = (
            f"The flow definition for '{spec.api_name}' is valid. Nothing was saved to "
            "the org — this was a validation-only deployment."
        )
        result["plan"] = describe_flow_plan(spec)
    return result


registry.register(
    Tool(
        name="validate_flow",
        description=(
            "Validate a flow definition against the org without saving anything.\n\n"
            "Runs a real check-only Metadata API deployment, so a success here means "
            "Salesforce accepted the definition — not that it looked plausible. Use it "
            "before proposing a flow so the human reviews something that will deploy."
        ),
        input_schema=INPUT_SCHEMA,
        output_schema=OUTPUT_SCHEMA,
        risk=RiskLevel.LOW,
        requires_approval=False,
        execute=_validate_only_execute,
        audit_action="salesforce.validate_flow",
        tags=["flow", "automation", "read"],
        long_running=True,
    )
)
