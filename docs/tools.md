# Tools

52 tools, registered in `app/tools/registry.py`. Adding a capability means
adding a module to `NATIVE_TOOL_MODULES` — the agent runtime never changes.

Every tool declares:

| Field | Meaning |
| --- | --- |
| `risk` | Base classification. The risk engine can only raise it, never lower it. |
| `requires_approval` | Whether the *base* case needs a human. Policy can add more. |
| `mutating` | Whether it changes the org. Drives idempotency. |
| `tags` | Drive the approval **category** (`app/tenancy/policy.py`), so a new tool inherits the right approver policy automatically. |
| `validate` | Deterministic pre-flight. Raises before anything runs. |
| `plan` | The human-readable change plan on the approval card. |
| `fingerprint` | Org state the change was proposed against; re-checked before execution. |
| `verify` | Reads the org back after execution. A tool whose verification fails is reported as **failed**, whatever the API returned. |

---

## Schema and data

| Tool | Risk | Approval | What it does |
| --- | --- | --- | --- |
| `describe_object` | LOW | — | Object schema: fields, types, picklists, relationships, CRUD flags. |
| `query_salesforce` | LOW | — | SOQL, through the validator in `app/salesforce/soql.py`: SELECT-only, mandatory bounded LIMIT, comment stripping, no second statement. |
| `create_record` | MEDIUM | yes | Creates one record after inspecting required fields. |
| `update_record` | MEDIUM | yes | Updates one record; old/new values go to the audit log. |

## Metadata

| Tool | Risk | Approval | What it does |
| --- | --- | --- | --- |
| `create_field` | MEDIUM | yes | Custom field via the Metadata API. Refuses duplicates; verifies by re-describing. Fingerprints "does this field exist" so a field added by someone else during review invalidates the approval. |
| `deploy_metadata` | HIGH | yes | Raw metadata deployment. Prefer a change set. |
| `analyze_dependencies` | LOW | — | What breaks if you change or delete this — see below. |

## Source control (Jira, GitHub, Bitbucket)

Reads are LOW and need no approval; every write needs one. Results from these
tools reach the model inside the external-system untrusted-data boundary — a
ticket description is the likeliest place someone tries to smuggle in an
instruction. See [`docs/integrations.md`](integrations.md).

| Tool | Risk | Approval | What it does |
| --- | --- | --- | --- |
| `jira_search` | LOW | — | JQL search. Summaries only, so a search does not drag a customer's backlog through the context. |
| `jira_get_issue` | LOW | — | One issue with its description, comments, and the transitions actually available on it. |
| `jira_list_projects` | LOW | — | Projects the connected account can see. |
| `jira_comment` | MEDIUM | yes | Posts a comment. Visible to the whole watching team, and cannot be unsent. |
| `jira_transition_issue` | MEDIUM | yes | Moves an issue by status *name*. Transitions are discovered per issue, never hardcoded, and the result reports the status read back afterwards. |
| `jira_create_issue` | MEDIUM | yes | Files follow-up work the agent found but was not asked to do. |
| `list_repositories` | LOW | — | Repositories this project authorized, with their branch and path rules. |
| `read_repository_file` | LOW | — | One file. Returns `exists: false` rather than failing when it is absent. |
| `list_repository_issues` | LOW | — | Open issues. Titles and bodies are untrusted text. |
| `get_pull_request` | LOW | — | Pull request state, including whether it merged. |
| `commit_to_repository` | MEDIUM | yes | Creates a branch if needed and commits. Refused unless the repository is registered, the branch matches its patterns, and every path is inside its allowed paths — checked at validation **and again** at execution. |
| `open_pull_request` | MEDIUM | yes | Opens a PR. **There is no tool that merges one** — an agent that could merge its own work would make review decorative. |
| `export_metadata_as_source` | LOW | — | Retrieves metadata and converts it to SFDX source format. Changes nothing; this is what makes the commit's approval card meaningful. |
| `commit_metadata_to_repository` | MEDIUM | yes | Retrieves, converts, commits and opens a PR. **Re-runs the retrieve** rather than trusting an earlier step, so the repository records the org's state and not a claim. |

