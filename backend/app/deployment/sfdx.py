"""Metadata API format → SFDX source format.

The Metadata API hands back one large file per object: `Account.object` holds
every field, validation rule, list view and record type in a single XML
document. No Salesforce team stores it that way, because two people adding
different fields to the same object produce a merge conflict in a file neither
of them meaningfully touched.

Source format decomposes it — one file per component — which is what `sfdx
force:source:retrieve` produces and what a repository should contain:

    unpackaged/objects/Account.object
        ↓
    force-app/main/default/objects/Account/Account.object-meta.xml
    force-app/main/default/objects/Account/fields/Tier__c.field-meta.xml
    force-app/main/default/objects/Account/fields/Rating__c.field-meta.xml
    force-app/main/default/objects/Account/validationRules/Tier_Required.validationRule-meta.xml
    force-app/main/default/objects/Account/listViews/All.listView-meta.xml

Written with the standard library rather than by shelling out to the Salesforce
CLI. This service has no Node runtime, no `sf` binary and no authenticated CLI
session, and pretending otherwise would mean a feature that works on a
developer's laptop and nowhere else.

`DECOMPOSED` lists exactly what is split out. Anything else stays in the
object's own file, which is correct — the CLI does the same for the parts it
does not decompose — and `describe()` says so rather than implying completeness.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from defusedxml import ElementTree as DefusedET

#: Where source format lives in a repository. The default SFDX package
#: directory; a project may use another, which `to_source_format` accepts.
SOURCE_ROOT = "force-app/main/default"

METADATA_NS = "http://soap.sforce.com/2006/04/metadata"

#: Child elements of CustomObject that become their own file:
#:   XML tag -> (folder, filename suffix, **root element of the new file**)
#:
#: The third value is the one that is easy to get wrong and expensive when it
#: is. A decomposed field file's root element is `CustomField` — the *metadata
#: type* — not `fields`, the tag it was nested under. Emitting `<fields>` there
#: produces a file that looks plausible, that git accepts, and that the
#: Metadata API rejects on deploy.
DECOMPOSED: dict[str, tuple[str, str, str]] = {
    "fields": ("fields", "field", "CustomField"),
    "validationRules": ("validationRules", "validationRule", "ValidationRule"),
    "listViews": ("listViews", "listView", "ListView"),
    "recordTypes": ("recordTypes", "recordType", "RecordType"),
    "compactLayouts": ("compactLayouts", "compactLayout", "CompactLayout"),
    "webLinks": ("webLinks", "webLink", "WebLink"),
    "fieldSets": ("fieldSets", "fieldSet", "FieldSet"),
    "businessProcesses": ("businessProcesses", "businessProcess", "BusinessProcess"),
    "sharingReasons": ("sharingReasons", "sharingReason", "SharingReason"),
    "indexes": ("indexes", "index", "Index"),
}

#: Metadata-API folder → (source folder, file suffix). A type not listed keeps
#: its folder name and gains `-meta.xml`, which is the general rule.
FOLDER_RULES: dict[str, tuple[str, str]] = {
    "classes": ("classes", "cls"),
    "triggers": ("triggers", "trigger"),
    "pages": ("pages", "page"),
    "components": ("components", "component"),
    "aura": ("aura", ""),
    "lwc": ("lwc", ""),
    "staticresources": ("staticresources", "resource"),
}

#: Types whose payload is code, not XML: the body file is stored as-is and its
#: `-meta.xml` sidecar stays beside it, exactly as the CLI does.
CODE_SUFFIXES = {"cls", "trigger", "page", "component"}


@dataclass
class ConversionResult:
    files: dict[str, str] = field(default_factory=dict)
    #: Component counts by type, for reporting what was actually produced.
    counts: dict[str, int] = field(default_factory=dict)
    #: Paths that were passed through without decomposition, and why.
    passthrough: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {
            "file_count": len(self.files),
            "counts": dict(sorted(self.counts.items())),
            "passthrough_count": len(self.passthrough),
            "warnings": self.warnings,
        }


def to_source_format(
    files: dict[str, str], *, root: str = SOURCE_ROOT
) -> ConversionResult:
    """Convert a Metadata API retrieve into SFDX source format.

    `files` is what `unpack_zip` produces: `{path: text}` with paths like
    `unpackaged/objects/Account.object`.
    """
    result = ConversionResult()

    for path, content in sorted(files.items()):
        relative = _strip_package_root(path)
        if not relative or relative == "package.xml":
            # package.xml is a manifest of the retrieve, not source. Keeping it
            # in a repository would go stale the moment anything else changed.
            continue

        parts = relative.split("/")
        if len(parts) < 2:
            result.passthrough.append(path)
            continue

        folder, name = parts[0], "/".join(parts[1:])
        if folder == "objects" and name.endswith(".object"):
            _decompose_object(name[: -len(".object")], content, root, result)
            continue

        target = _source_path(folder, name, root)
        if target is None:
            result.passthrough.append(path)
            continue
        result.files[target] = content
        _count(result, folder)

    return result


def _strip_package_root(path: str) -> str:
    """Drop the retrieve's package folder, whatever it is called.

    A retrieve of the unpackaged set uses `unpackaged/`; a named package uses
    its own name. Assuming `unpackaged/` would silently mis-handle the second.
    """
    cleaned = path.lstrip("/")
    parts = cleaned.split("/")
    if len(parts) > 1 and parts[0] not in FOLDER_RULES and parts[0] != "objects":
        # The first segment is the package folder when what follows looks like
        # a metadata folder, or when it is package.xml.
        if len(parts) > 2 or parts[1] == "package.xml" or "." not in parts[0]:
            return "/".join(parts[1:])
    return cleaned


def _source_path(folder: str, name: str, root: str) -> str | None:
    """Where one non-object file belongs in source format."""
    if not name:
        return None

    # Bundle types keep their directory shape unchanged.
    if folder in {"aura", "lwc"}:
        return f"{root}/{folder}/{name}"

    if name.endswith("-meta.xml"):
        # A sidecar that already exists in the retrieve (Apex, pages, static
        # resources) travels beside its body.
        return f"{root}/{folder}/{name}"

    suffix = name.rsplit(".", 1)[-1] if "." in name else ""
    if suffix in CODE_SUFFIXES:
        return f"{root}/{folder}/{name}"

    # Everything else is XML metadata and gains the -meta.xml marker.
    return f"{root}/{folder}/{name}-meta.xml"


def _count(result: ConversionResult, kind: str) -> None:
    result.counts[kind] = result.counts.get(kind, 0) + 1


# ---------------------------------------------------------------------------
# CustomObject decomposition
# ---------------------------------------------------------------------------
def _decompose_object(
    object_name: str, xml: str, root: str, result: ConversionResult
) -> None:
    """Split one `.object` file into the files a repository should hold."""
    try:
        tree = DefusedET.fromstring(xml)
    except Exception as exc:
        # A file that will not parse is passed through whole rather than
        # dropped: losing a customer's metadata to a parse error would be far
        # worse than an undecomposed file in their repository.
        result.files[f"{root}/objects/{object_name}/{object_name}.object-meta.xml"] = xml
        result.warnings.append(
            f"{object_name}.object could not be parsed ({type(exc).__name__}); it "
            "was written whole rather than decomposed."
        )
        _count(result, "objects")
        return

    base = f"{root}/objects/{object_name}"
    remaining: list[str] = []

    for child in list(tree):
        tag = _localname(child.tag)
        rule = DECOMPOSED.get(tag)
        if rule is None:
            remaining.append(_serialize(child))
            continue

        folder, suffix, root_tag = rule
        member = _member_name(child)
        if not member:
            # A component with no fullName cannot be given a filename. Keeping
            # it in the object file preserves it.
            remaining.append(_serialize(child))
            result.warnings.append(
                f"A <{tag}> in {object_name} has no fullName and stayed in the "
                "object file."
            )
            continue

        # The component's *children* become the body; the element itself is
        # replaced by the metadata type as the root.
        result.files[f"{base}/{folder}/{member}.{suffix}-meta.xml"] = _wrap(
            root_tag, _serialize_children(child)
        )
        _count(result, folder)

    result.files[f"{base}/{object_name}.object-meta.xml"] = _wrap(
        "CustomObject", "\n".join(remaining)
    )
    _count(result, "objects")


def _localname(tag: str) -> str:
    return tag.split("}", 1)[-1] if "}" in tag else tag


def _member_name(element: Any) -> str:
    for child in element:
        if _localname(child.tag) == "fullName":
            return (child.text or "").strip()
    return ""


def _serialize_children(element: Any) -> str:
    """The element's children, serialized — without the element itself."""
    return "\n".join(_serialize(child) for child in element)


