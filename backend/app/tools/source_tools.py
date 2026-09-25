"""Getting Salesforce metadata into source control, in the format teams use.

Two tools, and the split matters:

  * `export_metadata_as_source` retrieves from the org and converts. It changes
    nothing anywhere, so it needs no approval — and it is what makes the second
    tool's approval card meaningful, because the human sees the actual files.
  * `commit_metadata_to_repository` does the same retrieve and then writes the
    result to a branch and opens a pull request. It changes source control, so
    it is approved, and it re-runs the retrieve rather than trusting whatever a
    previous step reported.

That second point is the important one. Committing metadata the agent *said* it
retrieved earlier would mean the repository records a claim rather than the
org's actual state. The org is read again at the moment of the commit.
"""

from __future__ import annotations

from typing import Any

from app.deployment import sfdx
from app.integrations import service as integrations
from app.integrations.base import IntegrationError
from app.integrations.bitbucket import BitbucketClient
from app.integrations.github import refuse_write
from app.models import RiskLevel
from app.salesforce.metadata import MetadataClient
from app.tools.base import Tool, ToolContext, ToolValidationError, fail, ok
from app.tools.registry import registry

#: A retrieve of everything is a request nobody means literally, and it times
#: out rather than failing usefully. Named types only.
MAX_TYPES = 20
MAX_MEMBERS_PER_TYPE = 200


def _manifest(args: dict[str, Any]) -> dict[str, list[str]]:
    types = args.get("types")
    if not isinstance(types, dict) or not types:
        raise ToolValidationError(
            "A metadata manifest is required: which types and which members.",
            error_type="MISSING_TYPES",
            missing=["types"],
            suggested_action=(
                'Pass types like {"CustomObject": ["Account"], "ApexClass": '
                '["AccountService"]}. Use list_metadata first if you need the names.'
            ),
        )
    if len(types) > MAX_TYPES:
        raise ToolValidationError(
            f"Asking for {len(types)} metadata types at once will time out.",
            error_type="TOO_MANY_TYPES",
            suggested_action=f"Retrieve at most {MAX_TYPES} types per call.",
        )
    cleaned: dict[str, list[str]] = {}
    for kind, members in types.items():
        names = [str(m) for m in (members or []) if str(m).strip()]
        if not names:
            raise ToolValidationError(
                f"'{kind}' was requested with no members.",
                error_type="EMPTY_MEMBERS",
                suggested_action=(
                    "Name the components explicitly. A wildcard retrieve of a "
                    "whole type is slow enough to time out and almost never what "
                    "was meant."
                ),
            )
        if len(names) > MAX_MEMBERS_PER_TYPE:
            raise ToolValidationError(
                f"'{kind}' was requested with {len(names)} members.",
                error_type="TOO_MANY_MEMBERS",
                suggested_action=f"At most {MAX_MEMBERS_PER_TYPE} members per type.",
            )
        cleaned[str(kind)] = names
    return cleaned


async def _retrieve_source(
    ctx: ToolContext, types: dict[str, list[str]], root: str
) -> tuple[sfdx.ConversionResult, dict[str, list[str]]]:
    """Retrieve from the org and convert. The org is always read live."""
    sf = ctx.require_sf()
    client = MetadataClient(sf)
    # RetrieveResult already carries the unpacked package as {path: text}.
    result = await client.retrieve(types)
    converted = sfdx.to_source_format(result.files, root=root)
    return converted, sfdx.manifest_from_source(converted.files)


