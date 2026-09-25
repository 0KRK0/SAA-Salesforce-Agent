import pytest

from app.tools.base import ToolContext, ToolValidationError
from app.tools.registry import build_registry


async def _noop_emit(_type, _data):
    return None


def make_ctx(user, db, connection, sf_client) -> ToolContext:
    return ToolContext(
        user=user,
        db=db,
        connection=connection,
        sf=sf_client,
        agent_run_id="run_test",
        conversation_id="conv_test",
        emit=_noop_emit,
    )


@pytest.fixture
def registry():
    return build_registry()


async def test_describe_object_returns_schema(registry, user, db, connection, sf_client):
    tool = registry.require("describe_object")
    ctx = make_ctx(user, db, connection, sf_client)
    result = await tool.execute(ctx, {"object": "Account"})
    assert result["success"] is True
    assert result["object"] == "Account"
    names = [f["name"] for f in result["fields"]]
    assert "Name" in names and "Industry" in names
    name_field = next(f for f in result["fields"] if f["name"] == "Name")
    assert name_field["required"] is True
    assert name_field["createable"] is True


async def test_describe_object_filters_fields(registry, user, db, connection, sf_client):
    tool = registry.require("describe_object")
    ctx = make_ctx(user, db, connection, sf_client)
    result = await tool.execute(ctx, {"object": "Account", "field_filter": "indus"})
    assert result["returned_field_count"] == 1
    assert result["fields"][0]["name"] == "Industry"


async def test_describe_object_unknown_object_suggests(registry, user, db, connection, sf_client):
    tool = registry.require("describe_object")
    ctx = make_ctx(user, db, connection, sf_client)
    result = await tool.execute(ctx, {"object": "Accounts"})
    assert result["success"] is False
    assert "Account" in result["similar_objects"]


async def test_query_validation_rejects_dml(registry, user, db, connection, sf_client):
    tool = registry.require("query_salesforce")
    ctx = make_ctx(user, db, connection, sf_client)
    with pytest.raises(ToolValidationError) as exc:
        await tool.validate(ctx, {"soql": "DELETE FROM Account"})
    assert exc.value.error_type == "SOQL_VALIDATION_ERROR"


async def test_query_returns_records_marked_untrusted(registry, user, db, connection, sf_client):
    tool = registry.require("query_salesforce")
    ctx = make_ctx(user, db, connection, sf_client)
    result = await tool.execute(ctx, {"soql": "SELECT Id, Name FROM Account"})
    assert result["success"] is True
    assert result["data_trust"] == "untrusted_external_data"
    assert all("attributes" not in r for r in result["records"])


async def test_create_record_rejects_unknown_field(registry, user, db, connection, sf_client):
    tool = registry.require("create_record")
    ctx = make_ctx(user, db, connection, sf_client)
    with pytest.raises(ToolValidationError) as exc:
        await tool.validate(ctx, {"object": "Account", "values": {"Nope__c": "x"}})
    assert exc.value.error_type == "INVALID_FIELD"


async def test_create_record_rejects_read_only_field(registry, user, db, connection, sf_client):
    tool = registry.require("create_record")
    ctx = make_ctx(user, db, connection, sf_client)
    with pytest.raises(ToolValidationError) as exc:
        await tool.validate(
            ctx, {"object": "Account", "values": {"Name": "X", "CreatedDate": "2026-01-01"}}
        )
    assert exc.value.error_type == "INVALID_FIELD_FOR_INSERT_UPDATE"


async def test_create_record_requires_required_fields(registry, user, db, connection, sf_client):
    tool = registry.require("create_record")
    ctx = make_ctx(user, db, connection, sf_client)
    with pytest.raises(ToolValidationError) as exc:
        await tool.validate(ctx, {"object": "Account", "values": {"Industry": "Banking"}})
    assert exc.value.error_type == "REQUIRED_FIELD_MISSING"
    assert "Name" in exc.value.missing