def _serialize(element: Any) -> str:
    """One element as indented XML, with the namespace prefix removed.

    ElementTree writes `ns0:` prefixes for a namespaced document, which are
    valid but produce a diff against CLI-generated source on every single line.
    A repository where every file differs cosmetically is a repository where
    nobody reads diffs.
    """
    import xml.etree.ElementTree as ET

    clone = _strip_namespace(element)
    ET.indent(clone, space="    ")
    body = ET.tostring(clone, encoding="unicode")
    return _indent_block(body.strip())


def _strip_namespace(element: Any) -> Any:
    import copy
    import xml.etree.ElementTree as ET

    clone = copy.deepcopy(element)
    for node in clone.iter():
        if isinstance(node.tag, str):
            node.tag = _localname(node.tag)
        node.attrib = {
            _localname(k): v for k, v in node.attrib.items() if not k.startswith("xmlns")
        }
    if not isinstance(clone, ET.Element):  # pragma: no cover - defensive
        return clone
    return clone


def _indent_block(body: str) -> str:
    return "\n".join(f"    {line}" if line.strip() else line for line in body.split("\n"))


def _wrap(root_tag: str, body: str) -> str:
    inner = body.strip("\n")
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<{root_tag} xmlns="{METADATA_NS}">\n'
        f"{inner}\n"
        f"</{root_tag}>\n"
    )


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
def describe() -> dict[str, Any]:
    """What this converter does and does not do, stated plainly."""
    return {
        "source_root": SOURCE_ROOT,
        "decomposed_object_children": sorted(DECOMPOSED),
        "implementation": "pure Python; no Salesforce CLI or Node runtime required",
        "limitations": [
            "Binary members (static resource archives, documents) are skipped by "
            "the retrieve unpacker and never reach this converter.",
            "Object children not in the decomposed list stay inside the object's "
            "own file, which is also what the Salesforce CLI does.",
            "package.xml is dropped: it describes one retrieve, not the source.",
            "This produces source format for a repository. It does not create an "
            "sfdx-project.json or a full SFDX project scaffold.",
        ],
    }


