# Salesforce integration

## Whose app is it?

A Connected App — an **External Client App** since Spring '26 — is not a piece
of vendor plumbing. It is the object a Salesforce administrator uses to control
who reaches their org through an integration: which profiles may use it, from
which IP ranges, how long a refresh token lives, and the one button that
revokes every session at once.

That makes "who owns the app" an architectural decision, not a configuration
detail. This product supports both answers, resolved in this order:

| Source | When it is used | Where the secret lives |
|---|---|---|
| **A project's own app** | One project needs a different Salesforce app than the rest of the company | Secret store, bound to that company + project |
| **The company's app** | The normal enterprise case: their security team registered it and can revoke it | Secret store, bound to that company |
| **The deployment's app** | Fallback for self-serve signups with no Salesforce administrator yet | `SALESFORCE_CLIENT_SECRET` in the environment |

A company registers its own under **Settings → Salesforce app**. The consumer
secret is submitted once and handed to the secret store; no endpoint returns
it, and the UI shows a fingerprint so an administrator can confirm a rotation
took effect without ever seeing the credential.

Two flags decide the policy:

- `FEATURE_CUSTOMER_SALESFORCE_APPS` (default **true**) — companies may bring
  their own.
- `FEATURE_REQUIRE_CUSTOMER_SALESFORCE_APP` (default false) — they **must**;
  no company falls back to the shared app.

Leaving `SALESFORCE_CLIENT_ID` empty is a legitimate configuration. It means
every company brings its own app, which is what a security team that will not
authorise a third-party app in their org requires.

### Why not one shared app for everyone

It is simpler, and it is how a lot of SaaS integrations work. It also means:

- **One revocation is everyone's revocation.** A single customer's security
  team pulling the app takes every other customer offline with it.
- **The control belongs to the vendor.** IP restrictions, permitted profiles
  and session policy are set in *our* org, not theirs — which is the first
  thing an enterprise security review asks about and cannot be argued away.
- **The app's API usage is pooled.** One tenant's behaviour is visible in
  another's org-level app metrics.

So the shared app stays as an onboarding convenience, and never as the only
option.

### Which app authorised which connection

Both the in-flight handshake (`oauth_states.salesforce_app_id`) and the stored
connection (`salesforce_connections.salesforce_app_id`) record the app they
used. This is not bookkeeping:

- A token exchange must present the **same** `client_id` the authorize URL did.
  Re-resolving at exchange time would let an administrator editing the app
  mid-login swap the credentials underneath an in-flight handshake, which fails
  as an `invalid_grant` pointing nowhere near the cause.
- A refresh must go back to the app that **issued** the token. A company
  switching apps must not silently break every existing connection's refresh.

Removing an app reports how many active connections were authorised through it,
because those tokens can no longer be refreshed. Discovering that as an
authentication failure in the middle of a deployment is the alternative.

## The callback URL

There is exactly one, and it is **derived, never typed**:

```
{PUBLIC_BASE_URL}/api/v1/salesforce/oauth/callback
```

Settings → Salesforce app shows the exact string with a copy button, generated
from the route the server actually serves.

This is not fussiness. A hand-written `SALESFORCE_REDIRECT_URI` that omits the
`/v1` segment sends a person through a completely **successful** Salesforce
login — username, password, MFA, consent — and then drops them on a 404 holding
a valid authorization code. It reads as Salesforce's fault and is entirely
ours. So `SALESFORCE_REDIRECT_URI` now defaults to empty; when it is set it is
validated at startup against the route that exists, reported by `/health` as
`salesforce_callback_problem`, and refused by the OAuth start endpoint before
any redirect happens.

## Registering the app (OAuth client)

Registration produces a **pair from one app**, which Salesforce names
*Consumer Key* and *Consumer Secret*. Put them under Settings → Salesforce app
(per company), or in `SALESFORCE_CLIENT_ID` / `SALESFORCE_CLIENT_SECRET` (the
deployment fallback).

