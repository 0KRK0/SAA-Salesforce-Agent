# Architecture

```
frontend (Next.js)
   │  HTTP + SSE
   ▼
FastAPI  ── TenantContext (company + project + roles + policy) on every request
   │
   ├─ agent/runtime.py ── the state machine
   │     CREATED → QUEUED → PLANNING → INSPECTING → EXECUTING
   │          → [WAITING_FOR_APPROVAL] → VERIFYING
   │          → COMPLETED | FAILED | CANCELLED | EXPIRED
   │
   │     per step:  model → tool_use → validate → classify risk
   │                → gate on approval → execute → verify → audit
   │
   ├─ execution/ ───────── durable runs: state machine, queue, worker, timeline
   ├─ integrations/ ────── Jira, GitHub, Bitbucket: OAuth, clients, guard rails
   ├─ llm/ ─────────────── the model gateway. One canonical message shape;
   │                       every provider translates to and from it.
   ├─ tools/registry.py ── native tools (import-time registration)
   ├─ mcp/manager.py ───── MCP tools, built per run, per tenant
   │
   ├─ risk/engine.py ───── deterministic. Outside the model.
   ├─ tenancy/policy.py ── who must approve what, for how long
   │
   ├─ salesforce/ ──────── client (REST/SOQL/Composite/Tooling/Bulk 2.0),
   │                       metadata (deploy + retrieve), inspect, flow, apex,
   │                       dependencies, oauth, soql, errors
   ├─ analysis/ ────────── duplicates, quality, permissions  (server-side)
   ├─ diagnostics/ ─────── collector (evidence) → diagnose (findings)
   ├─ deployment/ ──────── change sets: diff, rollback planning,
   │                       SFDX source-format conversion
   ├─ knowledge/ ───────── org knowledge: remember / recall under a budget
   ├─ security/ ────────── secrets (references, not secrets), crypto, auth,
   │                       identity (local | OIDC | SAML stub)
   └─ observability/ ───── structured logs, request/run ids, redaction
```

---

## The agent loop

`app/agent/runtime.py` is the only place that orchestrates. Each step:

1. **Call the model gateway** with the system prompt, the compacted transcript, and the
   tool schemas this tenant is allowed to see.
2. **Dispatch** each `tool_use` block:
   - `validate` — deterministic pre-flight. Raises before anything happens.
   - `classify` — the risk engine, using tool tags, arguments, org posture and
     tenant policy.
   - **blocked?** structured refusal, audit row, no execution.
   - **needs approval?** create an `Approval` bound to the arguments, the org
     state (`fingerprint`) and a clock; persist the transcript; pause.
   - otherwise **execute** under a timeout, then `verify`, then audit.
3. **Feed results back** wrapped in an untrusted-data boundary.
4. Stop at `max_agent_steps` (tenant policy) — halting is a reported outcome,
   not a crash.

Resume after approval re-enters at the gate: the approval must still be inside
its window, still describe the same operation, and still match the org state it
was proposed against. Any failure invalidates it and nothing executes.

## Why the pieces are separate

**Risk classification is not in the model.** A model can be argued into a
conclusion. `risk/engine.py` reads declared risk, arguments, org posture and
tenant policy, and returns a decision the runtime obeys.

**Approval policy is not in the risk engine.** *How dangerous* and *who may
approve it* are different questions with different owners. `tenancy/policy.py`
maps a tool's **tags** to a category, so a new tool inherits the right approver
policy without anyone remembering to configure it.

**Collection is not diagnosis.** `diagnostics/collector.py` gathers observations
and can only be wrong about what it saw. `diagnostics/diagnose.py` turns those
into findings that cite their evidence. A rule cannot fire unless the org
actually shows the condition.

**Analysis is not in the context window.** `analysis/` processes whole datasets
in Python. Fifty thousand records become "312 duplicate groups, here are the
twenty largest".

**MCP is a transport, not a trust boundary.** MCP tools are `Tool` objects like
any other, so the runtime cannot treat them differently even by accident.

## Data model

Three words that would otherwise collide, fixed by naming:

