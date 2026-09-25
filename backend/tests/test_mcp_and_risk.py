"""MCP as a tool provider, and the risk engine's newer rules.

The controlling idea in both: an MCP server is a transport, never a trust
boundary. A tool it supplies runs through exactly the same classification,
approval gate and audit trail as a first-party one, and it can never be
classified below the configured floor no matter how it describes itself.
"""

from __future__ import annotations

import pytest

from app.mcp.manager import (
    build_tools,
    infer_risk,
    looks_mutating,
    namespaced,
    split_namespaced,
)
from app.models import McpServerConfig, RiskLevel
from app.risk.engine import OrgContext, classify
from app.tenancy.policy import (
    CATEGORY_BULK,
    CATEGORY_SECURITY,
    PolicySnapshot,
)
from app.tools.registry import ToolRegistry


def _server(**overrides) -> McpServerConfig:
    fields = {
        "company_id": "co_1",
        "project_id": "prj_1",
        "name": "acme",
        "transport": "stdio",
        "command": "acme-mcp",
        "discovered_tools": [
            {"name": "search", "description": "Search records", "input_schema": {}},
            {"name": "delete_thing", "description": "Delete a thing", "input_schema": {}},
        ],
    }
    fields.update(overrides)
    return McpServerConfig(**fields)


# ---------------------------------------------------------------- namespacing
def test_mcp_tools_are_namespaced_so_they_cannot_shadow_native_ones():
    assert namespaced("acme", "query") == "mcp__acme__query"
    assert split_namespaced("mcp__acme__query") == ("acme", "query")
    assert split_namespaced("query_salesforce") is None


def test_a_native_tool_wins_a_name_collision():
    """An MCP server naming a tool `create_field` must not displace ours."""
    registry = ToolRegistry()
    tools = {t.name: t for t in build_tools(_server())}
    assert all(name.startswith("mcp__") for name in tools)
    registry.replace_provider("mcp:acme", tools.values())
    assert "create_field" not in registry.names()


# ------------------------------------------------------------ risk inference
def test_an_mcp_tool_never_sits_below_the_configured_floor():
    """A server can call its tool 'harmless'. That is a claim, not evidence."""
    assert infer_risk("search", "A totally harmless read-only search", None) in {
        RiskLevel.MEDIUM,
        RiskLevel.HIGH,
    }


def test_destructive_sounding_tools_are_raised_to_high():
    assert infer_risk("delete_thing", "Deletes a thing", None) is RiskLevel.HIGH
    assert infer_risk("purge_all", "", None) is RiskLevel.HIGH
    assert infer_risk("revoke_access", "", None) is RiskLevel.HIGH


def test_an_admin_override_is_respected_because_a_human_looked_at_it():
    assert infer_risk("search", "", "LOW") is RiskLevel.LOW


def test_a_nonsense_override_falls_back_to_the_floor_rather_than_failing_open():
    assert infer_risk("search", "", "TOTALLY_SAFE") is not RiskLevel.LOW


def test_mutation_is_inferred_conservatively_from_the_verb():
    assert looks_mutating("create_issue", "") is True
    assert looks_mutating("send_message", "") is True
    assert looks_mutating("get_status", "Returns the status") is False


# ------------------------------------------------------------- tool building
def test_built_tools_carry_the_provider_and_require_approval():
    tools = {t.name: t for t in build_tools(_server())}
    delete = tools["mcp__acme__delete_thing"]
    assert delete.provider == "mcp:acme"
    assert delete.risk is RiskLevel.HIGH
    assert delete.requires_approval is True
    assert delete.mutating is True
    assert "mcp" in delete.tags and "external" in delete.tags


def test_the_tool_description_tells_the_model_where_it_came_from():
    tools = {t.name: t for t in build_tools(_server())}
    description = tools["mcp__acme__search"].description
    assert "external MCP server 'acme'" in description
    assert "untrusted external data" in description
    assert "Prefer a native Salesforce tool" in description


def test_an_allowlist_hides_everything_not_on_it():
    tools = build_tools(_server(tool_allowlist=["search"]))
    assert [t.name for t in tools] == ["mcp__acme__search"]


def test_a_server_with_no_discovered_tools_contributes_nothing():
    assert build_tools(_server(discovered_tools=None)) == []


def test_replace_provider_removes_stale_tools_on_rediscovery():
    registry = ToolRegistry()
    registry.replace_provider("mcp:acme", build_tools(_server()))
    assert len(registry.by_provider("mcp:acme")) == 2

    registry.replace_provider(
        "mcp:acme",
        build_tools(
            _server(
                discovered_tools=[
                    {"name": "search", "description": "Search", "input_schema": {}}
                ]
            )
        ),
    )
    names = [t.name for t in registry.by_provider("mcp:acme")]
    assert names == ["mcp__acme__search"]


def test_native_tools_can_never_be_swapped_at_runtime():
    registry = ToolRegistry()
    with pytest.raises(ValueError):
        registry.replace_provider("native", [])


# ---------------------------------------------------------------- risk engine
SANDBOX = OrgContext(is_sandbox=True)
PRODUCTION = OrgContext(is_sandbox=False)
POLICY = PolicySnapshot(project_id="ten_1", max_bulk_records=1000)


def test_an_mcp_tool_is_floored_even_when_declared_low():
    decision = classify(
        "mcp__acme__search",
        {},
        RiskLevel.LOW,
        SANDBOX,
        False,
        tags=["mcp", "external"],
        policy=POLICY,
        provider="mcp:acme",
    )
    assert decision.risk is not RiskLevel.LOW
    assert any("describes its own capabilities" in r for r in decision.reasons)


