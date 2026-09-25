"""Salesforce Metadata API (SOAP) deployment layer.

This is a real Metadata API integration: it builds a deployment package
(package.xml + component source), base64-encodes the zip, calls deploy(),
polls checkDeployStatus() and returns the parsed component/test results.

Supported component generation in the MVP:
  * CustomField on an existing object (extensible field-type registry)
  * Profile fieldPermissions (optional field-level security grant)
Anything else can still be deployed by passing raw source files.
"""

from __future__ import annotations

import asyncio
import base64
import io
import re
import time
import zipfile
from dataclasses import dataclass, field
from typing import Any
from xml.sax.saxutils import escape

import httpx
from defusedxml import ElementTree as SafeET  # type: ignore[import-untyped]

from app.config import settings
from app.observability.logging import get_logger
from app.salesforce.errors import SalesforceError

log = get_logger("salesforce.metadata")

MD_NS = "http://soap.sforce.com/2006/04/metadata"
SOAP_NS = "http://schemas.xmlsoap.org/soap/envelope/"

API_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


# --------------------------------------------------------------------- types
@dataclass(frozen=True)
class FieldTypeSpec:
    salesforce_type: str
    requires: tuple[str, ...] = ()
    optional: tuple[str, ...] = ()
    defaults: dict[str, Any] = field(default_factory=dict)


# Deliberately a small, extensible registry — not every Salesforce type.
FIELD_TYPES: dict[str, FieldTypeSpec] = {
    "Text": FieldTypeSpec("Text", ("length",), ("unique", "required", "defaultValue"),
                          {"length": 255}),
    "LongTextArea": FieldTypeSpec(
        "LongTextArea", ("length", "visibleLines"), (), {"length": 32768, "visibleLines": 3}
    ),
    "Number": FieldTypeSpec(
        "Number", ("precision", "scale"), ("unique", "required"),
        {"precision": 18, "scale": 0},
    ),
    "Currency": FieldTypeSpec(
        "Currency", ("precision", "scale"), ("required",), {"precision": 18, "scale": 2}
    ),
    "Percent": FieldTypeSpec(
        "Percent", ("precision", "scale"), ("required",), {"precision": 18, "scale": 2}
    ),
    "Checkbox": FieldTypeSpec("Checkbox", (), ("defaultValue",), {"defaultValue": "false"}),
    "Date": FieldTypeSpec("Date", (), ("required",)),
    "DateTime": FieldTypeSpec("DateTime", (), ("required",)),
    "Email": FieldTypeSpec("Email", (), ("required", "unique")),
    "Phone": FieldTypeSpec("Phone", (), ("required",)),
    "Url": FieldTypeSpec("Url", (), ("required",)),
    "Picklist": FieldTypeSpec("Picklist", ("picklist_values",), ("required",)),
    "MultiselectPicklist": FieldTypeSpec(
        "MultiselectPicklist", ("picklist_values", "visibleLines"), (), {"visibleLines": 4}
    ),
    "Lookup": FieldTypeSpec(
        "Lookup", ("referenceTo", "relationshipLabel", "relationshipName"),
        ("required", "deleteConstraint"), {"deleteConstraint": "SetNull"},
    ),
}


class MetadataValidationError(ValueError):
    def __init__(self, message: str, suggested_action: str = "", missing: list[str] | None = None):
        super().__init__(message)
        self.message = message
        self.suggested_action = suggested_action
        self.missing = missing or []


def normalize_field_api_name(name: str) -> str:
    """Accept 'Customer Tier', 'Customer_Tier' or 'Customer_Tier__c'."""
    base = name.strip()
    if base.lower().endswith("__c"):
        base = base[:-3]
    base = re.sub(r"[^A-Za-z0-9_]+", "_", base).strip("_")
    base = re.sub(r"_{2,}", "_", base)
    if not base:
        raise MetadataValidationError("Field name is empty after normalization.")
    if not base[0].isalpha():
        base = f"X{base}"
    return f"{base}__c"


