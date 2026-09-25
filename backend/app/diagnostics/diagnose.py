"""Deterministic diagnosis from collected evidence.

This is the half of debugging that must not be improvised. Given the
observations the collector gathered, these rules produce candidate causes, each
one citing the observation that supports it and carrying an explicit confidence
level.

Why rules rather than letting the model infer freely: a model asked "why didn't
this Opportunity get assigned" will produce a fluent, plausible answer whether
or not the evidence supports one. These rules can only fire when the org
actually shows the condition, so a finding is never invented. The model still
does the reasoning the rules cannot — reading a flow's logic against the
record's values — but it does it on top of findings that are checkable.

Confidence means something specific here:
  high    — the evidence directly demonstrates the cause.
  medium  — the evidence shows a plausible cause that fits the symptom.
  low     — the evidence shows something worth checking, nothing more.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.diagnostics.collector import Evidence

HIGH = "high"
MEDIUM = "medium"
LOW = "low"


@dataclass
class Finding:
    cause: str
    confidence: str
    evidence: list[str] = field(default_factory=list)
    recommendation: str = ""
    area: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "likely_cause": self.cause,
            "confidence": self.confidence,
            "evidence": self.evidence,
            "recommended_fix": self.recommendation,
            "area": self.area,
        }


_RANK = {HIGH: 3, MEDIUM: 2, LOW: 1}


def diagnose(evidence: Evidence, question: str = "") -> dict[str, Any]:
    """Turn observations into ranked, evidence-cited candidate causes."""
    findings: list[Finding] = []
    grouped = evidence.by_area()
    intent = _intent(question)

    findings += _permission_findings(grouped, intent)
    findings += _schema_findings(grouped, intent)
    findings += _automation_findings(grouped, intent)
    findings += _validation_findings(grouped, intent)
    findings += _assignment_findings(grouped, intent)
    findings += _ownership_findings(grouped)
    findings += _history_findings(grouped, intent)

    findings.sort(key=lambda f: -_RANK.get(f.confidence, 0))

    return {
        "question": question,
        "interpreted_as": intent,
        "observed_facts": evidence.to_dict(),
        "findings": [f.to_dict() for f in findings],
        "overall_confidence": findings[0].confidence if findings else "none",
        "verdict": _verdict(findings, evidence),
        "not_inspected": evidence.unavailable,
    }


def _verdict(findings: list[Finding], evidence: Evidence) -> str:
    if not findings:
        base = (
            "No configuration in the org explains this on the evidence collected. "
        )
        if evidence.unavailable:
            return (
                base
                + "Note that "
                + str(len(evidence.unavailable))
                + " area(s) could not be inspected, listed under not_inspected — the "
                "answer may be in one of them."
            )
        return (
            base
            + "The next thing to check is a debug log for an actual save of this "
            "record, which shows the execution order directly."
        )
    top = findings[0]
    if top.confidence == HIGH:
        return f"Most likely cause ({top.confidence} confidence): {top.cause}"
    return (
        f"No single cause is proven. The strongest candidate ({top.confidence} "
        f"confidence) is: {top.cause}"
    )


#: Markers that suggest what is being asked. Scored rather than matched in
#: sequence, because real questions mix vocabularies — "why can't this user
#: edit the field" is about permissions even though it contains "edit", and a
#: first-match-wins chain gets that wrong.
_INTENT_MARKERS: dict[str, tuple[str, ...]] = {
    "assignment": (
        "assign", "assigned", "assignment", "owner", "ownership", "routed",
        "routing", "route", "queue",
    ),
    "permission": (
        "permission", "permissions", "access", "visible", "visibility", "see",
        "view", "edit", "editable", "read-only", "readonly", "profile",
        "user", "can't", "cannot", "denied", "restricted",
    ),
    "automation": (
        "flow", "flows", "automation", "trigger", "triggered", "firing", "fire",
        "process", "run", "ran", "running",
    ),
    "validation": (
        "error", "errors", "validation", "blocked", "reject", "rejected",
        "save", "saving", "fails", "failed", "failing",
    ),
    "field_value": (
        "value", "values", "change", "changed", "changing", "overwritten",
        "overwrite", "reset", "blank", "empty", "cleared", "wrong",
    ),
}

#: Tie-break order when two intents score equally. Earlier is preferred.
_INTENT_PRIORITY = ("assignment", "permission", "validation", "automation", "field_value")

_WORD_SPLIT = re.compile(r"[^a-z0-9'-]+")


def _intent(question: str) -> str:
    """Classify what is being asked, so irrelevant rules stay quiet.

    Scores each intent by how many of its markers appear as whole words. A
    question that matches nothing is 'general', and the intent-independent
    rules still run — a formula field explains itself whatever was asked.
    """
    words = {w for w in _WORD_SPLIT.split((question or "").lower()) if w}
    if not words:
        return "general"
    scores = {
        intent: len(words & set(markers)) for intent, markers in _INTENT_MARKERS.items()
    }
    best = max(scores.values())
    if best == 0:
        return "general"
    return min(
        (i for i, s in scores.items() if s == best),
        key=lambda i: _INTENT_PRIORITY.index(i) if i in _INTENT_PRIORITY else 99,
    )


def _texts(rows: list[dict[str, Any]]) -> list[str]:
    return [str(r.get("observed", "")) for r in rows]


def _permission_findings(
    grouped: dict[str, list[dict[str, Any]]], intent: str
) -> list[Finding]:
    findings: list[Finding] = []
    for row in grouped.get("permissions", []):
        observed = str(row.get("observed", ""))
        detail = row.get("detail")

        if "No profile or permission set grants any access" in observed:
            findings.append(
                Finding(
                    cause=(
                        "The field is not visible to any profile or permission set, so "
                        "only administrators can see or edit it."
                    ),
                    confidence=HIGH if intent == "permission" else MEDIUM,
                    evidence=[observed],
                    recommendation=(
                        "Grant field-level security on the relevant profiles or, "
                        "preferably, a permission set."
                    ),
                    area="permissions",
                )
            )
        elif "can edit it" in observed and isinstance(detail, list):
            editable = [d for d in detail if d.get("edit")]
            if not editable:
                findings.append(
                    Finding(
                        cause=(
                            "Every profile with access to this field has it read-only, "
                            "so no ordinary user can change it."
                        ),
                        confidence=HIGH if intent == "permission" else MEDIUM,
                        evidence=[observed],
                        recommendation=(
                            "Grant edit access on the profile or permission set the "
                            "user actually holds."
                        ),
                        area="permissions",
                    )
                )
        if "has no object permissions" in observed:
            findings.append(
                Finding(
                    cause="The user has no object-level access at all to this object.",
                    confidence=HIGH,
                    evidence=[observed],
                    recommendation=(
                        "Assign a permission set granting the needed object permissions."
                    ),
                    area="permissions",
                )
            )
        if isinstance(detail, dict) and "effective" in detail:
            effective = detail["effective"]
            if intent == "permission" and not effective.get("edit"):
                findings.append(
                    Finding(
                        cause=(
                            "The user has read access to this object but not edit "
                            "access."
                        ),
                        confidence=HIGH,
                        evidence=[observed],
                        recommendation=(
                            "Grant Edit on the object through a permission set."
                        ),
                        area="permissions",
                    )
                )
    return findings


def _schema_findings(
    grouped: dict[str, list[dict[str, Any]]], intent: str
) -> list[Finding]:
    findings: list[Finding] = []
    for row in grouped.get("schema", []):
        observed = str(row.get("observed", ""))
        detail = row.get("detail") or {}
        if not isinstance(detail, dict):
            continue
        if detail.get("calculated"):
            findings.append(
                Finding(
                    cause=(
                        "The field is a formula field. Its value is computed on read "
                        "and cannot be set by anything — not a user, not a flow, not "
                        "Apex."
                    ),
                    confidence=HIGH,
                    evidence=[observed, f"Formula: {detail.get('calculatedFormula')}"],
                    recommendation=(
                        "If the value needs to be editable it has to stop being a "
                        "formula field. Otherwise change the formula."
                    ),
                    area="schema",
                )
            )
        elif detail.get("updateable") is False and "type" in detail:
            findings.append(
                Finding(
                    cause=(
                        "The field is not updateable through the API at all "
                        f"(type {detail.get('type')})."
                    ),
                    confidence=HIGH if intent in {"permission", "field_value"} else MEDIUM,
                    evidence=[observed],
                    recommendation=(
                        "Use a writable field, or a supported mechanism for this "
                        "field type."
                    ),
                    area="schema",
                )
            )
        if detail.get("restrictedPicklist") and detail.get("picklistValues"):
            findings.append(
                Finding(
                    cause=(
                        "The field is a restricted picklist, so any value outside its "
                        "defined set is rejected on save."
                    ),
                    confidence=LOW if intent != "validation" else MEDIUM,
                    evidence=[
                        observed,
                        "Allowed values: " + ", ".join(detail["picklistValues"][:12]),
                    ],
                    recommendation=(
                        "Use one of the allowed values, or add the value to the "
                        "picklist."
                    ),
                    area="schema",
                )
            )
        if "has no field named" in observed:
            findings.append(
                Finding(
                    cause="The field named in the question does not exist on this object.",
                    confidence=HIGH,
                    evidence=[observed],
                    recommendation="Check the API name with describe_object.",
                    area="schema",
                )
            )
    return findings


def _automation_findings(
    grouped: dict[str, list[dict[str, Any]]], intent: str
) -> list[Finding]:
    findings: list[Finding] = []
    rows = grouped.get("automation", [])
    texts = _texts(rows)

    no_flows = any("No active flows" in t for t in texts)
    no_triggers = any("No Apex triggers exist" in t for t in texts)

    if intent in {"assignment", "field_value", "automation"} and no_flows and no_triggers:
        findings.append(
            Finding(
                cause=(
                    "There is no active automation on this object — no flows and no "
                    "Apex triggers — so nothing in the org is setting this "
                    "automatically."
                ),
                confidence=HIGH,
                evidence=[t for t in texts if t.startswith("No ")],
                recommendation=(
                    "If the value is expected to be set automatically, the automation "
                    "that would do it does not exist and needs to be built."
                ),
                area="automation",
            )
        )
        return findings

    for row in rows:
        observed = str(row.get("observed", ""))
        detail = row.get("detail")
        if "active flow(s) run on" in observed and isinstance(detail, list):
            for flow in detail:
                logic = flow.get("logic") or {}
                trigger = logic.get("trigger") or {}
                if trigger.get("filters") or trigger.get("filter_logic"):
                    findings.append(
                        Finding(
                            cause=(
                                f"Flow '{flow.get('api_name')}' only runs on records "
                                "that match its entry conditions. A record that does "
                                "not match is skipped silently."
                            ),
                            confidence=MEDIUM,
                            evidence=[
                                observed,
                                f"Entry conditions: {trigger.get('filters')}",
                            ],
                            recommendation=(
                                "Compare the record's field values against the entry "
                                "conditions above; that comparison usually settles it."
                            ),
                            area="automation",
                        )
                    )
            if len(detail) > 1:
                findings.append(
                    Finding(
                        cause=(
                            f"{len(detail)} active flows run on this object. Flows on "
                            "the same object execute in an unspecified order, so one "
                            "can overwrite another's result."
                        ),
                        confidence=MEDIUM if intent == "field_value" else LOW,
                        evidence=[observed],
                        recommendation=(
                            "Set explicit trigger order on the flows, or consolidate "
                            "them into one."
                        ),
                        area="automation",
                    )
                )
        if "Apex trigger(s) on" in observed and isinstance(detail, list):
            findings.append(
                Finding(
                    cause=(
                        "Apex trigger code runs on this object and can change or block "
                        "the save regardless of what any flow does."
                    ),
                    confidence=MEDIUM,
                    evidence=[observed],
                    recommendation=(
                        "Read the trigger source in the observations and check whether "
                        "it touches the field in question."
                    ),
                    area="automation",
                )
            )
    return findings


def _validation_findings(
    grouped: dict[str, list[dict[str, Any]]], intent: str
) -> list[Finding]:
    findings: list[Finding] = []
    for row in grouped.get("validation", []):
        observed = str(row.get("observed", ""))
        detail = row.get("detail")
        if "no active validation rules" in observed:
            if intent == "validation":
                findings.append(
                    Finding(
                        cause=(
                            "No validation rule on this object can be causing the "
                            "error — there are none active."
                        ),
                        confidence=HIGH,
                        evidence=[observed],
                        recommendation=(
                            "Look at Apex triggers, required fields, or a validation "
                            "rule on a related object instead."
                        ),
                        area="validation",
                    )
                )
            continue
        if isinstance(detail, list) and detail:
            findings.append(
                Finding(
                    cause=(
                        f"{len(detail)} active validation rule(s) can block saves on "
                        "this object."
                    ),
                    confidence=HIGH if intent == "validation" else LOW,
                    evidence=[observed]
                    + [f"{r['name']}: {r['formula']}" for r in detail[:5]],
                    recommendation=(
                        "Evaluate each formula against the record's values; the one "
                        "that evaluates true is the rule producing the error."
                    ),
                    area="validation",
                )
            )
    return findings


def _assignment_findings(
    grouped: dict[str, list[dict[str, Any]]], intent: str
) -> list[Finding]:
    findings: list[Finding] = []
    for row in grouped.get("assignment", []):
        observed = str(row.get("observed", ""))
        detail = row.get("detail")
        if "No assignment rules were found" in observed:
            findings.append(
                Finding(
                    cause=(
                        "The object has no assignment rules, so records keep whichever "
                        "owner they were created with. Nothing is routing them."
                    ),
                    confidence=HIGH if intent == "assignment" else MEDIUM,
                    evidence=[observed],
                    recommendation=(
                        "Create an assignment rule, or handle ownership in a flow."
                    ),
                    area="assignment",
                )
            )
        elif isinstance(detail, list) and detail:
            inactive = [r for r in detail if not r.get("active")]
            if inactive and len(inactive) == len(detail):
                findings.append(
                    Finding(
                        cause=(
                            "Assignment rules exist but none of them is active, so no "
                            "routing happens."
                        ),
                        confidence=HIGH if intent == "assignment" else MEDIUM,
                        evidence=[observed],
                        recommendation="Activate the correct assignment rule set.",
                        area="assignment",
                    )
                )
            else:
                findings.append(
                    Finding(
                        cause=(
                            "An assignment rule set is active. Assignment rules only "
                            "run when the caller explicitly requests them — the API "
                            "and most integrations do not, so records created that way "
                            "are never routed."
                        ),
                        confidence=MEDIUM if intent == "assignment" else LOW,
                        evidence=[observed],
                        recommendation=(
                            "Check how the record was created. API and Bulk API inserts "
                            "must set the assignment-rule header explicitly; otherwise "
                            "route ownership in a flow, which always runs."
                        ),
                        area="assignment",
                    )
                )
    return findings


def _ownership_findings(grouped: dict[str, list[dict[str, Any]]]) -> list[Finding]:
    findings: list[Finding] = []
    for row in grouped.get("ownership", []):
        observed = str(row.get("observed", ""))
        detail = row.get("detail") or {}
        if isinstance(detail, dict) and detail.get("active") is False:
            findings.append(
                Finding(
                    cause=(
                        "The record is owned by an inactive user. Inactive owners "
                        "break sharing, reporting and any automation that emails or "
                        "assigns work to the owner."
                    ),
                    confidence=HIGH,
                    evidence=[observed],
                    recommendation="Reassign the record to an active owner or a queue.",
                    area="ownership",
                )
            )
    return findings


def _history_findings(
    grouped: dict[str, list[dict[str, Any]]], intent: str
) -> list[Finding]:
    findings: list[Finding] = []
    for row in grouped.get("history", []):
        observed = str(row.get("observed", ""))
        detail = row.get("detail")
        if "no tracked field has ever changed" in observed and intent == "field_value":
            findings.append(
                Finding(
                    cause=(
                        "Field history shows the value has never been changed since "
                        "the record was created, so it holds whatever it was created "
                        "with."
                    ),
                    confidence=HIGH,
                    evidence=[observed],
                    recommendation=(
                        "Look at how the record was created rather than at what "
                        "changed it."
                    ),
                    area="history",
                )
            )
        elif isinstance(detail, list) and detail and intent == "field_value":
            findings.append(
                Finding(
                    cause=(
                        f"The field was last changed by "
                        f"{detail[0].get('by')} at {detail[0].get('at')} "
                        f"({detail[0].get('from')} → {detail[0].get('to')})."
                    ),
                    confidence=HIGH,
                    evidence=[observed],
                    recommendation=(
                        "If that user is an integration or automation user, the change "
                        "came from that integration rather than from a person."
                    ),
                    area="history",
                )
            )
    return findings
