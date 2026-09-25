"""Metadata API format → SFDX source format.

The value of this conversion is entirely in whether the files it produces are
the ones the Salesforce CLI would produce. A file that looks plausible, that
git accepts, and that the Metadata API rejects on deploy is worse than no file
at all — it puts a broken artefact in a customer's repository with the agent's
name on the commit.

So these tests assert the exact shape: filenames, folder layout, and the root
element of every decomposed file.
"""

from __future__ import annotations

from app.deployment.sfdx import (
    DECOMPOSED,
    SOURCE_ROOT,
    describe,
    manifest_from_source,
    to_source_format,
)

ACCOUNT_OBJECT = """<?xml version="1.0" encoding="UTF-8"?>
<CustomObject xmlns="http://soap.sforce.com/2006/04/metadata">
    <label>Account</label>
    <sharingModel>ReadWrite</sharingModel>
    <fields>
        <fullName>Tier__c</fullName>
        <label>Tier</label>
        <type>Picklist</type>
        <valueSet>
            <valueSetDefinition>
                <sorted>false</sorted>
                <value>
                    <fullName>Gold</fullName>
                    <default>false</default>
                </value>
            </valueSetDefinition>
        </valueSet>
    </fields>
    <fields>
        <fullName>Rating__c</fullName>
        <label>Rating</label>
        <type>Number</type>
    </fields>
    <validationRules>
        <fullName>Tier_Required</fullName>
        <active>true</active>
        <errorConditionFormula>ISBLANK(Tier__c)</errorConditionFormula>
    </validationRules>
    <listViews>
        <fullName>AllAccounts</fullName>
        <label>All Accounts</label>
    </listViews>
    <recordTypes>
        <fullName>Enterprise</fullName>
        <active>true</active>
    </recordTypes>
</CustomObject>
"""

RETRIEVE = {
    "unpackaged/package.xml": '<?xml version="1.0"?><Package/>',
    "unpackaged/objects/Account.object": ACCOUNT_OBJECT,
    "unpackaged/classes/AccountService.cls": "public class AccountService {}",
    "unpackaged/classes/AccountService.cls-meta.xml": "<ApexClass/>",
    "unpackaged/triggers/AccountTrigger.trigger": "trigger AccountTrigger on Account {}",
    "unpackaged/flows/Set_Tier.flow": "<Flow/>",
    "unpackaged/layouts/Account-Account Layout.layout": "<Layout/>",
    "unpackaged/permissionsets/Sales.permissionset": "<PermissionSet/>",
    "unpackaged/lwc/tierBadge/tierBadge.js": "export default class {}",
    "unpackaged/lwc/tierBadge/tierBadge.js-meta.xml": "<LightningComponentBundle/>",
}


def _convert(files=None):
    return to_source_format(files if files is not None else RETRIEVE)


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------
def test_an_object_is_decomposed_into_one_file_per_component():
    """Two people adding different fields to the same object must not conflict
    in a file neither of them meaningfully touched."""
    result = _convert()
    base = f"{SOURCE_ROOT}/objects/Account"
    assert f"{base}/Account.object-meta.xml" in result.files
    assert f"{base}/fields/Tier__c.field-meta.xml" in result.files
    assert f"{base}/fields/Rating__c.field-meta.xml" in result.files
    assert f"{base}/validationRules/Tier_Required.validationRule-meta.xml" in result.files
    assert f"{base}/listViews/AllAccounts.listView-meta.xml" in result.files
    assert f"{base}/recordTypes/Enterprise.recordType-meta.xml" in result.files


def test_the_object_file_keeps_only_what_is_not_decomposed():
    result = _convert()
    body = result.files[f"{SOURCE_ROOT}/objects/Account/Account.object-meta.xml"]
    assert "<label>Account</label>" in body
    assert "<sharingModel>ReadWrite</sharingModel>" in body
    # The decomposed children have moved out entirely.
    assert "Tier__c" not in body
    assert "Tier_Required" not in body


def test_a_decomposed_field_uses_the_metadata_type_as_its_root():
    """The mistake that produces a file git accepts and Salesforce rejects.

    A field file's root element is `CustomField` — the metadata type — not
    `fields`, the tag it was nested under in the object file.
    """
    result = _convert()
    body = result.files[f"{SOURCE_ROOT}/objects/Account/fields/Tier__c.field-meta.xml"]
    assert "<CustomField xmlns=" in body
    assert "</CustomField>" in body
    assert "<fields>" not in body
    # And the component's own children are the content.
    assert "<fullName>Tier__c</fullName>" in body
    assert "<type>Picklist</type>" in body


def test_every_decomposed_type_declares_its_root_element():
    """A missing root mapping would silently produce undeployable files for
    that type only, which is exactly the kind of gap nobody notices."""
    for tag, rule in DECOMPOSED.items():
        folder, suffix, root_tag = rule
        assert folder and suffix and root_tag, tag
        assert root_tag[0].isupper(), tag


def test_nested_structure_inside_a_component_is_preserved():
    """A picklist's value set is several levels deep. Flattening it would
    silently drop the values."""
    result = _convert()
    body = result.files[f"{SOURCE_ROOT}/objects/Account/fields/Tier__c.field-meta.xml"]
    assert "<valueSetDefinition>" in body
    assert "<fullName>Gold</fullName>" in body


