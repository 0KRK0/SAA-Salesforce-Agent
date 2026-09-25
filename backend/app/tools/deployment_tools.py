"""Deployment lifecycle tools: change sets, validation, deploy, rollback.

A change set is the unit a release moves in. These tools take it through:

    create → validate (check-only, with tests, plus a diff against the org)
           → approve → deploy → verify → rollback if needed

Use these rather than deploy_metadata when a change is more than one component,
when it needs to run tests, or when someone will want to undo it.
"""

from __future__ import annotations

from typing import Any

from app.deployment.engine import (
    build_destructive_package,
    build_rollback_plan,
    compute_diff,
    manifest_from_files,
    retrieve_current,
    summarize_diff,
)
from app.models import ChangeSet, ChangeSetState, RiskLevel
from app.salesforce.errors import SalesforceError
from app.salesforce.metadata import MetadataClient
from app.tenancy import service as tenancy
from app.tools._deploy import deploy_package, interpret_errors
from app.tools.base import Tool, ToolContext, ToolValidationError, fail, ok
from app.tools.registry import registry

TEST_LEVELS = {"NoTestRun", "RunSpecifiedTests", "RunLocalTests", "RunAllTestsInOrg"}


# ---------------------------------------------------------------------------
# create_change_set
# ---------------------------------------------------------------------------
CREATE_DESCRIPTION = """Assemble a named change set from metadata source files.

A change set is how a multi-component change moves as one unit: a field, the
flow that uses it and the Apex test that covers it deploy together or not at
all. Creating one changes nothing in Salesforce — it records what you intend to
deploy.

`files` maps metadata paths to source, e.g.
  "classes/AccountService.cls"          -> the Apex source
  "classes/AccountService.cls-meta.xml" -> its metadata file
  "flows/Set_Tier.flow"                 -> the flow definition

Then call validate_change_set to check it against the org and see the diff.
"""


async def _create_execute(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    conn = ctx.connection
    if conn is None:
        return fail("NO_SALESFORCE_CONNECTION", "No Salesforce org is connected.")
    files = dict(args.get("files") or {})
    if not files:
        return fail("MISSING_ARGUMENT", "`files` is required and must not be empty.")

    manifest = manifest_from_files(files)
    if not manifest:
        return fail(
            "UNRECOGNIZED_METADATA",
            "None of the supplied paths map to a known metadata type.",
            suggested_action=(
                "Use standard metadata paths, e.g. classes/Foo.cls, flows/Bar.flow, "
                "objects/Account.object."
            ),
        )

    test_level = str(args.get("test_level") or "RunLocalTests")
    if test_level not in TEST_LEVELS:
        return fail(
            "INVALID_TEST_LEVEL",
            f"'{test_level}' is not a valid test level.",
            suggested_action=f"Use one of: {', '.join(sorted(TEST_LEVELS))}.",
        )

    change_set = ChangeSet(
        company_id=ctx.company_id,
        project_id=ctx.project_id,
        user_id=ctx.user.id,
        salesforce_connection_id=conn.id,
        agent_run_id=ctx.agent_run_id,
        name=str(args.get("name") or "Untitled change set"),
        description=str(args.get("description") or ""),
        source_files=files,
        package_manifest=manifest,
        test_level=test_level,
        run_tests=list(args.get("run_tests") or []) or None,
        state=ChangeSetState.DRAFT,
    )
    ctx.db.add(change_set)
    await ctx.db.flush()

    return ok(
        change_set_id=change_set.id,
        name=change_set.name,
        state=change_set.state.value,
        manifest=manifest,
        component_count=sum(len(v) for v in manifest.values()),
        next_step="Call validate_change_set to check it against the org and see the diff.",
    )


registry.register(
    Tool(
        name="create_change_set",
        description=CREATE_DESCRIPTION,
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "description": {"type": "string"},
                "files": {
                    "type": "object",
                    "description": "Metadata path -> source content.",
                    "additionalProperties": {"type": "string"},
                },
                "test_level": {"type": "string", "enum": sorted(TEST_LEVELS)},
                "run_tests": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Test classes, when test_level is RunSpecifiedTests.",
                },
            },
            "required": ["name", "files"],
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "properties": {
                "success": {"type": "boolean"},
                "change_set_id": {"type": "string"},
            },
        },
        # Creating a change set touches nothing in Salesforce.
        risk=RiskLevel.LOW,
        requires_approval=False,
        execute=_create_execute,
        audit_action="deployment.create_change_set",
        tags=["deployment", "metadata"],
    )
)