**This has nothing to do with MCP.** MCP support in this product is a separate,
optional per-project feature (Project settings → External tool providers). The app
registration below is the core requirement for every Salesforce tool.

### Which kind of app

Salesforce changed this in **Spring '26**: creating new **Connected Apps** is
disabled by default in all orgs, and re-enabling it requires a support request.
**External Client Apps (ECAs)** are the supported path for new registrations.

- **New setup** → create an External Client App.
- **Existing Connected App** → keep it. They continue to work, and can still be
  edited, installed and deleted. Nothing in this codebase distinguishes the two;
  both are just OAuth clients issuing the same tokens.

### External Client App

Setup → Quick Find **"External Client App Manager"** → **New External Client App**.

| Section | Setting |
|---|---|
| Basic Information | Name, API Name, Contact Email; **Distribution State: Local** |
| API (Enable OAuth Settings) | **Enable OAuth** |
| Callback URL | Copy it from **Settings -> Salesforce app**. Locally: `http://localhost:8000/api/v1/salesforce/oauth/callback` |
| Scopes | `api` (Manage user data via APIs), `refresh_token, offline_access` (Perform requests at any time) |
| Flow enablement | **Authorization Code and Credentials Flow** |
| Require PKCE | recommended — the client always sends S256 |
| Require secret for Web Server Flow | either; the secret is sent when configured |

Credentials afterwards: **Manage External Client Apps → your app → Settings →
OAuth Settings → Consumer Key and Secret**.

Then **Policies** → set *Permitted Users* and refresh-token validity.

Only those two scopes are needed. Anything broader is access the agent will
never use, and access it never has cannot be misused.

Permissions the connected Salesforce user needs:

* API Enabled (all tools)
* object/field CRUD + FLS for the data you want the agent to touch
* **Customize Application** or **Modify All Data** for `create_field` /
  `deploy_metadata`

## OAuth flow

`POST /api/v1/salesforce/oauth/start?sandbox=true` → authorize URL (PKCE verifier
and CSRF state stored server-side) → user consents → Salesforce redirects to
`/api/v1/salesforce/oauth/callback` → code exchanged for tokens → identity fetched
→ `SalesforceConnection` upserted, tokens replaced by **secret-store references**
bound to this company and project → connection
validated with `/limits` + an `Organization` SOQL query → redirect back to the console.

Sandbox is detected from the login host (`test.salesforce.com`) and the
`Organization.IsSandbox` field, and drives the risk engine.

Access tokens are refreshed automatically on `401 INVALID_SESSION_ID` and the
new token is re-encrypted in place; if there is no refresh token the user is
told to reconnect.

## APIs used

| API | Where | Why |
|---|---|---|
| REST `sobjects/{X}/describe` | `client.describe` | schema of record |
| REST `query` / `queryMore` | `client.query_all_pages` | SOQL with pagination |
| REST `sobjects/{X}` POST/PATCH/GET | record tools | create/update/verify |
| REST `composite` | available in `client.composite` | batched calls |
| Tooling API `query`, `executeAnonymous` | `client.tooling_*` | ApexClass/Flow metadata |
| Bulk API 2.0 query jobs | `client.bulk_query` | large extracts without loading them into the model |
| Metadata API `deploy` / `checkDeployStatus` (SOAP) | `metadata.MetadataClient` | field creation and package deployment |

API version comes from `SALESFORCE_API_VERSION` (default `62.0`) and is stored
per connection.

`Sforce-Limit-Info` is parsed on every call so daily API consumption is
observable in the structured logs.

## Metadata deployment details

The package is built in memory:

```
package.xml                     <- generated from the manifest
objects/Account.object          <- <CustomObject><fields>…</fields></CustomObject>
profiles/Admin.profile          <- optional fieldPermissions (FLS)
```

zipped, base64-encoded and sent as a SOAP `deploy()` call to
`/services/Soap/m/{version}` with the session id in `SessionHeader`. The client
polls `checkDeployStatus(includeDetails=true)` with backoff until `done`, then
parses `componentFailures` and Apex test `failures` into structured records.

