# Development

## Layout

```
backend/
  app/
    agent/       runtime, prompts, context budgeting
    llm/         provider gateway, BYOK, tiers, routing, cost estimates
    execution/   run state machine, queue, worker, durable timeline
    integrations/ Jira, GitHub, Bitbucket clients and OAuth
    api/         FastAPI routers
    risk/        risk engine
    salesforce/  client, oauth, metadata, soql, errors
    security/    crypto, auth
    tools/       registry + one module per tool
    observability/logging
    config.py db.py models.py main.py
  tests/         unit, tool, agent and API tests
frontend/        Next.js console
docs/            this documentation
```

## Backend

```bash
cd backend
pip install -r requirements-dev.txt
cp ../.env.example .env      # then fill it in
uvicorn app.main:app --reload --port 8000
```

Tables are created on startup (`init_db`). SQLite is the local default; point
`DATABASE_URL` at Postgres for anything shared. Schema migrations are not wired
to Alembic yet — for the MVP, changing a model means recreating the local
database.

## Frontend

```bash
cd frontend
npm install
echo "NEXT_PUBLIC_API_BASE=http://localhost:8000" > .env.local
npm run dev
```

## Quality gates

```bash
cd backend
python -m pytest -q        # 69 tests
python -m ruff check .
cd ../frontend
npm run typecheck
npm run build
```

## What the tests cover

| File | Covers |
|---|---|
| `test_soql.py` | SELECT-only enforcement, LIMIT injection/capping, literals vs verbs, OFFSET ceiling |
| `test_metadata.py` | API-name normalization, per-type required options, package/zip shape, XML escaping |
| `test_risk.py` | escalation rules, production blocking, policy override |
| `test_tools.py` | each tool's validation and execution against a Salesforce double |
| `test_agent.py` | tool selection, untrusted-data framing, approval gate, "sounds good" is not approval, resume-after-approval, rejection, production block, max-step halt, unknown tool, missing connection |
| `test_api.py` | auth, tenancy isolation, tool catalogue, approval decision endpoint, SSE stream |

The Salesforce double is an `httpx.MockTransport` app in `tests/conftest.py`. It
is a test fixture, not a runtime stub — production code paths always call real
Salesforce APIs.

## Manual integration checklist (sandbox)

1. `GET /health` → `salesforce_configured` true, `secrets_configured` true.
2. Connect a sandbox org; confirm the connection validates.
3. "Inspect the Account object." → real field list.
4. "Find the 10 newest Accounts." → real records, LIMIT enforced.
5. "Create an Account called Integration Test Co." → approval card → approve →
   record id returned → verify in Salesforce.
6. "Create a Customer Tier picklist on Account with Enterprise, SMB and Startup."
   → approval card → approve → deploy id → field visible in Setup → the agent
   reports verified.
7. Repeat step 6 → the agent reports the field already exists and deploys nothing.
8. Point the conversation at a production org and repeat step 6 → blocked by policy.

## Debugging

Logs are JSON with `request_id` and `agent_run_id` on every line. Useful events:
`salesforce.http`, `llm.call_failed`, `metadata.deploy_started`,
`metadata.deploy_finished`, `agent.run_failed`, `tool.unhandled_error`.

`GET /api/v1/tool-executions?agent_run_id=…` shows exactly what ran, with duration,
risk, approval and execution state. `GET /api/v1/deployments` shows deployment
outcomes including component and test failures.

## Conventions

* Never let a tool return an unstructured error; map it in
  `app/salesforce/errors.py` or return the `{success:false, error_type, …}` shape.
* Never mark a mutation successful without a `verify` implementation.
* Keep deterministic policy out of prompts and inside `risk/` and tool validators.
* Label anything incomplete explicitly in the tool description and the README.

---

## Working on the newer subsystems

The build is layered, and the layers below are what the ones above depend on.

| Layer | Modules | Depends on |
| --- | --- | --- |
| Tenancy + policy | `app/tenancy/` | nothing |
| Salesforce access | `app/salesforce/` | tenancy (for the policy snapshot only) |
| Analysis / diagnostics / deployment / knowledge | `app/analysis/`, `app/diagnostics/`, `app/deployment/`, `app/knowledge/` | `app/salesforce/` |
| Tools | `app/tools/` | everything above |
| Runtime | `app/agent/runtime.py` | the tool contract only |

The runtime never imports a specific tool. Adding a capability means adding a
module to `NATIVE_TOOL_MODULES`.

### Migrations

```bash
cd backend
alembic upgrade head
alembic revision --autogenerate -m "what changed"
```

`migrations/env.py` reads `DATABASE_URL` through `app.config`, so no credential
is ever written into a committed file. SQLite runs in batch mode because it
cannot `ALTER` most things in place.

### Testing against a real org

Everything unit-testable is unit-tested; the doubles live in
`tests/conftest.py`. What genuinely needs an org, and should be exercised in a
**sandbox** before any release:

1. OAuth connect → `describe_object` → `query_salesforce`.
2. `create_field` → approve → deploy → verify (the original vertical slice).
3. `validate_flow` on a real object, then `create_flow` → approve → deploy →
   `inspect_flow` to confirm the logic deployed as intended.
4. `validate_apex` (the Tooling compile is genuinely different from a lint),
   then `write_apex` with a deliberately failing test to confirm the failure is
   reported as a failure.
5. `find_duplicates` on an object with real volume, to check the Bulk extract
   path and the blocking thresholds.
6. `create_change_set` → `validate_change_set` (confirm the diff matches what
   you expect) → `deploy_change_set` → `rollback_change_set`.
7. `audit_security` on an org with real profiles.

### Things that are easy to get wrong

- **Flow value typing.** A checkbox compared against the string `"true"` is
  always unequal. `app/salesforce/flow.py` types every value from the field's
  describe type; if you add a field type, add it to `VALUE_ELEMENT`.
- **Meta files are not components.** Including `Foo.cls-meta.xml` as a package
  member produces a manifest Salesforce rejects. `manifest_from_files()` filters
  them.
- **Naive datetimes.** SQLite round-trips `expires_at` without a timezone.
  `is_expired()` normalizes to UTC; comparing directly raises.
- **Tenant filters.** Any new query must filter on `project_id` — use
  `tenancy.owned()` / `tenancy.scoped()` rather than writing the where clause.
- **SOQL has no bind variables.** The REST API takes SOQL as a query-string
  parameter. Interpolated values go through `soql_literal()`; the modules that
  do this carry a documented `S608` ignore in `ruff.toml`.
