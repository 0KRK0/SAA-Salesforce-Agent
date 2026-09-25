# Salesforce AI Agent

An autonomous agent that operates a real Salesforce organization
through natural language: inspect schema, query data, create and update records,
build custom fields and record-triggered Flows, write and test Apex, find and
plan merges for duplicates, diagnose why the org behaved the way it did, analyse
what a change would break, audit access, and move releases through validate →
diff → approve → deploy → verify → rollback.

Every one of those runs behind a deterministic safety layer: risk classification
outside the model, human approval bound to the exact change, verification
against real org state, and a full audit trail.

This is an **agent runtime**, not a chatbot:

```
user request → understand → inspect org → plan → select tools → execute
   → observe → reason again → validate → approval (when required)
   → execute mutation → verify in Salesforce → report
```

A large language model is the reasoning engine — Anthropic, OpenAI, Azure
OpenAI, Google or a self-hosted one, chosen per project. This application is
the runtime. Salesforce APIs
are the execution layer. Authorization, risk classification and approval live
**outside** the model and cannot be talked around.

---

## Capability matrix

52 tools across twelve subsystems, 687 tests. Full tool table:
[`docs/tools.md`](docs/tools.md).

Everything below is either **Implemented** — meaning there is a real API call
behind it and a test that exercises it — or it is in the *Not implemented*
section, with the reason. There is no third category.

### Salesforce

| Capability | State |
|---|---|
| OAuth 2.0 web-server flow with PKCE, refresh, single-use state | Implemented |
| Per-company External Client App — customer owns the Connected App, secret in the secret store | Implemented |
| Callback URL derived from the mounted route and validated at startup | Implemented |
| Schema and SOQL — `describe_object`, validated `query_salesforce` | Implemented |
| Records — create, update, with before/after in the audit trail | Implemented |
| Metadata — `create_field`, `deploy_metadata`, retrieve, list | Implemented |
| Flow agent — list, inspect, validate, create, update, activate, deactivate | Implemented (record-triggered flows) |
| Apex agent — inspect, compile-check, write with tests, run tests, coverage | Implemented |
| Data quality — duplicates, completeness, anomalies, merge plans, Bulk API 2.0 | Implemented |
| Org debugger — evidence collection, then confidence-rated diagnosis | Implemented |
| Dependency analysis — Dependency API and source scan, reported separately | Implemented |
| Reports — list, inspect, create (tabular and summary) | Implemented |
| Permissions — effective access, org-wide risk audit, permission-set assignment | Implemented |
| Deployment lifecycle — change sets, diff, check-only with tests, deploy, verify, rollback plan | Implemented |
| Environments — DEVELOPMENT / SANDBOX / UAT / PRODUCTION, driving policy | Implemented |
| Production clearance across three levels — deployment, company, project — with the refusing level named | Implemented |
| Developer Edition recognised as neither sandbox nor production | Implemented |
| Environment scoping gates **changes**, never reads — a production org you may not touch is still readable | Implemented |
| Dashboard **creation** | Not implemented — dashboards are read-only |

### The agent

| Capability | State |
|---|---|
| Deterministic risk engine, outside the model | Implemented |
| Approval binding — expiry, change hash, org-state fingerprint, environment, multi-approver, separation of duties | Implemented |
| CRITICAL tier — irreversible production change always needs two people | Implemented |
| Quorum is honest — the card says who may still decide, and names a project that has too few approvers to ever finish | Implemented |
| Policy alternatives — a blocked action returns what you *can* do instead | Implemented |
| Durable runs — server-owned, worker queue, atomic claims, stale reclaim, deadline expiry | Implemented |
| Resumable event stream — replay from any cursor, `Last-Event-ID` honoured | Implemented |
| Cooperative cancel — stops at a step boundary, never mid-call | Implemented |
| Untrusted-data boundaries — Salesforce, MCP and external systems each named | Implemented |
| Org knowledge — progressive retrieval under a context budget | Implemented |
| MCP — per-project servers as an extra tool provider, risk-floored | Implemented |

### Models

| Capability | State |
|---|---|
| Anthropic, OpenAI, Azure OpenAI, Google, Mistral, Groq, DeepSeek, Together, Ollama, any OpenAI-compatible endpoint | Implemented |
| Bring your own key, per project, with tiers and live connection tests | Implemented |
| Routing and controlled fallback; a 401 never fails over to another vendor | Implemented |
| Cost estimates from dated list prices; unknown models report no cost | Implemented |
| AWS Bedrock, Google Vertex | Not implemented — see [`docs/ai-providers.md`](docs/ai-providers.md) |

