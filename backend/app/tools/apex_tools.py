"""Apex engineering agent: inspect, write, validate, deploy, test.

The workflow, end to end:

    inspect schema → inspect existing Apex → write class/trigger + tests
    → compile check → propose (with code smells surfaced) → approval
    → deploy with tests → read the real test outcome → verify → report

What this refuses to do:
  * deploy a class whose declaration disagrees with its name;
  * deploy production code with no test class;
  * describe tests as passing when Salesforce has not returned an outcome;
  * quietly accept `without sharing` — it is surfaced to the human approving.
"""

from __future__ import annotations

from typing import Any

from app.models import RiskLevel
from app.salesforce.apex import (
    ApexUnit,
    ApexValidationError,
    build_package,
    compile_check,
    count_test_methods,
    parse_unit,
    run_tests,
)
from app.salesforce.errors import SalesforceError
from app.salesforce.inspect import (
    apex_code_coverage,
    get_apex_body,
    list_apex_classes,
    list_apex_triggers,
)
from app.tools._deploy import deploy_package
from app.tools.base import Tool, ToolContext, ToolValidationError, fail, ok
from app.tools.registry import registry


# ---------------------------------------------------------------------------
# list_apex
# ---------------------------------------------------------------------------
async def _list_execute(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    sf = ctx.require_sf()
    kind = str(args.get("kind") or "all").lower()
    out: dict[str, Any] = {}
    if kind in {"all", "class"}:
        classes = await list_apex_classes(sf, name_like=args.get("name_like"))
        out["classes"] = classes
        out["class_count"] = len(classes)
        out["test_class_count"] = sum(1 for c in classes if c["is_test"])
    if kind in {"all", "trigger"}:
        triggers = await list_apex_triggers(sf, object_name=args.get("object"))
        out["triggers"] = triggers
        out["trigger_count"] = len(triggers)
    return ok(**out)


registry.register(
    Tool(
        name="list_apex",
        description=(
            "List the Apex classes and triggers in the org.\n\n"
            "Call this before writing Apex. An org that already has a trigger on the "
            "object you are about to add one to needs an addition to that trigger, not "
            "a second one — two triggers on one object run in an undefined order."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["all", "class", "trigger"]},
                "object": {"type": "string", "description": "Filter triggers by object."},
                "name_like": {"type": "string", "description": "Filter classes by name."},
            },
            "additionalProperties": False,
        },
        output_schema={"type": "object", "properties": {"success": {"type": "boolean"}}},
        risk=RiskLevel.LOW,
        requires_approval=False,
        execute=_list_execute,
        audit_action="salesforce.list_apex",
        tags=["apex", "read"],
    )
)


# ---------------------------------------------------------------------------
# inspect_apex
# ---------------------------------------------------------------------------
async def _inspect_execute(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    sf = ctx.require_sf()
    name = str(args.get("name") or "").strip()
    kind = str(args.get("kind") or "class").lower()
    if not name:
        return fail("MISSING_ARGUMENT", "`name` is required.")

    record = await get_apex_body(sf, kind, name)
    if record is None:
        return fail(
            "APEX_NOT_FOUND",
            f"No Apex {kind} named '{name}' exists in this org.",
            suggested_action="Call list_apex to see what exists.",
        )
    coverage = await apex_code_coverage(sf, [name])
    body = record.get("body") or ""
    return ok(
        kind=kind,
        name=record.get("name"),
        api_version=record.get("api_version"),
        status=record.get("status"),
        line_count=len(body.splitlines()),
        body=body if bool(args.get("include_body", True)) else None,
        test_methods=count_test_methods(body),
        coverage=coverage,
    )


registry.register(
    Tool(
        name="inspect_apex",
        description=(
            "Read the source of one Apex class or trigger, with its current test "
            "coverage.\n\nUse it before modifying Apex, and when debugging why "
            "records change in ways no flow explains."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "kind": {"type": "string", "enum": ["class", "trigger"], "default": "class"},
                "include_body": {"type": "boolean", "default": True},
            },
            "required": ["name"],
            "additionalProperties": False,
        },
        output_schema={"type": "object", "properties": {"success": {"type": "boolean"}}},
        risk=RiskLevel.LOW,
        requires_approval=False,
        execute=_inspect_execute,
        audit_action="salesforce.inspect_apex",
        tags=["apex", "read"],
    )
)