## Flow (Phase B)

| Tool | Risk | Approval | What it does |
| --- | --- | --- | --- |
| `list_flows` | LOW | — | Every flow, with trigger object and active version. |
| `inspect_flow` | LOW | — | One flow's real logic: trigger, entry conditions, decisions, assignments, record operations. |
| `validate_flow` | LOW | — | Check-only Metadata API deployment. Success means Salesforce accepted it, not that it looked plausible. |
| `create_flow` | MEDIUM | yes | Record-triggered flow. Every field is checked against describe; values are typed from the field's Salesforce type. |
| `update_flow` | MEDIUM | yes | New flow version. The card shows the currently active version's behaviour beside the proposal. |
| `activate_flow` / `deactivate_flow` | HIGH | yes | Changes what happens to live records. |

**Scope, stated plainly.** `app/salesforce/flow.py` builds record-triggered flows
that evaluate conditions and set fields — before-save (no extra DML) and
after-save (Update Records against the triggering record). Screen flows, loops,
scheduled paths, subflows and invocable actions are **not supported**, and
`validate_shape()` rejects them rather than emitting a flow that silently drops
what was asked for.

## Apex (Phase C)

| Tool | Risk | Approval | What it does |
| --- | --- | --- | --- |
| `list_apex` | LOW | — | Classes and triggers, with test-class detection. |
| `inspect_apex` | LOW | — | Source plus current coverage. |
| `validate_apex` | LOW | — | Real Tooling API compile (MetadataContainer), nothing saved. |
| `write_apex` | MEDIUM | yes | Deploys class/trigger **with its tests**, then reads the test outcome. |
| `run_apex_tests` | LOW | — | `runTestsAsynchronous`, polled to completion. |

`write_apex` refuses rather than shipping a broken change:

- the declaration in the source must match `name`;
- non-test code must come with `test_body`;
- a second trigger on an object that already has one is rejected (execution
  order between two triggers is undefined);
- **critical** smells block: `without sharing`, unfiltered `delete [SELECT …]`.
  Warnings — DML/SOQL in loops, hardcoded record Ids, empty `catch`,
  `SeeAllData=true` — are surfaced on the approval card, not silently fixed.

A test run that has not finished is reported as **unfinished**, never as a pass.

## Data quality (Phase D)

| Tool | Risk | Approval | What it does |
| --- | --- | --- | --- |
| `find_duplicates` | LOW | — | Normalize → block → score → group, with a reason per group. |
| `analyze_data_quality` | LOW | — | Field completeness, invalid values, picklist sprawl, MAD outliers. |
| `prepare_merge_plan` | LOW | — | Survivor + what data would be destroyed. Produces a plan; never merges. |
| `bulk_update` | HIGH | yes | Counts matches, refuses above the tenant limit, runs a real Bulk API 2.0 ingest, reports Salesforce's own processed/failed numbers. |

**Records never enter the conversation.** Extraction uses Bulk API 2.0 past
2,000 rows; analysis runs in `app/analysis/`; the model receives findings.

## Diagnostics (Phases E, F)

| Tool | Risk | Approval | What it does |
| --- | --- | --- | --- |
| `debug_org_behaviour` | LOW | — | Collect evidence, then diagnose. |
| `analyze_dependencies` | LOW | — | Impact analysis by two independent methods. |

`debug_org_behaviour` inspects schema, active flows *and their real logic*, Apex
trigger source, validation-rule formulas, record types, assignment rules,
field-level security, object permissions, ownership and field history. Collection
(`app/diagnostics/collector.py`) is separate from diagnosis
(`app/diagnostics/diagnose.py`) so a finding can only fire when the org actually
shows the condition. Every finding cites its observation and carries
high/medium/low confidence, and areas that could not be inspected are listed —
a gap is never reported as an all-clear.