### Tenancy, identity and security

| Capability | State |
|---|---|
| Company → project tenancy, project as the security boundary | Implemented |
| 8 project roles and 4 company roles; project admin ≠ Salesforce authority | Implemented |
| Local sessions, OIDC with PKCE and JWKS verification, SCIM 2.0 Users | Implemented |
| IdP group → role mapping, authoritative on **every** login | Implemented |
| Session revocation — every token already issued dies immediately | Implemented |
| Per-company SCIM tokens — one IdP cannot provision into another's project | Implemented |
| Secret store — references bound to company + project; local Fernet and AWS KMS envelope | Implemented |
| Redaction — key names and credential-shaped values, on every log line | Implemented |
| Retention sweeper — four clocks per project, live runs untouched, 30-day audit floor | Implemented |
| Rate limiting — per process, and it says so | Implemented |
| Posture report generated from live configuration | Implemented |
| SAML single sign-on | Not implemented — cannot be enabled; see [`docs/security.md`](docs/security.md) |
| Vault, Azure Key Vault, GCP KMS secret backends | Not implemented — selecting one fails at startup |

### Source control and delivery

| Capability | State |
|---|---|
| Jira — JQL search, read issue, comment, transition (discovered, not guessed), create | Implemented |
| GitHub and Bitbucket — read files and issues, branch, commit, open a pull request | Implemented |
| Repository guard rails — registration, branch patterns, path allowlist, pull request required | Implemented |
| SFDX source-format export — Metadata API → decomposed source, pure Python, no CLI | Implemented |
| Metadata → branch → pull request, retrieved live at commit time | Implemented |
| Cross-system traceability — one correlation id across Jira, Salesforce and Git | Implemented |
| **Merging** a pull request | Deliberately not a tool — a person reviews and merges |

### Product

| Capability | State |
|---|---|
| Apple-glass interface, light and dark, risk signalled by shape as well as colour | Implemented |
| Plan entitlements — projects, users, connections, run allowance — all enforced | Implemented |
| Alembic migrations, Docker Compose, structured logging with correlation ids | Implemented |
| Payment collection | Not implemented — no billing provider is contacted anywhere |
| SOC 2 / ISO 27001 attestation | **Not claimed.** No certification is asserted anywhere in this product |

**Nothing here fakes an API call.** Every tool talks to a real Salesforce, Jira,
GitHub, Bitbucket or model-provider API; the only doubles are in
`backend/tests/`. Where a capability is not built, the code refuses at the
earliest possible moment — an unimplemented model provider or secret backend
raises at *construction*, so a deployment that selects one fails at startup
rather than at a customer's first request.

Verification for this build:

| Check | Result |
|---|---|
| `pytest` on SQLite | 687 pass |
| `pytest` on **PostgreSQL 16** | 687 pass |
| `ruff check` | clean |
| `tsc --noEmit` | clean |
| `next build` | succeeds |
| `alembic upgrade head` → `downgrade base` → `upgrade head` | round-trips **on Postgres**, enum types included |
| `alembic check` | models match the migrations |
| `alembic upgrade head` against live Postgres 16 | 31 tables created |
| Backend serving on Postgres | `/health` ok; 22-check API smoke passes |
| Production `next build` served, all 7 routes | HTTP 200 |
| Interface | rendered in a browser, light and dark, and looked at |
| `docker compose up --build` | both images build; stack serves |

The suite defaults to SQLite because it needs no server, but SQLite is not what
this ships on. Run it against the real engine before believing it:

```bash
DATABASE_URL=postgresql+asyncpg://user@host:5432/dbname pytest
```

That run is what caught the last defect: a durable-execution test that
manufactured a run pointing at a deleted conversation. SQLite does not enforce
foreign keys by default and accepted the row; Postgres refuses it, so the test
had been asserting behaviour for a state the shipping database cannot reach.

### On the Docker images

Both images now build and the stack serves. Getting there found three defects
that only a real `docker compose up` could surface:

- **A Postgres volume created by an earlier build could not be migrated.** Its
  schema was built by `init_db()` and never stamped, so `alembic upgrade head`
  replayed the initial revision over objects that already existed and the
  backend exited 1 on every boot. Now reconciled at startup — and only when the
  live schema actually matches the models; otherwise it refuses and says what
  differs. See `app/db_bootstrap.py`.
- **`downgrade base` left 14 enum types behind on Postgres**, so the next
  `upgrade head` failed. The round-trip had only ever been checked on SQLite,
  which has no such objects.