# ---------------------------------------------------------------------------
# validate_change_set
# ---------------------------------------------------------------------------
VALIDATE_DESCRIPTION = """Validate a change set against the target org without
deploying anything.

Does three things, all against the real org:
  1. retrieves the current source of every component and computes a diff, so
     you can see exactly what changes;
  2. runs a check-only Metadata API deployment at the change set's test level,
     which compiles the code and runs the tests;
  3. captures a rollback plan from the retrieved state.

Nothing is saved to the org. A change set must validate before it can deploy.
"""


async def _validate_execute(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    sf = ctx.require_sf()
    change_set = await _owned(ctx, str(args.get("change_set_id") or ""))
    if isinstance(change_set, dict):
        return change_set

    files = dict(change_set.source_files or {})
    manifest = dict(change_set.package_manifest or manifest_from_files(files))
    change_set.state = ChangeSetState.VALIDATING
    await ctx.db.flush()

    await ctx.emit(
        "changeset.validating",
        {"change_set": change_set.id, "components": sum(len(v) for v in manifest.values())},
    )

    current = await retrieve_current(sf, manifest)
    diffs = compute_diff(files, current)
    summary = summarize_diff(diffs)
    rollback = build_rollback_plan(files, current, manifest)

    change_set.diff = [d.to_dict() for d in diffs]
    change_set.rollback_plan = rollback
    await ctx.db.flush()

    if summary["no_effective_change"]:
        change_set.state = ChangeSetState.VALIDATED
        await ctx.db.flush()
        return ok(
            change_set_id=change_set.id,
            state=change_set.state.value,
            diff_summary=summary,
            message=(
                "Every component in this change set is byte-identical to what the org "
                "already has. Deploying it would change nothing."
            ),
        )

    result, deployment, error = await deploy_package(
        ctx,
        files=files,
        types=manifest,
        check_only=True,
        test_level=change_set.test_level,
        run_tests=change_set.run_tests,
        label=f"validate {change_set.name}",
        change_set_id=change_set.id,
    )
    if error is not None:
        change_set.state = ChangeSetState.VALIDATION_FAILED
        change_set.validation_result = error
        await ctx.db.flush()
        return error | {
            "change_set_id": change_set.id,
            "state": change_set.state.value,
            "diff_summary": summary,
        }

    assert result is not None
    change_set.state = ChangeSetState.VALIDATED
    change_set.validation_deploy_id = result.id
    change_set.validation_result = result.to_dict()
    await ctx.db.flush()

    return ok(
        change_set_id=change_set.id,
        state=change_set.state.value,
        validation_deploy_id=result.id,
        deployment_id=deployment.id,
        diff_summary=summary,
        diff=[d.to_dict() for d in diffs if d.status != "unchanged"][:40],
        tests={
            "level": change_set.test_level,
            "total": result.tests_total,
            "failed": result.tests_failed,
        },
        rollback={
            "possible": rollback["possible"],
            "summary": rollback["summary"],
            "caveats": rollback["caveats"],
        },
        next_step=(
            "The change set is validated and nothing was saved. Call deploy_change_set "
            "to apply it; that step requires human approval."
        ),
    )


registry.register(
    Tool(
        name="validate_change_set",
        description=VALIDATE_DESCRIPTION,
        input_schema={
            "type": "object",
            "properties": {"change_set_id": {"type": "string"}},
            "required": ["change_set_id"],
            "additionalProperties": False,
        },
        output_schema={"type": "object", "properties": {"success": {"type": "boolean"}}},
        risk=RiskLevel.LOW,
        requires_approval=False,
        execute=_validate_execute,
        audit_action="deployment.validate_change_set",
        tags=["deployment", "metadata"],
        long_running=True,
    )
)


# ---------------------------------------------------------------------------
# deploy_change_set
# ---------------------------------------------------------------------------
DEPLOY_DESCRIPTION = """Deploy a validated change set to the connected org.

Requires the change set to have validated first — an unvalidated deployment is
a deployment nobody has seen the diff of. Always requires human approval, and
the approval card carries the component diff, the test level and what rollback
would mean.

After deployment the components are read back from the org. If they are not
there, the result reports failure regardless of what the deployment status said.
"""


async def _deploy_validate(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    change_set = await _owned(ctx, str(args.get("change_set_id") or ""))
    if isinstance(change_set, dict):
        raise ToolValidationError(
            change_set.get("message", "Change set not found."), "CHANGE_SET_NOT_FOUND"
        )
    if change_set.state not in {
        ChangeSetState.VALIDATED,
        ChangeSetState.APPROVED,
        ChangeSetState.DEPLOY_FAILED,
    }:
        raise ToolValidationError(
            f"Change set '{change_set.name}' is {change_set.state.value}; only a "
            "validated change set can be deployed.",
            "CHANGE_SET_NOT_VALIDATED",
            "Call validate_change_set first so the diff and tests are checked.",
        )
    return {"change_set": change_set.id}


async def _deploy_plan(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    change_set = await _owned(ctx, str(args["change_set_id"]))
    if isinstance(change_set, dict):
        return {"title": "Deploy change set", "summary": "Change set not found."}
    diffs = change_set.diff or []
    changed = [d for d in diffs if d.get("status") != "unchanged"]
    validation = change_set.validation_result or {}
    rollback = change_set.rollback_plan or {}
    conn = ctx.connection
    return {
        "title": f"Deploy change set '{change_set.name}'",
        "change_type": "deployment.change_set",
        "summary": (
            f"{len(changed)} component(s) change in "
            f"{'sandbox' if conn and conn.is_sandbox else 'PRODUCTION'}."
        ),
        "details": [
            {"field": "Target org", "new_value": conn.instance_url if conn else "unknown"},
            {
                "field": "Sandbox",
                "new_value": bool(conn.is_sandbox) if conn else "unknown",
            },
            {"field": "Components", "new_value": len(changed)},
            {"field": "Test level", "new_value": change_set.test_level},
            {
                "field": "Validation",
                "new_value": (
                    f"passed ({validation.get('tests', {}).get('total', 0)} tests, "
                    f"{validation.get('tests', {}).get('failed', 0)} failed)"
                    if validation.get("success")
                    else "not validated"
                ),
            },
            {
                "field": "Rollback",
                "new_value": rollback.get("summary", "no rollback plan captured"),
            },
        ],
        "diff": changed[:30],
        "rollback_caveats": rollback.get("caveats", []),
        "impact": (
            "Applies these metadata changes to the connected org. Automation and Apex "
            "take effect immediately for every user."
        ),
        "reason": args.get("reason", ""),
    }


async def _deploy_fingerprint(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """The org's current state of these components is the fingerprint.

    If someone edited a class in the org while this change set sat awaiting
    approval, the approved diff is no longer the diff that would be applied.
    """
    sf = ctx.require_sf()
    change_set = await _owned(ctx, str(args["change_set_id"]))
    if isinstance(change_set, dict):
        return {"change_set": "missing"}
    manifest = dict(change_set.package_manifest or {})
    current = await retrieve_current(sf, manifest)
    return {
        "component_hashes": {
            path: hash(content) for path, content in sorted(current.items())
        }
    }


async def _deploy_execute(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    change_set = await _owned(ctx, str(args["change_set_id"]))
    if isinstance(change_set, dict):
        return change_set

    files = dict(change_set.source_files or {})
    manifest = dict(change_set.package_manifest or {})
    change_set.state = ChangeSetState.DEPLOYING
    await ctx.db.flush()

    result, deployment, error = await deploy_package(
        ctx,
        files=files,
        types=manifest,
        check_only=False,
        test_level=change_set.test_level,
        run_tests=change_set.run_tests,
        label=f"deploy {change_set.name}",
        change_set_id=change_set.id,
    )
    if error is not None:
        change_set.state = ChangeSetState.DEPLOY_FAILED
        change_set.deploy_result = error
        await ctx.db.flush()
        return error | {"change_set_id": change_set.id, "state": change_set.state.value}

    assert result is not None
    change_set.state = ChangeSetState.DEPLOYED
    change_set.deploy_id = result.id
    change_set.deploy_result = result.to_dict()
    await ctx.db.flush()

    return ok(
        change_set_id=change_set.id,
        state=change_set.state.value,
        deploy_id=result.id,
        deployment_id=deployment.id,
        status=result.status,
        components=result.to_dict()["components"],
        tests=result.to_dict()["tests"],
        rollback_available=bool((change_set.rollback_plan or {}).get("possible")),
    )


async def _deploy_verify(
    ctx: ToolContext, args: dict[str, Any], result: dict[str, Any]
) -> dict[str, Any]:
    """Read the components back from the org.

    A deployment status of Succeeded is Salesforce reporting on its own job. A
    retrieve is the org reporting on its own contents, and only the second is
    evidence that the change is really there.
    """
    if not result.get("success"):
        return {"verified": False, "reason": "The deployment did not succeed."}
    sf = ctx.require_sf()
    change_set = await _owned(ctx, str(args["change_set_id"]))
    if isinstance(change_set, dict):
        return {"verified": False, "reason": "The change set could not be re-read."}

    manifest = dict(change_set.package_manifest or {})
    expected = {
        p: c for p, c in (change_set.source_files or {}).items()
        if not p.endswith("-meta.xml") and p != "package.xml"
    }
    after = await retrieve_current(sf, manifest)
    missing = [p for p in expected if p not in after]
    if missing:
        return {
            "verified": False,
            "reason": (
                "The deployment reported success but these components are not present "
                f"in the org: {', '.join(missing[:10])}."
            ),
        }
    remaining = compute_diff(expected, after)
    still_different = [d.path for d in remaining if d.status != "unchanged"]

    change_set.verified = not still_different
    change_set.verification = {
        "components_confirmed": len(expected),
        "still_different": still_different,
    }
    await ctx.db.flush()

    if still_different:
        return {
            "verified": False,
            "reason": (
                "These components are present but their source in the org does not "
                f"match what was deployed: {', '.join(still_different[:10])}. Salesforce "
                "may normalize some metadata on save; check before reporting success."
            ),
        }
    return {
        "verified": True,
        "method": "Metadata API retrieve after deployment",
        "after": {"components_confirmed": len(expected)},
    }


registry.register(
    Tool(
        name="deploy_change_set",
        description=DEPLOY_DESCRIPTION,
        input_schema={
            "type": "object",
            "properties": {
                "change_set_id": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["change_set_id"],
            "additionalProperties": False,
        },
        output_schema={"type": "object", "properties": {"success": {"type": "boolean"}}},
        risk=RiskLevel.HIGH,
        requires_approval=True,
        execute=_deploy_execute,
        validate=_deploy_validate,
        plan=_deploy_plan,
        verify=_deploy_verify,
        fingerprint=_deploy_fingerprint,
        mutating=True,
        audit_action="deployment.deploy_change_set",
        tags=["deployment", "metadata", "release"],
        long_running=True,
    )
)


# ---------------------------------------------------------------------------
# rollback_change_set
# ---------------------------------------------------------------------------
ROLLBACK_DESCRIPTION = """Roll back a deployed change set.

Restores every component that existed before the deployment to its previous
source, and removes components the deployment created. Both use the state
captured at validation time, so this is a real revert rather than a hopeful
redeploy.

Read the caveats before promising an undo: deleting a custom field deletes its
data, flow versions are never removed by Salesforce, and some component types
cannot be deleted by a destructive change at all. The plan names each case.

Always HIGH risk and always requires approval — a rollback is a deployment.
"""


async def _rollback_validate(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    change_set = await _owned(ctx, str(args.get("change_set_id") or ""))
    if isinstance(change_set, dict):
        raise ToolValidationError("Change set not found.", "CHANGE_SET_NOT_FOUND")
    if change_set.state != ChangeSetState.DEPLOYED:
        raise ToolValidationError(
            f"Change set '{change_set.name}' is {change_set.state.value}; only a "
            "deployed change set can be rolled back.",
            "CHANGE_SET_NOT_DEPLOYED",
        )
    plan = change_set.rollback_plan or {}
    if not plan.get("possible"):
        raise ToolValidationError(
            "No rollback plan was captured for this change set, so there is nothing "
            "to restore.",
            "NO_ROLLBACK_PLAN",
            "Tell the user the change has to be reversed manually, and what it changed.",
        )
    return {"change_set": change_set.id}


async def _rollback_plan_fn(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    change_set = await _owned(ctx, str(args["change_set_id"]))
    if isinstance(change_set, dict):
        return {"title": "Roll back change set", "summary": "Change set not found."}
    plan = change_set.rollback_plan or {}
    destructive = plan.get("destructive_manifest") or {}
    return {
        "title": f"Roll back '{change_set.name}'",
        "change_type": "deployment.rollback",
        "summary": plan.get("summary", ""),
        "details": [
            {
                "field": "Components restored",
                "new_value": len(plan.get("restore_files") or {}),
            },
            {
                "field": "Components deleted",
                "new_value": sum(len(v) for v in destructive.values()),
            },
            {
                "field": "Deleted components",
                "new_value": ", ".join(
                    f"{t}: {', '.join(v)}" for t, v in destructive.items()
                )
                or "none",
            },
        ],
        "impact": (
            "Reverses the deployment. "
            + " ".join(plan.get("caveats") or [])
        ),
        "reason": args.get("reason", ""),
    }


async def _rollback_execute(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    sf = ctx.require_sf()
    change_set = await _owned(ctx, str(args["change_set_id"]))
    if isinstance(change_set, dict):
        return change_set

    plan = change_set.rollback_plan or {}
    restore_files = dict(plan.get("restore_files") or {})
    restore_manifest = dict(plan.get("restore_manifest") or {})
    destructive = dict(plan.get("destructive_manifest") or {})
    outcomes: dict[str, Any] = {}

    if restore_files:
        result, deployment, error = await deploy_package(
            ctx,
            files=restore_files,
            types=restore_manifest,
            check_only=False,
            test_level="NoTestRun",
            label=f"rollback restore {change_set.name}",
            change_set_id=change_set.id,
        )
        if error is not None:
            change_set.state = ChangeSetState.DEPLOY_FAILED
            await ctx.db.flush()
            return error | {
                "change_set_id": change_set.id,
                "phase": "restore",
                "message": (
                    "The restore step of the rollback failed. The org is in a mixed "
                    "state; do not describe the rollback as complete."
                ),
            }
        assert result is not None
        outcomes["restore"] = {"deploy_id": result.id, "status": result.status}
        change_set.rolled_back_deploy_id = result.id

    if destructive:
        await ctx.emit(
            "changeset.rollback_delete",
            {"components": sum(len(v) for v in destructive.values())},
        )
        md = MetadataClient(sf.instance_url, sf.access_token, sf.api_version)
        try:
            destructive_result = await md.deploy_and_wait(
                build_destructive_package(destructive, sf.api_version),
                check_only=False,
            )
        except SalesforceError as exc:
            return exc.to_dict() | {
                "change_set_id": change_set.id,
                "phase": "delete",
                "restore": outcomes.get("restore"),
                "message": (
                    "Previous source was restored but the newly created components "
                    "could not be deleted. The rollback is partial."
                ),
            }
        if not destructive_result.success:
            return {
                "success": False,
                "error_type": "ROLLBACK_DELETE_FAILED",
                "message": (
                    "Previous source was restored but deleting the new components "
                    f"failed ({destructive_result.status})."
                ),
                "retryable": False,
                "suggested_action": interpret_errors(destructive_result),
                "change_set_id": change_set.id,
                "errors": destructive_result.errors[:10],
                "partial": True,
            }
        outcomes["delete"] = {
            "deploy_id": destructive_result.id,
            "status": destructive_result.status,
        }

    change_set.state = ChangeSetState.ROLLED_BACK
    await ctx.db.flush()
    return ok(
        change_set_id=change_set.id,
        state=change_set.state.value,
        outcomes=outcomes,
        caveats=plan.get("caveats", []),
    )


registry.register(
    Tool(
        name="rollback_change_set",
        description=ROLLBACK_DESCRIPTION,
        input_schema={
            "type": "object",
            "properties": {
                "change_set_id": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["change_set_id"],
            "additionalProperties": False,
        },
        output_schema={"type": "object", "properties": {"success": {"type": "boolean"}}},
        risk=RiskLevel.HIGH,
        requires_approval=True,
        execute=_rollback_execute,
        validate=_rollback_validate,
        plan=_rollback_plan_fn,
        mutating=True,
        audit_action="deployment.rollback_change_set",
        tags=["deployment", "metadata", "release"],
        long_running=True,
    )
)


async def _owned(ctx: ToolContext, change_set_id: str) -> ChangeSet | dict[str, Any]:
    """Tenant-scoped change-set lookup used by every tool in this module."""
    if not change_set_id:
        return fail("MISSING_ARGUMENT", "`change_set_id` is required.")
    change_set = await tenancy.owned(ctx.db, ChangeSet, change_set_id, ctx.project_id)
    if change_set is None:
        return fail(
            "CHANGE_SET_NOT_FOUND",
            f"No change set '{change_set_id}' exists in this organization.",
        )
    return change_set