Timeout produces `METADATA_DEPLOY_TIMEOUT` **with the deploy id**, because the
deployment is still running in Salesforce — the agent is told to report that
rather than to retry.

XML responses are parsed with `defusedxml` (XXE/entity-expansion safe) and every
value written into XML is escaped.

## Sandbox first

`ALLOW_PRODUCTION_MUTATIONS=false` (default) blocks `create_field` and
`deploy_metadata` against any non-sandbox org — before the tool runs, not inside
the model. Record mutations against production are permitted but escalated to
HIGH risk and always require approval.

## Not yet integrated

Flow/Apex authoring, `retrieve()`, destructive changes, report metadata,
permission analysis, bulk write jobs, and duplicate-rule inspection. The client
and registry are built so these are additive.

---

## API surface actually used

| API | Where | Used for |
| --- | --- | --- |
| REST (sObjects, describe, query) | `salesforce/client.py` | schema, SOQL, single-record CRUD |
| Composite | `salesforce/client.py` | batched record operations |
| Tooling — query | `salesforce/inspect.py` | Flow versions, Apex, validation rules, dependencies, coverage |
| Tooling — sObject read | `salesforce/inspect.py` | full Flow metadata for one version |
| Tooling — `MetadataContainer` | `salesforce/apex.py` | real compile-only check for Apex |
| Tooling — `runTestsAsynchronous` | `salesforce/apex.py` | Apex test runs, polled to completion |
| Metadata API — `deploy` | `salesforce/metadata.py` | fields, flows, Apex, change sets, destructive changes |
| Metadata API — `retrieve` | `salesforce/metadata.py` | current source for diffs, rollback plans, validation formulas |
| Metadata API — `listMetadata` | `salesforce/metadata.py` | enumerating a type before retrieving it |
| Bulk API 2.0 — query | `salesforce/client.py` | analysis extracts past 2,000 rows |
| Bulk API 2.0 — ingest | `salesforce/client.py` | `bulk_update`, polled with a failure sample |
| Analytics API | `tools/report_tools.py` | report list/describe/create/run, dashboard read |

## Org objects the agent reads

`FlowDefinitionView`, `Flow`, `FlowDefinition`, `ApexClass`, `ApexTrigger`,
`ApexTestResult`, `ApexTestRunResult`, `ApexCodeCoverageAggregate`,
`ValidationRule`, `EntityDefinition`, `FieldDefinition`, `CustomField`,
`MetadataComponentDependency`, `RecordType`, `DuplicateRule`, `Report`,
`Dashboard`, `User`, `Profile`, `PermissionSet`, `PermissionSetAssignment`,
`ObjectPermissions`, `FieldPermissions`, and `<Object>History` where field
history tracking is enabled.

## Org configuration this needs

Beyond the Connected App, some capabilities depend on org settings. Where one is
missing the tool says so rather than returning an empty result:

| Capability | Requires |
| --- | --- |
| Metadata deployment (fields, flows, Apex) | **Modify All Data** or **Customize Application** on the connected user's profile |
| `analyze_dependencies` (authoritative half) | The **Dependency API** — not available in every org. The tool reports when it is missing. |
| Field history in `debug_org_behaviour` | **Field history tracking** enabled on the object |
| `create_report` | A **report type** that exposes the fields you name — check `list_report_types` |
| Apex tests | Apex enabled; Developer/Enterprise edition or a sandbox |
| Production metadata changes | `ALLOW_PRODUCTION_MUTATIONS=true` **and** the tenant policy enabling it |

## API version

`SALESFORCE_API_VERSION` (default 62.0) is used for REST, Tooling, Metadata and
Bulk consistently. `FlowDefinitionView` needs a reasonably recent version;
`list_flows` falls back to the Tooling `Flow` object (with less trigger detail)
on older ones rather than failing.
