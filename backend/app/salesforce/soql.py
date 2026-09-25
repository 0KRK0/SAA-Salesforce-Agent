"""Deterministic SOQL safety validation.

Model-generated SOQL is never executed blind. This module enforces:
  * SELECT-only (no DML / no Apex-ish statements / no multiple statements)
  * a mandatory, bounded LIMIT
  * bounded result size
  * comment stripping (a classic injection/obfuscation vector)
It is a conservative validator, not a full SOQL grammar: anything it cannot
prove safe is rejected.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.config import settings

_FORBIDDEN = (
    "insert",
    "update",
    "delete",
    "undelete",
    "upsert",
    "merge",
    "create",
    "drop",
    "alter",
    "truncate",
    "grant",
    "revoke",
    "execute",
    "call",
    "system.",
    "database.",
)

_COMMENT_BLOCK = re.compile(r"/\*.*?\*/", re.DOTALL)
_COMMENT_LINE = re.compile(r"//[^\n]*")
_LIMIT_RE = re.compile(r"\blimit\s+(\d+)\b", re.IGNORECASE)
_OFFSET_RE = re.compile(r"\boffset\s+(\d+)\b", re.IGNORECASE)
_FROM_RE = re.compile(r"\bfrom\s+([A-Za-z_][A-Za-z0-9_]*(?:__[cC])?)", re.IGNORECASE)
_SELECT_RE = re.compile(r"^\s*select\b", re.IGNORECASE)
_STRING_LITERAL = re.compile(r"'(?:\\.|[^'\\])*'")


class SoqlValidationError(ValueError):
    def __init__(self, message: str, suggested_action: str = ""):
        super().__init__(message)
        self.message = message
        self.suggested_action = suggested_action or (
            "Rewrite the query as a single read-only SELECT with an explicit LIMIT."
        )


@dataclass
class ValidatedQuery:
    query: str
    object_name: str
    limit: int
    offset: int
    had_explicit_limit: bool


def strip_comments(query: str) -> str:
    return _COMMENT_LINE.sub(" ", _COMMENT_BLOCK.sub(" ", query))


def validate_soql(
    query: str,
    max_limit: int | None = None,
    default_limit: int | None = None,
) -> ValidatedQuery:
    max_limit = max_limit or settings.max_query_rows
    default_limit = default_limit or settings.default_query_limit

    if not query or not query.strip():
        raise SoqlValidationError("Empty SOQL query.")

    cleaned = strip_comments(query).strip().rstrip(";").strip()

    scan = _STRING_LITERAL.sub("''", cleaned)
    if ";" in scan:
        raise SoqlValidationError(
            "Multiple statements are not allowed.",
            "Send exactly one SELECT statement.",
        )
    if not _SELECT_RE.match(cleaned):
        raise SoqlValidationError(
            "query_salesforce only accepts SELECT statements.",
            "Use create_record / update_record for writes.",
        )

    # String literals are data, not syntax: blank them before scanning for DML
    # verbs so that WHERE Name LIKE '%update%' is not falsely rejected.
    lowered = scan.lower()
    # Word boundaries keep legitimate identifiers such as CreatedDate or
    # Updated__c from matching the forbidden verbs.
    for word in _FORBIDDEN:
        if re.search(rf"(?<![A-Za-z0-9_]){re.escape(word)}(?![A-Za-z0-9_])", lowered):
            # 'update' inside 'LastModifiedDate' etc. cannot match due to boundaries.
            raise SoqlValidationError(
                f"Statement contains a forbidden keyword: '{word}'.",
                "query_salesforce is strictly read-only.",
            )

    m = _FROM_RE.search(cleaned)
    if not m:
        raise SoqlValidationError(
            "Could not determine the FROM object.",
            "Include an explicit FROM <SObject> clause.",
        )
    object_name = m.group(1)

    offset_m = _OFFSET_RE.search(cleaned)
    offset = int(offset_m.group(1)) if offset_m else 0
    if offset > 2000:
        raise SoqlValidationError(
            "OFFSET greater than 2000 is not supported by Salesforce.",
            "Use keyset pagination on Id or a Bulk API export instead.",
        )

    limit_m = _LIMIT_RE.search(cleaned)
    had_explicit = bool(limit_m)
    if limit_m:
        limit = int(limit_m.group(1))
        if limit > max_limit:
            limit = max_limit
            cleaned = _LIMIT_RE.sub(f"LIMIT {max_limit}", cleaned, count=1)
        if limit <= 0:
            raise SoqlValidationError("LIMIT must be a positive integer.")
    else:
        limit = min(default_limit, max_limit)
        cleaned = f"{cleaned} LIMIT {limit}"

    return ValidatedQuery(
        query=cleaned,
        object_name=object_name,
        limit=limit,
        offset=offset,
        had_explicit_limit=had_explicit,
    )