async def test_create_record_rejects_bad_picklist(registry, user, db, connection, sf_client):
    tool = registry.require("create_record")
    ctx = make_ctx(user, db, connection, sf_client)
    with pytest.raises(ToolValidationError) as exc:
        await tool.validate(ctx, {"object": "Account", "values": {"Name": "X", "Industry": "Fish"}})
    assert exc.value.error_type == "FIELD_INTEGRITY_EXCEPTION"


async def test_create_record_executes_and_verifies(
    registry, user, db, connection, sf_client, fake_sf
):
    tool = registry.require("create_record")
    ctx = make_ctx(user, db, connection, sf_client)
    args = {"object": "Account", "values": {"Name": "Acme Corporation"}}
    await tool.validate(ctx, args)
    result = await tool.execute(ctx, args)
    assert result["success"] is True
    verification = await tool.verify(ctx, args, result)
    assert verification["verified"] is True
    assert fake_sf.created[0][0] == "Account"


async def test_update_record_plan_shows_before_and_after(
    registry, user, db, connection, sf_client
):
    tool = registry.require("update_record")
    ctx = make_ctx(user, db, connection, sf_client)
    args = {
        "object": "Account",
        "record_id": "001000000000001AAA",
        "values": {"Name": "Acme Holdings"},
    }
    await tool.validate(ctx, args)
    plan = await tool.plan(ctx, args)
    detail = plan["details"][0]
    assert detail["old_value"] == "Acme Corporation"
    assert detail["new_value"] == "Acme Holdings"
    result = await tool.execute(ctx, args)
    assert result["success"] is True
    assert (await tool.verify(ctx, args, result))["verified"] is True


async def test_update_record_rejects_invalid_id(registry, user, db, connection, sf_client):
    tool = registry.require("update_record")
    ctx = make_ctx(user, db, connection, sf_client)
    with pytest.raises(ToolValidationError) as exc:
        await tool.validate(ctx, {"object": "Account", "record_id": "nope", "values": {"Name": "x"}})
    assert exc.value.error_type == "INVALID_ID"


async def test_create_field_refuses_existing_field(
    registry, user, db, connection, sf_client, fake_sf
):
    fake_sf.extra_fields.append(
        {
            "name": "Customer_Tier__c", "label": "Customer Tier", "type": "picklist",
            "nillable": True, "createable": True, "updateable": True, "custom": True,
        }
    )
    tool = registry.require("create_field")
    ctx = make_ctx(user, db, connection, sf_client)
    with pytest.raises(ToolValidationError) as exc:
        await tool.validate(
            ctx,
            {
                "object": "Account", "api_name": "Customer_Tier", "type": "Picklist",
                "picklist_values": ["Enterprise"],
            },
        )
    assert exc.value.error_type == "FIELD_ALREADY_EXISTS"


async def test_create_field_requires_picklist_values(registry, user, db, connection, sf_client):
    tool = registry.require("create_field")
    ctx = make_ctx(user, db, connection, sf_client)
    with pytest.raises(ToolValidationError) as exc:
        await tool.validate(ctx, {"object": "Account", "api_name": "Tier", "type": "Picklist"})
    assert exc.value.error_type == "METADATA_VALIDATION_ERROR"
    assert "picklist_values" in exc.value.missing


async def test_deploy_metadata_rejects_supplied_package_xml(
    registry, user, db, connection, sf_client
):
    tool = registry.require("deploy_metadata")
    ctx = make_ctx(user, db, connection, sf_client)
    with pytest.raises(ToolValidationError):
        await tool.validate(
            ctx,
            {
                "files": [{"path": "package.xml", "content": "<Package/>"}],
                "manifest": {"CustomField": ["Account.X__c"]},
            },
        )


async def test_registry_exposes_expected_tools(registry):
    for name in (
        "describe_object", "query_salesforce", "create_record", "update_record",
        "create_field", "deploy_metadata",
    ):
        assert registry.get(name) is not None
    schemas = registry.tool_schemas()
    assert all({"name", "description", "input_schema"} <= set(s) for s in schemas)
