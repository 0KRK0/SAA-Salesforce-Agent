# Security

The product's claim is "an AI agent can operate my Salesforce org safely." This
document is what that claim rests on, and where it stops.

Every control below has a test in `backend/tests/` — mostly `test_security.py`,
`test_tenancy.py` and `test_approval_binding.py` — because a security control
nobody tests is a security control nobody has.

---

## 1. Tenant isolation

`project_id` is the isolation key on **every** row that can reach
Salesforce: connections, conversations, agent runs, tool executions, approvals,
deployments, change sets, data jobs, audit logs and org knowledge.

- Handlers take a `TenantContext`, never a bare user. The tenant is resolved
  from the session (or `X-Project-Id`) and validated against an **active
  membership** — a caller cannot name a tenant they do not belong to.
- Reads go through `tenancy.owned()` / `tenancy.scoped()`. A row from another
  tenant reads as **404**, not as someone else's data, and not as a 403 that
  would confirm the id exists.
- A Salesforce org connects once per *tenant*, not per user: colleagues share
  one connection; a different tenant connecting the same org gets its own row.
- MCP tools are built per agent run and never enter the process-wide registry.

Tested: cross-tenant read, resume, approve, connection attachment, listing
leakage, forged org header, and knowledge scoping.

## 2. Authorization

Seven roles — `OWNER`, `ADMIN`, `RELEASE_MANAGER`, `SECURITY_ADMIN`,
`DEVELOPER`, `ANALYST`, `VIEWER`. New memberships default to `VIEWER`.

- A `VIEWER` cannot drive the agent. Even a read-only request spends org API
  calls and can be steered toward mutations that land in someone's queue.
- Connecting or removing a Salesforce org, editing agent policy and registering
  MCP servers are role-gated.
- Approval eligibility is decided by explicit role sets in
  `app/tenancy/policy.py`, never by a rank comparison.

## 3. The risk engine

`app/risk/engine.py` sits outside the model. No scoring, no model input, no
heuristic that can be argued with. It reads the tool's declared risk, the
arguments, the target org's posture and the tenant policy.

It **raises** risk for: security objects and security metadata, deletion,
blast radius (>20 ids, or an estimated row count), Apex and automation changes,
non-sandbox targets, and non-validation deployments. It **blocks** outright
for: tools the tenant disabled, bulk operations above the tenant limit, and
metadata changes to a non-sandbox org without an explicit production policy.

Model output can never bypass it — the engine runs on tool dispatch, before
execution, and again on resume after approval.

## 4. Approval, and why an APPROVED row is not authority

Approvals are created only by the runtime and decided only by an explicit API
call. Nothing in a chat message approves anything: "sounds good, approved!" is
covered by a test.

Before a mutation runs, the runtime re-checks three independent things:

| Guard | Failure mode it prevents |
| --- | --- |
| `expires_at` | An approval from last month authorizing a change today. |
| `change_hash` | The approved operation being swapped for a different one. |
| `state_fingerprint` | The org moving while a human was deciding, so the approved plan is no longer correct. |

Any one failing → `approval.invalidated`, an audit row, and **nothing executes**.

**Multi-approver.** Security and production-deployment changes at HIGH risk
require **two distinct** eligible humans by default. One person cannot satisfy
it by voting twice. **Editing the arguments discards every vote already cast** —
the people who approved reviewed something else. A tenant may make the matrix
stricter; the built-in floor cannot be lowered.

**Separation of duties** (`require_separate_approver`) stops the requester
approving their own HIGH-risk change. Off by default because it deadlocks a
single-operator deployment; turn it on in Project settings for a real team.

## 5. Prompt injection

A Salesforce record is data. An MCP result is data. Neither is an instruction.

Every tool result is wrapped before it reaches the model — org data in
`BEGIN_UNTRUSTED_SALESFORCE_DATA`, third-party MCP output in
`BEGIN_UNTRUSTED_EXTERNAL_TOOL_DATA`. There is no unwrapped path a tool can opt
into. Results are JSON-encoded inside the boundary, so a record trying to forge
a closing marker cannot escape it, and payloads are size-capped so org content
cannot flood the context.

