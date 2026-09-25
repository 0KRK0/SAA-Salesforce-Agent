import type {
  Approval,
  AuditEntry,
  ChangeSetSummary,
  ChatMessage,
  CompanySummary,
  Connection,
  Conversation,
  DataJobSummary,
  Environment,
  KnowledgeEntry,
  LLMCredential,
  ModelTier,
  ProjectSummary,
  ProductionPosture,
  ProviderSpec,
  RoutingInfo,
  RunDetail,
  RunSummary,
  SalesforceApp,
  SalesforceAppInput,
  SSOConfig,
  UsageReport,
  ToolCatalogEntry,
  UserOut,
} from "./types";

export const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8000";

/**
 * Versioned API prefix. Every call goes through it so a future v2 is a one-line
 * change here rather than a hunt through the components.
 */
export const API_V1 = "/api/v1";

export class ApiError extends Error {
  status: number;
  detail: unknown;
  constructor(status: number, detail: unknown, message: string) {
    super(message);
    this.status = status;
    this.detail = detail;
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    ...init,
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      ...(init.headers ?? {}),
    },
  });
  if (!res.ok) {
    let detail: unknown = null;
    try {
      detail = await res.json();
    } catch {
      detail = await res.text();
    }
    const message =
      typeof detail === "object" && detail && "detail" in detail
        ? String((detail as { detail: unknown }).detail)
        : `Request failed (${res.status})`;
    throw new ApiError(res.status, detail, message);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

/** Convenience for building a versioned path. */
const v1 = (path: string) => `${API_V1}${path}`;

export const api = {
  health: () =>
    request<{
      status: string;
      tools: string[];
      model: string;
      features: Record<string, boolean>;
      secrets_configured: boolean;
    }>("/health"),

  me: () => request<UserOut>(v1("/auth/me")),
  login: (email: string, display_name = "") =>
    request<UserOut>(v1("/auth/login"), {
      method: "POST",
      body: JSON.stringify({ email, display_name }),
    }),
  logout: () =>
    request<{ success: boolean }>(v1("/auth/logout"), { method: "POST" }),

  salesforceConfig: () =>
    request<{
      configured: boolean;
      setup_hint: string | null;
      api_version: string;
      environments: Environment[];
      callback_url: string;
      callback_problem: string | null;
      using_deployment_app: boolean;
      customer_apps_enabled: boolean;
      customer_app_required: boolean;
      app: {
        id: string;
        name: string;
        client_id: string;
        has_client_secret: boolean;
        login_url: string;
        scope: string;
      } | null;
    }>(v1("/salesforce/config")),

  /**
   * A company's own Salesforce External Client App.
   *
   * The consumer secret is write-only here as it is on the server: it is sent
   * once and never comes back. The list returns a fingerprint so an admin can
   * confirm a rotation took effect without ever seeing the credential.
   */
  salesforceApps: () =>
    request<{
      apps: SalesforceApp[];
      enabled: boolean;
      required: boolean;
      deployment_app_available: boolean;
      callback_url: string;
      required_scopes: string[];
      instructions: string;
    }>(v1("/salesforce/apps")),
  createSalesforceApp: (body: SalesforceAppInput) =>
    request<SalesforceApp>(v1("/salesforce/apps"), {
      method: "POST",
      body: JSON.stringify(body),
    }),
  updateSalesforceApp: (id: string, body: SalesforceAppInput) =>
    request<SalesforceApp>(v1(`/salesforce/apps/${id}`), {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
  deleteSalesforceApp: (id: string) =>
    request<{ success: boolean; connections_affected: number; message: string }>(
      v1(`/salesforce/apps/${id}`),
      { method: "DELETE" },
    ),
  connections: () => request<Connection[]>(v1("/salesforce/connections")),
  startOAuth: (sandbox: boolean, environment?: Environment, label = "") => {
    const params = new URLSearchParams({ sandbox: String(sandbox) });
    if (environment) params.set("environment", environment);
    if (label) params.set("label", label);
    return request<{ authorize_url: string }>(
      v1(`/salesforce/oauth/start?${params.toString()}`),
      { method: "POST" },
    );
  },
  validateConnection: (id: string) =>
    request<{ success: boolean; org: Record<string, unknown> }>(
      v1(`/salesforce/connections/${id}/validate`),
      { method: "POST" },
    ),
  updateConnection: (
    id: string,
    patch: { label?: string; environment?: Environment },
  ) =>
    request<Connection>(v1(`/salesforce/connections/${id}`), {
      method: "PATCH",
      body: JSON.stringify(patch),
    }),
  disconnect: (id: string) =>
    request<{ success: boolean }>(v1(`/salesforce/connections/${id}`), {
      method: "DELETE",
    }),

  conversations: () => request<Conversation[]>(v1("/conversations")),
  createConversation: (salesforce_connection_id: string | null) =>
    request<Conversation>(v1("/conversations"), {
      method: "POST",
      body: JSON.stringify({
        title: "New conversation",
        salesforce_connection_id,
      }),
    }),
  conversation: (id: string) =>
    request<{ conversation: Conversation; messages: ChatMessage[] }>(
      v1(`/conversations/${id}`),
    ),
  setConversationOrg: (id: string, salesforce_connection_id: string) =>
    request<Conversation>(v1(`/conversations/${id}`), {
      method: "PATCH",
      body: JSON.stringify({ salesforce_connection_id }),
    }),
  deleteConversation: (id: string) =>
    request<{ success: boolean }>(v1(`/conversations/${id}`), {
      method: "DELETE",
    }),

  /**
   * Queue a run. Returns immediately with a run id — the work happens on the
   * server, and following it is a separate call.
   */
  sendMessage: (conversationId: string, message: string, tier: ModelTier = "BALANCED") =>
    request<{
      run_id: string;
      conversation_id: string;
      state: string;
      events_url: string;
    }>(v1(`/conversations/${conversationId}/messages`), {
      method: "POST",
      body: JSON.stringify({ message, tier }),
    }),
  resumeRun: (runId: string) =>
    request<{ run_id: string; state: string }>(v1(`/runs/${runId}/resume`), {
      method: "POST",
    }),
  /** Cooperative: the run stops at its next step boundary, never mid-call. */
  cancelRun: (runId: string) =>
    request<{ success: boolean; state: string; message: string }>(
      v1(`/runs/${runId}/cancel`),
      { method: "POST" },
    ),
  run: (runId: string) => request<RunDetail>(v1(`/runs/${runId}`)),
  runTimeline: (runId: string, after = 0) =>
    request<{
      run_id: string;
      state: string;
      terminal: boolean;
      count: number;
      last_sequence: number;
      events: Array<{ sequence: number; type: string; data: Record<string, unknown> }>;
    }>(v1(`/runs/${runId}/timeline?after=${after}`)),
  /** Which runs in a conversation are still live — how a returning tab catches up. */
  conversationRuns: (conversationId: string) =>
    request<{ count: number; active: string[]; runs: RunSummary[] }>(
      v1(`/conversations/${conversationId}/runs`),
    ),

  approvals: (conversationId?: string) =>
    request<{ approvals: Approval[] }>(
      v1(
        `/approvals${
          conversationId ? `?conversation_id=${conversationId}` : ""
        }`,
      ),
    ),
  decide: (
    id: string,
    decision: "approve" | "reject",
    note?: string,
    modified_arguments?: Record<string, unknown>,
  ) =>
    request<{ resume_ready: boolean; run_id: string }>(
      v1(`/approvals/${id}/decision`),
      {
        method: "POST",
        body: JSON.stringify({ decision, note, modified_arguments }),
      },
    ),

  // --- tenancy -------------------------------------------------------------
  projects: () =>
    request<{ projects: ProjectSummary[] }>(v1("/auth/projects")),
  companies: () =>
    request<{ companies: CompanySummary[] }>(v1("/auth/companies")),
  switchProject: (project_id: string) =>
    request<UserOut>(v1("/auth/projects/switch"), {
      method: "POST",
      body: JSON.stringify({ project_id }),
    }),
  createProject: (name: string, description = "") =>
    request<ProjectSummary>(v1("/auth/projects"), {
      method: "POST",
      body: JSON.stringify({ name, description }),
    }),
  members: () =>
    request<{ members: Array<Record<string, unknown>> }>(
      v1("/auth/projects/members"),
    ),
  invite: (email: string, role: string, ttl_hours = 168) =>
    request<{
      id: string;
      email: string;
      role: string;
      expires_at: string;
      /** Returned exactly once; never retrievable from the API again. */
      invite_url: string;
    }>(v1("/auth/invitations"), {
      method: "POST",
      body: JSON.stringify({ email, role, ttl_hours }),
    }),

  // --- enterprise identity -------------------------------------------------
  /** Sign out everywhere. Invalidates tokens already in other browsers. */
  revokeMySessions: () =>
    request<{ success: boolean; message: string }>(v1("/auth/sessions/revoke"), {
      method: "POST",
    }),
  revokeMemberSessions: (userId: string) =>
    request<{ success: boolean; message: string }>(
      v1(`/auth/projects/members/${userId}/revoke-sessions`),
      { method: "POST" },
    ),
  sso: () =>
    request<{
      configurations: SSOConfig[];
      supported: Record<string, boolean>;
      saml_note: string;
      assignable_roles: string[];
      projects: Array<{ id: string; name: string }>;
      group_claims_read: string[];
    }>(v1("/sso")),
  updateSso: (payload: Record<string, unknown>) =>
    request<SSOConfig>(v1("/sso"), { method: "PUT", body: JSON.stringify(payload) }),
  /** Returns the SCIM token once. It cannot be read back afterwards. */
  rotateScimToken: () =>
    request<{ success: boolean; token: string; message: string }>(
      v1("/sso/scim-token"),
      { method: "POST" },
    ),
  /** What a set of IdP groups would grant, without signing anyone in. */
  previewMapping: (groups: string[]) =>
    request<{
      matched_groups: string[];
      unmatched_groups: string[];
      company_role: string | null;
      fell_back_to_default: boolean;
      explained: Array<{ project_id: string; project_name: string; role: string }>;
      note: string;
    }>(v1("/sso/preview"), { method: "POST", body: JSON.stringify(groups) }),

  // --- project settings ----------------------------------------------------
  policy: () =>
    request<{
      stored: Record<string, unknown>;
      effective: Record<string, unknown>;
      deployment_ceilings: Record<string, unknown>;
      project: Record<string, unknown>;
      company: {
        id: string;
        name: string;
        allow_production_mutations: boolean;
        you_can_change_it: boolean;
      };
      /** Which of the three levels is refusing, and how each is changed. */
      production: ProductionPosture;
      environments: Environment[];
      categories: string[];
    }>(v1("/project/policy")),
  updatePolicy: (patch: Record<string, unknown>) =>
    request<Record<string, unknown>>(v1("/project/policy"), {
      method: "PATCH",
      body: JSON.stringify(patch),
    }),

  /**
   * Company-level production clearance — the level between the deployment
   * ceiling and a project's own setting. Company administrators only.
   */
  productionAccess: () =>
    request<{
      company_id: string;
      company_name: string;
      allow_production_mutations: boolean;
      you_can_change_it: boolean;
      deployment_permits_it: boolean;
      confirmation_phrase: string;
      note: string;
    }>(v1("/project/production-access")),
  setProductionAccess: (allow: boolean, confirm = "") =>
    request<{ success: boolean; allow_production_mutations: boolean; message: string }>(
      v1("/project/production-access"),
      {
        method: "PUT",
        body: JSON.stringify({ allow_production_mutations: allow, confirm }),
      },
    ),
  subscription: () =>
    request<{
      plan: string;
      limits: Record<string, number>;
      usage: Record<string, number>;
      features: Record<string, boolean>;
      billing: { provider_connected: boolean; note: string };
    }>(v1("/project/subscription")),
  mcpServers: () =>
    request<{
      mcp_enabled: boolean;
      risk_floor: string;
      servers: Array<Record<string, unknown>>;
    }>(v1("/project/mcp-servers")),
  discoverMcpServer: (id: string) =>
    request<Record<string, unknown>>(
      v1(`/project/mcp-servers/${id}/discover`),
      { method: "POST" },
    ),

  // --- AI providers --------------------------------------------------------
  aiProviders: () =>
    request<{
      providers: ProviderSpec[];
      tiers: ModelTier[];
      policy: { allowed_llm_providers: string[]; allow_llm_fallback: boolean };
      platform_managed_ai: boolean;
      deployment_providers: string[];
      byok_required: boolean;
    }>(v1("/ai/providers")),
  aiCredentials: () =>
    request<{ count: number; credentials: LLMCredential[] }>(v1("/ai/credentials")),
  createAiCredential: (payload: {
    name: string;
    provider: string;
    /** Write-only. No endpoint ever returns it again. */
    api_key?: string;
    base_url?: string;
    region?: string;
    config?: Record<string, unknown>;
    tier_models?: Record<string, string>;
    is_default?: boolean;
  }) =>
    request<LLMCredential>(v1("/ai/credentials"), {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  updateAiCredential: (id: string, patch: Record<string, unknown>) =>
    request<LLMCredential>(v1(`/ai/credentials/${id}`), {
      method: "PATCH",
      body: JSON.stringify(patch),
    }),
  deleteAiCredential: (id: string) =>
    request<{ success: boolean }>(v1(`/ai/credentials/${id}`), { method: "DELETE" }),
  /** Makes a real call to the provider. "Connected" here means it answered. */
  testAiCredential: (id: string, tier: ModelTier = "BALANCED") =>
    request<{ success: boolean; message?: string; model?: string }>(
      v1(`/ai/credentials/${id}/test?tier=${tier}`),
      { method: "POST" },
    ),
  aiRouting: (tier: ModelTier = "BALANCED") =>
    request<RoutingInfo>(v1(`/ai/routing?tier=${tier}`)),
  aiUsage: (days = 30) => request<UsageReport>(v1(`/ai/usage?days=${days}`)),

  tools: () =>
    request<{
      tools: ToolCatalogEntry[];
      count: number;
      enabled_count: number;
      providers: string[];
    }>(v1("/tools")),
  changeSets: () =>
    request<{ change_sets: ChangeSetSummary[] }>(v1("/change-sets")),
  changeSet: (id: string) =>
    request<Record<string, unknown>>(v1(`/change-sets/${id}`)),
  dataJobs: () => request<{ jobs: DataJobSummary[] }>(v1("/data-jobs")),
  knowledge: () => request<{ entries: KnowledgeEntry[] }>(v1("/knowledge")),
  audit: (conversationId?: string) =>
    request<{ entries: AuditEntry[] }>(
      v1(`/audit${conversationId ? `?conversation_id=${conversationId}` : ""}`),
    ),
  deployments: () =>
    request<{ deployments: Array<Record<string, unknown>> }>(v1("/deployments")),
};