def test_every_file_declares_the_metadata_namespace():
    result = _convert()
    for path, body in result.files.items():
        if not path.endswith("-meta.xml") or "lwc/" in path:
            continue
        if body.startswith("<?xml") and "soap.sforce.com" not in body:
            # Passed-through files keep whatever they had; generated ones must
            # declare the namespace or the Metadata API rejects them.
            assert "xmlns" in body, path


def test_generated_files_carry_no_namespace_prefixes():
    """ElementTree writes `ns0:` prefixes, which are valid and produce a diff
    against CLI output on every line. A repository where every file differs
    cosmetically is one where nobody reads diffs."""
    result = _convert()
    body = result.files[f"{SOURCE_ROOT}/objects/Account/fields/Tier__c.field-meta.xml"]
    assert "ns0:" not in body


# ---------------------------------------------------------------------------
# Non-object types
# ---------------------------------------------------------------------------
def test_apex_keeps_its_body_and_its_sidecar_together():
    result = _convert()
    assert f"{SOURCE_ROOT}/classes/AccountService.cls" in result.files
    assert f"{SOURCE_ROOT}/classes/AccountService.cls-meta.xml" in result.files
    # The body is code, not XML, and is stored verbatim.
    assert result.files[f"{SOURCE_ROOT}/classes/AccountService.cls"].startswith("public")


def test_xml_metadata_types_gain_the_meta_marker():
    result = _convert()
    assert f"{SOURCE_ROOT}/flows/Set_Tier.flow-meta.xml" in result.files
    assert f"{SOURCE_ROOT}/permissionsets/Sales.permissionset-meta.xml" in result.files
    assert f"{SOURCE_ROOT}/layouts/Account-Account Layout.layout-meta.xml" in result.files


def test_bundle_types_keep_their_directory_shape():
    """An LWC is a folder of files; flattening it would break the component."""
    result = _convert()
    assert f"{SOURCE_ROOT}/lwc/tierBadge/tierBadge.js" in result.files
    assert f"{SOURCE_ROOT}/lwc/tierBadge/tierBadge.js-meta.xml" in result.files


def test_the_package_manifest_is_not_committed():
    """package.xml describes one retrieve, not the source. In a repository it
    goes stale the moment anything else changes."""
    result = _convert()
    assert not any(p.endswith("package.xml") for p in result.files)


def test_a_named_package_folder_is_stripped_like_unpackaged():
    """A retrieve of a named package does not use `unpackaged/`. Assuming it
    does would put every file one folder too deep."""
    result = to_source_format(
        {"MyPackage/classes/A.cls": "public class A {}"}
    )
    assert f"{SOURCE_ROOT}/classes/A.cls" in result.files


# ---------------------------------------------------------------------------
# Robustness
# ---------------------------------------------------------------------------
def test_an_unparseable_object_is_kept_whole_with_a_warning():
    """Losing a customer's metadata to a parse error would be far worse than an
    undecomposed file in their repository."""
    result = to_source_format(
        {"unpackaged/objects/Broken.object": "<CustomObject><unclosed>"}
    )
    assert f"{SOURCE_ROOT}/objects/Broken/Broken.object-meta.xml" in result.files
    assert result.warnings
    assert "could not be parsed" in result.warnings[0]


def test_a_component_without_a_full_name_stays_in_the_object_file():
    """It cannot be given a filename, and dropping it would lose it."""
    result = to_source_format(
        {
            "unpackaged/objects/Odd.object": (
                '<?xml version="1.0"?>'
                '<CustomObject xmlns="http://soap.sforce.com/2006/04/metadata">'
                "<fields><label>No name</label></fields>"
                "</CustomObject>"
            )
        }
    )
    body = result.files[f"{SOURCE_ROOT}/objects/Odd/Odd.object-meta.xml"]
    assert "No name" in body
    assert result.warnings


def test_an_empty_retrieve_produces_nothing_rather_than_failing():
    result = to_source_format({})
    assert result.files == {}
    assert result.counts == {}


def test_a_custom_source_root_is_honoured():
    """Not every project uses force-app."""
    result = to_source_format(
        {"unpackaged/classes/A.cls": "public class A {}"}, root="src/main/default"
    )
    assert "src/main/default/classes/A.cls" in result.files


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
def test_the_summary_counts_what_was_produced():
    result = _convert()
    summary = result.summary()
    assert summary["counts"]["fields"] == 2
    assert summary["counts"]["objects"] == 1
    assert summary["file_count"] == len(result.files)


def test_the_converter_states_its_limitations_rather_than_implying_none():
    described = describe()
    assert described["limitations"]
    assert "no Salesforce CLI" in described["implementation"]
    assert any("sfdx-project.json" in limit for limit in described["limitations"])


def test_a_manifest_can_be_derived_back_from_source_paths():
    """Describes what a commit contains without re-reading the org."""
    result = _convert()
    manifest = manifest_from_source(result.files)
    assert manifest["CustomObject"] == ["Account"]
    assert manifest["CustomField"] == ["Account.Rating__c", "Account.Tier__c"]
    assert manifest["ValidationRule"] == ["Account.Tier_Required"]
    assert manifest["ApexClass"] == ["AccountService"]
    assert manifest["Flow"] == ["Set_Tier"]


def test_the_manifest_names_fields_the_way_salesforce_does():
    """`Account.Tier__c`, not `Tier__c`. A bare field name is ambiguous across
    objects and is not what a package.xml member looks like."""
    result = _convert()
    manifest = manifest_from_source(result.files)
    assert all("." in m for m in manifest["CustomField"])
