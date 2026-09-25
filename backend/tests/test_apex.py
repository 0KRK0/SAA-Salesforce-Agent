"""Apex validation, smell detection and packaging."""

from __future__ import annotations

import pytest

from app.salesforce.apex import (
    ApexValidationError,
    build_package,
    count_test_methods,
    detect_smells,
    parse_unit,
)

GOOD_CLASS = """
public with sharing class AccountService {
    public static void touch(List<Account> accounts) {
        update accounts;
    }
}
"""

GOOD_TRIGGER = """
trigger ContactDedupe on Contact (before insert, before update) {
    ContactDedupeHandler.run(Trigger.new);
}
"""


def test_a_class_whose_declaration_disagrees_with_its_name_is_rejected():
    """Deploying `Foo.cls` containing `class Bar` fails obscurely in Salesforce;
    catching it here costs nothing."""
    with pytest.raises(ApexValidationError) as exc:
        parse_unit("class", "AccountHelper", GOOD_CLASS, "62.0")
    assert "declares" in exc.value.message


def test_a_valid_class_parses_and_reports_no_critical_smells():
    unit = parse_unit("class", "AccountService", GOOD_CLASS, "62.0")
    assert unit.name == "AccountService"
    assert unit.path == "classes/AccountService.cls"
    assert unit.metadata_type == "ApexClass"
    assert not unit.has_critical_smell()


def test_a_trigger_declaration_yields_its_object_and_events():
    unit = parse_unit("trigger", "ContactDedupe", GOOD_TRIGGER, "62.0")
    assert unit.trigger_object == "Contact"
    assert unit.trigger_events == ["before insert", "before update"]
    assert unit.path == "triggers/ContactDedupe.trigger"
    assert unit.metadata_type == "ApexTrigger"


def test_source_that_is_not_apex_is_rejected():
    with pytest.raises(ApexValidationError):
        parse_unit("class", "Foo", "this is not apex", "62.0")
    with pytest.raises(ApexValidationError):
        parse_unit("class", "Foo", "", "62.0")


def test_without_sharing_is_a_critical_finding():
    """It silently ignores the org's record-level security — the reviewer must
    see it, and the tool refuses to deploy it unremarked."""
    unit = parse_unit(
        "class", "Sneaky", "public without sharing class Sneaky { }", "62.0"
    )
    critical = [s for s in unit.smells if s.severity == "critical"]
    assert any(s.code == "WITHOUT_SHARING" for s in critical)
    assert unit.has_critical_smell()


def test_unfiltered_delete_is_critical():
    body = """
    public class Cleaner {
        public static void run() {
            delete [SELECT Id FROM Account];
        }
    }
    """
    unit = parse_unit("class", "Cleaner", body, "62.0")
    assert any(s.code == "DELETE_WITHOUT_FILTER" for s in unit.smells)
    assert unit.has_critical_smell()


def test_a_filtered_delete_is_not_flagged_as_unfiltered():
    body = """
    public class Cleaner {
        public static void run() {
            delete [SELECT Id FROM Account WHERE CreatedDate < LAST_YEAR];
        }
    }
    """
    unit = parse_unit("class", "Cleaner", body, "62.0")
    assert not any(s.code == "DELETE_WITHOUT_FILTER" for s in unit.smells)


def test_dml_and_soql_in_loops_are_warned_about():
    body = """
    public class Slow {
        public static void run(List<Account> accounts) {
            for (Account a : accounts) { update a; }
            for (Account a : accounts) { List<Contact> c = [SELECT Id FROM Contact]; }
        }
    }
    """
    codes = {s.code for s in detect_smells(parse_unit("class", "Slow", body, "62.0"))}
    assert "DML_IN_LOOP" in codes
    assert "SOQL_IN_LOOP" in codes


def test_hardcoded_record_ids_are_warned_about():
    body = 'public class Ids { static String x = \'001D000000IqhSLIAZ\'; }'
    codes = {s.code for s in parse_unit("class", "Ids", body, "62.0").smells}
    assert "HARDCODED_ID" in codes


def test_silently_swallowed_exceptions_are_warned_about():
    body = """
    public class Quiet {
        public static void run() {
            try { insert new Account(); } catch (Exception e) {}
        }
    }
    """
    codes = {s.code for s in parse_unit("class", "Quiet", body, "62.0").smells}
    assert "EMPTY_CATCH" in codes


def test_see_all_data_is_only_flagged_on_tests():
    test_body = """
    @isTest(SeeAllData=true)
    public class ThingTest {
        @isTest static void t() {}
    }
    """
    unit = parse_unit("class", "ThingTest", test_body, "62.0")
    assert any(s.code == "SEEALLDATA" for s in unit.smells)
    assert unit.is_test


def test_run_as_is_expected_in_tests_and_notable_elsewhere():
    prod = parse_unit(
        "class",
        "Impersonator",
        "public class Impersonator { void go(User u) { System.runAs(u) {} } }",
        "62.0",
    )
    assert any(s.code == "SYSTEM_RUNAS_PROD" for s in prod.smells)

    test = parse_unit(
        "class",
        "ImpersonatorTest",
        "@isTest public class ImpersonatorTest { @isTest static void t(User u) "
        "{ System.runAs(u) {} } }",
        "62.0",
    )
    assert not any(s.code == "SYSTEM_RUNAS_PROD" for s in test.smells)


def test_test_methods_are_counted():
    body = """
    @isTest
    public class ThingTest {
        @isTest static void one() {}
        @isTest static void two() {}
    }
    """
    assert count_test_methods(body) >= 2


def test_packaging_produces_source_and_metadata_files_with_a_manifest():
    units = [
        parse_unit("class", "AccountService", GOOD_CLASS, "62.0"),
        parse_unit("trigger", "ContactDedupe", GOOD_TRIGGER, "62.0"),
    ]
    files, types = build_package(units)
    assert "classes/AccountService.cls" in files
    assert "classes/AccountService.cls-meta.xml" in files
    assert "triggers/ContactDedupe.trigger" in files
    assert types == {"ApexClass": ["AccountService"], "ApexTrigger": ["ContactDedupe"]}
    assert "<apiVersion>62.0</apiVersion>" in files["classes/AccountService.cls-meta.xml"]


def test_an_oversized_class_is_rejected_before_deployment():
    with pytest.raises(ApexValidationError):
        parse_unit("class", "Big", "public class Big {" + "x" * 1_000_001 + "}", "62.0")
