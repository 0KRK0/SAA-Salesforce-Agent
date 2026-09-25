"""Impact analysis: matching, ranking, and honesty about method coverage."""

from __future__ import annotations

from app.salesforce.dependencies import (
    IMPACT_HIGH,
    IMPACT_LOW,
    IMPACT_MEDIUM,
    IMPACT_NONE,
    Dependency,
    DependencyReport,
    _only_in_comments,
    _reference_pattern,
)


def _report(*dependencies: Dependency, api_available: bool = True) -> DependencyReport:
    report = DependencyReport(target="Account.Tier__c", target_type="CustomField")
    report.dependencies = list(dependencies)
    report.dependency_api_available = api_available
    return report


def _dep(component: str, kind: str, method: str = "source_scan") -> Dependency:
    return Dependency(component=component, component_type=kind, method=method)


# ---------------------------------------------------------------- matching
def test_a_field_name_matches_as_a_whole_token_not_a_substring():
    """Without boundaries, Tier__c matches Customer_Tier__c and the report
    fills with false positives until nobody reads it."""
    pattern = _reference_pattern("Tier__c")
    assert pattern.search("record.Tier__c = 'A';")
    assert pattern.search("{!$Record.Tier__c}")
    assert not pattern.search("record.Customer_Tier__c = 'A';")
    assert not pattern.search("Tier__country")


def test_regex_metacharacters_in_a_name_are_escaped():
    pattern = _reference_pattern("Odd.Name__c")
    assert pattern.search("Odd.Name__c")
    assert not pattern.search("OddXName__c")


def test_a_hit_only_inside_comments_is_recognized_as_such():
    body = """
    // Tier__c used to be set here
    /* and mentioned in this block about Tier__c */
    public class Thing { Integer x = 1; }
    """
    assert _only_in_comments(body, _reference_pattern("Tier__c")) is True


def test_a_real_reference_alongside_a_comment_is_not_comment_only():
    body = """
    // sets Tier__c
    public class Thing { void go(Account a) { a.Tier__c = 'A'; } }
    """
    assert _only_in_comments(body, _reference_pattern("Tier__c")) is False


# ------------------------------------------------------------------ impact
def test_no_dependencies_is_no_impact():
    assert _report().impact == IMPACT_NONE


def test_code_and_automation_dominate_the_impact_rating():
    """A report needing an edit is not the same as a flow breaking."""
    assert _report(_dep("Pipeline", "Report")).impact == IMPACT_LOW
    assert _report(_dep("Set_Tier", "Flow")).impact == IMPACT_MEDIUM
    assert (
        _report(
            _dep("Set_Tier", "Flow"),
            _dep("AccountService", "ApexClass"),
            _dep("Require_Tier", "ValidationRule"),
        ).impact
        == IMPACT_HIGH
    )


def test_many_low_severity_dependencies_still_raise_the_rating():
    report = _report(*[_dep(f"Report {i}", "Report") for i in range(5)])
    assert report.impact == IMPACT_MEDIUM


# --------------------------------------------------------- recommendations
def test_automation_dependencies_block_deletion():
    recommendation = _report(_dep("Set_Tier", "Flow")).recommendation()
    assert "Do not delete yet" in recommendation


def test_report_only_dependencies_warn_without_blocking():
    recommendation = _report(_dep("Pipeline", "Report")).recommendation()
    assert "Do not delete yet" not in recommendation
    assert "silently lose the field" in recommendation


def test_an_empty_result_from_an_unavailable_api_is_not_an_all_clear():
    """This is the distinction that stops someone deleting a field three flows
    use because a silent method returned nothing."""
    recommendation = _report(api_available=False).recommendation()
    assert "not a complete answer" in recommendation
    assert "Where is this used?" in recommendation


def test_an_empty_result_from_a_working_api_still_warns_that_deletion_is_final():
    recommendation = _report(api_available=True).recommendation()
    assert "cannot be undone" in recommendation


# --------------------------------------------------------------- reporting
def test_the_report_groups_by_type_and_names_both_methods():
    report = _report(
        _dep("Set_Tier", "Flow", method="dependency_api"),
        _dep("AccountService", "ApexClass"),
        _dep("Other", "ApexClass"),
    )
    report.scanned = {"apex": 40, "flows": 12}
    payload = report.to_dict()

    assert payload["by_type"] == {"Flow": 1, "ApexClass": 2}
    assert payload["dependency_count"] == 3
    assert payload["methods"]["dependency_api"]["available"] is True
    assert payload["methods"]["source_scan"]["scanned"] == {"apex": 40, "flows": 12}
    assert payload["impact"] == IMPACT_HIGH


def test_each_dependency_records_which_method_found_it():
    report = _report(_dep("Set_Tier", "Flow", method="dependency_api"))
    entry = report.to_dict()["dependencies"]["Flow"][0]
    assert entry["found_by"] == "dependency_api"
    assert entry["confidence"] == "high"


def test_errors_during_scanning_are_surfaced_not_swallowed():
    report = _report()
    report.errors.append("Apex could not be listed: INSUFFICIENT_ACCESS")
    assert report.to_dict()["errors"]
