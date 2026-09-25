"""create_field — add a custom field to an existing object via the Metadata API.

State-aware and idempotent: if the field already exists it reports that
instead of deploying a duplicate. After deployment it re-describes the object
and only reports success when Salesforce actually shows the field.
"""

from __future__ import annotations

from typing import Any

from app.models import Deployment, RiskLevel
from app.salesforce.errors import SalesforceError
from app.salesforce.metadata import (
    FIELD_TYPES,
    MetadataClient,
    MetadataValidationError,
    build_custom_field_xml,
    build_object_file,
    build_package_xml,
    build_profile_file,
    build_zip,
    normalize_field_api_name,
)
from app.tools.base import Tool, ToolContext, ToolValidationError, ok
from app.tools.registry import registry

DESCRIPTION = f"""Create a custom field on an existing Salesforce object using the
Metadata API.

Always call describe_object first: if the field already exists this tool will
refuse rather than create a duplicate, and you should tell the user instead.

Supported types: {', '.join(sorted(FIELD_TYPES))}.
Type-specific requirements:
  Text -> length (default 255)
  Number/Currency/Percent -> precision, scale
  Picklist/MultiselectPicklist -> picklist_values (list of strings)
  Lookup -> referenceTo, relationshipLabel, relationshipName
  LongTextArea -> length, visibleLines

If a required option is missing, ask the user rather than guessing.

Optionally pass `profiles` to grant field-level security to named profiles
(e.g. ["Admin"]); without it the field is deployed but not visible on any
profile beyond what Salesforce grants automatically.

This is a metadata change: it always goes through the approval flow, and
production orgs are blocked unless the deployment policy allows them.
"""

INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "object": {"type": "string", "description": "Target object API name, e.g. 'Account'."},
        "api_name": {
            "type": "string",
            "description": "Field API name; '__c' is appended automatically if missing.",
        },
        "label": {"type": "string", "description": "Field label shown in the UI."},
        "type": {"type": "string", "enum": sorted(FIELD_TYPES), "description": "Field type."},
        "length": {"type": "integer"},
        "visibleLines": {"type": "integer"},
        "precision": {"type": "integer"},
        "scale": {"type": "integer"},
        "picklist_values": {"type": "array", "items": {"type": "string"}},
        "restricted": {
            "type": "boolean",
            "description": "Restrict picklist values (default true).",
        },
        "referenceTo": {"type": "string", "description": "Lookup target object API name."},
        "relationshipLabel": {"type": "string"},
        "relationshipName": {"type": "string"},
        "required": {"type": "boolean"},
        "unique": {"type": "boolean"},
        "externalId": {"type": "boolean"},
        "defaultValue": {"type": "string"},
        "description": {"type": "string"},
        "inlineHelpText": {"type": "string"},
        "profiles": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Profile names to grant read/edit field-level security.",
        },
        "check_only": {
            "type": "boolean",
            "description": "Validate the deployment without saving (default false).",
        },
        "reason": {"type": "string"},
    },
    "required": ["object", "api_name", "type"],
    "additionalProperties": False,
}

OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "success": {"type": "boolean"},
        "object": {"type": "string"},
        "field": {"type": "string"},
        "deploy_id": {"type": "string"},
        "status": {"type": "string"},
        "verified": {"type": "boolean"},
        "already_exists": {"type": "boolean"},
    },
}


