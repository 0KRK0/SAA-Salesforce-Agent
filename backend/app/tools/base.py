"""Tool contract shared by every Salesforce capability exposed to the model."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import RiskLevel, SalesforceConnection, User
from app.salesforce.client import SalesforceClient


class ToolValidationError(ValueError):
    """Raised by a tool's validator: deterministic, pre-execution rejection."""

    def __init__(
        self,
        message: str,
        error_type: str = "VALIDATION_ERROR",
        suggested_action: str = "",
        missing: list[str] | None = None,
    ):
        super().__init__(message)
        self.message = message
        self.error_type = error_type
        self.suggested_action = suggested_action
        self.missing = missing or []

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": False,
            "error_type": self.error_type,
            "message": self.message,
            "retryable": False,
            "missing": self.missing,
            "suggested_action": self.suggested_action
            or "Ask the user for the missing information, or re-inspect the object.",
        }


EmitFn = Callable[[str, dict[str, Any]], Awaitable[None]]


@dataclass
class ToolContext:
    """Everything a tool may touch. Credentials live here, never in the model."""

    user: User
    db: AsyncSession
    connection: SalesforceConnection | None
    sf: SalesforceClient | None
    agent_run_id: str
    conversation_id: str
    emit: EmitFn
    # Security boundary. Every row a tool writes must carry it, and every read
    # must filter on it.
    project_id: str = ""
    # The owning company. Narrows to a customer; never an isolation boundary on
    # its own.
    company_id: str = ""
    # Resolved project policy (app.tenancy.policy.PolicySnapshot). Typed as Any
    # to keep the tool contract free of a tenancy import cycle.
    policy: Any = None
    settings_overrides: dict[str, Any] = field(default_factory=dict)

    @property
    def environment(self) -> Any:
        """Environment of the connected Salesforce org, or None if unconnected."""
        return getattr(self.connection, "environment", None)

    def require_sf(self) -> SalesforceClient:
        if self.sf is None or self.connection is None:
            raise ToolValidationError(
                "No Salesforce organization is connected to this conversation.",
                error_type="NO_SALESFORCE_CONNECTION",
                suggested_action=(
                    "Tell the user to connect a Salesforce org from the Connections "
                    "screen before Salesforce operations can run."
                ),
            )
        return self.sf


class ExecuteFn(Protocol):
    async def __call__(
        self, ctx: ToolContext, args: dict[str, Any]
    ) -> dict[str, Any]:  # pragma: no cover - protocol
        ...


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    risk: RiskLevel
    requires_approval: bool
    execute: ExecuteFn
    # Deterministic pre-flight validation (may raise ToolValidationError).
    validate: Callable[[ToolContext, dict[str, Any]], Awaitable[dict[str, Any]]] | None = None
    # Human-readable change plan for the approval card.
    plan: Callable[[ToolContext, dict[str, Any]], Awaitable[dict[str, Any]]] | None = None
    # Post-execution verification against real Salesforce state.
    verify: (
        Callable[[ToolContext, dict[str, Any], dict[str, Any]], Awaitable[dict[str, Any]]]
        | None
    ) = None
    # Snapshot of the org state this change was proposed against. Captured when
    # an approval is created and recomputed before execution: if it moved while
    # a human was deciding, the approval no longer authorizes the change.
    fingerprint: (
        Callable[[ToolContext, dict[str, Any]], Awaitable[dict[str, Any]]] | None
    ) = None
    mutating: bool = False
    audit_action: str = ""
    tags: list[str] = field(default_factory=list)
    # Which tool provider supplied this tool: 'native', 'mcp', or an external
    # system name. Surfaced to the UI so a human always knows what they are
    # approving.
    provider: str = "native"
    # Long-running tools (deployments, test runs, bulk jobs) use the long
    # timeout ceiling rather than the interactive one.
    long_running: bool = False

    def tool_schema(self) -> dict[str, Any]:
        """The declaration handed to the model.

        Named for what it is rather than for a vendor: this is the canonical
        shape, and each provider in `app/llm` translates it into its own.
        """
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description.strip().split("\n")[0],
            "risk": self.risk.value,
            "requires_approval": self.requires_approval,
            "mutating": self.mutating,
            "input_schema": self.input_schema,
            "output_schema": self.output_schema,
            "tags": self.tags,
            "provider": self.provider,
            "long_running": self.long_running,
        }


def ok(**payload: Any) -> dict[str, Any]:
    return {"success": True, **payload}


def fail(
    error_type: str,
    message: str,
    *,
    retryable: bool = False,
    suggested_action: str = "",
    **extra: Any,
) -> dict[str, Any]:
    return {
        "success": False,
        "error_type": error_type,
        "message": message,
        "retryable": retryable,
        "suggested_action": suggested_action,
        **extra,
    }