The system prompt states that this content cannot grant permissions, change
policy, or declare an approval unnecessary. But the real defence is structural:
the risk engine and approval gate are code, and the model cannot reach them.

## 6. SOQL safety

`app/salesforce/soql.py` rejects anything it cannot prove safe: SELECT-only,
single statement, comments stripped, mandatory bounded `LIMIT`, capped `OFFSET`.
String literals are blanked before scanning for DML verbs, so
`WHERE Name LIKE '%update%'` passes while an actual `update` does not. The same
validator screens the `where` clause of `bulk_update`.

Large reads go through Bulk API 2.0 into server-side analysis. The model gets
findings, never the rows.

## 7. Credentials

Salesforce tokens are encrypted at rest with Fernet (`ENCRYPTION_KEY`).
`SalesforceClient` owns the whole lifecycle — decrypt, use, refresh, re-encrypt.
No caller, and certainly not the model, ever handles a credential. MCP server
secrets are encrypted the same way. Tokens are never placed in prompts, tool
results or audit rows; `redact()` scrubs argument and result payloads.

## 8. Idempotency and verification

Mutations carry an idempotency key `(run, tool, canonical args)`; an identical
mutation that already succeeded in the run returns the prior result instead of
running again. State-aware tools additionally re-check live state immediately
before deploying.

Every mutating tool verifies against the org afterwards, and **a tool whose
verification fails is reported as failed** regardless of what the API returned.
A deployment status of Succeeded is Salesforce reporting on its own job; a
retrieve is the org reporting on its contents, and only the second is evidence.

## 9. Audit

Every tool execution, approval decision, policy change, invalidation and SCIM
operation writes an `AuditLog` row with tenant, user, run, tool, redacted
arguments, result summary, risk, approval state, execution state, deployment id
and outcome. De-provisioning through SCIM removes the membership but keeps the
user row, so history stays attributable.

## 10. Identity

- **Local** — signed JWT sessions over the users table. Real, verified on every
  request; appropriate for pilots.
- **OIDC** — fully implemented: discovery, authorization-code with PKCE, state
  and nonce, and ID-token verification against the provider's JWKS. An
  unverifiable token is an authentication failure, not a warning.
- **SCIM 2.0 Users** — create, list, replace, patch (including `active: false`)
  and de-provision. Bearer token compared in constant time. Disabled endpoints
  return 404 rather than advertising the surface.
- **SAML** — **not implemented.** It needs XML-signature verification and
  assertion replay protection; approximating either would be worse than not
  shipping it. `SamlProvider` raises, and `/api/v1/auth/providers` reports it as
  unavailable.

## Known limitations

Stated because a security document that only lists strengths is not useful:

- **SAML is not implemented.** OIDC is the enterprise SSO path.
- **Dashboards are read-only.** No dashboard creation or editing.
- **The source-scan half of dependency analysis can produce false positives**
  (a similarly-named field, a mention in a comment). Comment-only hits are
  flagged as low confidence rather than hidden.
- **Rate limiting is per-tenant policy (agent steps, bulk ceilings), not
  per-IP.** Put a reverse proxy or API gateway in front for network-level rate
  limiting.
- **`require_separate_approver` defaults off** so a single operator is not
  locked out. Enable it for any team of more than one.
- **The Dependency API is not available in every org.** When it is missing, the
  report says so instead of implying a clean result.
- **Field history requires tracking to be enabled** on the object; the debugger
  reports its absence rather than concluding nothing ever changed.

## Deployment checklist

- [ ] `ENCRYPTION_KEY` set (`make key`). Without it, tokens are not encrypted.
- [ ] `SESSION_SECRET` changed from the development default. Startup logs an
      error in production if it is not.
