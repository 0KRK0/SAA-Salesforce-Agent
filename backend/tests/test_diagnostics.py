"""The org debugger: evidence in, confidence-rated findings out.

The property under test throughout is that a finding only appears when the
evidence supports it. A debugger that produces a fluent answer regardless of
what the org shows is worse than no debugger — it sends someone looking in the
wrong place.
"""

from __future__ import annotations

from app.diagnostics.collector import Evidence
from app.diagnostics.diagnose import HIGH, LOW, MEDIUM, diagnose


def _evidence(**areas) -> Evidence:
    evidence = Evidence(object_name="Opportunity", record_id="006x")
    for area, observations in areas.items():
        for summary, detail in observations:
            evidence.add(area, summary, detail, source="test")
    return evidence


def _causes(result) -> str:
    return " ".join(f["likely_cause"] for f in result["findings"])


# ---------------------------------------------------------------- no evidence
def test_no_evidence_produces_no_invented_cause():
    result = diagnose(Evidence(object_name="Opportunity"), "why isn't this assigned?")
    assert result["findings"] == []
    assert result["overall_confidence"] == "none"
    assert "No configuration" in result["verdict"]


def test_the_verdict_admits_when_areas_could_not_be_inspected():
    evidence = Evidence(object_name="Opportunity")
    evidence.unavailable.append("field history: not enabled")
    result = diagnose(evidence, "why did this change?")
    assert "not_inspected" in result
    assert "could not be inspected" in result["verdict"]


# ------------------------------------------------------------------ schema
def test_a_formula_field_explains_why_nothing_can_set_it():
    result = diagnose(
        _evidence(
            schema=[
                (
                    "Tier__c is a string field, NOT updateable, calculated (formula)",
                    {
                        "type": "string",
                        "calculated": True,
                        "calculatedFormula": "IF(Amount > 1000, 'A', 'B')",
                        "updateable": False,
                    },
                )
            ]
        ),
        "why can't the flow set Tier__c?",
    )
    top = result["findings"][0]
    assert "formula field" in top["likely_cause"]
    assert top["confidence"] == HIGH
    assert any("IF(Amount" in e for e in top["evidence"])


def test_a_restricted_picklist_is_offered_as_a_validation_explanation():
    result = diagnose(
        _evidence(
            schema=[
                (
                    "Stage is a picklist field",
                    {
                        "type": "picklist",
                        "updateable": True,
                        "restrictedPicklist": True,
                        "picklistValues": ["Prospecting", "Closed Won"],
                    },
                )
            ]
        ),
        "why does saving throw an error?",
    )
    assert "restricted picklist" in _causes(result)


# ------------------------------------------------------------- permissions
def test_a_field_nobody_can_see_is_diagnosed_with_high_confidence():
    result = diagnose(
        _evidence(
            permissions=[
                (
                    "No profile or permission set grants any access to "
                    "Opportunity.Tier__c.",
                    None,
                )
            ]
        ),
        "why can't this user see the field?",
    )
    top = result["findings"][0]
    assert top["confidence"] == HIGH
    assert "not visible" in top["likely_cause"]


def test_read_only_field_access_is_diagnosed():
    result = diagnose(
        _evidence(
            permissions=[
                (
                    "3 profile(s)/permission set(s) can read Tier__c; 0 can edit it.",
                    [{"granted_by": "Sales", "read": True, "edit": False}],
                )
            ]
        ),
        "why can't this user edit the field?",
    )
    assert "read-only" in _causes(result)


def test_missing_object_access_is_diagnosed():
    result = diagnose(
        _evidence(
            permissions=[("This user has no object permissions on Opportunity", None)]
        ),
        "why can't they see opportunities?",
    )
    assert result["findings"][0]["confidence"] == HIGH
    assert "no object-level access" in _causes(result)


# -------------------------------------------------------------- automation
def test_absent_automation_is_itself_the_answer():
    """"Nothing is setting this" is a real, high-confidence diagnosis."""
    result = diagnose(
        _evidence(
            automation=[
                ("No active flows are triggered by Opportunity.", None),
                ("No Apex triggers exist on Opportunity.", None),
            ]
        ),
        "why isn't the owner being assigned?",
    )
    top = result["findings"][0]
    assert top["confidence"] == HIGH
    assert "no active automation" in top["likely_cause"].lower()


def test_flow_entry_conditions_are_surfaced_as_the_thing_to_check():
    result = diagnose(
        _evidence(
            automation=[
                (
                    "2 active flow(s) run on Opportunity.",
                    [
                        {
                            "api_name": "Set_At_Risk",
                            "logic": {
                                "trigger": {
                                    "filters": [
                                        {"field": "Probability", "operator": "LessThan"}
                                    ],
                                    "filter_logic": "and",
                                }
                            },
                        },
                        {"api_name": "Other_Flow", "logic": {}},
                    ],
                )
            ]
        ),
        "why didn't the flow run?",
    )
    causes = _causes(result)
    assert "entry conditions" in causes
    # Two flows on one object is its own hazard and is reported.
    assert "unspecified order" in causes