def _el(tag: str, value: Any) -> str:
    return f"<{tag}>{escape(str(value))}</{tag}>"


def build_custom_field_xml(spec: dict[str, Any]) -> str:
    """Build the <fields> block for a CustomField.

    spec keys: api_name, label, type, plus type-specific options.
    """
    ftype = spec.get("type")
    if ftype not in FIELD_TYPES:
        raise MetadataValidationError(
            f"Unsupported field type '{ftype}'.",
            f"Supported types: {', '.join(sorted(FIELD_TYPES))}",
        )
    tspec = FIELD_TYPES[ftype]
    merged = {**tspec.defaults, **{k: v for k, v in spec.items() if v is not None}}

    missing = [k for k in tspec.requires if not merged.get(k)]
    if missing:
        raise MetadataValidationError(
            f"Missing required configuration for a {ftype} field: {', '.join(missing)}.",
            "Ask the user for these values before proposing the change.",
            missing,
        )

    api_name = normalize_field_api_name(str(spec["api_name"]))
    label = str(spec.get("label") or api_name[:-3].replace("_", " "))

    parts = [_el("fullName", api_name), _el("label", label), _el("type", tspec.salesforce_type)]

    if ftype in {"Text", "LongTextArea", "MultiselectPicklist"} and merged.get("length"):
        parts.append(_el("length", int(merged["length"])))
    if ftype in {"LongTextArea", "MultiselectPicklist"}:
        parts.append(_el("visibleLines", int(merged.get("visibleLines", 3))))
    if ftype in {"Number", "Currency", "Percent"}:
        parts.append(_el("precision", int(merged["precision"])))
        parts.append(_el("scale", int(merged["scale"])))
    if ftype == "Checkbox":
        dv = str(merged.get("defaultValue", "false")).lower()
        parts.append(_el("defaultValue", "true" if dv in {"true", "1", "yes"} else "false"))
    if ftype == "Lookup":
        parts.append(_el("referenceTo", merged["referenceTo"]))
        parts.append(_el("relationshipLabel", merged["relationshipLabel"]))
        parts.append(_el("relationshipName", merged["relationshipName"]))
        parts.append(_el("deleteConstraint", merged.get("deleteConstraint", "SetNull")))
    if ftype in {"Picklist", "MultiselectPicklist"}:
        values = merged["picklist_values"]
        if isinstance(values, str):
            values = [v.strip() for v in values.split(",") if v.strip()]
        vs = "".join(
            "<value>"
            + _el("fullName", v)
            + _el("default", "true" if i == 0 and merged.get("first_value_default") else "false")
            + "</value>"
            for i, v in enumerate(values)
        )
        parts.append(
            "<valueSet><restricted>"
            + ("true" if merged.get("restricted", True) else "false")
            + "</restricted><valueSetDefinition><sorted>false</sorted>"
            + vs
            + "</valueSetDefinition></valueSet>"
        )
    if merged.get("description"):
        parts.append(_el("description", merged["description"]))
    if merged.get("inlineHelpText"):
        parts.append(_el("inlineHelpText", merged["inlineHelpText"]))
    if ftype != "Checkbox" and merged.get("required"):
        parts.append(_el("required", "true"))
    if merged.get("unique") and ftype in {"Text", "Number", "Email"}:
        parts.append(_el("unique", "true"))
    if merged.get("externalId") and ftype in {"Text", "Number", "Email"}:
        parts.append(_el("externalId", "true"))

    return "<fields>" + "".join(parts) + "</fields>"


def build_object_file(object_api_name: str, field_blocks: list[str]) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<CustomObject xmlns="{MD_NS}">' + "".join(field_blocks) + "</CustomObject>"
    )


