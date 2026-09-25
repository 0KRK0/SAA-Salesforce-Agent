"""Environment-driven application configuration.

No secret is ever hardcoded. Everything comes from the environment (or a
local .env file, which must never be committed).
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Both locations are read so the same file works either way: docker-compose
    # passes the repo-root `.env`, while `uvicorn` run from `backend/` would
    # otherwise only see `backend/.env`. Later files win, so a `backend/.env`
    # overrides the root one when both exist.
    #
    # Real environment variables always take precedence over both, which is how
    # a container or a secret manager is expected to supply production values.
    model_config = SettingsConfigDict(
        env_file=("../.env", ".env"), env_file_encoding="utf-8", extra="ignore"
    )

    # --- App ---
    app_name: str = "Salesforce AI Platform"
    environment: Literal["local", "dev", "staging", "production"] = "local"
    log_level: str = "INFO"
    api_prefix: str = "/api"
    api_version: str = "v1"
    cors_origins: str = "http://localhost:3000"

    # --- Database ---
    database_url: str = "sqlite+aiosqlite:///./sfagent.db"

    # --- Session / auth ---
    session_secret: str = "dev-only-insecure-session-secret-change-me"
    session_ttl_seconds: int = 60 * 60 * 12

    # --- Secret storage ---
    # Nothing stores a credential in a column; every credential becomes an
    # opaque reference resolved by app/security/secrets.py.
    #   local    — Fernet, keyed by ENCRYPTION_KEY. Real, and honest about the
    #              key living in the application environment.
    #   aws-kms  — envelope encryption; the key never enters the application.
    # vault / azure-key-vault / gcp-kms are NOT IMPLEMENTED and fail at startup
    # rather than silently degrading.
    secret_backend: str = "local"
    secret_kms_key_id: str = ""
    secret_kms_region: str = ""
    # Generate with `make key`.
    encryption_key: str = ""

    # --- AI providers -------------------------------------------------------
    # The platform is model-independent: these are the *deployment-managed*
    # fallbacks used when a project has not brought its own key. A project's own
    # BYOK credential always takes precedence.
    default_llm_provider: str = "ANTHROPIC"
    llm_max_tokens: int = 4096
    llm_temperature: float | None = None
    llm_timeout_seconds: float = 120.0
    llm_max_retries: int = 3
    # Models used for each tier when a credential does not name its own.
    llm_fast_model: str = ""
    llm_balanced_model: str = ""
    llm_advanced_model: str = ""

    # Deployment-managed provider credentials (optional; BYOK is preferred).
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    openai_base_url: str = ""
    azure_openai_api_key: str = ""
    azure_openai_endpoint: str = ""
    azure_openai_api_version: str = "2024-10-21"
    google_api_key: str = ""
    mistral_api_key: str = ""
    groq_api_key: str = ""
    deepseek_api_key: str = ""
    together_api_key: str = ""
    ollama_base_url: str = "http://localhost:11434"
    aws_region: str = ""

    # Back-compatible alias: existing deployments set CLAUDE_MODEL.
    claude_model: str = "claude-sonnet-4-5"
    claude_max_tokens: int = 4096
    claude_temperature: float | None = None
    claude_timeout_seconds: float = 120.0
    claude_max_retries: int = 3

    # --- Agent runtime ---
    max_agent_steps: int = 20
    max_tool_result_chars: int = 20_000
    max_query_rows: int = 500
    default_query_limit: int = 200
    tool_timeout_seconds: float = 90.0
    # Long-running subsystems (deployments, Apex test runs, bulk jobs) get their
    # own ceiling; the per-tool timeout above is for interactive calls.
    long_tool_timeout_seconds: float = 900.0

    # --- Tenancy & approval policy (deployment-wide ceilings) ---
    # A tenant policy may be stricter than these, never looser.
    approval_ttl_seconds: int = 3600
    require_separate_approver: bool = False
    max_bulk_records: int = 50_000
    max_execution_seconds: int = 1800
    max_tool_calls: int = 120
    # Rows a data-quality scan may pull into the analyzer (not into the model).
    max_analysis_records: int = 20_000

    # --- MCP tool providers ---
    mcp_enabled: bool = True
    mcp_connect_timeout_seconds: float = 20.0
    mcp_call_timeout_seconds: float = 120.0
    # MCP tools are third-party code paths: they are never auto-approved above
    # this risk floor regardless of what the server advertises about itself.
    mcp_default_risk: Literal["LOW", "MEDIUM", "HIGH"] = "MEDIUM"

    # --- Salesforce OAuth ---
    #
    # These are the DEPLOYMENT-MANAGED fallback app: one External Client App,
    # used by any company that has not registered its own. A company's own app,
    # configured in the UI and held in the secret store, always takes
    # precedence — see app/salesforce/apps.py.
    #
    # Set FEATURE_CUSTOMER_SALESFORCE_APPS=false to require every company to
    # bring its own, which is what a security team that will not authorise a
    # third-party app in their org asks for.
    salesforce_client_id: str = ""
    salesforce_client_secret: str = ""
    #: Where this backend is reachable from a browser. The Salesforce callback
    #: URL is derived from it rather than typed out, because a callback that
    #: does not match a mounted route fails only at the very end of a
    #: successful login, with a 404 and no explanation.
    public_base_url: str = "http://localhost:8000"
    #: Legacy override. Leave empty to derive the callback from
    #: `public_base_url` + the real route. If set, it is validated at startup
    #: against the route that actually exists.
    salesforce_redirect_uri: str = ""
    salesforce_login_url: str = "https://login.salesforce.com"
    salesforce_api_version: str = "62.0"
    salesforce_scopes: str = "api refresh_token offline_access"
    # Refuse metadata mutations against non-sandbox orgs unless explicitly allowed.
    allow_production_mutations: bool = False

    # --- Enterprise identity (Phase L) ---
    # The local signed-session provider is always available. Setting an OIDC
    # issuer switches new logins to that provider; see app/security/identity.py
    # for exactly which parts are implemented.
    oidc_issuer: str = ""
    oidc_client_id: str = ""
    oidc_client_secret: str = ""
    oidc_redirect_uri: str = "http://localhost:8000/api/v1/auth/oidc/callback"
    oidc_scopes: str = "openid email profile"
    # Shared secret for SCIM provisioning calls. Empty disables the endpoints.
    scim_bearer_token: str = ""

    # --- Execution workers --------------------------------------------------
    # Runs are owned by the server, never by a browser connection.
    run_worker_enabled: bool = True
    run_worker_concurrency: int = 4
    run_worker_poll_seconds: float = 1.0
    # A run whose worker stops heartbeating for this long is reclaimable.
    run_heartbeat_timeout_seconds: int = 120
    # Identifies this process when it claims a run.
    worker_id: str = ""

    # --- Rate limiting ------------------------------------------------------
    rate_limit_enabled: bool = True
    rate_limit_login_per_minute: int = 10
    rate_limit_api_per_minute: int = 300
    rate_limit_run_start_per_minute: int = 20
    rate_limit_upload_per_minute: int = 20

    # --- Uploads ------------------------------------------------------------
    # Documents are processed and discarded unless a project retains them.
    max_upload_bytes: int = 20 * 1024 * 1024
    allowed_upload_types: str = (
        "application/pdf,text/plain,text/csv,text/markdown,application/json,"
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document,"
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )

    # --- Integrations (optional; each is a real OAuth app when configured) ---
    jira_client_id: str = ""
    jira_client_secret: str = ""
    jira_redirect_uri: str = "http://localhost:8000/api/v1/integrations/jira/callback"
    github_client_id: str = ""
    github_client_secret: str = ""
    github_redirect_uri: str = "http://localhost:8000/api/v1/integrations/github/callback"
    bitbucket_client_id: str = ""
    bitbucket_client_secret: str = ""
    bitbucket_redirect_uri: str = (
        "http://localhost:8000/api/v1/integrations/bitbucket/callback"
    )

    # --- Feature flags ------------------------------------------------------
    # A feature that is off is not advertised as available anywhere in the UI.
    feature_saml: bool = False
    feature_scim: bool = True
    feature_jira: bool = True
    feature_github: bool = True
    feature_bitbucket: bool = True
    feature_mcp: bool = True
    feature_production_deployment: bool = False
    feature_platform_managed_ai: bool = False
    feature_billing: bool = False
    #: Let a company register its own Salesforce External Client App instead of
    #: using this deployment's. On by default: an enterprise security team that
    #: will not authorise a third-party app in their org has no other way in,
    #: and one shared app across every tenant is a single revocation away from
    #: taking every customer offline at once.
    feature_customer_salesforce_apps: bool = True
    #: Require it. With no deployment-managed app configured this is already
    #: effectively true; setting it explicitly stops a shared app from being
    #: used by a company that was supposed to bring its own.
    feature_require_customer_salesforce_app: bool = False

    # --- Frontend ---
    frontend_base_url: str = "http://localhost:3000"

    @field_validator("cors_origins")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def sf_scope_list(self) -> list[str]:
        return [s.strip() for s in self.salesforce_scopes.split() if s.strip()]

    @property
    def claude_configured(self) -> bool:
        """Deprecated alias. The platform is not Anthropic-specific."""
        return bool(self.anthropic_api_key)

    @property
    def deployment_llm_providers(self) -> list[str]:
        """Providers this deployment holds its own key for.

        Having a key is not the same as using it: a project's own credential
        always wins, and these are reachable only when
        FEATURE_PLATFORM_MANAGED_AI is on.
        """
        configured = {
            "ANTHROPIC": self.anthropic_api_key,
            "OPENAI": self.openai_api_key,
            "AZURE_OPENAI": self.azure_openai_api_key and self.azure_openai_endpoint,
            "GOOGLE": self.google_api_key,
            "MISTRAL": self.mistral_api_key,
            "GROQ": self.groq_api_key,
            "DEEPSEEK": self.deepseek_api_key,
            "TOGETHER": self.together_api_key,
        }
        return sorted(name for name, value in configured.items() if value)

    @property
    def ai_available_without_byok(self) -> bool:
        """Whether a project can run the agent without bringing its own key."""
        return bool(self.feature_platform_managed_ai and self.deployment_llm_providers)

    @property
    def salesforce_callback_path(self) -> str:
        """The one path the OAuth callback is actually mounted at."""
        return f"{self.api_v1}/salesforce/oauth/callback"

    @property
    def salesforce_callback_url(self) -> str:
        """The callback URL to register in an External Client App.

        Derived, not typed. A hand-written `SALESFORCE_REDIRECT_URI` that omits
        the `/v1` segment sends the user all the way through a *successful*
        Salesforce login and then lands them on a 404 holding a valid
        authorization code — a failure that looks like Salesforce's fault and
        is entirely ours.
        """
        if self.salesforce_redirect_uri:
            return self.salesforce_redirect_uri
        return f"{self.public_base_url.rstrip('/')}{self.salesforce_callback_path}"

    @property
    def salesforce_redirect_uri_problem(self) -> str:
        """Why the configured callback cannot work, or an empty string.

        Checked at startup, reported by `/health`, and refused by the OAuth
        start endpoint — so the misconfiguration surfaces before a person
        spends a round-trip through Salesforce discovering it.
        """
        configured = self.salesforce_redirect_uri
        if not configured:
            return ""
        from urllib.parse import urlparse

        path = urlparse(configured).path.rstrip("/")
        expected = self.salesforce_callback_path.rstrip("/")
        if path != expected:
            return (
                f"SALESFORCE_REDIRECT_URI points at '{path or '/'}', but the callback "
                f"is served at '{expected}'. Salesforce would complete the login and "
                f"redirect to a route that does not exist. Set it to "
                f"'{self.public_base_url.rstrip('/')}{expected}' — or remove it and "
                f"let PUBLIC_BASE_URL derive it — and register that exact URL in the "
                f"External Client App."
            )
        return ""

    @property
    def salesforce_configured(self) -> bool:
        """Whether the *deployment* has a fallback app. Not the only way in.

        A company that registered its own Salesforce app can connect an org
        even when this is false, so this must never be used to decide whether
        the Salesforce feature is available — only whether the shared fallback
        exists.
        """
        return bool(self.salesforce_client_id) and not self.salesforce_redirect_uri_problem

    @property
    def oidc_configured(self) -> bool:
        return bool(self.oidc_issuer and self.oidc_client_id and self.oidc_client_secret)

    @property
    def scim_configured(self) -> bool:
        """Whether the deployment-wide SCIM token is set.

        Per-company SCIM tokens work independently of this; see
        app/api/routes_scim.py.
        """
        return bool(self.scim_bearer_token)

    @property
    def oidc_scope_list(self) -> list[str]:
        return [s.strip() for s in self.oidc_scopes.split() if s.strip()]

    @property
    def api_v1(self) -> str:
        return f"{self.api_prefix}/{self.api_version}"

    @property
    def upload_type_list(self) -> list[str]:
        return [t.strip() for t in self.allowed_upload_types.split(",") if t.strip()]

    @property
    def secrets_configured(self) -> bool:
        if self.secret_backend == "local":
            return bool(self.encryption_key)
        return bool(self.secret_kms_key_id)

    def feature(self, name: str) -> bool:
        return bool(getattr(self, f"feature_{name}", False))

    @property
    def feature_flags(self) -> dict[str, bool]:
        return {
            key[len("feature_") :]: bool(value)
            for key, value in self.model_dump().items()
            if key.startswith("feature_")
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