async def _validate(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    sf = ctx.require_sf()
    obj = str(args.get("object") or "").strip()
    if not obj:
        raise ToolValidationError("`object` is required.", "MISSING_ARGUMENT")

    try:
        api_name = normalize_field_api_name(str(args.get("api_name") or ""))
        field_xml = build_custom_field_xml({**args, "api_name": api_name})
    except MetadataValidationError as exc:
        raise ToolValidationError(
            exc.message,
            "METADATA_VALIDATION_ERROR",
            exc.suggested_action or "Ask the user for the missing field configuration.",
            exc.missing,
        ) from exc

    try:
        desc = await sf.describe(obj)
    except SalesforceError as exc:
        raise ToolValidationError(
            f"Object '{obj}' could not be described: {exc.message}",
            exc.error_type,
            "Confirm the object API name.",
        ) from exc

    existing = {f["name"].lower(): f for f in desc.get("fields") or []}
    if api_name.lower() in existing:
        raise ToolValidationError(
            f"Field {api_name} already exists on {obj} "
            f"(type {existing[api_name.lower()].get('type')}).",
            "FIELD_ALREADY_EXISTS",
            "Tell the user the field already exists; propose update_field or a "
            "different API name instead of creating a duplicate.",
        )
    return {"object": desc.get("name", obj), "field": api_name, "metadata_xml": field_xml}


async def _plan(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    api_name = normalize_field_api_name(str(args.get("api_name") or ""))
    details = [
        {"field": "Object", "new_value": args.get("object")},
        {"field": "API name", "new_value": api_name},
        {"field": "Label", "new_value": args.get("label") or api_name[:-3].replace("_", " ")},
        {"field": "Type", "new_value": args.get("type")},
    ]
    for key in (
        "length", "precision", "scale", "picklist_values", "referenceTo",
        "relationshipName", "required", "unique", "defaultValue", "profiles",
    ):
        if args.get(key) not in (None, "", []):
            details.append({"field": key, "new_value": args[key]})
    return {
        "title": f"Create field {api_name} on {args.get('object')}",
        "object": args.get("object"),
        "change_type": "metadata.create_field",
        "summary": f"Adds a new {args.get('type')} field to {args.get('object')}.",
        "details": details,
        "impact": (
            f"Deploys metadata to the org. Adds a column to {args.get('object')}; "
            "existing records get a null value. Removing a field later requires a "
            "destructive change."
        ),
        "reason": args.get("reason", ""),
    }


async def _fingerprint(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """The org state this change was proposed against.

    If someone else adds the same field while the proposal sits in an approval
    queue, the approved plan ("create it") is no longer the right plan. This is
    recompared before execution and a mismatch invalidates the approval.
    """
    sf = ctx.require_sf()
    obj = str(args.get("object") or "")
    api_name = normalize_field_api_name(str(args.get("api_name") or ""))
    try:
        desc = await sf.describe(obj, use_cache=False)
        exists = any(
            f["name"].lower() == api_name.lower() for f in desc.get("fields") or []
        )
    except SalesforceError:
        exists = False
    return {"object": obj, "field": api_name, "field_exists": exists}


def _build_package(args: dict[str, Any], api_name: str, api_version: str) -> tuple[bytes, dict]:
    obj = str(args["object"])
    field_xml = build_custom_field_xml({**args, "api_name": api_name})
    files = {
        f"objects/{obj}.object": build_object_file(obj, [field_xml]),
    }
    types: dict[str, list[str]] = {"CustomField": [f"{obj}.{api_name}"]}

    profiles = args.get("profiles") or []
    for profile in profiles:
        files[f"profiles/{profile}.profile"] = build_profile_file(
            profile, [{"field": f"{obj}.{api_name}", "readable": True, "editable": True}]
        )
    if profiles:
        types["Profile"] = list(profiles)

    files["package.xml"] = build_package_xml(types, api_version)
    return build_zip(files), types


async def _execute(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    sf = ctx.require_sf()
    conn = ctx.connection
    assert conn is not None
    obj = str(args["object"])
    api_name = normalize_field_api_name(str(args["api_name"]))

    # Idempotency: re-check live state immediately before deploying.
    sf.invalidate_describe(obj)
    desc = await sf.describe(obj, use_cache=False)
    if any(f["name"].lower() == api_name.lower() for f in desc.get("fields") or []):
        return ok(
            object=obj,
            field=api_name,
            already_exists=True,
            verified=True,
            status="AlreadyExists",
            message=f"{api_name} already exists on {obj}; no deployment was needed.",
        )

    zip_bytes, types = _build_package(args, api_name, sf.api_version)
    check_only = bool(args.get("check_only", False))

    deployment = Deployment(
        agent_run_id=ctx.agent_run_id,
        user_id=ctx.user.id,
        salesforce_connection_id=conn.id,
        check_only=check_only,
        package_manifest=types,
        status="Queued",
    )
    ctx.db.add(deployment)
    await ctx.db.flush()

    await ctx.emit(
        "deployment.started",
        {"deployment_id": deployment.id, "object": obj, "field": api_name},
    )

    md = MetadataClient(sf.instance_url, sf.access_token, sf.api_version)
    try:
        result = await md.deploy_and_wait(zip_bytes, check_only=check_only)
    except SalesforceError as exc:
        deployment.status = "Failed"
        deployment.errors = [exc.to_dict()]
        await ctx.db.flush()
        return exc.to_dict() | {"object": obj, "field": api_name, "deployment_id": deployment.id}

    deployment.salesforce_deploy_id = result.id
    deployment.status = result.status
    deployment.components_total = result.components_total
    deployment.components_failed = result.components_failed
    deployment.tests_total = result.tests_total
    deployment.tests_failed = result.tests_failed
    deployment.errors = result.errors
    await ctx.db.flush()

    if not result.success:
        return {
            "success": False,
            "error_type": "METADATA_DEPLOY_FAILED",
            "message": f"Deployment {result.id} finished with status {result.status}.",
            "retryable": False,
            "suggested_action": (
                "Read the component errors, correct the field definition, and propose "
                "the change again."
            ),
            "object": obj,
            "field": api_name,
            "deploy_id": result.id,
            "deployment_id": deployment.id,
            "errors": result.errors,
        }

    return ok(
        object=obj,
        field=api_name,
        deploy_id=result.id,
        deployment_id=deployment.id,
        status=result.status,
        check_only=check_only,
        components=result.to_dict()["components"],
    )


async def _verify(
    ctx: ToolContext, args: dict[str, Any], result: dict[str, Any]
) -> dict[str, Any]:
    if not result.get("success"):
        return {"verified": False, "reason": "Deployment did not succeed."}
    if result.get("check_only"):
        return {
            "verified": True,
            "reason": "Validation-only deployment: nothing was saved to the org.",
        }
    if result.get("already_exists"):
        return {"verified": True, "reason": "Field already present."}

    sf = ctx.require_sf()
    obj = str(args["object"])
    api_name = str(result.get("field"))
    sf.invalidate_describe(obj)
    try:
        desc = await sf.describe(obj, use_cache=False)
    except SalesforceError as exc:
        return {"verified": False, "reason": exc.message}
    match = next(
        (f for f in desc.get("fields") or [] if f["name"].lower() == api_name.lower()), None
    )
    if match is None:
        return {
            "verified": False,
            "reason": (
                f"Deployment reported success but {api_name} is not present on {obj}. "
                "Do not report this change as complete."
            ),
        }
    deployment_id = result.get("deployment_id")
    if deployment_id:
        dep = await ctx.db.get(Deployment, deployment_id)
        if dep:
            dep.verified = True
            await ctx.db.flush()
    return {
        "verified": True,
        "method": "describe_object after deployment",
        "field": {
            "name": match.get("name"),
            "label": match.get("label"),
            "type": match.get("type"),
            "length": match.get("length"),
        },
    }


TOOL = registry.register(
    Tool(
        name="create_field",
        description=DESCRIPTION,
        input_schema=INPUT_SCHEMA,
        output_schema=OUTPUT_SCHEMA,
        risk=RiskLevel.MEDIUM,
        requires_approval=True,
        execute=_execute,
        validate=_validate,
        plan=_plan,
        verify=_verify,
        fingerprint=_fingerprint,
        mutating=True,
        audit_action="salesforce.create_field",
        tags=["metadata", "write", "deploy"],
        long_running=True,
    )
)