# ---------------------------------------------------------------------------
# write_apex
# ---------------------------------------------------------------------------
WRITE_DESCRIPTION = """Deploy Apex you have written — a class or a trigger — together
with its test class.

You write the code. This tool checks it, packages it, deploys it and runs the
tests, and it will refuse rather than let a broken change through:

  * the declaration in the source must match `name`;
  * a non-test class or trigger must come with `test_body` (Salesforce requires
    coverage to deploy to production, and untested Apex is how orgs break);
  * `without sharing`, DML or SOQL inside loops, hardcoded record Ids, silent
    exception swallowing and unfiltered deletes are detected and shown to the
    human who approves the change;
  * the code is compiled against the org before it is proposed.

Before writing a trigger, call list_apex: if the object already has a trigger,
extend it instead of adding a second one — two triggers on one object run in an
undefined order.

Set `run_tests` (default true) to execute the tests after deployment. The result
reports what Salesforce actually returned; a timeout is reported as a timeout,
never as a pass.
"""

WRITE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": ["class", "trigger"], "default": "class"},
        "name": {"type": "string", "description": "Class or trigger API name."},
        "body": {"type": "string", "description": "Complete Apex source."},
        "test_name": {"type": "string", "description": "Test class name."},
        "test_body": {"type": "string", "description": "Complete Apex test class source."},
        "helper_classes": {
            "type": "array",
            "description": "Additional classes deployed in the same change.",
            "items": {
                "type": "object",
                "properties": {"name": {"type": "string"}, "body": {"type": "string"}},
                "required": ["name", "body"],
            },
        },
        "run_tests": {"type": "boolean", "default": True},
        "check_only": {"type": "boolean", "description": "Validate without saving."},
        "reason": {"type": "string"},
    },
    "required": ["kind", "name", "body"],
    "additionalProperties": False,
}

OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "success": {"type": "boolean"},
        "name": {"type": "string"},
        "deploy_id": {"type": "string"},
        "tests": {"type": "object"},
        "verified": {"type": "boolean"},
    },
}


def _units(ctx: ToolContext, args: dict[str, Any]) -> list[ApexUnit]:
    sf = ctx.require_sf()
    version = sf.api_version
    units = [
        parse_unit(
            str(args.get("kind") or "class"),
            str(args.get("name") or ""),
            str(args.get("body") or ""),
            version,
        )
    ]
    for helper in args.get("helper_classes") or []:
        units.append(
            parse_unit("class", str(helper.get("name")), str(helper.get("body")), version)
        )
    if args.get("test_body"):
        test_name = str(args.get("test_name") or f"{args.get('name')}Test")
        units.append(parse_unit("class", test_name, str(args["test_body"]), version))
    return units