def build_profile_file(
    profile_name: str, field_permissions: list[dict[str, Any]]
) -> str:
    perms = "".join(
        "<fieldPermissions>"
        + _el("editable", "true" if p.get("editable", True) else "false")
        + _el("field", p["field"])
        + _el("readable", "true" if p.get("readable", True) else "false")
        + "</fieldPermissions>"
        for p in field_permissions
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<Profile xmlns="{MD_NS}">{perms}</Profile>'
    )


def build_package_xml(types: dict[str, list[str]], api_version: str) -> str:
    blocks = []
    for name, members in types.items():
        member_xml = "".join(_el("members", m) for m in members)
        blocks.append(f"<types>{member_xml}{_el('name', name)}</types>")
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<Package xmlns="{MD_NS}">' + "".join(blocks) + _el("version", api_version) + "</Package>"
    )


def build_zip(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path, content in files.items():
            zf.writestr(path, content)
    return buf.getvalue()


# ----------------------------------------------------------------- SOAP layer
@dataclass
class DeployResult:
    id: str
    status: str
    done: bool
    success: bool
    components_total: int = 0
    components_deployed: int = 0
    components_failed: int = 0
    tests_total: int = 0
    tests_completed: int = 0
    tests_failed: int = 0
    errors: list[dict[str, Any]] = field(default_factory=list)
    test_failures: list[dict[str, Any]] = field(default_factory=list)
    raw_status: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "deploy_id": self.id,
            "status": self.status,
            "done": self.done,
            "success": self.success,
            "components": {
                "total": self.components_total,
                "deployed": self.components_deployed,
                "failed": self.components_failed,
            },
            "tests": {
                "total": self.tests_total,
                "completed": self.tests_completed,
                "failed": self.tests_failed,
            },
            "errors": self.errors,
            "test_failures": self.test_failures,
        }


def _text(node: Any, tag: str, default: str = "") -> str:
    found = node.find(f"{{{MD_NS}}}{tag}")
    return found.text if found is not None and found.text is not None else default