- **Company** — the paying customer. The top-level tenant.
- **Project** — a unit of work inside a company. **The security boundary.**
- **SalesforceConnection** — a connection to a *Salesforce* org. Never called
  "Organization" anywhere in this codebase, because in a Salesforce product
  that word already means something else.

```
Company ─┬─ CompanyMembership ── User      (COMPANY_ADMIN, COMPANY_AUDITOR, …)
         ├─ Subscription                   (plan + entitlements, enforced)
         ├─ SSOConfiguration               (OIDC / SAML / SCIM, per company)
         └─ Project ─┬─ ProjectMembership ── User   (8 project roles)
                     ├─ ProjectPolicy       (ceilings, approver matrix,
                     │                        allowed environments, retention)
                     ├─ Invitation
                     ├─ SalesforceConnection   (per environment; token refs)
                     ├─ LLMCredential          (BYOK; secret references)
                     ├─ IntegrationConnection ── Repository
                     ├─ McpServerConfig        (external tool providers)
                     ├─ Conversation ── Message
                     ├─ AgentRun ─┬─ RunEvent      (durable, replayable timeline)
                     │            └─ ToolExecution
                     ├─ Approval ── ApprovalDecision   (multi-approver votes)
                     ├─ ChangeSet ── Release ── Deployment
                     ├─ DataJob                (Bulk API jobs)
                     ├─ OrgKnowledge           (per Salesforce connection)
                     ├─ LLMUsage               (counts and cost, never prompts)
                     └─ AuditEvent
```

**`project_id` is the isolation key.** Every row that can reach customer data
carries `company_id` *and* `project_id`; queries filter on `project_id`, and
`company_id` exists so a company administrator can see across their own
projects and never beyond. Reads go through `tenancy.owned()` / `scoped()`,
which return "not found" for a foreign row rather than the row.

Alembic manages the schema; `init_db()` remains for local/test convenience.

## Runs are owned by the server

A request that accepts a message *creates* a run and returns; a worker executes
it; clients read a durable timeline. Closing a tab does not stop a change to a
customer's org, and reopening one does not start a second. See
`docs/execution.md`.

## Environments

A `SalesforceConnection` declares which environment it represents —
DEVELOPMENT, SANDBOX, UAT or PRODUCTION — and that drives policy: the same
change is routine in DEV and a governed release in PRODUCTION.

The declaration may only ever *raise* the posture. If an operator labels a
connection SANDBOX but Salesforce reports the org is not a sandbox, production
controls still apply, and the risk decision says why. Otherwise the label would
be an attack surface rather than a control. Developer Edition is the single
carve-out: not a sandbox, and not anyone's production either.

## Credentials

No credential is ever a column value. `security/secrets.py` issues a
*reference* bound to the owning company, project and purpose; the reference is
what lands in the database and in logs. A reference lifted out of one project's
row cannot be resolved as another's, and a Salesforce token reference cannot be
resolved as an LLM key.

Backends: `local` (Fernet, keyed by `ENCRYPTION_KEY`) and `aws-kms` (envelope
encryption; the key never enters the application). Vault, Azure Key Vault and
GCP KMS are **not implemented** and raise at construction, so a deployment that
selects one fails at startup rather than silently storing secrets more weakly
than the operator asked for.

## Context management

- Tool results are size-capped, and record lists are trimmed with a note saying
  how many were dropped and what to do about it.
- Transcripts keep the first turn plus a window, never orphaning a tool result.
- Big reads go through Bulk API 2.0 into server-side analysis.
- Org knowledge is recalled by relevance under a character budget and framed as
  possibly stale.
- Flow metadata is summarized to its logic before the model sees it.

## Extending

**A Salesforce capability** — write a module in `app/tools/`, register a `Tool`,
add it to `NATIVE_TOOL_MODULES`. Give it accurate `tags`; they decide its
approval category.

**An external system** (GitHub, Jira, Slack) — either register an MCP server for
the tenant, or write native tools tagged `external`. `ExternalConnection` holds
credentials the same encrypted way as Salesforce.

**An identity provider** — implement `IdentityProvider` in
`app/security/identity.py` and return a `ResolvedIdentity`. Authorization never
depends on which provider authenticated the human.
