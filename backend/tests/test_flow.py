"""Flow metadata generation and the Flow agent's safety rules."""

from __future__ import annotations

import re

import pytest

from app.salesforce.flow import (
    AFTER_SAVE,
    BEFORE_SAVE,
    Condition,
    FieldAssignment,
    FlowBuildError,
    FlowSpec,
    build_flow_definition_xml,
    build_flow_xml,
    describe_flow_plan,
    normalize_flow_api_name,
)


def _spec(**overrides) -> FlowSpec:
    base = {
        "api_name": "Mark_Opportunity_At_Risk",
        "label": "Mark Opportunity At Risk",
        "object_name": "Opportunity",
        "assignments": [FieldAssignment("At_Risk__c", True, "boolean")],
    }
    return FlowSpec(**{**base, **overrides})


def test_api_name_is_normalized():
    assert normalize_flow_api_name("Mark Opportunity At Risk") == "Mark_Opportunity_At_Risk"
    assert normalize_flow_api_name("2024 cleanup") == "X2024_cleanup"
    with pytest.raises(FlowBuildError):
        normalize_flow_api_name("   ")


def test_before_save_assigns_on_record_without_extra_dml():
    xml = build_flow_xml(_spec())
    assert "<triggerType>RecordBeforeSave</triggerType>" in xml
    assert "<assignToReference>$Record.At_Risk__c</assignToReference>" in xml
    # A before-save flow must not issue a record update.
    assert "<recordUpdates>" not in xml


def test_after_save_writes_the_record_back_by_id():
    xml = build_flow_xml(_spec(trigger=AFTER_SAVE))
    assert "<triggerType>RecordAfterSave</triggerType>" in xml
    assert "<recordUpdates>" in xml
    assert "<elementReference>$Record.Id</elementReference>" in xml


def test_values_are_typed_from_the_field_not_the_literal():
    """A checkbox compared against the string 'true' is always unequal — the
    builder must emit booleanValue/numberValue based on the describe type."""
    xml = build_flow_xml(
        _spec(
            assignments=[
                FieldAssignment("At_Risk__c", "true", "boolean"),
                FieldAssignment("Score__c", "42", "double"),
                FieldAssignment("Notes__c", "42", "string"),
            ]
        )
    )
    assert "<booleanValue>true</booleanValue>" in xml
    assert "<numberValue>42</numberValue>" in xml
    assert "<stringValue>42</stringValue>" in xml


def test_entry_conditions_render_as_start_filters():
    xml = build_flow_xml(
        _spec(
            entry_conditions=[
                Condition("Probability", "less_than", 40, "percent"),
                Condition("StageName", "not_equals", "Closed Won", "picklist"),
            ]
        )
    )
    assert xml.count("<filters>") == 2
    assert "<operator>LessThan</operator>" in xml
    assert "<operator>NotEqualTo</operator>" in xml
    assert "<filterLogic>and</filterLogic>" in xml


def test_entry_formula_supports_relative_dates():
    formula = "AND({!$Record.CloseDate} <= TODAY() + 14, {!$Record.Probability} < 40)"
    xml = build_flow_xml(_spec(entry_formula=formula))
    assert "<filterFormula>" in xml
    # The formula must be XML-escaped, not injected raw.
    assert "&lt;=" in xml
    assert "<filters>" not in xml


def test_conditions_and_formula_together_are_rejected():
    with pytest.raises(FlowBuildError) as exc:
        build_flow_xml(
            _spec(
                entry_conditions=[Condition("Probability", "less_than", 40, "percent")],
                entry_formula="TRUE",
            )
        )
    assert "not both" in exc.value.message


def test_a_flow_that_does_nothing_is_rejected():
    with pytest.raises(FlowBuildError) as exc:
        build_flow_xml(_spec(assignments=[]))
    assert "assignments" in exc.value.missing


def test_non_numeric_value_for_a_number_field_is_rejected():
    with pytest.raises(FlowBuildError) as exc:
        build_flow_xml(_spec(assignments=[FieldAssignment("Score__c", "high", "double")]))
    assert "not numeric" in exc.value.message


def test_status_reflects_activation_intent():
    assert "<status>Draft</status>" in build_flow_xml(_spec())
    assert "<status>Active</status>" in build_flow_xml(_spec(active=True))


def test_flow_definition_zero_deactivates():
    assert "<activeVersionNumber>0</activeVersionNumber>" in build_flow_definition_xml(None)
    assert "<activeVersionNumber>3</activeVersionNumber>" in build_flow_definition_xml(3)


def test_generated_xml_is_well_formed():
    from defusedxml import ElementTree as SafeET

    xml = build_flow_xml(
        _spec(
            entry_conditions=[Condition("Probability", "<", 40, "percent")],
            description="Flags opportunities that look at risk",
        )
    )
    root = SafeET.fromstring(xml.encode())
    assert root.tag.endswith("Flow")


def test_plan_reads_as_english_for_the_approval_card():
    plan = describe_flow_plan(
        _spec(entry_conditions=[Condition("Probability", "less_than", 40, "percent")])
    )
    assert "Opportunity" in plan["trigger"]
    assert "before it is saved" in plan["trigger"]
    assert "Probability" in plan["condition"]
    assert plan["actions"] == ["Set At_Risk__c to True"]


def test_after_save_plan_warns_about_the_extra_dml():
    plan = describe_flow_plan(_spec(trigger=AFTER_SAVE))
    assert any("DML" in note for note in plan["notes"])


def test_before_delete_field_assignment_is_rejected():
    with pytest.raises(FlowBuildError):
        build_flow_xml(_spec(trigger=BEFORE_SAVE, record_trigger_type="Delete"))


def test_xml_escaping_blocks_injection_through_labels():
    xml = build_flow_xml(_spec(label="Bad </label><status>Active</status><label>"))
    # Only the one real status element the builder emitted may be present.
    assert len(re.findall(r"<status>", xml)) == 1
