# Running it

## What it is made of

```
db          PostgreSQL 16
backend     FastAPI + an in-process run worker
frontend    Next.js 15
```

`docker compose up --build` starts all three. The backend's entrypoint runs
`alembic upgrade head` before uvicorn, so a fresh machine needs no second step.
`init_db()` remains for local and test convenience only.

That default is right for one replica and wrong for many: Alembic takes no lock
of its own, so containers starting together can race. Set
`RUN_MIGRATIONS_ON_START=false` and run the upgrade as a pre-deploy job wherever
more than one replica starts at once — the startup log says which mode it is in.

There is nothing else. No Redis, no message broker, no Node runtime inside the
backend, no Salesforce CLI. Each of those was considered and left out because
each is a component that can be down, and none of them buys something this
product needs:

- **The queue is a table.** Claims are an atomic conditional `UPDATE`, so
  several processes share a queue with no coordination beyond the database. A
  broker would shave a second off pickup latency and add a thing that can fail
  in a way where work exists and nothing knows about it.
- **The rate limiter is in-process** and says so. An exact global limit belongs
  at the edge.
- **SFDX conversion is pure Python.** Shelling out to `sf` would mean a feature
  that works on a developer's laptop and nowhere else.

## Before it can do anything

Two things are genuinely required:

| Setting | Why |
| --- | --- |
| `ENCRYPTION_KEY` (or a KMS backend) | Without a secret store, no credential can be held at rest, so no Salesforce org and no model provider can be connected. |
| `SESSION_SECRET` | Left at the default, every session token is forgeable. Startup logs an error in production if it has not been changed. |

Then, per project, **an AI provider key**. This deployment does not lend its own
key to projects unless `FEATURE_PLATFORM_MANAGED_AI=true`, so out of the box a
project brings its own — see [`ai-providers.md`](ai-providers.md).

Salesforce needs an **External Client App**; new Connected Apps cannot be
created by default since Spring '26. See [`salesforce.md`](salesforce.md).

## Scaling

Run several backend processes. Every one runs a worker, claims are atomic, and
a crashed process's runs are reclaimed when its heartbeat goes stale
(`RUN_HEARTBEAT_TIMEOUT_SECONDS`).

`RUN_WORKER_ENABLED=false` turns a process into API-only, so a deployment can
separate serving from executing. If *every* process has it off, queued runs sit
there — the startup log says so explicitly rather than leaving it to be
discovered.

`SESSION_SECRET` and `ENCRYPTION_KEY` must be identical across processes: the
first signs sessions, the second is what a `local:` secret reference resolves
against. A process with a different `ENCRYPTION_KEY` cannot read connections
that another one wrote.

## Shutdown

`RunWorker.stop()` stops claiming new work and lets in-flight runs finish. They
are **not** cancelled: a run interrupted between a Salesforce write and its
verification is the one state this system cannot describe honestly. Give the
container a termination grace period longer than a typical run.

## What to watch

`GET /health` — configuration and readiness, no auth. Reports the secret
backend, feature flags, whether a worker is running, and the registered tools.

`GET /api/v1/operations/metrics` — per project: run counts, failure rate, queue
depth, approval outcomes, tool latency and model spend. A rate over zero runs
reports `null`, not `0`, because both `0%` and `100%` read as facts.

Logs are structured JSON with `request_id`, `agent_run_id` and `correlation_id`
on every line, and every line passes through redaction. Ship them somewhere; the
metrics endpoint is a point-in-time read, not a time series.

## Periodic jobs

Two things want a schedule. Neither has a built-in scheduler, because a cron
that runs inside one of N replicas runs N times.

| Job | How | How often |
| --- | --- | --- |
| Retention sweep | `POST /api/v1/operations/retention/sweep?dry_run=false` per project | Daily |
| Stale-run reclaim | Automatic — the worker does it every tick | — |

## Backups

Back up the database **and** the secret material together. A database restored
without the `ENCRYPTION_KEY` that wrote it has unreadable connection rows: every
Salesforce and provider credential must be re-entered. With a KMS backend, the
key stays in the KMS and only the wrapped data keys are in the backup — which is
the main practical reason to use one.

Runs, audit rows and approvals are all in the database. There is no state
outside it except the secret backend.

## Upgrading

Migrations are forward-only in practice: the initial revision replaced a
pre-release schema outright rather than pretending to migrate from it. Run
`alembic upgrade head`; the backend's entrypoint does this on start unless
`RUN_MIGRATIONS_ON_START=false`.

`alembic check` in CI catches a model change that nobody wrote a migration for
— the failure mode where the code works locally against `init_db()` and breaks
on a real deployment.

## What is not implemented

Stated here so an operator does not discover it during a rollout:

- **SAML** — OIDC and SCIM are implemented; SAML is not, and it cannot be
  switched on.
- **AWS Bedrock, Google Vertex** — declared and refused at construction.
- **Vault, Azure Key Vault, GCP KMS** secret backends — selecting one fails at
  startup rather than silently falling back to something weaker.
- **Payment collection** — plan entitlements are enforced; no billing provider
  is contacted anywhere in this codebase.
- **Dashboard creation** — dashboards are read-only.

`GET /api/v1/operations/posture` returns this list from live configuration, so
it cannot drift from what the deployment actually does.

## No compliance claims

This product asserts no SOC 2, ISO 27001 or similar attestation, anywhere. The
posture endpoint describes the controls that run; a certification is a statement
about an audited organisation, which is not something software can claim on its
own behalf.
