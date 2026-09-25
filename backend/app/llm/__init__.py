"""Model-independent AI access.

`app.agent.runtime` calls `gateway.complete()` and never learns which vendor
answered. Provider selection, credentials, tiers, fallback and usage accounting
all live here; risk, approval and tenancy deliberately do not.
"""

from app.llm.base import (
    LLMError,
    LLMNotConfigured,
    LLMProvider,
    LLMResponse,
    ProviderConfig,
    ProviderNotImplemented,
)
from app.llm.catalog import build_provider, catalog, default_model, spec_for
from app.llm.gateway import NoModelAvailable, Route, complete, resolve_routes

__all__ = [
    "LLMError",
    "LLMNotConfigured",
    "LLMProvider",
    "LLMResponse",
    "NoModelAvailable",
    "ProviderConfig",
    "ProviderNotImplemented",
    "Route",
    "build_provider",
    "catalog",
    "complete",
    "default_model",
    "resolve_routes",
    "spec_for",
]
