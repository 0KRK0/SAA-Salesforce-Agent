"""Change-set diffing, manifests and rollback planning."""

from __future__ import annotations

from app.deployment.engine import (
    build_destructive_package,
    build_rollback_plan,
    compute_diff,
    manifest_from_files,
    summarize_diff,
)


def test_manifest_is_derived_from_source_paths():
    files = {
        "classes/AccountService.cls": "…",
        "classes/AccountService.cls-meta.xml": "…",
        "flows/Set_Tier.flow": "…",
        "objects/Account.object": "…",
        "package.xml": "…",
    }
    manifest = manifest_from_files(files)
    assert manifest == {
        "ApexClass": ["AccountService"],
        "Flow": ["Set_Tier"],
        "CustomObject": ["Account"],
    }


def test_meta_files_are_not_components():
    """Including a -meta.xml as a member produces a package Salesforce rejects."""
    manifest = manifest_from_files(
        {"classes/Foo.cls": "…", "classes/Foo.cls-meta.xml": "…"}
    )
    assert manifest["ApexClass"] == ["Foo"]


def test_unrecognized_paths_are_ignored_rather_than_guessed_at():
    assert manifest_from_files({"notes/readme.txt": "…"}) == {}


def test_a_component_absent_from_the_org_is_new():
    diffs = compute_diff({"classes/Foo.cls": "public class Foo {}"}, {})
    assert diffs[0].status == "new"
    assert diffs[0].added_lines == 1


def test_identical_source_is_reported_as_unchanged():
    source = "public class Foo {}"
    diffs = compute_diff({"classes/Foo.cls": source}, {"classes/Foo.cls": source})
    assert diffs[0].status == "unchanged"
    assert summarize_diff(diffs)["no_effective_change"] is True


def test_a_modified_component_produces_a_readable_unified_diff():
    diffs = compute_diff(
        {"classes/Foo.cls": "public class Foo {\n  Integer x = 2;\n}"},
        {"classes/Foo.cls": "public class Foo {\n  Integer x = 1;\n}"},
    )
    diff = diffs[0]
    assert diff.status == "modified"
    assert diff.added_lines == 1
    assert diff.removed_lines == 1
    assert "-  Integer x = 1;" in diff.diff
    assert "+  Integer x = 2;" in diff.diff


def test_diff_summary_counts_by_status():
    diffs = compute_diff(
        {
            "classes/New.cls": "new",
            "classes/Same.cls": "same",
            "classes/Changed.cls": "b",
        },
        {"classes/Same.cls": "same", "classes/Changed.cls": "a"},
    )
    summary = summarize_diff(diffs)
    assert summary == {
        "components": 3,
        "new": 1,
        "modified": 1,
        "unchanged": 1,
        "lines_added": summary["lines_added"],
        "lines_removed": summary["lines_removed"],
        "no_effective_change": False,
    }


def test_rollback_restores_prior_source_and_deletes_what_was_created():
    proposed = {
        "classes/Existing.cls": "public class Existing { int v = 2; }",
        "classes/Brand_New.cls": "public class Brand_New {}",
    }
    current = {"classes/Existing.cls": "public class Existing { int v = 1; }"}
    plan = build_rollback_plan(proposed, current, manifest_from_files(proposed))

    assert plan["possible"] is True
    assert plan["restore_files"]["classes/Existing.cls"] == current["classes/Existing.cls"]
    assert plan["destructive_manifest"]["ApexClass"] == ["Brand_New"]
    assert "classes/Brand_New.cls" in plan["components_created"]


def test_rollback_warns_that_deleting_a_field_deletes_its_data():
    proposed = {"objects/Account.object": "<CustomObject/>"}
    plan = build_rollback_plan(proposed, {}, {"CustomField": ["Account.Tier__c"]})
    assert any("deletes the data" in c for c in plan["caveats"])


def test_rollback_is_explicit_that_flow_versions_are_never_removed():
    proposed = {"flows/Set_Tier.flow": "<Flow/>"}
    plan = build_rollback_plan(proposed, {}, {"Flow": ["Set_Tier"]})
    assert any("never deleted" in c for c in plan["caveats"])


def test_components_that_cannot_be_destructively_removed_are_named():
    proposed = {"labels/CustomLabels.labels": "<CustomLabels/>"}
    plan = build_rollback_plan(proposed, {}, manifest_from_files(proposed))
    assert plan["not_removable"] == ["labels/CustomLabels.labels"]
    assert any("deleted manually" in c for c in plan["caveats"])


def test_a_change_set_that_only_modifies_has_no_deletions_to_roll_back():
    proposed = {"classes/Foo.cls": "b"}
    plan = build_rollback_plan(proposed, {"classes/Foo.cls": "a"}, {"ApexClass": ["Foo"]})
    assert plan["destructive_manifest"] == {}
    assert plan["restore_files"] == {"classes/Foo.cls": "a"}


def test_destructive_package_pairs_an_empty_manifest_with_the_deletions():
    import io
    import zipfile

    payload = build_destructive_package({"ApexClass": ["Gone"]}, "62.0")
    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
        names = set(zf.namelist())
        destructive = zf.read("destructiveChangesPost.xml").decode()
        package = zf.read("package.xml").decode()
    assert names == {"package.xml", "destructiveChangesPost.xml"}
    assert "<members>Gone</members>" in destructive
    assert "<members>" not in package
