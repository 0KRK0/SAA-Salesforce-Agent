"""Apex support: source validation, packaging and real test execution.

The agent writes the Apex — that is the point of an AI engineering agent — but
writing it is not the risky part. The risky parts are deploying code whose
declaration does not match its filename, deploying without tests, calling a
test run "passing" when Salesforce never ran it, and shipping code that quietly
bypasses the sharing model. This module is where those are caught.

There is no Apex parser here and no pretence of one. What there is:
  * declaration extraction, so the file name and the class name always agree;
  * a set of deterministic *smells* that escalate risk and are shown to the
    human approving the change, never silently "fixed";
  * a real Tooling API test runner that polls the actual job and reports what
    Salesforce reports.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from typing import Any

from app.observability.logging import get_logger
from app.salesforce.client import SalesforceClient
from app.salesforce.errors import SalesforceError

log = get_logger("salesforce.apex")

CLASS_DECL_RE = re.compile(
    r"\b(?:global|public|private|protected)?\s*(?:with\s+sharing|without\s+sharing|"
    r"inherited\s+sharing)?\s*(?:abstract\s+|virtual\s+)?class\s+([A-Za-z]\w*)",
    re.IGNORECASE,
)
TRIGGER_DECL_RE = re.compile(
    r"\btrigger\s+([A-Za-z]\w*)\s+on\s+([A-Za-z]\w*)\s*\(([^)]*)\)", re.IGNORECASE
)
TEST_ANNOTATION_RE = re.compile(r"@isTest", re.IGNORECASE)
TEST_METHOD_RE = re.compile(r"@isTest\s|\btestMethod\b", re.IGNORECASE)

MAX_BODY_CHARS = 1_000_000  # Salesforce's own class size ceiling.


class ApexValidationError(ValueError):
    def __init__(self, message: str, suggested_action: str = ""):
        super().__init__(message)
        self.message = message
        self.suggested_action = suggested_action


@dataclass
class ApexSmell:
    """A deterministic finding about the code, shown on the approval card."""

    code: str
    severity: str  # info | warning | critical
    message: str


@dataclass
class ApexUnit:
    kind: str  # class | trigger
    name: str
    body: str
    api_version: str
    trigger_object: str = ""
    trigger_events: list[str] = field(default_factory=list)
    smells: list[ApexSmell] = field(default_factory=list)

    @property
    def path(self) -> str:
        folder = "classes" if self.kind == "class" else "triggers"
        suffix = "cls" if self.kind == "class" else "trigger"
        return f"{folder}/{self.name}.{suffix}"

    @property
    def meta_path(self) -> str:
        return f"{self.path}-meta.xml"

    @property
    def metadata_type(self) -> str:
        return "ApexClass" if self.kind == "class" else "ApexTrigger"

    @property
    def is_test(self) -> bool:
        return bool(TEST_ANNOTATION_RE.search(self.body))

    def has_critical_smell(self) -> bool:
        return any(s.severity == "critical" for s in self.smells)


def meta_xml(api_version: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<ApexClass xmlns="http://soap.sforce.com/2006/04/metadata">'
        f"<apiVersion>{api_version}</apiVersion><status>Active</status>"
        "</ApexClass>"
    )


def trigger_meta_xml(api_version: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<ApexTrigger xmlns="http://soap.sforce.com/2006/04/metadata">'
        f"<apiVersion>{api_version}</apiVersion><status>Active</status>"
        "</ApexTrigger>"
    )


def parse_unit(kind: str, name: str, body: str, api_version: str) -> ApexUnit:
    """Validate that the source actually declares what it claims to declare.

    A class whose file is `Foo.cls` but whose source says `class Bar` deploys
    to a confusing failure. Catching it here costs nothing and saves an agent
    step plus a deployment.
    """
    body = (body or "").strip()
    if not body:
        raise ApexValidationError("The Apex body is empty.", "Write the code before deploying.")
    if len(body) > MAX_BODY_CHARS:
        raise ApexValidationError(
            f"The Apex body is {len(body):,} characters, above Salesforce's limit.",
            "Split the logic across classes.",
        )

    if kind == "trigger":
        match = TRIGGER_DECL_RE.search(body)
        if match is None:
            raise ApexValidationError(
                "The source does not contain a valid trigger declaration.",
                "A trigger must read: trigger Name on Object (before insert, ...) { ... }",
            )
        declared, obj, events = match.group(1), match.group(2), match.group(3)
        if declared.lower() != name.lower():
            raise ApexValidationError(
                f"The trigger is named '{name}' but the source declares '{declared}'.",
                "Make the declaration and the name match.",
            )
        unit = ApexUnit(
            kind="trigger",
            name=declared,
            body=body,
            api_version=api_version,
            trigger_object=obj,
            trigger_events=[e.strip() for e in events.split(",") if e.strip()],
        )
    else:
        match = CLASS_DECL_RE.search(body)
        if match is None:
            raise ApexValidationError(
                "The source does not contain a class declaration.",
                "Apex classes must declare: public class Name { ... }",
            )
        declared = match.group(1)
        if declared.lower() != name.lower():
            raise ApexValidationError(
                f"The class is named '{name}' but the source declares '{declared}'.",
                "Make the declaration and the name match.",
            )
        unit = ApexUnit(kind="class", name=declared, body=body, api_version=api_version)

    unit.smells = detect_smells(unit)
    return unit


#: Patterns worth telling a human about before they approve Apex. Each one is
#: a real production incident someone has already had.
_SMELL_RULES: tuple[tuple[str, str, str, str], ...] = (
    (
        "WITHOUT_SHARING",
        r"\bwithout\s+sharing\b",
        "critical",
        "Declared 'without sharing': this code ignores the org's record-level "
        "security and can read or write records the running user cannot see.",
    ),
    (
        "DML_IN_LOOP",
        r"for\s*\([^)]*\)\s*\{[^}]*\b(insert|update|delete|upsert)\s+\w",
        "warning",
        "DML appears inside a loop. This hits governor limits as soon as the "
        "org processes records in bulk.",
    ),
    (
        "SOQL_IN_LOOP",
        r"for\s*\([^)]*\)\s*\{[^}]*\[\s*SELECT\b",
        "warning",
        "A SOQL query appears inside a loop — the classic governor-limit bug.",
    ),
    (
        "HARDCODED_ID",
        r"['\"](?:00[1-9A-Za-z]|a0[0-9A-Za-z])[A-Za-z0-9]{12,15}['\"]",
        "warning",
        "A Salesforce record Id appears hardcoded. Ids differ between sandbox "
        "and production, so this breaks on deployment.",
    ),
    (
        "SEEALLDATA",
        r"@isTest\s*\(\s*SeeAllData\s*=\s*true\s*\)",
        "warning",
        "Test uses SeeAllData=true, so it depends on whatever data happens to "
        "exist in the org and will behave differently elsewhere.",
    ),
    (
        "EMPTY_CATCH",
        r"catch\s*\([^)]*\)\s*\{\s*\}",
        "warning",
        "An exception is caught and silently discarded, which hides failures.",
    ),
    (
        "DELETE_WITHOUT_FILTER",
        r"\bdelete\s+\[\s*SELECT\b(?![^\]]*\bWHERE\b)",
        "critical",
        "A delete operates on an unfiltered query — this would delete every "
        "record of that type the user can see.",
    ),
    (
        "SYSTEM_RUNAS_PROD",
        r"\bsystem\.runAs\b",
        "info",
        "Uses System.runAs — expected in tests, unusual elsewhere.",
    ),
    (
        "FUTURE_CALLOUT",
        r"@future\s*\(\s*callout\s*=\s*true\s*\)",
        "info",
        "Makes an asynchronous callout; confirm the remote site is configured.",
    ),
)


def detect_smells(unit: ApexUnit) -> list[ApexSmell]:
    findings: list[ApexSmell] = []
    for code, pattern, severity, message in _SMELL_RULES:
        if re.search(pattern, unit.body, re.IGNORECASE | re.DOTALL):
            if code == "SEEALLDATA" and not unit.is_test:
                continue
            if code == "SYSTEM_RUNAS_PROD" and unit.is_test:
                continue
            findings.append(ApexSmell(code=code, severity=severity, message=message))

    if unit.kind == "trigger" and not re.search(r"\b\w+Handler\b|\bTriggerHandler\b", unit.body):
        if len(unit.body.splitlines()) > 25:
            findings.append(
                ApexSmell(
                    code="LOGIC_IN_TRIGGER",
                    severity="info",
                    message=(
                        "Business logic lives directly in the trigger. Most orgs keep "
                        "triggers thin and delegate to a handler class."
                    ),
                )
            )
    return findings


def count_test_methods(body: str) -> int:
    return len(re.findall(r"@isTest\s*(?:\([^)]*\))?\s*(?:static\s+)?\w", body, re.IGNORECASE))


def build_package(units: list[ApexUnit]) -> tuple[dict[str, str], dict[str, list[str]]]:
    files: dict[str, str] = {}
    types: dict[str, list[str]] = {}
    for unit in units:
        files[unit.path] = unit.body
        files[unit.meta_path] = (
            meta_xml(unit.api_version) if unit.kind == "class"
            else trigger_meta_xml(unit.api_version)
        )
        types.setdefault(unit.metadata_type, []).append(unit.name)
    return files, types


# ---------------------------------------------------------------------- tests
@dataclass
class TestRunResult:
    job_id: str
    status: str
    methods_run: int = 0
    methods_failed: int = 0
    failures: list[dict[str, Any]] = field(default_factory=list)
    passed: list[str] = field(default_factory=list)
    timed_out: bool = False

    @property
    def success(self) -> bool:
        return self.status == "Completed" and self.methods_failed == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "status": self.status,
            "methods_run": self.methods_run,
            "methods_failed": self.methods_failed,
            "passed": self.passed[:50],
            "failures": self.failures[:20],
            "timed_out": self.timed_out,
        }


async def run_tests(
    sf: SalesforceClient,
    *,
    class_names: list[str] | None = None,
    test_level: str | None = None,
    poll_interval: float = 3.0,
    timeout: float = 600.0,
) -> TestRunResult:
    """Run Apex tests through the Tooling API and wait for the real result.

    Salesforce runs tests asynchronously. A tool that fires the request and
    returns would be reporting "tests started", so this polls the job and only
    returns once Salesforce has an outcome — or says plainly that it timed out
    and the job is still running.
    """
    payload: dict[str, Any] = {}
    if class_names:
        payload["classNames"] = ",".join(class_names)
    if test_level:
        payload["testLevel"] = test_level
    if not payload:
        payload["testLevel"] = "RunLocalTests"

    job_id = await sf.request(
        "POST", f"{sf.tooling_base}/runTestsAsynchronous/", json=payload
    )
    if isinstance(job_id, str):
        job_id = job_id.strip().strip('"')
    else:
        job_id = str(job_id)
    log.info("apex.test_run_started", job_id=job_id, classes=class_names)

    deadline = time.monotonic() + timeout
    status = "Queued"
    run_id = None
    while True:
        await asyncio.sleep(poll_interval)
        data = await sf.tooling_query(
            "SELECT Id, Status, ClassesCompleted, ClassesEnqueued, MethodsEnqueued "
            f"FROM ApexTestRunResult WHERE AsyncApexJobId = '{job_id}' LIMIT 1"
        )
        records = data.get("records") or []
        if records:
            run_id = records[0].get("Id")
            status = str(records[0].get("Status") or "Queued")
            if status in {"Completed", "Failed", "Aborted"}:
                break
        if time.monotonic() > deadline:
            return TestRunResult(
                job_id=job_id,
                status=status,
                timed_out=True,
            )

    return await _collect_results(sf, job_id=job_id, run_id=run_id, status=status)


async def _collect_results(
    sf: SalesforceClient, *, job_id: str, run_id: str | None, status: str
) -> TestRunResult:
    if run_id is None:
        return TestRunResult(job_id=job_id, status=status)
    data = await sf.tooling_query(
        "SELECT Id, Outcome, MethodName, Message, StackTrace, ApexClass.Name "
        f"FROM ApexTestResult WHERE ApexTestRunResultId = '{run_id}' LIMIT 500"
    )
    failures: list[dict[str, Any]] = []
    passed: list[str] = []
    for row in data.get("records") or []:
        klass = (row.get("ApexClass") or {}).get("Name", "")
        label = f"{klass}.{row.get('MethodName')}"
        if row.get("Outcome") == "Pass":
            passed.append(label)
        else:
            failures.append(
                {
                    "test": label,
                    "outcome": row.get("Outcome"),
                    "message": row.get("Message"),
                    "stack_trace": (row.get("StackTrace") or "")[:1000],
                }
            )
    return TestRunResult(
        job_id=job_id,
        status=status,
        methods_run=len(passed) + len(failures),
        methods_failed=len(failures),
        failures=failures,
        passed=passed,
    )


async def compile_check(sf: SalesforceClient, unit: ApexUnit) -> dict[str, Any]:
    """Ask Salesforce whether this Apex compiles, without saving it.

    Uses a ContainerAsyncRequest against a MetadataContainer — the Tooling
    API's own compile-only path. It is genuinely a compile, not a lint.
    """
    container = await sf.tooling_create(
        "MetadataContainer", {"Name": f"agent_{int(time.time())}"}
    )
    container_id = container.get("id")
    if not container_id:
        raise SalesforceError(
            error_type="APEX_CONTAINER_FAILED",
            message="Could not create a Tooling API MetadataContainer for the compile check.",
            retryable=True,
        )
    try:
        member_type = "ApexClassMember" if unit.kind == "class" else "ApexTriggerMember"
        await sf.tooling_create(
            member_type, {"MetadataContainerId": container_id, "Body": unit.body}
        )
        request = await sf.tooling_create(
            "ContainerAsyncRequest",
            {"MetadataContainerId": container_id, "IsCheckOnly": True},
        )
        request_id = request.get("id")
        deadline = time.monotonic() + 120
        while True:
            await asyncio.sleep(2.0)
            data = await sf.tooling_query(
                "SELECT Id, State, ErrorMsg, DeployDetails FROM ContainerAsyncRequest "
                f"WHERE Id = '{request_id}' LIMIT 1"
            )
            records = data.get("records") or []
            state = str(records[0].get("State")) if records else "Queued"
            if state in {"Completed", "Failed", "Error", "Aborted"}:
                details = records[0].get("DeployDetails") or {}
                problems = [
                    {
                        "line": f.get("lineNumber"),
                        "column": f.get("columnNumber"),
                        "problem": f.get("problem"),
                    }
                    for f in (details.get("componentFailures") or [])
                ]
                return {
                    "compiled": state == "Completed" and not problems,
                    "state": state,
                    "error": records[0].get("ErrorMsg"),
                    "problems": problems,
                }
            if time.monotonic() > deadline:
                return {
                    "compiled": False,
                    "state": state,
                    "error": "The compile check did not finish within 120 seconds.",
                    "problems": [],
                }
    finally:
        try:
            await sf.request(
                "DELETE",
                f"{sf.tooling_base}/sobjects/MetadataContainer/{container_id}",
                expect_json=False,
            )
        except SalesforceError:  # pragma: no cover - cleanup is best effort
            log.warning("apex.container_cleanup_failed", container_id=container_id)