def test_apex_triggers_are_reported_as_able_to_override_a_flow():
    result = diagnose(
        _evidence(
            automation=[
                ("1 Apex trigger(s) on Opportunity: OppTrigger (before update)", [{}])
            ]
        ),
        "why does the field keep changing?",
    )
    assert "Apex trigger code runs" in _causes(result)


# -------------------------------------------------------------- validation
def test_active_validation_rules_are_ranked_high_for_a_save_error():
    result = diagnose(
        _evidence(
            validation=[
                (
                    "2 active validation rule(s) on Opportunity.",
                    [
                        {"name": "Require_Reason", "formula": "ISBLANK(Reason__c)"},
                        {"name": "Positive_Amount", "formula": "Amount < 0"},
                    ],
                )
            ]
        ),
        "why do I get an error when saving?",
    )
    top = result["findings"][0]
    assert top["confidence"] == HIGH
    assert any("ISBLANK" in e for e in top["evidence"])


def test_absence_of_validation_rules_redirects_the_search():
    result = diagnose(
        _evidence(validation=[("Opportunity has no active validation rules.", None)]),
        "why does saving fail with a validation error?",
    )
    assert "no validation rule" in _causes(result).lower()
    assert "Apex triggers" in result["findings"][0]["recommended_fix"]


# -------------------------------------------------------------- assignment
def test_missing_assignment_rules_answer_the_assignment_question():
    result = diagnose(
        _evidence(
            assignment=[("No assignment rules were found for Lead.", None)]
        ),
        "why isn't this lead being assigned?",
    )
    assert result["findings"][0]["confidence"] == HIGH
    assert "no assignment rules" in _causes(result).lower()


def test_active_assignment_rules_surface_the_api_header_gotcha():
    """The real cause is almost always that the API insert did not ask for
    assignment rules to run."""
    result = diagnose(
        _evidence(
            assignment=[
                ("Lead has 1 assignment rule set(s), 1 active.", [{"active": True}])
            ]
        ),
        "why isn't this lead being assigned?",
    )
    assert "explicitly requests them" in _causes(result)


def test_inactive_assignment_rules_are_diagnosed():
    result = diagnose(
        _evidence(
            assignment=[
                ("Lead has 2 assignment rule set(s), 0 active.", [{"active": False}])
            ]
        ),
        "why isn't this lead routed?",
    )
    assert "none of them is active" in _causes(result)


# ---------------------------------------------------------- ownership/history
def test_an_inactive_owner_is_always_worth_reporting():
    result = diagnose(
        _evidence(
            ownership=[
                ("The record is owned by Dana Ex (INACTIVE user)", {"active": False})
            ]
        ),
        "why are the notifications not arriving?",
    )
    assert result["findings"][0]["confidence"] == HIGH
    assert "inactive user" in _causes(result)


def test_field_history_answers_who_changed_the_value():
    result = diagnose(
        _evidence(
            history=[
                (
                    "3 tracked field change(s) on this record",
                    [
                        {
                            "field": "Amount",
                            "from": "100",
                            "to": "0",
                            "by": "Integration User",
                            "at": "2026-01-01T00:00:00Z",
                        }
                    ],
                )
            ]
        ),
        "why did the amount get reset to zero?",
    )
    top = result["findings"][0]
    assert "Integration User" in top["likely_cause"]
    assert top["confidence"] == HIGH


def test_never_changed_history_redirects_to_record_creation():
    result = diagnose(
        _evidence(
            history=[
                (
                    "Field history tracking is enabled but no tracked field has ever "
                    "changed on this record.",
                    None,
                )
            ]
        ),
        "why is this field blank?",
    )
    assert "never been changed" in _causes(result)


# --------------------------------------------------------------- relevance
def test_findings_are_ranked_by_confidence():
    result = diagnose(
        _evidence(
            schema=[
                (
                    "Stage is a picklist",
                    {"type": "picklist", "restrictedPicklist": True,
                     "picklistValues": ["A"], "updateable": True},
                )
            ],
            permissions=[
                ("No profile or permission set grants any access to Stage.", None)
            ],
        ),
        "why can't this user edit the field?",
    )
    confidences = [f["confidence"] for f in result["findings"]]
    assert confidences[0] == HIGH
    assert confidences == sorted(
        confidences, key=lambda c: -{HIGH: 3, MEDIUM: 2, LOW: 1}[c]
    )


def test_the_question_steers_which_rules_fire():
    """A permission question must not be answered with assignment findings."""
    evidence = _evidence(
        assignment=[("No assignment rules were found for Lead.", None)]
    )
    permission_result = diagnose(evidence, "why can't this user edit the field?")
    assignment_result = diagnose(evidence, "why isn't this lead being assigned?")
    assert permission_result["interpreted_as"] == "permission"
    assert assignment_result["interpreted_as"] == "assignment"
    # The same evidence yields a stronger claim when the question matches it.
    assert assignment_result["findings"][0]["confidence"] == HIGH
    assert permission_result["findings"][0]["confidence"] == MEDIUM
