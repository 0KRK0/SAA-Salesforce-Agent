"""Change-set lifecycle engine.

Every state transition here is driven by what Salesforce actually returned. A
change set becomes VALIDATED because a check-only deployment succeeded, and
DEPLOYED because a real deployment succeeded and verification found the
components in the org. There is no path that advances state optimistically.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from typing import Any

from app.models import ChangeSet, ChangeSetState, Deployment
from app.observability.logging import get_logger
from app.salesforce.client import SalesforceClient
from app.salesforce.errors import SalesforceError
from app.salesforce.metadata import (
    MetadataClient,
    MetadataReader,
    build_package_xml,
    build_zip,
)

log = get_logger("deployment.engine")

#: Metadata source paths map to a package.xml type by their folder.
FOLDER_TO_TYPE = {
    "objects": "CustomObject",
    "classes": "ApexClass",
    "triggers": "ApexTrigger",
    "flows": "Flow",
    "flowDefinitions": "FlowDefinition",
    "profiles": "Profile",
    "permissionsets": "PermissionSet",
    "layouts": "Layout",
    "workflows": "Workflow",
    "reports": "Report",
    "dashboards": "Dashboard",
    "labels": "CustomLabels",
    "tabs": "CustomTab",
    "applications": "CustomApplication",
    "staticresources": "StaticResource",
    "aura": "AuraDefinitionBundle",
    "lwc": "LightningComponentBundle",
}

#: Components that a destructive change can genuinely remove. Anything outside
#: this set cannot be rolled back by deletion, and the plan says so.
DESTRUCTIVELY_REMOVABLE = {
    "ApexClass", "ApexTrigger", "Flow", "CustomField", "CustomObject",
    "ValidationRule", "Layout", "CustomTab", "Report", "Dashboard",
}


def manifest_from_files(files: dict[str, str]) -> dict[str, list[str]]:
    """Derive a package.xml manifest from source paths.

    Meta files (`-meta.xml`) describe their sibling and are not components in
    their own right; including them produces a package Salesforce rejects.
    """
    types: dict[str, list[str]] = {}
    for path in files:
        if path == "package.xml" or path.endswith("-meta.xml"):
            continue
        parts = path.split("/")
        if len(parts) < 2:
            continue
        metadata_type = FOLDER_TO_TYPE.get(parts[0])
        if metadata_type is None:
            continue
        member = parts[-1].rsplit(".", 1)[0]
        types.setdefault(metadata_type, [])
        if member not in types[metadata_type]:
            types[metadata_type].append(member)
    return types


@dataclass
class ComponentDiff:
    path: str
    metadata_type: str
    component: str
    status: str  # 'new' | 'modified' | 'unchanged'
    added_lines: int = 0
    removed_lines: int = 0
    diff: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "type": self.metadata_type,
            "component": self.component,
            "status": self.status,
            "added_lines": self.added_lines,
            "removed_lines": self.removed_lines,
            "diff": self.diff,
        }


def _reader(sf: SalesforceClient) -> MetadataReader:
    return MetadataReader(
        MetadataClient(sf.instance_url, sf.access_token, sf.api_version)
    )


async def retrieve_current(
    sf: SalesforceClient, types: dict[str, list[str]]
) -> dict[str, str]:
    """The target org's current source for the components in a change set.

    A retrieve that fails because nothing matches is not an error — it means
    every component is new. That distinction matters for the rollback plan, so
    the failure is swallowed here and reflected in the diff instead.
    """
    try:
        result = await _reader(sf).retrieve(types)
    except SalesforceError as exc:
        log.info("deployment.retrieve_empty", reason=exc.message)
        return {}
    return {p: c for p, c in result.files.items() if p != "package.xml"}


def compute_diff(
    proposed: dict[str, str], current: dict[str, str], *, context: int = 3
) -> list[ComponentDiff]:
    """Compare proposed source against what the org has today."""
    diffs: list[ComponentDiff] = []
    for path, new_source in sorted(proposed.items()):
        if path == "package.xml":
            continue
        parts = path.split("/")
        metadata_type = FOLDER_TO_TYPE.get(parts[0], parts[0])
        component = parts[-1].rsplit(".", 1)[0]
        old_source = current.get(path)

        if old_source is None:
            diffs.append(
                ComponentDiff(
                    path=path,
                    metadata_type=metadata_type,
                    component=component,
                    status="new",
                    added_lines=len(new_source.splitlines()),
                    diff="",
                )
            )
            continue

        old_lines = old_source.splitlines()
        new_lines = new_source.splitlines()
        if old_lines == new_lines:
            diffs.append(
                ComponentDiff(
                    path=path,
                    metadata_type=metadata_type,
                    component=component,
                    status="unchanged",
                )
            )
            continue

        unified = list(
            difflib.unified_diff(
                old_lines, new_lines, fromfile=f"org/{path}", tofile=f"new/{path}",
                lineterm="", n=context,
            )
        )
        diffs.append(
            ComponentDiff(
                path=path,
                metadata_type=metadata_type,
                component=component,
                status="modified",
                added_lines=sum(
                    1 for line in unified if line.startswith("+") and not line.startswith("+++")
                ),
                removed_lines=sum(
                    1 for line in unified if line.startswith("-") and not line.startswith("---")
                ),
                # Cap the stored diff: an approval card needs the shape of the
                # change, not a whole file.
                diff="\n".join(unified[:400]),
            )
        )
    return diffs


def build_rollback_plan(
    proposed: dict[str, str], current: dict[str, str], types: dict[str, list[str]]
) -> dict[str, Any]:
    """What undoing this deployment would actually mean.

    Two mechanisms, and the plan is explicit about which applies where:
      * a component that existed before is restored by redeploying its prior
        source, which is a genuine revert;
      * a component created by this deployment is removed by a destructive
        change — possible for most types, and named as not-possible for the
        rest rather than glossed over.
    """
    restore = {p: c for p, c in current.items() if p in proposed}
    created = [p for p in proposed if p not in current and not p.endswith("-meta.xml")]

    destructive: dict[str, list[str]] = {}
    not_removable: list[str] = []
    for path in created:
        parts = path.split("/")
        metadata_type = FOLDER_TO_TYPE.get(parts[0])
        component = parts[-1].rsplit(".", 1)[0]
        if metadata_type in DESTRUCTIVELY_REMOVABLE:
            destructive.setdefault(metadata_type, []).append(component)
        else:
            not_removable.append(path)

    caveats: list[str] = []
    if not_removable:
        caveats.append(
            "These components cannot be removed by a destructive change and would "
            "have to be deleted manually: " + ", ".join(sorted(not_removable)) + "."
        )
    if any(t == "CustomField" for t in types):
        caveats.append(
            "Removing a custom field deletes the data stored in it. A rollback that "
            "deletes fields is not a no-op."
        )
    if "Flow" in types:
        caveats.append(
            "Flow versions are never deleted by Salesforce. Rolling back a flow "
            "reactivates the previous version; the new version stays in the org as an "
            "inactive version."
        )

    return {
        "restore_files": restore,
        "restore_manifest": manifest_from_files(restore),
        "destructive_manifest": destructive,
        "components_created": created,
        "not_removable": not_removable,
        "caveats": caveats,
        "possible": bool(restore or destructive),
        "summary": (
            f"{len(restore)} component(s) would be restored to their previous source; "
            f"{sum(len(v) for v in destructive.values())} newly created component(s) "
            "would be deleted."
        ),
    }


def build_destructive_package(
    destructive: dict[str, list[str]], api_version: str
) -> bytes:
    """A destructiveChangesPost package: an empty manifest plus deletions."""
    files = {
        "package.xml": build_package_xml({}, api_version),
        "destructiveChangesPost.xml": build_package_xml(destructive, api_version),
    }
    return build_zip(files)


def summarize_diff(diffs: list[ComponentDiff]) -> dict[str, Any]:
    by_status: dict[str, int] = {}
    for diff in diffs:
        by_status[diff.status] = by_status.get(diff.status, 0) + 1
    return {
        "components": len(diffs),
        "new": by_status.get("new", 0),
        "modified": by_status.get("modified", 0),
        "unchanged": by_status.get("unchanged", 0),
        "lines_added": sum(d.added_lines for d in diffs),
        "lines_removed": sum(d.removed_lines for d in diffs),
        "no_effective_change": all(d.status == "unchanged" for d in diffs) and bool(diffs),
    }


def advance(change_set: ChangeSet, state: ChangeSetState, **fields: Any) -> None:
    change_set.state = state
    for key, value in fields.items():
        setattr(change_set, key, value)


def deployment_row(
    change_set: ChangeSet, *, check_only: bool, manifest: dict[str, list[str]]
) -> Deployment:
    return Deployment(
        company_id=change_set.company_id,
        project_id=change_set.project_id,
        agent_run_id=change_set.agent_run_id,
        change_set_id=change_set.id,
        user_id=change_set.user_id,
        salesforce_connection_id=change_set.salesforce_connection_id,
        check_only=check_only,
        package_manifest=manifest,
        status="Queued",
    )