async def _write_validate(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    try:
        units = _units(ctx, args)
    except ApexValidationError as exc:
        raise ToolValidationError(
            exc.message, "APEX_VALIDATION_ERROR", exc.suggested_action
        ) from exc

    primary = units[0]
    if not primary.is_test and not args.get("test_body"):
        raise ToolValidationError(
            f"No test class was supplied for {primary.name}.",
            "APEX_TEST_REQUIRED",
            "Write a test class covering the new behaviour and pass it as `test_body`. "
            "Salesforce will not deploy untested Apex to production, and untested Apex "
            "in a sandbox becomes untested Apex in production.",
            ["test_body"],
        )

    if primary.kind == "trigger":
        sf = ctx.require_sf()
        try:
            existing = await list_apex_triggers(sf, object_name=primary.trigger_object)
        except SalesforceError:
            existing = []
        others = [t for t in existing if t["name"].lower() != primary.name.lower()]
        if others:
            raise ToolValidationError(
                f"{primary.trigger_object} already has "
                f"{len(others)} trigger(s): {', '.join(t['name'] for t in others)}.",
                "TRIGGER_ALREADY_EXISTS",
                "Two triggers on one object execute in an undefined order. Inspect the "
                "existing trigger and extend it instead, or tell the user why a second "
                "one is genuinely required.",
            )

    critical = [s for unit in units for s in unit.smells if s.severity == "critical"]
    if critical:
        raise ToolValidationError(
            "The Apex has critical problems: "
            + " ".join(s.message for s in critical),
            "APEX_UNSAFE",
            "Rewrite the code to remove these problems. If 'without sharing' is truly "
            "required, say so explicitly to the user and explain the consequence — do "
            "not deploy it silently.",
        )
    return {"units": [u.name for u in units]}


async def _write_plan(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    units = _units(ctx, args)
    primary = units[0]
    smells = [
        {"code": s.code, "severity": s.severity, "message": s.message, "unit": u.name}
        for u in units
        for s in u.smells
    ]
    test_units = [u for u in units if u.is_test]
    return {
        "title": f"Deploy Apex {primary.kind} {primary.name}",
        "change_type": "apex.write",
        "object": primary.trigger_object or None,
        "summary": (
            f"Deploys {len(units)} Apex file(s): "
            + ", ".join(f"{u.name} ({u.kind})" for u in units)
        ),
        "details": [
            {"field": "Primary", "new_value": f"{primary.name} ({primary.kind})"},
            *(
                [{"field": "Trigger events", "new_value": ", ".join(primary.trigger_events)}]
                if primary.kind == "trigger"
                else []
            ),
            {"field": "Lines", "new_value": len(primary.body.splitlines())},
            {
                "field": "Tests",
                "new_value": (
                    ", ".join(
                        f"{u.name} ({count_test_methods(u.body)} methods)"
                        for u in test_units
                    )
                    or "none"
                ),
            },
            {"field": "Run tests after deploy", "new_value": bool(args.get("run_tests", True))},
        ],
        "code": {u.name: u.body for u in units},
        "findings": smells,
        "impact": (
            "Apex runs on every qualifying record operation in the org, including "
            "integrations and bulk loads. "
            + (
                f"This trigger fires on {primary.trigger_object} "
                f"({', '.join(primary.trigger_events)})."
                if primary.kind == "trigger"
                else "This class is callable by anything that references it."
            )
        ),
        "reason": args.get("reason", ""),
    }


async def _write_fingerprint(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    sf = ctx.require_sf()
    name = str(args.get("name") or "")
    kind = str(args.get("kind") or "class")
    try:
        record = await get_apex_body(sf, kind, name)
    except SalesforceError:
        record = None
    triggers: list[str] = []
    if kind == "trigger":
        try:
            unit = parse_unit(kind, name, str(args.get("body") or ""), sf.api_version)
            triggers = sorted(
                t["name"] for t in await list_apex_triggers(sf, object_name=unit.trigger_object)
            )
        except (ApexValidationError, SalesforceError):
            triggers = []
    return {
        "exists": record is not None,
        "existing_length": len(record.get("body") or "") if record else None,
        "object_triggers": triggers,
    }


async def _write_execute(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    sf = ctx.require_sf()
    units = _units(ctx, args)
    primary = units[0]
    check_only = bool(args.get("check_only"))

    files, types = build_package(units)
    test_classes = [u.name for u in units if u.is_test]
    should_run_tests = bool(args.get("run_tests", True)) and bool(test_classes)

    result, deployment, error = await deploy_package(
        ctx,
        files=files,
        types=types,
        check_only=check_only,
        # Running the new tests as part of the deployment is what makes a
        # failing test block the change rather than land and then be noticed.
        test_level="RunSpecifiedTests" if should_run_tests else "NoTestRun",
        run_tests=test_classes if should_run_tests else None,
        label=f"apex {primary.name}",
    )
    if error is not None:
        return error | {"name": primary.name, "kind": primary.kind}
    assert result is not None

    payload = ok(
        name=primary.name,
        kind=primary.kind,
        object=primary.trigger_object or None,
        deploy_id=result.id,
        deployment_id=deployment.id,
        status=result.status,
        check_only=check_only,
        deployed=[u.name for u in units],
        deployment_tests={
            "total": result.tests_total,
            "failed": result.tests_failed,
            "failures": result.test_failures[:10],
        },
        findings=[
            {"code": s.code, "severity": s.severity, "message": s.message}
            for u in units
            for s in u.smells
        ],
    )

    if check_only or not should_run_tests:
        return payload

    # A second, explicit test run gives per-method outcomes and coverage that
    # the deployment summary does not carry.
    await ctx.emit("apex.tests_started", {"classes": test_classes})
    try:
        run = await run_tests(sf, class_names=test_classes)
    except SalesforceError as exc:
        payload["tests"] = {
            "ran": False,
            "error": exc.message,
            "note": "The deployment succeeded but the follow-up test run could not start.",
        }
        return payload

    payload["tests"] = run.to_dict() | {"ran": True}
    await ctx.emit(
        "apex.tests_finished",
        {
            "status": run.status,
            "methods_run": run.methods_run,
            "methods_failed": run.methods_failed,
        },
    )
    if run.timed_out:
        payload["success"] = False
        payload["error_type"] = "APEX_TESTS_TIMEOUT"
        payload["message"] = (
            f"The code deployed, but the test run ({run.job_id}) had not finished when "
            "we stopped waiting. The tests are still running in Salesforce."
        )
        payload["suggested_action"] = (
            "Tell the user the deployment landed and the tests are still running; "
            "do not claim they passed."
        )
        return payload
    if not run.success:
        payload["success"] = False
        payload["error_type"] = "APEX_TESTS_FAILED"
        payload["message"] = (
            f"{run.methods_failed} of {run.methods_run} test method(s) failed after "
            "deployment."
        )
        payload["suggested_action"] = (
            "Read the failures, fix the code or the tests, and deploy again. Do not "
            "describe this change as working."
        )
    payload["coverage"] = await apex_code_coverage(sf, [primary.name])
    return payload


async def _write_verify(
    ctx: ToolContext, args: dict[str, Any], result: dict[str, Any]
) -> dict[str, Any]:
    if not result.get("success"):
        return {"verified": False, "reason": "Deployment or tests did not succeed."}
    if result.get("check_only"):
        return {
            "verified": True,
            "reason": "Validation-only deployment: nothing was saved to the org.",
        }
    sf = ctx.require_sf()
    name = str(result.get("name"))
    kind = str(result.get("kind") or "class")
    record = await get_apex_body(sf, kind, name)
    if record is None:
        return {
            "verified": False,
            "reason": (
                f"The deployment reported success but no Apex {kind} named '{name}' is "
                "present in the org."
            ),
        }
    expected = str(args.get("body") or "").strip()
    actual = str(record.get("body") or "").strip()
    if expected and actual and expected != actual:
        return {
            "verified": False,
            "reason": (
                f"'{name}' exists in the org but its source differs from what was "
                "deployed. Something else may have changed it."
            ),
        }
    return {
        "verified": True,
        "method": "Tooling API source read after deployment",
        "after": {"name": name, "kind": kind, "api_version": record.get("api_version")},
    }


registry.register(
    Tool(
        name="write_apex",
        description=WRITE_DESCRIPTION,
        input_schema=WRITE_SCHEMA,
        output_schema=OUTPUT_SCHEMA,
        risk=RiskLevel.MEDIUM,
        requires_approval=True,
        execute=_write_execute,
        validate=_write_validate,
        plan=_write_plan,
        verify=_write_verify,
        fingerprint=_write_fingerprint,
        mutating=True,
        audit_action="salesforce.write_apex",
        tags=["apex", "metadata", "deploy"],
        long_running=True,
    )
)


# ---------------------------------------------------------------------------
# validate_apex — compile without saving
# ---------------------------------------------------------------------------
async def _validate_apex_execute(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    sf = ctx.require_sf()
    try:
        unit = parse_unit(
            str(args.get("kind") or "class"),
            str(args.get("name") or ""),
            str(args.get("body") or ""),
            sf.api_version,
        )
    except ApexValidationError as exc:
        return fail("APEX_VALIDATION_ERROR", exc.message, suggested_action=exc.suggested_action)

    try:
        compiled = await compile_check(sf, unit)
    except SalesforceError as exc:
        return exc.to_dict()

    findings = [
        {"code": s.code, "severity": s.severity, "message": s.message} for s in unit.smells
    ]
    if not compiled.get("compiled"):
        return fail(
            "APEX_COMPILE_FAILED",
            compiled.get("error") or "The Apex did not compile against this org.",
            suggested_action="Fix the reported problems and validate again.",
            problems=compiled.get("problems"),
            findings=findings,
        )
    return ok(
        name=unit.name,
        kind=unit.kind,
        compiled=True,
        test_methods=count_test_methods(unit.body),
        findings=findings,
        message=(
            f"{unit.name} compiles against this org. Nothing was saved — this was a "
            "compile-only check."
        ),
    )


registry.register(
    Tool(
        name="validate_apex",
        description=(
            "Compile Apex against the org without saving it.\n\n"
            "This is a real Tooling API compile, so success means Salesforce accepted "
            "the code, not that it looked right. Use it before proposing Apex so the "
            "human reviews something that will actually deploy."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["class", "trigger"], "default": "class"},
                "name": {"type": "string"},
                "body": {"type": "string"},
            },
            "required": ["name", "body"],
            "additionalProperties": False,
        },
        output_schema=OUTPUT_SCHEMA,
        risk=RiskLevel.LOW,
        requires_approval=False,
        execute=_validate_apex_execute,
        audit_action="salesforce.validate_apex",
        tags=["apex", "read"],
        long_running=True,
    )
)


# ---------------------------------------------------------------------------
# run_apex_tests
# ---------------------------------------------------------------------------
async def _run_tests_execute(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    sf = ctx.require_sf()
    class_names = args.get("class_names") or []
    test_level = args.get("test_level")
    if not class_names and not test_level:
        test_level = "RunLocalTests"

    await ctx.emit("apex.tests_started", {"classes": class_names, "level": test_level})
    try:
        run = await run_tests(sf, class_names=class_names or None, test_level=test_level)
    except SalesforceError as exc:
        return exc.to_dict()

    payload = run.to_dict()
    if run.timed_out:
        return fail(
            "APEX_TESTS_TIMEOUT",
            f"Test run {run.job_id} had not finished when we stopped waiting.",
            retryable=True,
            suggested_action=(
                "The tests are still running in Salesforce. Tell the user the run is "
                "in progress; do not report an outcome."
            ),
            **payload,
        )
    if not run.success:
        return fail(
            "APEX_TESTS_FAILED",
            f"{run.methods_failed} of {run.methods_run} test method(s) failed.",
            suggested_action=(
                "Read each failure message and stack trace, identify the cause, and "
                "propose a fix. Do not re-run the same tests unchanged."
            ),
            **payload,
        )
    coverage = await apex_code_coverage(sf, class_names) if class_names else {}
    return ok(**payload, coverage=coverage)


registry.register(
    Tool(
        name="run_apex_tests",
        description=(
            "Run Apex tests in the org and wait for the real outcome.\n\n"
            "Pass `class_names` to run specific test classes, or `test_level` "
            "(RunLocalTests / RunAllTestsInOrg) to run a suite. The result reports what "
            "Salesforce returned: pass, fail with messages and stack traces, or "
            "still-running. A run that has not finished is reported as unfinished — "
            "never as a pass."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "class_names": {"type": "array", "items": {"type": "string"}},
                "test_level": {
                    "type": "string",
                    "enum": ["RunLocalTests", "RunAllTestsInOrg", "RunSpecifiedTests"],
                },
            },
            "additionalProperties": False,
        },
        output_schema=OUTPUT_SCHEMA,
        # Running tests executes org code and consumes limits, but changes no
        # metadata; it is a read-heavy operation with a real cost.
        risk=RiskLevel.LOW,
        requires_approval=False,
        execute=_run_tests_execute,
        audit_action="salesforce.run_apex_tests",
        tags=["apex", "read"],
        long_running=True,
    )
)