- [ ] `DATABASE_URL` points at PostgreSQL; `alembic upgrade head` run.
- [ ] `ALLOW_PRODUCTION_MUTATIONS` left `false` unless deliberately enabling it.
- [ ] `CORS_ORIGINS` restricted to your frontend origin.
- [ ] TLS terminated in front of the app; session cookies are `secure` outside
      local/dev.
- [ ] Salesforce Connected App scoped to `api refresh_token offline_access` —
      no more.
- [ ] Connect a **sandbox** first.
- [ ] `require_separate_approver` enabled if more than one person operates it.


---

## Enterprise identity

### The rule that makes group mapping a control

**The identity provider is authoritative on every login, not just the first
one.** Remove someone from `salesforce-admins` in Okta and their next sign-in
here downgrades them; remove them from every mapped group and their membership
is deactivated.

A mapping that only ever adds access means de-provisioning silently fails —
which is the first thing an enterprise security review tests. Three properties
follow:

- mappings are applied on **every** authentication;
- a role that came from the IdP is **replaced**, never merged;
- an unrecognised group grants nothing. Unknown is not "probably fine".

There is one deliberate exception. A membership granted by a **person** — an
invitation, a project admin adding a contractor — carries no `external_id` and
is left alone by SSO. It was not the directory's to grant, so it is not the
directory's to revoke. Without that carve-out, one SSO login would silently
remove every hand-added collaborator.

A mapping that points at another company's project is refused twice: when it is
configured, and again when it is applied. Honouring it would be a tenancy
breach dressed as a configuration value.

`POST /api/v1/sso/preview` shows what a given set of groups would grant without
signing anyone in. Group mappings are the one part of SSO that fails silently —
a typo grants nothing and nobody finds out until a person cannot get in.

### What an IdP group may not grant

`PLATFORM_OWNER`. Operating this deployment is not something a customer's
directory gets to hand out.

### Session revocation

A session token carries `sv`, the user's session version at the moment it was
minted. Bumping `users.session_version` invalidates **every token ever issued
to that user**, in every browser, on their next request.

- `POST /api/v1/auth/sessions/revoke` — sign out everywhere.
- `POST /api/v1/auth/projects/members/{id}/revoke-sessions` — a project or
  security admin cutting off a member now, without waiting for a cookie to
  expire. Deliberately separate from removing them: an administrator responding
  to a suspected compromise wants both, and wants the fast one first.

Revoked, disabled and nonexistent all answer with the same message. Telling an
attacker which of the three it was hands them information they did not have.

### SCIM tokens

Two sources, checked in order:

1. **The company's own token**, minted at `POST /api/v1/sso/scim-token` and
   shown exactly once. It only works for that company's projects.
2. **`SCIM_BEARER_TOKEN`**, deployment-wide, for single-tenant installs. It
   works anywhere — which is precisely why the per-company token exists. A
   token shared across tenants would let one customer's IdP provision into
   another's project.

Both comparisons are constant-time. An unreadable stored token is **not** a
match: failing open there would turn a secret-store outage into an open
provisioning endpoint.

An unknown role name from an IdP is an error, not a silent downgrade to
whatever seemed closest. De-provisioning removes the membership and keeps the
user row, so audit history stays attributable.

### Rate limiting

In-process, and it says so. N application processes allow roughly N times the
configured rate; `/health` reports this, and an exact global limit belongs at
the edge. What it genuinely buys: credential stuffing becomes slow, a runaway
client script cannot empty a model budget before a human notices, and accidental
retry loops are contained.

Authenticated routes are keyed per session — fairer, since one person's runaway
script should not rate-limit a colleague behind the same NAT. **Unauthenticated
routes are keyed on the client address only.** Keying login on a cookie the
caller chose would let an attacker reset their own limit by rotating a junk
value, making the limiter evadable by exactly the traffic it exists to stop.

### SAML

Still not implemented, and it cannot be switched on: `PUT /api/v1/sso` refuses
`kind: SAML` while `FEATURE_SAML` is off, because enabling it would leave a
login method that cannot authenticate anyone. It needs XML-signature
verification and assertion replay protection, neither of which should be
approximated.


