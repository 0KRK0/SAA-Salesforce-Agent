"""Record-triggered Flow metadata generation.

Scope is deliberate. This builds the flows an admin actually asks an agent
for — "when X happens on this object, check these conditions and set these
fields" — and it builds them correctly, including the parts that are easy to
get subtly wrong (value typing, before- vs after-save semantics, entry
conditions vs decisions, connector wiring).

It does NOT try to be a Flow Builder. Screen flows, loops, scheduled paths,
subflow orchestration and invocable actions are out of scope; `validate_shape`
says so explicitly rather than emitting a flow that silently drops them.

Two rules hold throughout:
  * Every field named here is checked against the org's real describe before
    any XML is produced (see app/tools/flow_tools.py).
  * Values are typed from the field's actual Salesforce type, not guessed
    from what the string looks like.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import Any
from xml.sax.saxutils import escape

MD_NS = "http://soap.sforce.com/2006/04/metadata"

API_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")

BEFORE_SAVE = "before_save"
AFTER_SAVE = "after_save"

TRIGGER_TYPES = {
    BEFORE_SAVE: "RecordBeforeSave",
    AFTER_SAVE: "RecordAfterSave",
}

RECORD_TRIGGER_TYPES = {"Create", "Update", "CreateAndUpdate", "Delete"}

#: Flow condition operators, mapped from the plain words a model will produce.
OPERATORS = {
    "equals": "EqualTo",
    "eq": "EqualTo",
    "=": "EqualTo",
    "not_equals": "NotEqualTo",
    "!=": "NotEqualTo",
    "greater_than": "GreaterThan",
    ">": "GreaterThan",
    "greater_or_equal": "GreaterThanOrEqualTo",
    ">=": "GreaterThanOrEqualTo",
    "less_than": "LessThan",
    "<": "LessThan",
    "less_or_equal": "LessThanOrEqualTo",
    "<=": "LessThanOrEqualTo",
    "starts_with": "StartsWith",
    "ends_with": "EndsWith",
    "contains": "Contains",
    "is_null": "IsNull",
    "is_changed": "IsChanged",
    "was_set": "WasSet",
    "was_selected": "WasSelected",
}

#: Salesforce field type -> the Flow value element that carries it.
VALUE_ELEMENT = {
    "boolean": "booleanValue",
    "currency": "numberValue",
    "double": "numberValue",
    "int": "numberValue",
    "integer": "numberValue",
    "long": "numberValue",
    "percent": "numberValue",
    "date": "dateValue",
    "datetime": "dateTimeValue",
}


class FlowBuildError(ValueError):
    """The requested flow cannot be expressed by this builder."""

    def __init__(self, message: str, suggested_action: str = "", missing: list[str] | None = None):
        super().__init__(message)
        self.message = message
        self.suggested_action = suggested_action
        self.missing = missing or []


def normalize_flow_api_name(name: str) -> str:
    base = re.sub(r"[^A-Za-z0-9_]+", "_", (name or "").strip()).strip("_")
    base = re.sub(r"_{2,}", "_", base)
    if not base:
        raise FlowBuildError("Flow API name is empty after normalization.")
    if not base[0].isalpha():
        base = f"X{base}"
    return base[:80]


def _el(tag: str, value: Any) -> str:
    return f"<{tag}>{escape(str(value))}</{tag}>"


def _value_xml(raw: Any, field_type: str) -> str:
    """Render a literal as the Flow value element its field type requires.

    Getting this wrong is the classic cause of a flow that deploys and then
    misbehaves — a checkbox compared against the string "true" is always
    unequal. Typing comes from describe, never from the shape of the literal.
    """
    if isinstance(raw, dict):
        # An explicit reference to another element or a global variable.
        if "reference" in raw:
            return f"<value>{_el('elementReference', raw['reference'])}</value>"
        if "formula" in raw:
            return f"<value>{_el('elementReference', raw['formula'])}</value>"
    element = VALUE_ELEMENT.get((field_type or "").lower(), "stringValue")
    if element == "booleanValue":
        truthy = str(raw).strip().lower() in {"true", "1", "yes", "y", "checked"}
        return f"<value>{_el('booleanValue', 'true' if truthy else 'false')}</value>"
    if element == "numberValue":
        try:
            number = float(raw)
        except (TypeError, ValueError) as exc:
            raise FlowBuildError(
                f"Value {raw!r} is not numeric, but the field is a {field_type}.",
                "Ask the user for a number, or check the field type.",
            ) from exc
        rendered = int(number) if number.is_integer() else number
        return f"<value>{_el('numberValue', rendered)}</value>"
    return f"<value>{_el(element, raw)}</value>"


@dataclass
class Condition:
    field: str
    operator: str
    value: Any = None
    field_type: str = "string"

    def to_xml(self, tag: str, *, reference_prefix: str = "") -> str:
        operator = OPERATORS.get(str(self.operator).lower(), self.operator)
        parts = [_el("field" if tag == "filters" else "leftValueReference",
                     f"{reference_prefix}{self.field}" if reference_prefix else self.field)]
        parts.append(_el("operator", operator))
        if operator not in {"IsChanged", "WasSet", "WasSelected"} or self.value is not None:
            if operator == "IsNull":
                parts.append(f"<value>{_el('booleanValue', 'true')}</value>")
            elif self.value is not None:
                value_xml = _value_xml(self.value, self.field_type)
                if tag == "filters":
                    parts.append(value_xml)
                else:
                    parts.append(value_xml.replace("<value>", "<rightValue>").replace(
                        "</value>", "</rightValue>"
                    ))
        return f"<{tag}>{''.join(parts)}</{tag}>"


@dataclass
class FieldAssignment:
    field: str
    value: Any
    field_type: str = "string"


@dataclass
class FlowSpec:
    """A record-triggered flow, described in terms an admin would use."""

    api_name: str
    label: str
    object_name: str
    trigger: str = BEFORE_SAVE
    record_trigger_type: str = "CreateAndUpdate"
    description: str = ""
    entry_conditions: list[Condition] = dataclass_field(default_factory=list)
    entry_logic: str = "and"
    entry_formula: str = ""
    assignments: list[FieldAssignment] = dataclass_field(default_factory=list)
    active: bool = False
    api_version: str = "62.0"

    @property
    def status(self) -> str:
        return "Active" if self.active else "Draft"


def validate_shape(spec: FlowSpec) -> None:
    """Reject anything this builder cannot express, before generating XML."""
    if spec.trigger not in TRIGGER_TYPES:
        raise FlowBuildError(
            f"Unsupported trigger '{spec.trigger}'.",
            f"Use one of: {', '.join(sorted(TRIGGER_TYPES))}.",
        )
    if spec.record_trigger_type not in RECORD_TRIGGER_TYPES:
        raise FlowBuildError(
            f"Unsupported record trigger type '{spec.record_trigger_type}'.",
            f"Use one of: {', '.join(sorted(RECORD_TRIGGER_TYPES))}.",
        )
    if not API_NAME_RE.match(spec.api_name):
        raise FlowBuildError(f"'{spec.api_name}' is not a valid Flow API name.")
    if not spec.assignments:
        raise FlowBuildError(
            "The flow has no field updates, so it would do nothing.",
            "Say which field should be set, and to what value.",
            ["assignments"],
        )
    if spec.entry_conditions and spec.entry_formula:
        raise FlowBuildError(
            "A flow can use entry conditions or an entry formula, not both.",
            "Express the whole entry condition as one formula, or as a list of "
            "field comparisons.",
        )
    if spec.record_trigger_type == "Delete" and spec.trigger == BEFORE_SAVE:
        raise FlowBuildError(
            "Before-delete flows cannot assign fields on the deleted record.",
            "Use an after-save flow, or a different trigger.",
        )


def build_flow_xml(spec: FlowSpec) -> str:
    """Generate deployable Flow metadata.

    Before-save and after-save are genuinely different automations and are
    generated differently: before-save assigns onto `$Record` in memory (no
    extra DML), after-save has to issue an Update Records element against the
    triggering record's Id.
    """
    validate_shape(spec)
    if spec.trigger == BEFORE_SAVE:
        elements, start_target = _before_save_elements(spec)
    else:
        elements, start_target = _after_save_elements(spec)

    start = _start_element(spec, start_target)
    parts = [
        _el("apiVersion", spec.api_version),
    ]
    if spec.description:
        parts.append(_el("description", spec.description))
    parts.extend(
        [
            _el("environments", "Default"),
            _el("interviewLabel", f"{spec.label} {{!$Flow.CurrentDateTime}}"),
            _el("label", spec.label),
            _el("processType", "AutoLaunchedFlow"),
            start,
            _el("status", spec.status),
        ]
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<Flow xmlns="{MD_NS}">' + "".join(elements) + "".join(parts) + "</Flow>"
    )


def _start_element(spec: FlowSpec, target: str) -> str:
    parts = [
        _el("locationX", 50),
        _el("locationY", 0),
        f"<connector>{_el('targetReference', target)}</connector>",
    ]
    if spec.entry_formula:
        parts.append(_el("filterFormula", spec.entry_formula))
    elif spec.entry_conditions:
        parts.append(_el("filterLogic", spec.entry_logic))
        parts.extend(c.to_xml("filters") for c in spec.entry_conditions)
    parts.append(_el("object", spec.object_name))
    parts.append(_el("recordTriggerType", spec.record_trigger_type))
    parts.append(_el("triggerType", TRIGGER_TYPES[spec.trigger]))
    return f"<start>{''.join(parts)}</start>"


def _before_save_elements(spec: FlowSpec) -> tuple[list[str], str]:
    name = "Set_Fields"
    items = "".join(
        "<assignmentItems>"
        + _el("assignToReference", f"$Record.{a.field}")
        + _el("operator", "Assign")
        + _value_xml(a.value, a.field_type)
        + "</assignmentItems>"
        for a in spec.assignments
    )
    element = (
        "<assignments>"
        + _el("name", name)
        + _el("label", "Set fields")
        + _el("locationX", 176)
        + _el("locationY", 158)
        + items
        + "</assignments>"
    )
    return [element], name


def _after_save_elements(spec: FlowSpec) -> tuple[list[str], str]:
    """After-save must write the record back explicitly.

    Salesforce charges a second DML for this, which is exactly why before-save
    is the right choice for same-record updates; the tool says so to the user
    rather than quietly picking for them.
    """
    name = "Update_Record"
    input_assignments = "".join(
        "<inputAssignments>"
        + _el("field", a.field)
        + _value_xml(a.value, a.field_type)
        + "</inputAssignments>"
        for a in spec.assignments
    )
    filters = (
        "<filters>"
        + _el("field", "Id")
        + _el("operator", "EqualTo")
        + f"<value>{_el('elementReference', '$Record.Id')}</value>"
        + "</filters>"
    )
    element = (
        "<recordUpdates>"
        + _el("name", name)
        + _el("label", "Update record")
        + _el("locationX", 176)
        + _el("locationY", 158)
        + filters
        + _el("filterLogic", "and")
        + input_assignments
        + _el("object", spec.object_name)
        + "</recordUpdates>"
    )
    return [element], name


def build_flow_definition_xml(active_version: int | None) -> str:
    """FlowDefinition controls which version is active.

    `activeVersionNumber` of 0 deactivates every version — that is the
    documented mechanism, not a trick.
    """
    version = 0 if active_version is None else int(active_version)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<FlowDefinition xmlns="{MD_NS}">'
        + _el("activeVersionNumber", version)
        + "</FlowDefinition>"
    )


def describe_flow_plan(spec: FlowSpec) -> dict[str, Any]:
    """A human-readable rendering of the flow, for the approval card."""
    when = {
        "Create": "a record is created",
        "Update": "a record is updated",
        "CreateAndUpdate": "a record is created or updated",
        "Delete": "a record is deleted",
    }[spec.record_trigger_type]
    timing = "before it is saved" if spec.trigger == BEFORE_SAVE else "after it is saved"

    if spec.entry_formula:
        condition = f"the formula {spec.entry_formula} is true"
    elif spec.entry_conditions:
        joiner = " and " if spec.entry_logic == "and" else " or "
        condition = joiner.join(
            f"{c.field} {OPERATORS.get(str(c.operator).lower(), c.operator)} {c.value}"
            for c in spec.entry_conditions
        )
    else:
        condition = "every record qualifies (no entry condition)"

    return {
        "trigger": f"When {when} on {spec.object_name}, {timing}",
        "condition": condition,
        "actions": [f"Set {a.field} to {a.value}" for a in spec.assignments],
        "status": spec.status,
        "notes": (
            [
                "After-save flows issue an extra DML update on the same record. "
                "A before-save flow does the same job without it."
            ]
            if spec.trigger == AFTER_SAVE
            else []
        ),
    }