def test_a_first_party_integration_is_not_floored_like_an_mcp_server():
    """Jira and GitHub tools are implemented in this repository: their declared
    risk is evidence, not a claim. Flooring a ticket read at MEDIUM would put an
    approval in front of reading, which teaches people to approve without
    reading."""
    decision = classify(
        "jira_search",
        {"jql": "project = SF"},
        RiskLevel.LOW,
        SANDBOX,
        False,
        tags=["jira", "external", "read"],
        policy=POLICY,
        provider="jira",
    )
    assert decision.risk is RiskLevel.LOW
    assert decision.requires_approval is False


def test_a_first_party_write_still_requires_approval():
    decision = classify(
        "jira_comment",
        {"issue_key": "SF-1"},
        RiskLevel.MEDIUM,
        SANDBOX,
        True,
        tags=["jira", "external", "write"],
        policy=POLICY,
        provider="jira",
    )
    assert decision.requires_approval is True


def test_a_native_low_risk_tool_stays_low():
    decision = classify(
        "describe_object", {"object": "Account"}, RiskLevel.LOW, SANDBOX, False,
        tags=["read"], policy=POLICY,
    )
    assert decision.risk is RiskLevel.LOW
    assert decision.requires_approval is False


def test_a_tenant_can_disable_a_tool_outright():
    policy = PolicySnapshot(project_id="ten_1", disabled_tools=frozenset({"bulk_update"}))
    decision = classify(
        "bulk_update", {}, RiskLevel.HIGH, SANDBOX, True, tags=["bulk"], policy=policy
    )
    assert decision.blocked is True
    assert "disabled" in decision.blocked_reason


def test_security_metadata_in_a_deployment_escalates_to_a_security_change():
    decision = classify(
        "deploy_metadata",
        {"types": {"PermissionSet": ["Admin_Extras"]}},
        RiskLevel.MEDIUM,
        SANDBOX,
        True,
        tags=["metadata", "deploy"],
        policy=POLICY,
    )
    assert decision.risk is RiskLevel.HIGH
    assert decision.category == CATEGORY_SECURITY
    assert decision.approvals_required == 2


def test_a_bulk_operation_above_the_tenant_limit_is_blocked_outright():
    decision = classify(
        "bulk_update",
        {"object": "Account", "estimated_records": 5000},
        RiskLevel.HIGH,
        SANDBOX,
        True,
        tags=["bulk", "data"],
        policy=POLICY,
    )
    assert decision.blocked is True
    assert "5,000" in decision.blocked_reason or "5000" in decision.blocked_reason


def test_a_large_but_permitted_bulk_operation_needs_approval():
    decision = classify(
        "bulk_update",
        {"object": "Account", "estimated_records": 900},
        RiskLevel.MEDIUM,
        SANDBOX,
        True,
        tags=["bulk", "data"],
        policy=POLICY,
    )
    assert decision.blocked is False
    assert decision.risk is RiskLevel.HIGH
    assert decision.category == CATEGORY_BULK
    assert decision.requires_approval is True


def test_deletion_is_always_high_risk():
    decision = classify(
        "delete_flow", {"api_name": "X"}, RiskLevel.MEDIUM, SANDBOX, True,
        tags=["flow"], policy=POLICY,
    )
    assert decision.risk is RiskLevel.HIGH
    assert any("irreversible" in r for r in decision.reasons)


def test_production_metadata_changes_are_blocked_unless_explicitly_allowed():
    for tool in ("create_flow", "write_apex", "deploy_change_set", "modify_permissions"):
        decision = classify(
            tool, {"object": "Account"}, RiskLevel.MEDIUM, PRODUCTION, True,
            tags=["metadata"], policy=POLICY,
        )
        assert decision.blocked is True, tool


def test_production_changes_are_permitted_when_policy_allows_them():
    policy = PolicySnapshot(project_id="ten_1", allow_production_mutations=True)
    decision = classify(
        "create_flow", {"object": "Account"}, RiskLevel.MEDIUM, PRODUCTION, True,
        tags=["flow", "metadata"], policy=policy,
    )
    assert decision.blocked is False
    assert decision.risk is RiskLevel.HIGH  # still escalated for being production
    assert decision.requires_approval is True


def test_approval_requirements_are_attached_to_the_decision():
    decision = classify(
        "create_field", {"object": "Account"}, RiskLevel.MEDIUM, SANDBOX, True,
        tags=["metadata"], policy=POLICY,
    )
    assert decision.requires_approval is True
    assert decision.approvals_required >= 1
    assert decision.eligible_roles
    assert decision.ttl_seconds > 0


def test_touching_a_security_object_escalates_regardless_of_the_tool():
    decision = classify(
        "update_record",
        {"object": "PermissionSetAssignment", "record_id": "0Pa"},
        RiskLevel.MEDIUM,
        SANDBOX,
        True,
        tags=["data", "write"],
        policy=POLICY,
    )
    assert decision.risk is RiskLevel.HIGH
    assert decision.category == CATEGORY_SECURITY


def test_a_wide_record_id_list_is_treated_as_bulk():
    decision = classify(
        "update_record",
        {"object": "Account", "record_ids": [f"001{i}" for i in range(50)]},
        RiskLevel.MEDIUM,
        SANDBOX,
        True,
        tags=["data"],
        policy=POLICY,
    )
    assert decision.risk is RiskLevel.HIGH
    assert decision.category == CATEGORY_BULK
