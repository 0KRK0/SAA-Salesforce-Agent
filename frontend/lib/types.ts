export type RiskLevel = "LOW" | "MEDIUM" | "HIGH" | "CRITICAL";

/** Which Salesforce environment a connection represents. Drives policy. */
export type Environment = "DEVELOPMENT" | "SANDBOX" | "UAT" | "PRODUCTION";

export interface UserOut {
  id: string;
  email: string;
  display_name: string;
  company_id: string;
  company_name: string;
  project_id: string;
  project_name: string;
  role: Role;
  company_role: CompanyRole;
}

/**
 * Authority inside one project. Deliberately separate from Salesforce
 * authority: PROJECT_ADMIN runs the project and is not thereby the agent's
 * Salesforce superuser.
 */
export type Role =
  | "PROJECT_ADMIN"
  | "SALESFORCE_ADMIN"
  | "RELEASE_MANAGER"
  | "SECURITY_ADMIN"
  | "DEVELOPER"
  | "USER"
  | "AUDITOR"
  | "VIEWER";

/** Authority over the company itself: billing, projects, company-wide SSO. */
export type CompanyRole =
  | "PLATFORM_OWNER"
  | "COMPANY_ADMIN"
  | "COMPANY_MEMBER"
  | "COMPANY_AUDITOR";

export interface ProjectSummary {
  id: string;
  name: string;
  slug: string;
  description: string;
  company_id: string;
  company_name: string;
  role: Role;
  current: boolean;
}

export interface CompanySummary {
  id: string;
  name: string;
  slug: string;
  plan: string;
  role: CompanyRole;
  current: boolean;
}

export interface Connection {
  id: string;
  label: string;
  sf_org_id: string;
  username: string;
  instance_url: string;
  org_type: string;
  environment: Environment;
  is_sandbox: boolean;
  api_version: string;
  is_active: boolean;
  /**
   * Non-reversible fingerprint of the stored access token. Confirms *which*
   * credential is in use; the token itself never reaches the browser.
   */
  token_fingerprint: string;
  has_refresh_token: boolean;
  last_validated_at: string | null;
  last_error: string | null;
}

export interface Conversation {
  id: string;
  title: string;
  salesforce_connection_id: string | null;
  created_at: string;
  updated_at: string;
}

export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  text: string;
  agent_run_id: string | null;
  created_at: string;
}

export interface PlanDetail {
  field: string;
  old_value?: unknown;
  new_value?: unknown;
}

/** A code smell or dependency finding surfaced on an approval card. */
export interface PlanFinding {
  code?: string;
  severity?: string;
  message: string;
  unit?: string;
}

export interface ComponentDiff {
  path: string;
  type: string;
  component: string;
  status: "new" | "modified" | "unchanged";
  added_lines: number;
  removed_lines: number;
  diff: string;
}

export interface ChangePlan {
  title?: string;
  object?: string;
  record_id?: string;
  change_type?: string;
  summary?: string;
  details?: PlanDetail[];
  impact?: string;
  reason?: string;
  risk?: {
    risk: RiskLevel;
    reasons: string[];
    requires_approval: boolean;
    blocked?: boolean;
    blocked_reason?: string;
    reason_code?: string;
    /** What the user may do instead. Empty when there is nothing honest to offer. */
    alternatives?: PolicyAlternative[];
    category?: string;
    approvals_required?: number;
    eligible_roles?: string[];
  };
  /** Apex source proposed for deployment, keyed by class/trigger name. */
  code?: Record<string, string>;
  /** Static findings about the proposed Apex, or permission grants. */
  findings?: PlanFinding[];
  grants?: Array<{ permission: string; severity: string; why: string }>;
  /** Component-level diff for a change-set deployment. */
  diff?: ComponentDiff[];
  rollback_caveats?: string[];
  /** Automation already running on the same object. */
  existing_automation?: Array<Record<string, unknown>>;
  current_behaviour?: Record<string, unknown> | null;
  notes?: string[];
  sample_record_ids?: string[];
}

/** Something a user can actually do when policy blocks an action. */
export interface PolicyAlternative {
  label: string;
  detail: string;
  action: string;
}

export interface ApprovalVote {
  user_id: string;
  role: Role;
  decision: "approve" | "reject";
  note: string | null;
  at: string;
}

