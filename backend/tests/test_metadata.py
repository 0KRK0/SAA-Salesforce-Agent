import io
import zipfile

import pytest

from app.salesforce.metadata import (
    DeployResult,
    MetadataValidationError,
    build_custom_field_xml,
    build_object_file,
    build_package_xml,
    build_zip,
    normalize_field_api_name,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Customer Tier", "Customer_Tier__c"),
        ("Customer_Tier", "Customer_Tier__c"),
        ("Customer_Tier__c", "Customer_Tier__c"),
        ("2Tier", "X2Tier__c"),
    ],
)
def test_normalize_field_api_name(raw, expected):
    assert normalize_field_api_name(raw) == expected


def test_picklist_field_xml():
    xml = build_custom_field_xml(
        {
            "api_name": "Customer_Tier",
            "label": "Customer Tier",
            "type": "Picklist",
            "picklist_values": ["Enterprise", "SMB", "Startup"],
        }
    )
    assert "<fullName>Customer_Tier__c</fullName>" in xml
    assert "<type>Picklist</type>" in xml
    for value in ("Enterprise", "SMB", "Startup"):
        assert f"<fullName>{value}</fullName>" in xml


def test_text_field_defaults_length():
    xml = build_custom_field_xml({"api_name": "Note", "type": "Text"})
    assert "<length>255</length>" in xml


def test_missing_required_option_raises():
    with pytest.raises(MetadataValidationError) as exc:
        build_custom_field_xml({"api_name": "Tier", "type": "Picklist"})
    assert "picklist_values" in exc.value.missing


def test_unsupported_type_raises():
    with pytest.raises(MetadataValidationError):
        build_custom_field_xml({"api_name": "X", "type": "Geolocation"})


def test_lookup_requires_reference():
    with pytest.raises(MetadataValidationError):
        build_custom_field_xml({"api_name": "Owner_Link", "type": "Lookup"})


def test_package_zip_contents():
    field_xml = build_custom_field_xml(
        {"api_name": "Customer_Tier", "type": "Picklist", "picklist_values": ["A"]}
    )
    files = {
        "objects/Account.object": build_object_file("Account", [field_xml]),
        "package.xml": build_package_xml({"CustomField": ["Account.Customer_Tier__c"]}, "62.0"),
    }
    data = build_zip(files)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        assert sorted(zf.namelist()) == ["objects/Account.object", "package.xml"]
        manifest = zf.read("package.xml").decode()
    assert "<members>Account.Customer_Tier__c</members>" in manifest
    assert "<version>62.0</version>" in manifest


def test_xml_escaping_blocks_injection():
    xml = build_custom_field_xml(
        {"api_name": "Note", "type": "Text", "label": "<script>&bad</script>"}
    )
    assert "<script>" not in xml
    assert "&lt;script&gt;" in xml


def test_deploy_result_serialization():
    result = DeployResult(
        id="0Af000", status="Succeeded", done=True, success=True, components_total=1,
        components_deployed=1,
    )
    payload = result.to_dict()
    assert payload["deploy_id"] == "0Af000"
    assert payload["components"]["deployed"] == 1