- **No `.dockerignore` anywhere.** The backend's `COPY . .` would have baked
  `.env` into an image layer, and the frontend's would have copied a
  developer's host `node_modules` — with its platform-specific native
  binaries — over the freshly installed one.

---

## Architecture

```
frontend (Next.js)  ──HTTP/SSE──►  backend (FastAPI)
                                       │
                                       ├── agent runtime (state machine, step cap)
                                       │      └── model gateway (tool use)
                                       ├── tool registry ── tools ──► Salesforce
                                       │                              REST / SOQL
                                       │                              Metadata API
                                       │                              Tooling / Bulk
                                       │                              Analytics API
                                       ├── MCP providers (per tenant) ──► MCP servers
                                       ├── risk engine (deterministic, outside the model)
                                       ├── tenancy + approval policy (roles, multi-approver)
                                       ├── analysis (duplicates, quality, permissions)
                                       ├── diagnostics (evidence → diagnosis)
                                       ├── deployment engine (diff, rollback plan)
                                       ├── knowledge store (progressive retrieval)
                                       └── audit log + deployments (Postgres/SQLite)
```

### The three rules the architecture exists to enforce

1. **The model proposes; policy decides.** Risk classification, approval
   requirements and tenant isolation are code the model cannot reach.
2. **An approval authorizes one operation, once, for a while.** It is bound to
   the exact arguments, to the org state at proposal time, and to a clock.
3. **Nothing is "done" until the org says so.** Every mutation reads the org
   back; a tool whose verification fails is reported as failed no matter what
   the API returned.

Details: [`docs/architecture.md`](docs/architecture.md) ·
[`docs/tools.md`](docs/tools.md) · [`docs/salesforce.md`](docs/salesforce.md) ·
[`docs/security.md`](docs/security.md) · [`docs/ai-providers.md`](docs/ai-providers.md) ·
[`docs/execution.md`](docs/execution.md) · [`docs/integrations.md`](docs/integrations.md) ·
[`docs/source-control.md`](docs/source-control.md) ·
[`docs/interface.md`](docs/interface.md) ·
[`docs/operations.md`](docs/operations.md) ·
[`docs/development.md`](docs/development.md)

---

## Setup

### 1. Register the app in Salesforce

This is how the agent authenticates to your org over OAuth 2.0. It is required —
without it no Salesforce tool can run. (It is unrelated to MCP, which is a
separate, optional feature configured per project in the app.)

> **Since Spring '26, new Connected Apps cannot be created by default.** Use an
> **External Client App** instead. Existing Connected Apps keep working, so if
> you already have one, nothing needs to change.

In your Salesforce **sandbox**: Setup → Quick Find **"External Client App
Manager"** → **New External Client App**.

| Section | Setting |
| --- | --- |
| Basic Information | Name, API Name, Contact Email. **Distribution State: Local** |
| API (Enable OAuth Settings) | Tick **Enable OAuth** |
| | Callback URL: `http://localhost:8000/api/v1/salesforce/oauth/callback` |
| | Scopes: **Manage user data via APIs (api)** and **Perform requests at any time (refresh_token, offline_access)** — those two only |
| | Flow: **Enable Authorization Code and Credentials Flow** |
| | **Require Proof Key for Code Exchange (PKCE)** — safe to enable; the client always sends S256 |
| After saving | **Settings → OAuth Settings → Consumer Key and Secret** |

Copy the **Consumer Key** and **Consumer Secret** — a pair from this one app.

**Where they go is a decision, not a detail.** A Connected App is the customer's
security control: it decides which profiles reach their org, from which IP
ranges, and it carries the button that revokes every session. So each company
registers its own under **Settings → Salesforce app**, where the secret goes to
the secret store and no endpoint reads it back. `SALESFORCE_CLIENT_ID` /
`SALESFORCE_CLIENT_SECRET` in `.env` remain as a *shared fallback* for
self-serve signups, and can be left empty. See
[`docs/salesforce.md`](docs/salesforce.md).

The **callback URL is derived, never typed** — Settings → Salesforce app shows
the exact string to paste, generated from the route the server actually serves.
A hand-written one missing the `/v1` segment completes a Salesforce login and
then lands on a 404 holding a valid authorization code.

Then **Manage External Client Apps → your app → Policies** and set *Permitted
Users* and refresh-token validity appropriately.

Metadata deployments (fields, flows, Apex) require the connecting user's profile
to have **Modify All Data** or **Customize Application**.