export interface Approval {
  id: string;
  agent_run_id: string;
  conversation_id: string;
  tool_name: string;
  risk_level: RiskLevel;
  /** Which environment the change would land in. Null before a connection is chosen. */
  environment?: Environment | null;
  state: "PENDING" | "APPROVED" | "REJECTED" | "EXPIRED" | "NOT_REQUIRED";
  arguments: Record<string, unknown> | null;
  plan: ChangePlan | null;
  decision_note: string | null;
  created_at: string;
  /** Approval hardening: binding, expiry and multi-approver state. */
  expires_at?: string | null;
  expired?: boolean;
  approvals_required?: number;
  approvals_recorded?: number;
  eligible_roles?: string[];
  require_separate_approver?: boolean;
  requested_by?: string;

  /**
   * Whether the person looking at this card may decide, answered by the server
   * that owns the rule. The card must not re-derive it: the browser would get
   * separation of duties, role eligibility and duplicate votes subtly wrong,
   * and offer a button the API then refuses.
   */
  you_can_decide?: boolean;
  you_have_decided?: boolean;
  you_cannot_decide_because?: string;
  outstanding_approvals?: number;
  /** Active project members holding one of `eligible_roles`. */
  eligible_approver_count?: number;
  /** Fewer eligible approvers exist than the change requires. */
  deadlocked?: boolean;
  deadlock_detail?: string;
  approved_by?: string | null;
  invalidated_reason?: string | null;
  decisions?: ApprovalVote[];
}

/** One line in the visible execution trace (never internal reasoning). */
export interface TraceEntry {
  id: string;
  kind:
    | "state"
    | "tool"
    | "approval"
    | "deployment"
    | "error"
    | "info"
    | "assistant";
  label: string;
  detail?: string;
  status?: "running" | "ok" | "failed" | "blocked" | "waiting";
  risk?: RiskLevel;
  at: number;
}

export interface ToolCatalogEntry {
  name: string;
  description: string;
  risk: RiskLevel;
  requires_approval: boolean;
  mutating: boolean;
  tags: string[];
  provider: string;
  long_running: boolean;
  enabled: boolean;
}

export interface ChangeSetSummary {
  id: string;
  name: string;
  description: string;
  state: string;
  test_level: string;
  components: number;
  verified: boolean;
  rollback_available: boolean;
  created_at: string;
  updated_at: string;
}

export interface DataJobSummary {
  id: string;
  operation: string;
  object: string;
  salesforce_job_id: string | null;
  state: string;
  records_total: number;
  records_processed: number;
  records_failed: number;
  error: string | null;
  created_at: string;
}

export interface KnowledgeEntry {
  id: string;
  kind: string;
  key: string;
  summary: string;
  source: string;
  hit_count: number;
  observed_at: string;
}

export interface AuditEntry {
  id: string;
  action: string;
  tool_name: string | null;
  salesforce_object: string | null;
  record_ids: string[] | null;
  risk_level: RiskLevel | null;
  approval_state: string | null;
  execution_state: string | null;
  outcome: string;
  error: string | null;
  result_summary: Record<string, unknown> | null;
  before_values: Record<string, unknown> | null;
  after_values: Record<string, unknown> | null;
  agent_run_id: string | null;
  environment: Environment | null;
  actor_type: string;
  correlation_id: string | null;
  created_at: string;
}


// ---------------------------------------------------------------------------
// AI providers
// ---------------------------------------------------------------------------
export type ModelTier = "FAST" | "BALANCED" | "ADVANCED";

/** One provider the platform knows about — including ones it has not built. */
export interface ProviderSpec {
  kind: string;
  label: string;
  /** False means there is no working implementation here. `notes` says why. */
  implemented: boolean;
  permitted_by_policy: boolean;
  requires_api_key: boolean;
  requires_base_url: boolean;
  config_fields: string[];
  default_models: Record<string, string>;
  supports_tools: boolean;
  notes: string;
}

/**
 * A stored provider credential. There is deliberately no `api_key` field:
 * the API has no path that returns one, so the browser never holds a key after
 * the moment it was submitted.
 */
export interface LLMCredential {
  id: string;
  name: string;
  provider: string;
  base_url: string | null;
  region: string | null;
  config: Record<string, unknown>;
  tier_models: Record<string, string>;
  is_default: boolean;
  is_active: boolean;
  has_key: boolean;
  /** Identifies which key is stored. Not derived from the key itself. */
  key_fingerprint: string;
  /** null = never tested. Not the same as working, and shown differently. */
  last_test_ok: boolean | null;
  last_test_error: string | null;
  last_tested_at: string | null;
  created_at: string;
}