## External systems

A Jira description, a GitHub issue body and a Bitbucket comment are text a
stranger can write. They reach the model inside their own untrusted-data
boundary — `BEGIN_UNTRUSTED_EXTERNAL_SYSTEM_DATA` — which names the source and
states that it does not extend the agent's permissions. A test asserts that
every tool tagged `jira`, `repository` or `git` declares an external provider,
because a tool that forgot would have its results wrapped as first-party
Salesforce data.

Writes to a repository pass four checks, in code: the repository must be
registered to the project, the branch must match its allowed patterns and is
never the default branch, every path must be inside its allowed paths, and the
change arrives as a pull request. The checks run at validation **and again at
execution**, because an approval's arguments can be edited in between.

There is no tool that merges a pull request. See `docs/integrations.md`.


---

## Redaction

Key-name redaction is the easy half and is not enough. A credential does not
only arrive as a named field:

```
{"message": "401 for Bearer sk-ant-api03-Xy9..."}
{"detail":  "curl -H 'Authorization: Bearer ghp_16C7e...' failed"}
{"stderr":  "SFDX_AUTH_URL=force://PlatformCLI::5Aep861..."}
```

Every one of those is a live credential somewhere nothing is guarding, and
every one gets past a key check. `app/observability/redaction.py` does both:
hides values under secret-shaped **key names** (hyphens normalised, so
`x-api-key` and `X-Auth-Token` count) and scrubs credential-shaped
**substrings** out of the values it keeps.

Covered issuers: Anthropic, OpenAI, GitHub (classic and fine-grained), Google,
Slack, Atlassian, AWS access key ids, Salesforce session ids, SFDX auth URLs,
JWTs, PEM private key blocks, and this platform's own secret-store references.

**Anchored on issuer prefixes, never on entropy.** An entropy heuristic redacts
Salesforce record ids, SOQL and Apex — exactly the evidence someone reads an
audit trail for. A token shape not on the list is a gap; a heuristic that eats
the evidence is worse.

It also runs as a structlog processor on **every** log line this process emits.
Call sites are supposed to redact first; the one that forgets is the one that
writes a token to disk.

## Retention

The default is **no customer-data retention** — but "processed and discarded"
has to be a job that runs, not a sentence in a README. A tool result sits in
`tool_executions.result` because the run needed it, and unless something removes
it the default is quietly the opposite of what was promised.

`app/retention.py` sweeps four clocks per project, all defaulting to zero:
conversation content, tool payloads, uploaded documents, and audit rows (365).

Two rules it never breaks:

1. **A live run is never touched.** Deleting the transcript of a run waiting for
   approval would destroy what the approval authorizes.
2. **Audit metadata outlives audit payloads.** The row saying *what* happened,
   *who* approved it and *when* is the compliance artefact; the argument blob is
   not. Payloads clear on the tool-payload clock; rows survive at least 30 days
   whatever a policy says, so a policy edit cannot erase last week's approvals.

Tool executions keep their name, object, record ids, risk, approval state and
outcome after a sweep. Only the payloads go.

`POST /api/v1/operations/retention/sweep` **defaults to a dry run**. Deleting a
customer's history is not something an endpoint should do because somebody was
curious what the button did.

## Cost control

`monthly_run_allowance` is enforced at run **creation**, not at execution.
Refusing after a worker has already spent a model call and a Salesforce
round-trip would charge for the thing being refused.

## Posture

`GET /api/v1/operations/posture` generates the answer to a security
questionnaire **from the running configuration** rather than from a document
written once and left to drift: secret backend, redaction coverage, rate limits,
retention, execution ceilings, approval binding, Salesforce posture, model
routing, and an explicit list of what is not implemented.

It ends with the sentence that matters: *"This is not a certification claim: no
SOC 2, ISO 27001 or similar attestation is asserted anywhere in this product."*
