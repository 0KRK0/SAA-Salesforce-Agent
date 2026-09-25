# Salesforce metadata in source control

A Salesforce change that only exists in an org is a change nobody can review,
diff, or roll back with confidence. This is how a change gets from an org into
a repository in the format a team actually uses.

## Why source format

The Metadata API returns one large file per object. `Account.object` holds every
field, validation rule, list view and record type in a single XML document. No
team stores it that way, because two people adding different fields to the same
object produce a merge conflict in a file neither of them meaningfully touched.

Source format decomposes it — one file per component:

```
unpackaged/objects/Account.object
    ↓
force-app/main/default/objects/Account/Account.object-meta.xml
force-app/main/default/objects/Account/fields/Tier__c.field-meta.xml
force-app/main/default/objects/Account/fields/Rating__c.field-meta.xml
force-app/main/default/objects/Account/validationRules/Tier_Required.validationRule-meta.xml
force-app/main/default/objects/Account/listViews/AllAccounts.listView-meta.xml
```

## No Salesforce CLI

The conversion is pure Python, standard library only. This service has no Node
runtime, no `sf` binary and no authenticated CLI session; shelling out to one
would mean a feature that works on a developer's laptop and nowhere else.

## The detail that matters most

A decomposed field's root element is **`CustomField`** — the metadata type —
not `fields`, the tag it was nested under:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<CustomField xmlns="http://soap.sforce.com/2006/04/metadata">
    <fullName>Tier__c</fullName>
    <label>Tier</label>
    <type>Picklist</type>
</CustomField>
```

Getting this wrong produces a file that looks plausible, that git accepts, and
that the Metadata API rejects on deploy — a broken artefact in a customer's
repository with the agent's name on the commit. Every decomposed type declares
its root element explicitly, and a test asserts that none is missing.

Two smaller things in the same category: generated files carry **no `ns0:`
prefixes** (ElementTree's default output is valid but differs from CLI output on
every line, and a repository where every file differs cosmetically is one where
nobody reads diffs), and nested structure inside a component is preserved —
flattening a picklist's `valueSet` would silently drop its values.

## What is decomposed

`fields`, `validationRules`, `listViews`, `recordTypes`, `compactLayouts`,
`webLinks`, `fieldSets`, `businessProcesses`, `sharingReasons`, `indexes`.

Anything else stays in the object's own file — which is also what the CLI does.
`describe()` returns this list and the limitations rather than implying
completeness.

Apex keeps its body and its `-meta.xml` sidecar together. LWC and Aura bundles
keep their directory shape; flattening a bundle would break the component.
`package.xml` is dropped: it describes one retrieve, not the source, and in a
repository it goes stale the moment anything else changes.

An object that will not parse is written **whole**, with a warning. Losing a
customer's metadata to a parse error would be far worse than an undecomposed
file in their repository.

## The two tools

**`export_metadata_as_source`** retrieves and converts. It changes nothing
anywhere, needs no approval, and is what makes the other tool's approval card
meaningful — the human sees the actual files before authorizing the commit.

**`commit_metadata_to_repository`** does the same retrieve, then commits to a
branch and opens a pull request. It **re-runs the retrieve** rather than
trusting what an earlier step reported: committing metadata the agent *said* it
retrieved would mean the repository records a claim rather than the org's state.

Both are subject to the repository guard rails in `docs/integrations.md`. The
branch rule is checked before the retrieve — a source root outside the allowed
paths would fail on every file, and a slow retrieve is a slow way to learn that
— and the path rule is checked again afterwards, once the real paths are known.

The pull request body states its provenance: which org, which environment, which
agent run, and that the metadata was retrieved at commit time.

## Traceability across systems

One request — *"add a Tier field, the ticket is SF-142"* — touches Jira,
Salesforce and a repository. Afterwards three people want three different views
of it, and every row this platform writes carries a `correlation_id`.

- `GET /api/v1/traceability/runs/{run_id}` — everything one run touched,
  grouped by system, with its Salesforce executions, deployments and approvals.
- `GET /api/v1/traceability/correlation/{id}` — the same across every run in
  the chain, so a run that paused for approval and resumed reads as one piece
  of work.
- `GET /api/v1/traceability/search?reference=SF-142` — the question an auditor
  actually asks. Works for a ticket key, a repository name or an object name.

**Nothing here is inferred.** If the agent said it updated Jira and no Jira
write was recorded, the trail shows no Jira write. An action the classifier does
not recognise is reported as `other` rather than attributed to a system it might
not belong to — a wrong system attribution in an audit trail is worse than an
unclassified one.
