"""deploy_metadata — controlled Metadata API deployment (HIGH risk).

Accepts explicit package source files plus a package manifest, validates the
package locally, deploys via the Metadata API, polls to completion, and
returns component/test results. `check_only: true` runs a real Salesforce
validation deployment (nothing is saved) and is the default.
"""

from __future__ import annotations

from typing import Any

from app.models import Deployment, RiskLevel
from app.salesforce.errors import SalesforceError
from app.salesforce.metadata import MetadataClient, build_package_xml, build_zip
from app.tools.base import Tool, ToolContext, ToolValidationError, ok
from app.tools.registry import registry

VALID_TEST_LEVELS = {
    "NoTestRun",
    "RunSpecifiedTests",
    "RunLocalTests",
    "RunAllTestsInOrg",
}

DESCRIPTION = """Deploy a metadata package to the connected Salesforce org.

Provide the package source files (relative path + content, exactly as they
would appear inside a metadata zip) and the package manifest (metadata type ->
member names). package.xml is generated for you.

`check_only: true` (the default) performs a real validation deployment: the
package is compiled and tested by Salesforce but nothing is saved. Use it
before every real deployment.

`test_level` controls Apex test execution: NoTestRun, RunSpecifiedTests
(with run_tests), RunLocalTests, RunAllTestsInOrg.

Deployment progress is polled until Salesforce reports done; the result
contains the deploy id, component successes/failures and test failures.
Never tell the user a deployment succeeded unless this tool returned
success: true.
"""

INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "files": {
            "type": "array",
            "description": "Package source files.",
            "items": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path inside the package, e.g. 'objects/Account.object'.",
                    },
                    "content": {"type": "string", "description": "File contents (XML/Apex)."},
                },
                "required": ["path", "content"],
            },
        },
        "manifest": {
            "type": "object",
            "description": "Metadata type -> array of member names, e.g. "
            '{"CustomField": ["Account.Tier__c"]}.',
            "additionalProperties": {"type": "array", "items": {"type": "string"}},
        },
        "check_only": {"type": "boolean", "description": "Validation-only run (default true)."},
        "test_level": {"type": "string", "enum": sorted(VALID_TEST_LEVELS)},
        "run_tests": {"type": "array", "items": {"type": "string"}},
        "reason": {"type": "string"},
    },
    "required": ["files", "manifest"],
    "additionalProperties": False,
}

OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "success": {"type": "boolean"},
        "deploy_id": {"type": "string"},
        "status": {"type": "string"},
        "check_only": {"type": "boolean"},
        "components": {"type": "object"},
        "tests": {"type": "object"},
        "errors": {"type": "array"},
    },
}


