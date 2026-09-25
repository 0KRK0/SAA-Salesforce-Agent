"""Central tool registry.

New Salesforce (or external-system) capabilities are added by registering a
Tool here; the agent runtime does not change.
"""

from __future__ import annotations

from collections.abc import Iterable

from app.tools.base import Tool


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> Tool:
        if tool.name in self._tools:
            raise ValueError(f"Tool '{tool.name}' is already registered")
        self._tools[tool.name] = tool
        return tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def require(self, name: str) -> Tool:
        tool = self._tools.get(name)
        if tool is None:
            raise KeyError(f"Unknown tool '{name}'")
        return tool

    def all(self) -> list[Tool]:
        return list(self._tools.values())

    def names(self) -> list[str]:
        return sorted(self._tools)

    def tool_schemas(
        self,
        only: Iterable[str] | None = None,
        exclude: Iterable[str] | None = None,
    ) -> list[dict]:
        """Schemas offered to the model.

        `exclude` is how a tenant's disabled-tool list is enforced: a disabled
        tool is never described to the model, and the risk engine refuses it even
        if the model names it anyway.
        """
        allowed = set(only) if only else None
        denied = set(exclude or ())
        return [
            t.tool_schema()
            for t in self._tools.values()
            if (allowed is None or t.name in allowed) and t.name not in denied
        ]

    def catalog(self) -> list[dict]:
        return [t.describe() for t in self._tools.values()]

    def by_provider(self, provider: str) -> list[Tool]:
        return [t for t in self._tools.values() if t.provider == provider]

    def unregister(self, name: str) -> None:
        """Remove a tool. Only dynamic providers (MCP) ever need this."""
        self._tools.pop(name, None)

    def replace_provider(self, provider: str, tools: Iterable[Tool]) -> list[Tool]:
        """Atomically swap every tool contributed by one dynamic provider.

        Native tools are registered once at import time and are never touched
        by this; it exists so an MCP server's tool list can be re-discovered
        without leaving stale tools callable.
        """
        if provider == "native":
            raise ValueError("Native tools cannot be replaced at runtime.")
        for name in [t.name for t in self.by_provider(provider)]:
            self._tools.pop(name, None)
        registered = []
        for tool in tools:
            self._tools[tool.name] = tool
            registered.append(tool)
        return registered


registry = ToolRegistry()


#: Native tool modules. Registration happens on import; adding a capability
#: means adding a module here, never editing the agent runtime.
NATIVE_TOOL_MODULES = (
    "describe_object",
    "query_salesforce",
    "create_record",
    "update_record",
    "create_field",
    "deploy_metadata",
    "flow_tools",
    "apex_tools",
    "data_quality_tools",
    "dependency_tools",
    "debug_tools",
    "report_tools",
    "permission_tools",
    "deployment_tools",
    "knowledge_tools",
    "jira_tools",
    "repository_tools",
    "source_tools",
)


def build_registry() -> ToolRegistry:
    """Import side-effect registration exactly once."""
    if registry.all():
        return registry
    import importlib

    for module in NATIVE_TOOL_MODULES:
        importlib.import_module(f"app.tools.{module}")
    return registry