`analyze_dependencies` runs the Salesforce Dependency API **and** a source scan
of flow metadata, Apex bodies, validation formulas and report definitions, and
reports which found what. "No dependencies found" and "the Dependency API was
unavailable" are different answers.

## Reports and dashboards (Phase G)

| Tool | Risk | Approval | What it does |
| --- | --- | --- | --- |
| `list_reports` / `list_report_types` | LOW | — | Existing reports; valid report types. |
| `inspect_report` | LOW | — | Definition, and optionally summary totals (never rows). |
| `create_report` | MEDIUM | yes | Tabular or summary report via the Analytics API. |
| `inspect_dashboard` | LOW | — | **Read-only.** |

Matrix and joined reports, bucket fields, cross filters and custom summary
formulas are rejected, not approximated. **Dashboard creation and editing are
not implemented** — the Analytics API does not support it and a hand-built
Metadata API dashboard definition is not something to improvise.

## Security (Phase H)

| Tool | Risk | Approval | What it does |
| --- | --- | --- | --- |
| `inspect_permissions` | LOW | — | What a user can do / who can act on an object / what a grant carries. |
| `audit_security` | LOW | — | Ranked risk findings across the org's access posture. |
| `modify_permissions` | HIGH | yes | Assign or remove **one permission set** for **one user**. |

Effective access is always the **union** of profile and every assigned
permission set. `modify_permissions` is deliberately narrow: it cannot edit a
profile, change field-level security, or alter what a permission set contains.
Its approval card lists what the permission set actually grants.

## Deployment lifecycle (Phase I)

| Tool | Risk | Approval | What it does |
| --- | --- | --- | --- |
| `create_change_set` | LOW | — | Bundle metadata into one unit. Touches nothing. |
| `validate_change_set` | LOW | — | Retrieve current source → diff → check-only deploy with tests → capture rollback plan. |
| `deploy_change_set` | HIGH | yes | Deploy, then **retrieve the components back** to verify. |
| `rollback_change_set` | HIGH | yes | Restore prior source; destructive-change the components this deployment created. |

The rollback plan names its own limits: deleting a custom field deletes its
data, Salesforce never removes flow versions, and some component types cannot be
deleted by a destructive change at all.

## Org knowledge (Phase M)

| Tool | Risk | Approval | What it does |
| --- | --- | --- | --- |
| `recall_org_knowledge` | LOW | — | What earlier work found: schema, deployment history, past failures. |
| `remember_about_org` | LOW | — | Record a durable fact **a human stated**. |

`app/knowledge/store.py` refuses `source="model"`. The agent's own conclusions
are never written back as knowledge. Recall is relevance-scored under a
character budget and injected into the system prompt framed as possibly stale.

## MCP tools (Phase A)

MCP servers registered per tenant contribute tools named
`mcp__<server>__<tool>`, so they can never shadow a native tool. They are built
per agent run from `app/mcp/manager.py` and are **never** installed into the
process-wide registry — that registry is shared by every tenant in the process.

An MCP tool passes through the same risk engine, approval gate, audit trail and
step limit as a native tool. Its declared risk is a claim, so it is floored at
`MCP_DEFAULT_RISK` (default MEDIUM) and raised to HIGH on destructive-sounding
names. Its results are wrapped in `BEGIN_UNTRUSTED_EXTERNAL_TOOL_DATA`.

## Adding a tool

```python
registry.register(Tool(
    name="your_tool",
    description="...",          # what the model reads to choose it
    input_schema={...},
    output_schema={...},
    risk=RiskLevel.MEDIUM,
    requires_approval=True,
    execute=_execute,
    validate=_validate,          # deterministic pre-flight
    plan=_plan,                  # the approval card
    fingerprint=_fingerprint,    # org state at proposal time
    verify=_verify,              # read the org back
    mutating=True,
    tags=["metadata", "deploy"], # drives the approver policy
))
```

Then add the module to `NATIVE_TOOL_MODULES`. Nothing in the runtime changes.