### 2. Configuration

```bash
cp .env.example backend/.env
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# paste into ENCRYPTION_KEY
```

Only `ENCRYPTION_KEY` is genuinely required. Add `ANTHROPIC_API_KEY` (or bring a
key per project in the UI), and the Salesforce app either here or — better —
per company in Settings. `CLAUDE_MODEL` is configuration; the code never
hardcodes a model name.

### 3. Run it

**Local (no Docker):**

```bash
cd backend
pip install -r requirements-dev.txt
uvicorn app.main:app --reload --port 8000

cd ../frontend
npm install
npm run dev            # http://localhost:3000
```

**Docker:**

```bash
cp .env.example .env   # fill it in
docker compose up --build
```

`GET http://localhost:8000/health` reports which parts are configured.

---

## First run — the vertical slice

1. Open http://localhost:3000, sign in with your email (local identity layer).
2. **Salesforce connection → Connect sandbox**, complete the OAuth flow.
3. Ask: **"Inspect the Account object."**
   → agent calls `describe_object`, returns the real schema.
4. Ask: **"Find the 10 newest Accounts."**
   → agent builds SOQL, the validator enforces read-only + LIMIT, records come back.
5. Ask: **"Create a Customer Tier picklist on Account with Enterprise, SMB and Startup."**
   → agent inspects `Account`, checks whether the field exists, generates metadata,
   shows a change plan, **waits for your approval**, deploys via the Metadata API,
   polls the deployment, re-describes the object, and only then reports success.

Nothing in step 5 is executed by the model saying "approved" — approval is a
recorded decision made through the UI/API.

### Then the rest of the product

6. **"Create a Flow that marks an Opportunity At Risk when CloseDate is within
   14 days and Probability is below 40%."**
   → lists existing flows to check nothing already does this, checks every field
   against `describe`, builds the Flow metadata (entry formula for the relative
   date), runs a **check-only deployment** so the plan you approve is one that
   deploys, then deploys and reads the flow version back.
7. **"Prevent duplicate Contacts by email."**
   → inspects existing Apex, refuses if `Contact` already has a trigger, writes
   the trigger *and its test class*, compiles it against the org, surfaces any
   code smells on the approval card, deploys with `RunSpecifiedTests`, and
   reports the actual test outcome.
8. **"Find duplicate Accounts."**
   → extracts via Bulk API 2.0, normalizes and blocks server-side, returns
   grouped candidates with the reason each matched. Records never enter the
   conversation.
9. **"Why isn't this Opportunity being assigned?"**
   → collects schema, flows *and their logic*, trigger source, validation
   formulas, assignment rules, permissions, ownership and field history, then
   returns candidate causes ranked by confidence, each citing its evidence.
10. **"Can I safely delete Customer_Tier__c?"**
    → Dependency API **and** a source scan across flows, Apex, validation rules
    and reports, reported separately with an impact rating.
11. **"Review our permissions."**
    → resolves effective access as the union of profile and permission sets,
    and ranks org-wide risks with an explanation of why each matters.

### Releases

For anything spanning more than one component, use a change set:
`create_change_set` → `validate_change_set` (retrieves current source, computes
a real diff, runs a check-only deploy with tests, captures a rollback plan) →
`deploy_change_set` (approval required) → verification by retrieving the
components back. `rollback_change_set` restores prior source and destructively
removes what the deployment created — and names the cases it cannot undo.

---

## Testing

```bash
cd backend
python -m pytest -q          # 620 tests
python -m ruff check .
cd ../frontend && npx next build
```

| File | What it defends |
| --- | --- |
| `test_security.py` | Tenant isolation (read, resume, approve, connection theft, forged org header), role gating, approval expiry/binding/multi-approver, untrusted-data boundaries |
| `test_tenancy.py` | Approver matrix, change hashing, expiry, eligibility, separation of duties, policy ceilings |
| `test_approval_binding.py` | End-to-end through the runtime: expired / swapped-argument / org-drifted approvals leave Salesforce untouched |
| `test_flow.py` | Flow XML generation, value typing from describe, entry formulas, XML-escaping injection |
| `test_apex.py` | Declaration/name mismatch, `without sharing`, unfiltered delete, DML-in-loop, hardcoded Ids, packaging |
| `test_analysis.py` | Duplicate normalization/blocking/transitivity, false-positive guards, merge data-loss detection, quality checks |
| `test_deployment.py` | Manifest derivation, diffing, rollback planning and its stated limits |
| `test_diagnostics.py` | Every diagnosis rule, and that no evidence produces no invented cause |
| `test_dependencies.py` | Whole-token matching, comment-only detection, impact rating, unavailable-API honesty |
| `test_mcp_and_risk.py` | MCP namespacing, risk flooring, allowlists, and the risk engine's escalation and blocking rules |
| `test_knowledge.py` | Model-sourced knowledge refused, relevance, budget, expiry |
| `test_agent.py`, `test_api.py`, `test_tools.py`, `test_soql.py`, `test_metadata.py`, `test_risk.py` | The original foundation, still green |