async def _validate(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    ctx.require_sf()
    files = args.get("files") or []
    manifest = args.get("manifest") or {}
    if not files:
        raise ToolValidationError("`files` must contain at least one file.", "MISSING_ARGUMENT")
    if not manifest:
        raise ToolValidationError(
            "`manifest` must map at least one metadata type to members.", "MISSING_ARGUMENT"
        )
    for f in files:
        path = str(f.get("path", ""))
        if not path or path.startswith("/") or ".." in path:
            raise ToolValidationError(
                f"Invalid package path '{path}'.",
                "INVALID_PACKAGE_PATH",
                "Use relative paths such as 'classes/MyClass.cls'.",
            )
        if path.lower() == "package.xml":
            raise ToolValidationError(
                "Do not supply package.xml; it is generated from `manifest`.",
                "INVALID_PACKAGE_PATH",
            )
    level = args.get("test_level", "NoTestRun")
    if level not in VALID_TEST_LEVELS:
        raise ToolValidationError(
            f"Invalid test_level '{level}'.",
            "INVALID_ARGUMENT",
            f"Use one of: {', '.join(sorted(VALID_TEST_LEVELS))}",
        )
    if level == "RunSpecifiedTests" and not args.get("run_tests"):
        raise ToolValidationError(
            "RunSpecifiedTests requires `run_tests`.",
            "MISSING_ARGUMENT",
            "List the Apex test class names to run.",
        )
    return {"file_count": len(files), "types": list(manifest)}


async def _plan(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    manifest = args.get("manifest") or {}
    check_only = bool(args.get("check_only", True))
    return {
        "title": ("Validate" if check_only else "Deploy") + " metadata package",
        "change_type": "metadata.deploy",
        "summary": (
            f"{'Validation-only deployment' if check_only else 'REAL deployment'} of "
            f"{sum(len(v) for v in manifest.values())} component(s)."
        ),
        "details": [
            {"field": mtype, "new_value": ", ".join(members)}
            for mtype, members in manifest.items()
        ]
        + [
            {"field": "check_only", "new_value": check_only},
            {"field": "test_level", "new_value": args.get("test_level", "NoTestRun")},
        ],
        "impact": (
            "Nothing is saved to the org (validation only)."
            if check_only
            else "Changes org metadata. Some components cannot be rolled back automatically."
        ),
        "reason": args.get("reason", ""),
    }


async def _execute(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    sf = ctx.require_sf()
    conn = ctx.connection
    assert conn is not None
    check_only = bool(args.get("check_only", True))
    manifest = {k: list(v) for k, v in (args.get("manifest") or {}).items()}
    files = {f["path"]: f["content"] for f in args["files"]}
    files["package.xml"] = build_package_xml(manifest, sf.api_version)
    zip_bytes = build_zip(files)

    deployment = Deployment(
        agent_run_id=ctx.agent_run_id,
        user_id=ctx.user.id,
        salesforce_connection_id=conn.id,
        check_only=check_only,
        package_manifest=manifest,
        status="Queued",
    )
    ctx.db.add(deployment)
    await ctx.db.flush()
    await ctx.emit(
        "deployment.started",
        {"deployment_id": deployment.id, "check_only": check_only, "manifest": manifest},
    )

    md = MetadataClient(sf.instance_url, sf.access_token, sf.api_version)
    try:
        result = await md.deploy_and_wait(
            zip_bytes,
            check_only=check_only,
            test_level=str(args.get("test_level", "NoTestRun")),
            run_tests=args.get("run_tests"),
        )
    except SalesforceError as exc:
        deployment.status = "Failed"
        deployment.errors = [exc.to_dict()]
        await ctx.db.flush()
        return exc.to_dict() | {"deployment_id": deployment.id}

    payload = result.to_dict()
    deployment.salesforce_deploy_id = result.id
    deployment.status = result.status
    deployment.components_total = result.components_total
    deployment.components_failed = result.components_failed
    deployment.tests_total = result.tests_total
    deployment.tests_failed = result.tests_failed
    deployment.errors = result.errors
    deployment.verified = result.success
    await ctx.db.flush()

    if not result.success:
        return {
            "success": False,
            "error_type": "METADATA_DEPLOY_FAILED",
            "message": f"Deployment {result.id} finished with status {result.status}.",
            "retryable": False,
            "suggested_action": (
                "Inspect `errors` and `test_failures`, fix the components, then "
                "re-validate with check_only: true."
            ),
            "deployment_id": deployment.id,
            **payload,
        }
    return ok(deployment_id=deployment.id, check_only=check_only, **payload)


async def _verify(
    ctx: ToolContext, args: dict[str, Any], result: dict[str, Any]
) -> dict[str, Any]:
    if not result.get("success") or not result.get("deploy_id"):
        return {"verified": False, "reason": "Deployment did not report success."}
    sf = ctx.require_sf()
    md = MetadataClient(sf.instance_url, sf.access_token, sf.api_version)
    try:
        status = await md.check_deploy_status(str(result["deploy_id"]))
    except SalesforceError as exc:
        return {"verified": False, "reason": exc.message}
    return {
        "verified": status.success and status.done,
        "status": status.status,
        "components_failed": status.components_failed,
        "method": "checkDeployStatus re-read",
    }


TOOL = registry.register(
    Tool(
        name="deploy_metadata",
        description=DESCRIPTION,
        input_schema=INPUT_SCHEMA,
        output_schema=OUTPUT_SCHEMA,
        risk=RiskLevel.HIGH,
        requires_approval=True,
        execute=_execute,
        validate=_validate,
        plan=_plan,
        verify=_verify,
        mutating=True,
        audit_action="salesforce.deploy_metadata",
        tags=["metadata", "deploy"],
    )
)