def manifest_from_source(files: dict[str, str]) -> dict[str, list[str]]:
    """Reverse view: which components a set of source files represents.

    Used to describe what a commit contains without re-reading the org.
    """
    types: dict[str, set[str]] = {}
    for path in files:
        parts = path.split("/")
        if "objects" in parts:
            index = parts.index("objects")
            tail = parts[index + 1 :]
            if len(tail) == 2 and tail[1].endswith(".object-meta.xml"):
                types.setdefault("CustomObject", set()).add(tail[0])
            elif len(tail) == 3:
                object_name = tail[0]
                member = re.sub(r"\.[^.]+-meta\.xml$", "", tail[2])
                kind = {
                    "fields": "CustomField",
                    "validationRules": "ValidationRule",
                    "listViews": "ListView",
                    "recordTypes": "RecordType",
                    "compactLayouts": "CompactLayout",
                    "webLinks": "WebLink",
                    "fieldSets": "FieldSet",
                }.get(tail[1])
                if kind:
                    types.setdefault(kind, set()).add(f"{object_name}.{member}")
            continue

        name = parts[-1]
        folder = parts[-2] if len(parts) > 1 else ""
        kind = {
            "classes": "ApexClass",
            "triggers": "ApexTrigger",
            "flows": "Flow",
            "layouts": "Layout",
            "permissionsets": "PermissionSet",
            "profiles": "Profile",
            "pages": "ApexPage",
        }.get(folder)
        if kind and not name.endswith("-meta.xml"):
            types.setdefault(kind, set()).add(name.rsplit(".", 1)[0])
        elif kind and name.endswith("-meta.xml"):
            types.setdefault(kind, set()).add(
                re.sub(r"\.[^.]+-meta\.xml$", "", name)
            )

    return {k: sorted(v) for k, v in sorted(types.items())}
