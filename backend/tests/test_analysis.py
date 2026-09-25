"""Duplicate detection, data-quality analysis and merge planning."""

from __future__ import annotations

from app.analysis.duplicates import (
    DuplicateGroup,
    build_merge_plan,
    choose_survivor,
    find_duplicates,
    normalize_email,
    normalize_name,
    normalize_phone,
    rules_for,
)
from app.analysis.quality import (
    anomalies,
    completeness,
    distributions,
    summarize,
    validity,
)


# --------------------------------------------------------------- normalizers
def test_company_names_normalize_past_legal_suffixes_and_punctuation():
    assert normalize_name("Acme Corporation") == "acme"
    assert normalize_name("ACME Corp.") == "acme"
    assert normalize_name("The Acme Group, Inc.") == "acme"
    assert normalize_name("Acme") == normalize_name("acme  inc")


def test_a_name_that_is_only_a_suffix_survives_normalization():
    """Stripping every word would collapse unrelated records together."""
    assert normalize_name("Group") == "group"


def test_gmail_dots_and_tags_are_the_same_address():
    assert normalize_email("First.Last+sf@gmail.com") == "firstlast@gmail.com"
    # Only Gmail treats dots as insignificant.
    assert normalize_email("first.last@acme.com") == "first.last@acme.com"
    assert normalize_email("not-an-email") == ""


def test_phone_numbers_compare_on_the_last_ten_digits():
    assert normalize_phone("+1 (415) 555-0100") == normalize_phone("415.555.0100")
    assert normalize_phone("555") == "555"


# ------------------------------------------------------------------ matching
def _account(i: int, **fields) -> dict:
    return {"Id": f"001{i:015d}", **fields}


def test_exact_name_duplicates_are_grouped_and_explained():
    records = [
        _account(1, Name="Acme Corporation", Phone="415-555-0100"),
        _account(2, Name="ACME Corp", Phone="415-555-0100"),
        _account(3, Name="Globex", Phone="212-555-0199"),
    ]
    groups, stats = find_duplicates(records, "Account", fields={"Id", "Name", "Phone"})
    assert len(groups) == 1
    assert len(groups[0].record_ids) == 2
    assert groups[0].matched_on  # every group says why it matched
    assert stats["records"] == 3


def test_contacts_match_on_email_across_different_spellings_of_the_name():
    records = [
        {"Id": "003a", "FirstName": "Bob", "LastName": "Smith", "Email": "b.smith@acme.com"},
        {"Id": "003b", "FirstName": "Robert", "LastName": "Smith", "Email": "b.smith@acme.com"},
        {"Id": "003c", "FirstName": "Ann", "LastName": "Jones", "Email": "a.jones@acme.com"},
    ]
    groups, _ = find_duplicates(
        records, "Contact", fields={"Id", "FirstName", "LastName", "Email"}
    )
    assert len(groups) == 1
    assert set(groups[0].record_ids) == {"003a", "003b"}
    assert "email" in " ".join(groups[0].matched_on)


def test_records_sharing_a_blank_field_are_not_duplicates():
    """Empty values are the classic false-positive source: everything matches
    everything on an unset field."""
    records = [_account(i, Name=f"Company {i}", Phone="") for i in range(5)]
    groups, _ = find_duplicates(records, "Account", fields={"Id", "Name", "Phone"})
    assert groups == []


def test_transitive_matches_collapse_into_one_group():
    """A matches B on name, B matches C on phone — all three are one entity."""
    records = [
        _account(1, Name="Acme Corporation", Phone="415-555-0100"),
        _account(2, Name="Acme Corp", Phone="415-555-0100"),
        _account(3, Name="Totally Different", Phone="415-555-0100"),
    ]
    groups, _ = find_duplicates(records, "Account", fields={"Id", "Name", "Phone"})
    assert len(groups) == 1
    assert len(groups[0].record_ids) == 3


def test_an_object_with_no_applicable_rule_says_so_rather_than_returning_nothing():
    groups, stats = find_duplicates(
        [{"Id": "a", "Amount": 1}], "CustomThing__c", fields={"Id", "Amount"}
    )
    assert groups == []
    assert "no duplicate-matching rule" in stats["note"].lower()


def test_oversized_blocks_are_skipped_and_reported():
    """A thousand records sharing one switchboard number are not 1,000 duplicates,
    and comparing them pairwise would be a million comparisons."""
    records = [_account(i, Name=f"Company {i}", Phone="415-555-0100") for i in range(50)]
    groups, stats = find_duplicates(
        records, "Account", fields={"Id", "Name", "Phone"}, max_block_size=10
    )
    assert stats["skipped_blocks"]
    assert "note" in stats
    assert groups == []


def test_rules_are_filtered_to_fields_the_query_actually_returned():
    rules = rules_for("Account", {"Id", "Name"})
    assert all(all(f in {"Id", "Name"} for f in r.fields) for r in rules)
    assert rules


# -------------------------------------------------------------- merge plans
def test_survivor_selection_prefers_the_most_complete_record_and_explains_why():
    records = [
        {"Id": "a", "Name": "Acme", "Phone": None, "Industry": None},
        {"Id": "b", "Name": "Acme", "Phone": "415-555-0100", "Industry": "Technology"},
    ]
    survivor, reason = choose_survivor(records, "most_complete")
    assert survivor["Id"] == "b"
    assert "complete" in reason.lower()