export interface RouteInfo {
  provider: string;
  model: string;
  tier: ModelTier;
  byok: boolean;
  credential_id: string | null;
}

export interface RoutingInfo {
  available: boolean;
  message?: string;
  primary: RouteInfo | null;
  fallbacks: RouteInfo[];
  fallback_enabled: boolean;
  fallback_note?: string;
}

export interface UsageRow {
  provider: string;
  model: string;
  byok: boolean;
  requests: number;
  input_tokens: number;
  output_tokens: number;
  estimated_cost_usd: number;
}

export interface UsageReport {
  days: number;
  totals: Omit<UsageRow, "provider" | "model" | "byok">;
  breakdown: UsageRow[];
  pricing: { as_of: string; unit: string; model_count: number; note: string };
}


// ---------------------------------------------------------------------------
// Runs
//
// A run is owned by the server, not by this tab. Its state comes from the API,
// never from whether a connection happens to be open.
// ---------------------------------------------------------------------------
export type RunState =
  | "CREATED"
  | "QUEUED"
  | "PLANNING"
  | "INSPECTING"
  | "WAITING_FOR_APPROVAL"
  | "EXECUTING"
  | "VERIFYING"
  | "COMPLETED"
  | "FAILED"
  | "CANCELLED"
  | "EXPIRED";

export interface RunSummary {
  id: string;
  state: RunState;
  /** A phrase for a person, e.g. "Waiting for approval". */
  state_label: string;
  terminal: boolean;
  created_at: string;
}

export interface RunDetail extends RunSummary {
  conversation_id: string;
  steps_used: number;
  max_steps: number;
  provider: string;
  model: string;
  model_tier: string;
  input_tokens: number;
  output_tokens: number;
  estimated_cost_usd: number;
  duration_ms: number;
  error: string | null;
  error_code: string | null;
  final_text: string;
  pending_approval_ids: string[];
  cancel_requested: boolean;
  claimed: boolean;
  /** Resume point for following this run's timeline. */
  last_sequence: number;
  started_at: string | null;
  finished_at: string | null;
}


// ---------------------------------------------------------------------------
// Enterprise identity
// ---------------------------------------------------------------------------
export interface SSOConfig {
  id: string;
  kind: "LOCAL" | "OIDC" | "SAML";
  enabled: boolean;
  /** False means this build cannot authenticate with it, whatever is stored. */
  implemented: boolean;
  issuer: string | null;
  client_id: string | null;
  /** There is no field for the secret itself — no endpoint returns one. */
  has_client_secret: boolean;
  client_secret_fingerprint: string;
  redirect_uri: string | null;
  scopes: string;
  domains: string[];
  /** {"idp-group": "ROLE"} or {"idp-group": "<project_id>:ROLE"} */
  group_mappings: Record<string, string | string[]>;
  default_project_id: string | null;
  default_project_role: Role;
  scim_enabled: boolean;
  has_scim_token: boolean;
  updated_at: string | null;
}

/**
 * A Salesforce External Client App owned by one company.
 *
 * The consumer key is here because it travels in every authorize URL anyway,
 * and showing it is what makes a mismatch diagnosable. There is no field for
 * the consumer secret: it is sent once and no endpoint returns it.
 */
export interface SalesforceApp {
  id: string;
  name: string;
  client_id: string;
  has_client_secret: boolean;
  client_secret_fingerprint: string;
  login_url: string;
  api_version: string;
  project_id: string | null;
  /** "company" for every project, "project" for one. */
  scope: "company" | "project";
  is_default: boolean;
  is_active: boolean;
  updated_at: string | null;
}

export interface SalesforceAppInput {
  name: string;
  client_id: string;
  /** Write-only. Omit to leave the stored secret untouched. */
  client_secret?: string;
  login_url: string;
  api_version?: string;
  project_id?: string | null;
  is_default: boolean;
}

/**
 * Production clearance, across the three levels that must all agree.
 *
 * `blocked_by` names the OUTERMOST level refusing, so the UI can point at a
 * remedy that would actually work. Pointing someone at their project checkbox
 * while the deployment ceiling is down is worse than saying nothing.
 */
export interface ProductionPosture {
  permitted: boolean;
  blocked_by: "deployment" | "company" | "project" | null;
  levels: Array<{
    level: "deployment" | "company" | "project";
    permitted: boolean;
    changed_by: string;
    how: string;
  }>;
  note: string;
}