class MetadataClient:
    """Metadata API client bound to one Salesforce connection."""

    def __init__(self, instance_url: str, access_token: str, api_version: str | None = None):
        self.instance_url = instance_url.rstrip("/")
        self._token = access_token
        self.api_version = api_version or settings.salesforce_api_version

    @property
    def endpoint(self) -> str:
        return f"{self.instance_url}/services/Soap/m/{self.api_version}"

    def _envelope(self, body: str) -> str:
        return (
            f'<?xml version="1.0" encoding="UTF-8"?>'
            f'<soapenv:Envelope xmlns:soapenv="{SOAP_NS}" xmlns:met="{MD_NS}">'
            f"<soapenv:Header><met:SessionHeader>"
            f"<met:sessionId>{escape(self._token)}</met:sessionId>"
            f"</met:SessionHeader></soapenv:Header>"
            f"<soapenv:Body>{body}</soapenv:Body></soapenv:Envelope>"
        )

    async def _call(self, action: str, body: str) -> Any:
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(
                self.endpoint,
                content=self._envelope(body).encode("utf-8"),
                headers={
                    "Content-Type": "text/xml; charset=UTF-8",
                    "SOAPAction": action,
                },
            )
        if resp.status_code >= 300:
            fault = _extract_fault(resp.text)
            raise SalesforceError(
                error_type=fault.get("code", "METADATA_API_ERROR"),
                message=fault.get("message", f"Metadata API HTTP {resp.status_code}"),
                status_code=resp.status_code,
                likely_cause="The Metadata API rejected the request.",
                suggested_action=(
                    "Check that the session is valid, the org allows metadata "
                    "deployments, and the package is well formed."
                ),
                retryable=resp.status_code >= 500,
            )
        return SafeET.fromstring(resp.text.encode("utf-8"))

    async def deploy(
        self,
        zip_bytes: bytes,
        *,
        check_only: bool = False,
        rollback_on_error: bool = True,
        test_level: str = "NoTestRun",
        run_tests: list[str] | None = None,
        ignore_warnings: bool = False,
    ) -> str:
        opts = [
            _el("met:checkOnly", "true" if check_only else "false"),
            _el("met:rollbackOnError", "true" if rollback_on_error else "false"),
            _el("met:singlePackage", "true"),
            _el("met:testLevel", test_level),
            _el("met:ignoreWarnings", "true" if ignore_warnings else "false"),
        ]
        for t in run_tests or []:
            opts.append(_el("met:runTests", t))
        body = (
            "<met:deploy>"
            f"<met:ZipFile>{base64.b64encode(zip_bytes).decode()}</met:ZipFile>"
            f"<met:DeployOptions>{''.join(opts)}</met:DeployOptions>"
            "</met:deploy>"
        )
        root = await self._call("deploy", body)
        node = root.find(f".//{{{MD_NS}}}result")
        if node is None:
            raise SalesforceError(
                error_type="METADATA_DEPLOY_NO_RESULT",
                message="Metadata API deploy() returned no result element.",
                suggested_action="Retry; if it persists inspect the raw SOAP response.",
                retryable=True,
            )
        return _text(node, "id")

    async def check_deploy_status(self, deploy_id: str) -> DeployResult:
        body = (
            "<met:checkDeployStatus>"
            f"{_el('met:asyncProcessId', deploy_id)}"
            f"{_el('met:includeDetails', 'true')}"
            "</met:checkDeployStatus>"
        )
        root = await self._call("checkDeployStatus", body)
        node = root.find(f".//{{{MD_NS}}}result")
        if node is None:
            raise SalesforceError(
                error_type="METADATA_STATUS_NO_RESULT",
                message=f"checkDeployStatus returned no result for {deploy_id}.",
                retryable=True,
            )

        errors: list[dict[str, Any]] = []
        for failure in node.findall(f".//{{{MD_NS}}}componentFailures"):
            errors.append(
                {
                    "component": _text(failure, "fullName"),
                    "type": _text(failure, "componentType"),
                    "file": _text(failure, "fileName"),
                    "problem_type": _text(failure, "problemType"),
                    "problem": _text(failure, "problem"),
                    "line": _text(failure, "lineNumber"),
                }
            )
        test_failures: list[dict[str, Any]] = []
        for tf in node.findall(f".//{{{MD_NS}}}failures"):
            test_failures.append(
                {
                    "class": _text(tf, "name"),
                    "method": _text(tf, "methodName"),
                    "message": _text(tf, "message"),
                    "stack_trace": _text(tf, "stackTrace"),
                }
            )
        if not errors and _text(node, "errorMessage"):
            errors.append(
                {
                    "component": "<deployment>",
                    "problem": _text(node, "errorMessage"),
                    "problem_type": _text(node, "errorStatusCode", "Error"),
                }
            )

        def _int(tag: str) -> int:
            try:
                return int(_text(node, tag, "0") or 0)
            except ValueError:
                return 0

        status = _text(node, "status", "Unknown")
        return DeployResult(
            id=_text(node, "id", deploy_id),
            status=status,
            done=_text(node, "done", "false").lower() == "true",
            success=_text(node, "success", "false").lower() == "true",
            components_total=_int("numberComponentsTotal"),
            components_deployed=_int("numberComponentsDeployed"),
            components_failed=_int("numberComponentErrors"),
            tests_total=_int("numberTestsTotal"),
            tests_completed=_int("numberTestsCompleted"),
            tests_failed=_int("numberTestErrors"),
            errors=errors,
            test_failures=test_failures,
            raw_status=status,
        )

    async def deploy_and_wait(
        self,
        zip_bytes: bytes,
        *,
        check_only: bool = False,
        test_level: str = "NoTestRun",
        run_tests: list[str] | None = None,
        poll_interval: float = 2.0,
        timeout: float = 600.0,
    ) -> DeployResult:
        deploy_id = await self.deploy(
            zip_bytes, check_only=check_only, test_level=test_level, run_tests=run_tests
        )
        log.info("metadata.deploy_started", deploy_id=deploy_id, check_only=check_only)
        deadline = time.monotonic() + timeout
        delay = poll_interval
        while True:
            await asyncio.sleep(delay)
            result = await self.check_deploy_status(deploy_id)
            if result.done:
                log.info(
                    "metadata.deploy_finished",
                    deploy_id=deploy_id,
                    status=result.status,
                    success=result.success,
                    failed=result.components_failed,
                )
                return result
            if time.monotonic() > deadline:
                raise SalesforceError(
                    error_type="METADATA_DEPLOY_TIMEOUT",
                    message=(
                        f"Deployment {deploy_id} was still {result.status} after "
                        f"{timeout:.0f}s."
                    ),
                    likely_cause="Long-running deployment or test execution.",
                    suggested_action=(
                        f"The deployment is still running in Salesforce (id {deploy_id}); "
                        "check its status in Setup > Deployment Status."
                    ),
                    retryable=False,
                    details={"deploy_id": deploy_id},
                )
            delay = min(delay * 1.5, 10.0)


