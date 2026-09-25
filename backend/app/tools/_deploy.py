"""Shared metadata-deployment helper used by every metadata-producing tool.

Having one path here means Flow, Apex, field and change-set deployments all
create the same Deployment audit row, emit the same execution-trace events,
and report failure the same way. It also means there is exactly one place that
can claim a deployment succeeded, and it only does so when Salesforce says so.
"""

from __future__ import annotations

from typing import Any

from app.models import Deployment
from app.salesforce.errors import SalesforceError
from app.salesforce.metadata import (
    DeployResult,
    MetadataClient,
    build_package_xml,
    build_zip,
)
from app.tools.base import ToolContext


def metadata_client(ctx: ToolContext) -> MetadataClient:
    sf = ctx.require_sf()
    return MetadataClient(sf.instance_url, sf.access_token, sf.api_version)


async def deploy_package(
    ctx: ToolContext,
    *,
    files: dict[str, str],
    types: dict[str, list[str]],
    check_only: bool = False,
    test_level: str = "NoTestRun",
    run_tests: list[str] | None = None,
    label: str = "",
    change_set_id: str | None = None,
) -> tuple[DeployResult | None, Deployment, dict[str, Any] | None]:
    """Build a package, deploy it, and record the outcome.

    Returns `(result, deployment_row, error_payload)`. When `error_payload` is
    not None the deployment failed and the payload is already in the tool's
    structured-error shape — callers return it unchanged rather than
    reinterpreting a failure as a partial success.
    """
    sf = ctx.require_sf()
    conn = ctx.connection
    assert conn is not None

    package = {**files, "package.xml": build_package_xml(types, sf.api_version)}
    deployment = Deployment(
        company_id=ctx.company_id,
        project_id=ctx.project_id,
        agent_run_id=ctx.agent_run_id,
        change_set_id=change_set_id,
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
        {
            "deployment_id": deployment.id,
            "label": label,
            "check_only": check_only,
            "components": sum(len(v) for v in types.values()),
            "types": {k: len(v) for k, v in types.items()},
            "test_level": test_level,
        },
    )

    md = metadata_client(ctx)
    try:
        result = await md.deploy_and_wait(
            build_zip(package),
            check_only=check_only,
            test_level=test_level,
            run_tests=run_tests,
        )
    except SalesforceError as exc:
        deployment.status = "Failed"
        deployment.errors = [exc.to_dict()]
        await ctx.db.flush()
        await ctx.emit(
            "deployment.failed",
            {"deployment_id": deployment.id, "error": exc.message},
        )
        return None, deployment, exc.to_dict() | {"deployment_id": deployment.id}

    deployment.salesforce_deploy_id = result.id
    deployment.status = result.status
    deployment.components_total = result.components_total
    deployment.components_failed = result.components_failed
    deployment.tests_total = result.tests_total
    deployment.tests_failed = result.tests_failed
    deployment.errors = result.errors
    await ctx.db.flush()

    await ctx.emit(
        "deployment.finished",
        {
            "deployment_id": deployment.id,
            "deploy_id": result.id,
            "status": result.status,
            "success": result.success,
            "components_failed": result.components_failed,
            "tests_failed": result.tests_failed,
        },
    )

    if not result.success:
        return (
            result,
            deployment,
            {
                "success": False,
                "error_type": "METADATA_DEPLOY_FAILED",
                "message": (
                    f"Deployment {result.id} finished with status {result.status}: "
                    f"{result.components_failed} component error(s), "
                    f"{result.tests_failed} test failure(s)."
                ),
                "retryable": False,
                "suggested_action": interpret_errors(result),
                "deploy_id": result.id,
                "deployment_id": deployment.id,
                "check_only": check_only,
                "errors": result.errors[:20],
                "test_failures": result.test_failures[:20],
            },
        )
    return result, deployment, None


#: Deployment problems Salesforce reports in prose, mapped to the next action
#: that actually resolves them. Matching is on a distinctive substring.
_ERROR_HINTS: tuple[tuple[str, str], ...] = (
    (
        "duplicate value found",
        "A component with this API name already exists. Inspect it first and "
        "propose an update instead of a create.",
    ),
    (
        "invalid field",
        "A field referenced by the component does not exist on that object. "
        "Re-describe the object and correct the API name.",
    ),
    (
        "insufficient access",
        "The connected Salesforce user lacks permission for this metadata type. "
        "Tell the user which permission is needed; do not retry.",
    ),
    (
        "code coverage",
        "Salesforce refused the deployment for insufficient Apex test coverage. "
        "Add test methods that exercise the uncovered paths, then redeploy.",
    ),
    (
        "test failure",
        "Existing Apex tests failed against this change. Read the failures and "
        "fix the code or the tests before deploying.",
    ),
    (
        "cannot be deleted",
        "Something still references this component. Run dependency analysis and "
        "remove the references first.",
    ),
    (
        "not available for deploy for this organization",
        "This metadata type or feature is not enabled in this org. Tell the user "
        "which feature to enable; there is no workaround from here.",
    ),
    (
        "flow must have",
        "The generated flow is structurally incomplete. Report the problem rather "
        "than redeploying the same definition.",
    ),
)


def interpret_errors(result: DeployResult) -> str:
    """Turn Salesforce's deployment errors into a specific next action.

    Generic advice ("check the errors") wastes an agent step. This maps the
    problems that actually recur onto the thing that resolves them.
    """
    blobs = [
        str(e.get("problem", "")).lower() for e in (result.errors or [])
    ] + [str(f.get("message", "")).lower() for f in (result.test_failures or [])]
    hints: list[str] = []
    for blob in blobs:
        for marker, hint in _ERROR_HINTS:
            if marker in blob and hint not in hints:
                hints.append(hint)
    if hints:
        return " ".join(hints)
    return (
        "Read the component errors below, correct the definition, and propose the "
        "change again. Do not report this change as applied."
    )
