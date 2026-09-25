import pytest

from app.salesforce.soql import SoqlValidationError, validate_soql


def test_adds_limit_when_missing():
    vq = validate_soql("SELECT Id, Name FROM Account")
    assert "LIMIT" in vq.query.upper()
    assert vq.object_name == "Account"


def test_caps_excessive_limit():
    vq = validate_soql("SELECT Id FROM Account LIMIT 100000", max_limit=500)
    assert vq.limit == 500
    assert "LIMIT 500" in vq.query


@pytest.mark.parametrize(
    "query",
    [
        "DELETE FROM Account",
        "SELECT Id FROM Account; DELETE FROM Account",
        "update Account set Name='x'",
        "SELECT Id FROM Account /* */ ; drop table",
    ],
)
def test_rejects_mutations(query):
    with pytest.raises(SoqlValidationError):
        validate_soql(query)


def test_allows_dml_words_inside_string_literals():
    vq = validate_soql("SELECT Id FROM Account WHERE Name LIKE '%update%'")
    assert vq.object_name == "Account"


def test_allows_identifiers_containing_verbs():
    vq = validate_soql("SELECT Id, CreatedDate, LastModifiedDate FROM Account")
    assert vq.object_name == "Account"


def test_rejects_non_select():
    with pytest.raises(SoqlValidationError):
        validate_soql("SHOW TABLES")


def test_rejects_huge_offset():
    with pytest.raises(SoqlValidationError):
        validate_soql("SELECT Id FROM Account OFFSET 5000")


def test_custom_object():
    vq = validate_soql("select id from My_Object__c limit 5")
    assert vq.object_name == "My_Object__c"
    assert vq.limit == 5