def _extract_fault(xml_text: str) -> dict[str, str]:
    try:
        root = SafeET.fromstring(xml_text.encode("utf-8"))
        fault = root.find(f".//{{{SOAP_NS}}}Fault")
        if fault is None:
            return {"message": xml_text[:500]}
        code = fault.findtext("faultcode") or ""
        msg = fault.findtext("faultstring") or ""
        return {"code": code.split(":")[-1] or "SOAP_FAULT", "message": msg or xml_text[:500]}
    except Exception:
        return {"message": xml_text[:500]}


# ------------------------------------------------------------------ retrieve
@dataclass
class RetrieveResult:
    """A completed Metadata API retrieve(), with the package unpacked."""

    id: str
    status: str
    done: bool
    success: bool
    files: dict[str, str] = field(default_factory=dict)
    messages: list[dict[str, Any]] = field(default_factory=list)
    error_message: str = ""

    def source_of(self, suffix: str) -> dict[str, str]:
        return {p: c for p, c in self.files.items() if p.endswith(suffix)}


def _members_xml(types: dict[str, list[str]]) -> str:
    blocks = []
    for name, members in types.items():
        member_xml = "".join(_el("met:members", m) for m in members)
        blocks.append(f"<met:types>{member_xml}{_el('met:name', name)}</met:types>")
    return "".join(blocks)