Integration against a real org is manual and sandbox-first — see
[`docs/development.md`](docs/development.md).

### Migrations

```bash
cd backend
alembic upgrade head          # DATABASE_URL from the environment
alembic revision --autogenerate -m "what changed"
```

Integration against a real org is manual and sandbox-first — see
[`docs/development.md`](docs/development.md).

---

## Environment variables

See [`.env.example`](.env.example). Summary:

| Variable | Purpose |
|---|---|
| `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, … | Deployment-managed model keys. Only used when `FEATURE_PLATFORM_MANAGED_AI=true`; a project's own key always wins. See `docs/ai-providers.md` |
| `SECRET_BACKEND`, `ENCRYPTION_KEY` | Where credentials are held at rest |
| `SALESFORCE_CLIENT_ID/_SECRET` | Shared fallback External Client App. Optional — each company can register its own in the UI |
| `PUBLIC_BASE_URL` | Where the backend is reachable; the Salesforce callback URL is derived from it |
| `SALESFORCE_API_VERSION`, `SALESFORCE_SCOPES` | API surface |
| `ENCRYPTION_KEY` | Fernet key encrypting OAuth tokens at rest (required) |
| `SESSION_SECRET` | Signs application session JWTs |
| `DATABASE_URL` | SQLite locally, Postgres in Docker |
| `MAX_AGENT_STEPS`, `MAX_TOOL_RESULT_CHARS`, `MAX_QUERY_ROWS` | Cost + blast-radius control |
| `MAX_BULK_RECORDS`, `MAX_ANALYSIS_RECORDS` | Ceiling on bulk mutations and analysis extracts |
| `APPROVAL_TTL_SECONDS`, `REQUIRE_SEPARATE_APPROVER` | Approval lifetime and separation of duties |
| `ALLOW_PRODUCTION_MUTATIONS` | Must be true before metadata can be deployed to a non-sandbox org |
| `MCP_ENABLED`, `MCP_DEFAULT_RISK` | External tool providers and their risk floor |
| `OIDC_ISSUER/_CLIENT_ID/_CLIENT_SECRET/_REDIRECT_URI` | Enterprise SSO (optional) |
| `SCIM_BEARER_TOKEN` | SCIM 2.0 user provisioning (optional; unset disables the endpoints) |

These are **deployment ceilings**. A project's own policy (Project settings)
may be stricter but never looser — the Project settings page shows stored
and effective values side by side so an admin can see when a setting did not
take effect.

Secrets are never committed, never logged (see `redact()` in
`app/observability/logging.py`) and never sent to any model.

---

## Security posture

* Tokens encrypted at rest (Fernet), refreshed server-side, never in prompts.
* Tenant isolation: every query is scoped by `user_id`; a record id supplied by
  the model is verified against the user's own connection before use.
* SOQL is parsed and constrained: SELECT-only, mandatory LIMIT, row caps.
* Salesforce data reaching the model is wrapped in explicit
  `BEGIN/END_UNTRUSTED_SALESFORCE_DATA` markers and the system prompt treats it
  as inert content.
* Risk engine escalates on security objects, deletions, bulk operations and
  non-sandbox orgs; HIGH-risk actions cannot execute without a stored approval.
* Everything is audited: user, org, tool, arguments, sanitized result, risk,
  approval state, execution state, before/after values, deployment id.

Full detail: [`docs/security.md`](docs/security.md).

---

## Known limitations

* Identity is a local email + signed session; no SSO/SCIM (deliberately out of MVP scope).
* Field-level security is only granted for profiles you name explicitly in `create_field`.
* `deploy_metadata` deploys source you supply; there is no repo/retrieve sync yet.
* No Flow, Apex, report, permission or duplicate-analysis tools yet — the
  registry is built so they can be added without touching the runtime.
* Approval expiry, multi-approver policy and role-based authorization are not implemented.
* Bulk write operations (bulk update/delete) are not exposed as tools; only Bulk
  API *query* is used, for large extracts.