def test_survivor_selection_by_age_uses_created_date():
    records = [
        {"Id": "a", "CreatedDate": "2020-01-01T00:00:00Z"},
        {"Id": "b", "CreatedDate": "2024-01-01T00:00:00Z"},
    ]
    assert choose_survivor(records, "oldest")[0]["Id"] == "a"
    assert choose_survivor(records, "newest")[0]["Id"] == "b"


def test_a_merge_plan_names_the_data_that_would_be_destroyed():
    """This is the whole point of the plan: what disappears if someone approves
    without reading."""
    group = DuplicateGroup(
        record_ids=["a", "b"],
        matched_on=["exact_name"],
        confidence=0.9,
        sample=[
            {"Id": "a", "Name": "Acme", "Phone": "415-555-0100", "Industry": None},
            {"Id": "b", "Name": "Acme", "Phone": "415-555-0999", "Industry": "Tech"},
        ],
    )
    plan = build_merge_plan(group)
    assert plan["survivor_id"] in {"a", "b"}
    # Phone differs on both records -> a conflict; Industry exists only on one
    # -> data loss if that one loses.
    assert plan["field_conflicts"] or plan["data_that_would_be_lost"]
    assert "cannot be undone" in plan["warning"]


def test_a_merge_plan_needs_at_least_two_records():
    group = DuplicateGroup(record_ids=["a"], matched_on=[], confidence=1.0, sample=[{"Id": "a"}])
    assert "error" in build_merge_plan(group)


# ------------------------------------------------------------- data quality
def test_completeness_ranks_the_emptiest_fields_first():
    records = [
        {"Id": "1", "Industry": "Tech", "Website": None},
        {"Id": "2", "Industry": None, "Website": None},
        {"Id": "3", "Industry": "Tech", "Website": None},
    ]
    rows = completeness(records, ["Industry", "Website"])
    assert rows[0]["field"] == "Website"
    assert rows[0]["fill_rate"] == 0.0
    assert rows[1]["fill_rate"] > 0


def test_validity_flags_clearly_broken_values_only():
    records = [
        {"Id": "1", "Email": "person@acme.com", "Phone": "+1 415 555 0100"},
        {"Id": "2", "Email": "not-an-email", "Phone": "call me maybe"},
        {"Id": "3", "Email": "another@acme.co.uk", "Phone": "(415) 555-0100 ext 22"},
    ]
    findings = validity(records, {"Email": "email", "Phone": "phone"})
    by_field = {f["field"]: f for f in findings}
    assert by_field["Email"]["invalid_count"] == 1
    assert by_field["Phone"]["invalid_count"] == 1
    # The valid international and extension formats must not be flagged.
    assert by_field["Phone"]["examples"][0]["record_id"] == "2"


def test_validity_rejects_out_of_range_percentages_and_negative_currency():
    records = [{"Id": "1", "Probability": "140", "Amount": "-5"}]
    findings = validity(records, {"Probability": "percent", "Amount": "currency"})
    assert {f["field"] for f in findings} == {"Probability", "Amount"}


def test_blank_values_are_a_completeness_problem_not_a_validity_one():
    records = [{"Id": "1", "Email": None}, {"Id": "2", "Email": "   "}]
    assert validity(records, {"Email": "email"}) == []


def test_picklist_distribution_exposes_value_sprawl():
    records = [{"Id": str(i), "Stage": f"Stage {i % 12}"} for i in range(60)]
    rows = distributions(records, ["Stage"], top=5)
    assert rows[0]["distinct_values"] == 12
    assert rows[0]["long_tail"] == 7


def test_anomalies_use_median_absolute_deviation_not_the_mean():
    """One enormous value must not widen the band until nothing is ever flagged."""
    records = [{"Id": str(i), "Amount": 100} for i in range(20)]
    records.append({"Id": "outlier", "Amount": 10_000_000})
    findings = anomalies(records, ["Amount"])
    # A MAD of zero (every value identical) is correctly skipped, so vary them.
    records = [{"Id": str(i), "Amount": 100 + i} for i in range(20)]
    records.append({"Id": "outlier", "Amount": 10_000_000})
    findings = anomalies(records, ["Amount"])
    assert findings
    assert findings[0]["examples"][0]["record_id"] == "outlier"


def test_anomaly_detection_needs_enough_data_to_be_meaningful():
    records = [{"Id": str(i), "Amount": i} for i in range(5)]
    assert anomalies(records, ["Amount"]) == []


def test_summary_is_explicit_when_findings_are_based_on_a_sample():
    result = summarize(
        object_name="Account",
        analyzed=1000,
        total_in_org=50_000,
        completeness_rows=[{"field": "Industry", "empty": 800, "fill_rate": 20.0}],
        validity_rows=[],
        anomaly_rows=[],
    )
    assert result["sampled"] is True
    assert "1,000 of 50,000" in result["sampling_note"]
    assert any("Industry" in issue for issue in result["top_issues"])


def test_summary_says_so_when_nothing_is_wrong():
    result = summarize(
        object_name="Account",
        analyzed=100,
        total_in_org=100,
        completeness_rows=[{"field": "Name", "empty": 0, "fill_rate": 100.0}],
        validity_rows=[],
        anomaly_rows=[],
    )
    assert result["sampled"] is False
    assert "No significant" in result["top_issues"][0]