class MetadataReader:
    """Read-side of the Metadata API: listMetadata() and retrieve().

    Deployment lives in `MetadataClient`; this is the half that lets the agent
    *look* at an org's automation before proposing to change it. Reading Flow,
    Apex and validation-rule source is what makes dependency analysis and
    debugging evidence-based rather than guesswork.
    """

    def __init__(self, client: MetadataClient):
        self.client = client

    @property
    def api_version(self) -> str:
        return self.client.api_version

    async def list_metadata(
        self, metadata_type: str, folder: str | None = None
    ) -> list[dict[str, Any]]:
        """listMetadata() for one type. Cheap; use it before retrieving."""
        query = f"<met:type>{escape(metadata_type)}</met:type>"
        if folder:
            query = f"<met:folder>{escape(folder)}</met:folder>" + query
        body = (
            "<met:listMetadata>"
            f"<met:queries>{query}</met:queries>"
            f"{_el('met:asOfVersion', self.api_version)}"
            "</met:listMetadata>"
        )
        root = await self.client._call("listMetadata", body)
        out: list[dict[str, Any]] = []
        for node in root.findall(f".//{{{MD_NS}}}result"):
            out.append(
                {
                    "full_name": _text(node, "fullName"),
                    "type": _text(node, "type", metadata_type),
                    "id": _text(node, "id"),
                    "created_by": _text(node, "createdByName"),
                    "last_modified_by": _text(node, "lastModifiedByName"),
                    "last_modified_date": _text(node, "lastModifiedDate"),
                    "manageable_state": _text(node, "manageableState"),
                    "namespace_prefix": _text(node, "namespacePrefix"),
                }
            )
        return out

    async def start_retrieve(self, types: dict[str, list[str]]) -> str:
        body = (
            "<met:retrieve><met:retrieveRequest>"
            f"{_el('met:apiVersion', self.api_version)}"
            f"<met:unpackaged>{_members_xml(types)}"
            f"{_el('met:version', self.api_version)}</met:unpackaged>"
            f"{_el('met:singlePackage', 'true')}"
            "</met:retrieveRequest></met:retrieve>"
        )
        root = await self.client._call("retrieve", body)
        node = root.find(f".//{{{MD_NS}}}result")
        if node is None:
            raise SalesforceError(
                error_type="METADATA_RETRIEVE_NO_RESULT",
                message="Metadata API retrieve() returned no result element.",
                retryable=True,
            )
        return _text(node, "id")

    async def check_retrieve_status(self, retrieve_id: str) -> RetrieveResult:
        body = (
            "<met:checkRetrieveStatus>"
            f"{_el('met:asyncProcessId', retrieve_id)}"
            f"{_el('met:includeZip', 'true')}"
            "</met:checkRetrieveStatus>"
        )
        root = await self.client._call("checkRetrieveStatus", body)
        node = root.find(f".//{{{MD_NS}}}result")
        if node is None:
            raise SalesforceError(
                error_type="METADATA_RETRIEVE_NO_STATUS",
                message=f"checkRetrieveStatus returned no result for {retrieve_id}.",
                retryable=True,
            )
        status = _text(node, "status", "Unknown")
        done = _text(node, "done", "false").lower() == "true"
        success = status == "Succeeded"
        messages = [
            {"file": _text(m, "fileName"), "problem": _text(m, "problem")}
            for m in node.findall(f".//{{{MD_NS}}}messages")
        ]
        files: dict[str, str] = {}
        zip_b64 = _text(node, "zipFile")
        if zip_b64:
            files = unpack_zip(base64.b64decode(zip_b64))
        return RetrieveResult(
            id=_text(node, "id", retrieve_id),
            status=status,
            done=done,
            success=success,
            files=files,
            messages=messages,
            error_message=_text(node, "errorMessage"),
        )

    async def retrieve(
        self,
        types: dict[str, list[str]],
        *,
        poll_interval: float = 2.0,
        timeout: float = 300.0,
    ) -> RetrieveResult:
        retrieve_id = await self.start_retrieve(types)
        log.info("metadata.retrieve_started", retrieve_id=retrieve_id, types=list(types))
        deadline = time.monotonic() + timeout
        delay = poll_interval
        while True:
            await asyncio.sleep(delay)
            result = await self.check_retrieve_status(retrieve_id)
            if result.done:
                if not result.success:
                    raise SalesforceError(
                        error_type="METADATA_RETRIEVE_FAILED",
                        message=(
                            result.error_message
                            or f"Retrieve {retrieve_id} finished with status {result.status}."
                        ),
                        likely_cause="The requested components may not exist in this org.",
                        suggested_action="List the metadata type first to confirm the names.",
                        details={"messages": result.messages[:10]},
                    )
                return result
            if time.monotonic() > deadline:
                raise SalesforceError(
                    error_type="METADATA_RETRIEVE_TIMEOUT",
                    message=f"Retrieve {retrieve_id} did not finish within {timeout:.0f}s.",
                    suggested_action="Retrieve fewer components at once.",
                    retryable=True,
                )
            delay = min(delay * 1.5, 10.0)


def unpack_zip(zip_bytes: bytes) -> dict[str, str]:
    """Unpack a retrieve zip into {path: text}. Binary members are skipped."""
    out: dict[str, str] = {}
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        for info in zf.infolist():
            if info.is_dir() or info.file_size > 2_000_000:
                continue
            raw = zf.read(info)
            try:
                out[info.filename] = raw.decode("utf-8")
            except UnicodeDecodeError:
                continue
    return out
