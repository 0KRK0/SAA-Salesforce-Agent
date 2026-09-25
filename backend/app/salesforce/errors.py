"""Translate raw Salesforce failures into structured, recoverable agent context.

The agent must never receive a bare stack trace or an opaque code like
INVALID_FIELD_FOR_INSERT_UPDATE. Every error becomes:
    error_type / message / likely_cause / retryable / suggested_action
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class SalesforceError(Exception):
    error_type: str
    message: str
    status_code: int | None = None
    likely_cause: str = ""
    suggested_action: str = ""
    retryable: bool = False
    fields: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.error_type}: {self.message}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": False,
            "error_type": self.error_type,
            "message": self.message,
            "likely_cause": self.likely_cause,
            "suggested_action": self.suggested_action,
            "retryable": self.retryable,
            "fields": self.fields,
            "status_code": self.status_code,
        }


class SalesforceAuthError(SalesforceError):
    pass


# code -> (likely_cause, suggested_action, retryable)
_KNOWN: dict[str, tuple[str, str, bool]] = {
    "INVALID_SESSION_ID": (
        "The Salesforce access token is expired or revoked.",
        "The runtime will refresh the token automatically; if this repeats the org "
        "connection must be re-authorized by the user.",
        True,
    ),
    "INVALID_FIELD": (
        "A field in the request does not exist on the object, or is not visible to "
        "the running user.",
        "Call describe_object for the target object and use only field API names it "
        "returns.",
        False,
    ),
    "INVALID_FIELD_FOR_INSERT_UPDATE": (
        "The field is read-only, formula, auto-number, system-maintained, or not "
        "writable by this profile.",
        "Inspect the field with describe_object and check createable/updateable; "
        "choose a writable field instead.",
        False,
    ),
    "REQUIRED_FIELD_MISSING": (
        "One or more required fields were not supplied.",
        "Call describe_object, collect the required createable fields, and ask the "
        "user for any value you cannot derive.",
        False,
    ),
    "FIELD_CUSTOM_VALIDATION_EXCEPTION": (
        "An org validation rule rejected the values.",
        "Report the validation message to the user; adjusting values may require "
        "business input.",
        False,
    ),
    "DUPLICATES_DETECTED": (
        "A duplicate rule matched an existing record.",
        "Query for the existing record and confirm with the user whether to update "
        "it instead of creating a new one.",
        False,
    ),
    "INSUFFICIENT_ACCESS_OR_READONLY": (
        "The authenticated Salesforce user lacks object/field permission, or the "
        "record is read-only.",
        "Report the permission gap; do not retry. Ask the user to grant access or "
        "choose a different record.",
        False,
    ),
    "INSUFFICIENT_ACCESS_ON_CROSS_REFERENCE_ENTITY": (
        "The user lacks access to a referenced (lookup/master-detail) record.",
        "Verify the referenced record id and the user's access to it.",
        False,
    ),
    "MALFORMED_QUERY": (
        "The SOQL is syntactically invalid or references an unknown relationship.",
        "Re-inspect the object with describe_object and rebuild the query.",
        False,
    ),
    "INVALID_TYPE": (
        "The sObject name is wrong or the object is not accessible.",
        "Call describe_object (or list objects) to confirm the exact API name.",
        False,
    ),
    "ENTITY_IS_DELETED": (
        "The record has been deleted.",
        "Report to the user; do not retry.",
        False,
    ),
    "NOT_FOUND": (
        "The record id does not exist, or is outside the user's sharing scope.",
        "Verify the record id with a SOQL query before mutating it.",
        False,
    ),
    "REQUEST_LIMIT_EXCEEDED": (
        "The org's API request limit has been reached.",
        "Stop issuing calls and report to the user; retry later.",
        True,
    ),
    "UNABLE_TO_LOCK_ROW": (
        "Row-level lock contention in Salesforce.",
        "Retry the operation after a short delay.",
        True,
    ),
    "SERVER_UNAVAILABLE": (
        "Salesforce returned a transient server error.",
        "Retry with backoff.",
        True,
    ),
    "QUERY_TIMEOUT": (
        "The SOQL query was too expensive.",
        "Add selective filters and a LIMIT, or use the Bulk API path.",
        False,
    ),
    "STRING_TOO_LONG": (
        "A value exceeds the field length.",
        "Check the field length via describe_object and shorten the value.",
        False,
    ),
    "FIELD_INTEGRITY_EXCEPTION": (
        "A value does not match the field definition (bad picklist value, bad id, "
        "bad relationship).",
        "Inspect the field definition and use a permitted value.",
        False,
    ),
}


def classify(
    code: str | None,
    message: str,
    status_code: int | None = None,
    fields: list[str] | None = None,
    details: dict[str, Any] | None = None,
) -> SalesforceError:
    code = (code or "UNKNOWN_ERROR").upper()
    cause, action, retryable = _KNOWN.get(
        code,
        (
            "Unmapped Salesforce error.",
            "Inspect the message, re-inspect metadata if it concerns schema, and "
            "report to the user if it cannot be resolved automatically.",
            status_code is not None and status_code >= 500,
        ),
    )
    cls = SalesforceAuthError if code == "INVALID_SESSION_ID" else SalesforceError
    return cls(
        error_type=code,
        message=message,
        status_code=status_code,
        likely_cause=cause,
        suggested_action=action,
        retryable=retryable,
        fields=fields or [],
        details=details or {},
    )


def from_response(status_code: int, payload: Any) -> SalesforceError:
    """Build a structured error from a Salesforce REST error body."""
    if isinstance(payload, list) and payload:
        first = payload[0] or {}
        return classify(
            first.get("errorCode"),
            first.get("message", "Salesforce returned an error."),
            status_code,
            first.get("fields") or [],
            {"all_errors": payload},
        )
    if isinstance(payload, dict):
        code = payload.get("errorCode") or payload.get("error")
        msg = (
            payload.get("message")
            or payload.get("error_description")
            or "Salesforce returned an error."
        )
        return classify(code, msg, status_code, payload.get("fields") or [], payload)
    return classify(
        "HTTP_ERROR", f"Salesforce HTTP {status_code}: {payload!s:.500}", status_code
    )