# ---------------------------------------------------------------------------
# Export (no side effects)
# ---------------------------------------------------------------------------
async def _export_execute(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    types = _manifest(args)
    root = str(args.get("source_root") or sfdx.SOURCE_ROOT)
    converted, manifest = await _retrieve_source(ctx, types, root)

    include_content = bool(args.get("include_content"))
    return ok(
        source_root=root,
        paths=sorted(converted.files),
        components=manifest,
        # Content is opt-in: a retrieve of a large object is far more text than
        # a model needs to decide what to do with it.
        files=converted.files if include_content else None,
        **converted.summary(),
        conversion=sfdx.describe(),
        note=(
            "Retrieved from the org and converted to SFDX source format. Nothing "
            "has been written anywhere."
        ),
    )


# ---------------------------------------------------------------------------
# Commit (writes to a repository)
# ---------------------------------------------------------------------------
async def _commit_validate(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    types = _manifest(args)
    repo = await integrations.repository_for(
        ctx.db, ctx.project_id, str(args.get("repository") or "")
    )
    if repo is None:
        raise ToolValidationError(
            f"'{args.get('repository')}' is not registered to this project.",
            error_type="REPOSITORY_NOT_REGISTERED",
            suggested_action=(
                "Call list_repositories to see which repositories are reachable."
            ),
        )
    branch = str(args.get("branch") or "").strip()
    if not branch:
        raise ToolValidationError(
            "A branch name is required.",
            error_type="MISSING_BRANCH",
            missing=["branch"],
        )
    root = str(args.get("source_root") or sfdx.SOURCE_ROOT)

    # The paths are not known until the retrieve runs, so the branch rule and
    # the source root are checked here — a source root outside the allowed
    # paths would fail on every file, and finding that out after a retrieve is
    # a slow way to learn it.
    refusal = refuse_write(branch, [f"{root}/"], repo)
    if refusal:
        raise ToolValidationError(
            refusal,
            error_type="WRITE_NOT_PERMITTED",
            suggested_action=(
                "A project admin sets the allowed branch patterns and paths on "
                "the Integrations page."
            ),
        )
    return {"repository": repo.full_name, "branch": branch, "types": list(types)}


async def _commit_plan(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    types = args.get("types") or {}
    return {
        "title": (
            f"Export {sum(len(v or []) for v in types.values())} component(s) "
            "to source control"
        ),
        "change_type": "metadata_to_source",
        "summary": (
            f"Retrieve the named metadata from the connected org, convert it to "
            f"SFDX source format, and commit it to '{args.get('branch')}' in "
            f"{args.get('repository')}"
            + (", then open a pull request." if args.get("open_pull_request", True) else ".")
        ),
        "details": [
            {"field": kind, "new_value": ", ".join(members or [])}
            for kind, members in list(types.items())[:20]
        ],
        "impact": (
            "Writes to source control only. Nothing is deployed and nothing in "
            "Salesforce changes."
        ),
    }


async def _commit_execute(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    types = _manifest(args)
    root = str(args.get("source_root") or sfdx.SOURCE_ROOT)
    repo = await integrations.repository_for(
        ctx.db, ctx.project_id, str(args.get("repository") or "")
    )
    if repo is None:
        return fail(
            "REPOSITORY_NOT_REGISTERED",
            f"'{args.get('repository')}' is not registered to this project.",
        )
    branch = str(args.get("branch") or "").strip()

    try:
        # Read the org again. Committing what a previous step *said* it saw
        # would mean the repository records a claim rather than the org's state.
        converted, manifest = await _retrieve_source(ctx, types, root)
    except Exception as exc:  # SalesforceError and friends
        detail = getattr(exc, "to_dict", None)
        return detail() if callable(detail) else fail("RETRIEVE_FAILED", str(exc))

    if not converted.files:
        return fail(
            "NOTHING_RETRIEVED",
            "The retrieve returned no files for those components.",
            suggested_action=(
                "Confirm the component names with list_metadata; a name that does "
                "not exist retrieves silently as nothing."
            ),
        )

    refusal = refuse_write(branch, list(converted.files), repo)
    if refusal:
        # Re-checked against the real paths, now that they are known.
        return fail("WRITE_NOT_PERMITTED", refusal)

    message = str(args.get("message") or "").strip() or (
        f"Export {', '.join(sorted(manifest))} from Salesforce"
    )
    from_branch = str(args.get("from_branch") or "") or repo.default_branch

    try:
        client = await integrations.build_client(ctx.db, ctx.project_id, repo.provider)
        async with client as api:
            branch_created = False
            try:
                await api.get_branch_sha(repo.full_name, branch)
            except IntegrationError as exc:
                if exc.error_type != "NOT_FOUND":
                    raise
                await api.create_branch(repo.full_name, branch, from_branch)
                branch_created = True

            if isinstance(api, BitbucketClient):
                commit = await api.commit_files(
                    repo.full_name, converted.files, message, branch
                )
                commit_sha = commit["commit_sha"]
            else:
                commit_sha = ""
                for path, content in converted.files.items():
                    written = await api.put_file(
                        repo.full_name, path, content, message, branch
                    )
                    commit_sha = written["commit_sha"] or commit_sha

            pull_request = None
            if args.get("open_pull_request", True):
                title = str(args.get("title") or "") or message
                body = _pr_body(manifest, converted, ctx)
                if isinstance(api, BitbucketClient):
                    pull_request = await api.create_pull_request(
                        repo.full_name, title, branch, repo.default_branch, body
                    )
                else:
                    pull_request = await api.create_pull_request(
                        repo.full_name, title, branch, repo.default_branch, body
                    )
    except IntegrationError as exc:
        return exc.to_dict()

    return ok(
        repository=repo.full_name,
        branch=branch,
        branch_created=branch_created,
        commit_sha=commit_sha,
        files_committed=sorted(converted.files),
        components=manifest,
        pull_request=pull_request,
        verified=True,
        **converted.summary(),
        note=(
            "Written to source control. Nothing was deployed and nothing in "
            "Salesforce changed."
            + (
                " A pull request is open for review; merging is a human decision."
                if pull_request
                else ""
            )
        ),
    )


def _pr_body(
    manifest: dict[str, list[str]], converted: sfdx.ConversionResult, ctx: ToolContext
) -> str:
    """A description a reviewer can act on, with its provenance stated."""
    lines = [
        "Salesforce metadata exported to SFDX source format by the Salesforce AI Agent.",
        "",
        "## Components",
    ]
    for kind, members in sorted(manifest.items()):
        lines.append(f"- **{kind}** ({len(members)}): {', '.join(members[:20])}")
    lines += [
        "",
        "## Provenance",
        f"- Source org: `{getattr(ctx.connection, 'sf_org_id', 'unknown')}`"
        + (
            f" ({ctx.connection.environment.value})"
            if getattr(ctx.connection, "environment", None)
            else ""
        ),
        f"- Instance: `{getattr(ctx.connection, 'instance_url', '')}`",
        f"- Agent run: `{ctx.agent_run_id}`",
        "",
        "Retrieved from the org at commit time — not from anything cached.",
    ]
    if converted.warnings:
        lines += ["", "## Warnings", *[f"- {w}" for w in converted.warnings]]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------
registry.register(
    Tool(
        name="export_metadata_as_source",
        description=(
            "Retrieve metadata from the org and convert it to SFDX source format.\n\n"
            "Changes nothing anywhere — use it to see exactly what would be "
            "committed, and to read the current source of a component.\n\n"
            "Name the components explicitly. A wildcard retrieve of a whole type "
            "is slow enough to time out and almost never what was meant."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "types": {
                    "type": "object",
                    "description": (
                        'Metadata type to member names, e.g. {"CustomObject": '
                        '["Account"], "ApexClass": ["AccountService"]}'
                    ),
                    "additionalProperties": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "source_root": {"type": "string", "default": sfdx.SOURCE_ROOT},
                "include_content": {
                    "type": "boolean",
                    "default": False,
                    "description": "Return file contents, not just paths",
                },
            },
            "required": ["types"],
        },
        output_schema={"type": "object"},
        risk=RiskLevel.LOW,
        requires_approval=False,
        execute=_export_execute,
        tags=["metadata", "read", "source"],
        long_running=True,
        audit_action="metadata.export_source",
    )
)

registry.register(
    Tool(
        name="commit_metadata_to_repository",
        description=(
            "Retrieve metadata from the org, convert it to SFDX source format, "
            "commit it to a branch and open a pull request.\n\n"
            "This is how a Salesforce change reaches source control. The org is "
            "read at commit time, so the repository records what is actually "
            "there rather than what an earlier step reported.\n\n"
            "Nothing is deployed and nothing in Salesforce changes."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "repository": {"type": "string", "description": "owner/name"},
                "branch": {"type": "string"},
                "from_branch": {"type": "string"},
                "types": {
                    "type": "object",
                    "additionalProperties": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "message": {"type": "string"},
                "title": {"type": "string"},
                "source_root": {"type": "string", "default": sfdx.SOURCE_ROOT},
                "open_pull_request": {"type": "boolean", "default": True},
            },
            "required": ["repository", "branch", "types"],
        },
        output_schema={"type": "object"},
        risk=RiskLevel.MEDIUM,
        requires_approval=True,
        mutating=True,
        execute=_commit_execute,
        validate=_commit_validate,
        plan=_commit_plan,
        tags=["repository", "git", "metadata", "write"],
        provider="github",
        long_running=True,
        audit_action="repository.commit_metadata",
    )
)
