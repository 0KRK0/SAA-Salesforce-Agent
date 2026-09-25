"""Permission analysis: who can do what, and where that is too much.

Salesforce access is the union of many grants — a profile, every assigned
permission set, permission set groups, role hierarchy, sharing rules. Asking
"who can delete Accounts?" therefore means resolving grants across all of them,
which is exactly the work an admin cannot do by clicking around.

Two rules shape this module:

  * **Effective access is a union, not a lookup.** A user's permission is the
    OR of every grant they hold. Reporting only the profile is the single most
    common way a permissions answer is wrong.

  * **A finding is a risk, not a verdict.** "This permission set grants Modify
    All Data to 40 users" is a fact worth surfacing. Whether it is wrong
    depends on the business, so findings carry severity and rationale and never
    instruct.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: Permissions that meaningfully change what someone can reach org-wide.
#: Field name on PermissionSet -> (label, severity, why it matters).
DANGEROUS_PERMISSIONS: dict[str, tuple[str, str, str]] = {
    "PermissionsModifyAllData": (
        "Modify All Data",
        "critical",
        "Bypasses every sharing rule, object permission and field-level security "
        "in the org, and permits mass delete.",
    ),
    "PermissionsViewAllData": (
        "View All Data",
        "high",
        "Reads every record in the org regardless of sharing — including data the "
        "person's own team cannot see.",
    ),
    "PermissionsAuthorApex": (
        "Author Apex",
        "critical",
        "Can write and deploy code that runs with system privileges.",
    ),
    "PermissionsModifyMetadata": (
        "Modify Metadata Through Metadata API",
        "high",
        "Can change org configuration through the API without going through Setup.",
    ),
    "PermissionsManageUsers": (
        "Manage Users",
        "critical",
        "Can create users, reset passwords and grant permissions — including to "
        "themselves.",
    ),
    "PermissionsDataExport": (
        "Weekly Data Export",
        "high",
        "Can export the entire org's data on a schedule.",
    ),
    "PermissionsViewAllUsers": (
        "View All Users",
        "low",
        "Sees every user record regardless of sharing.",
    ),
    "PermissionsManageSharing": (
        "Manage Sharing",
        "high",
        "Can change who sees which records org-wide.",
    ),
    "PermissionsCustomizeApplication": (
        "Customize Application",
        "high",
        "Can change objects, fields, page layouts and most configuration.",
    ),
    "PermissionsManageInternalUsers": (
        "Manage Internal Users",
        "critical",
        "Can create and modify internal user accounts.",
    ),
    "PermissionsInstallPackaging": (
        "Download AppExchange Packages",
        "medium",
        "Can install packages that bring their own code and permissions.",
    ),
    "PermissionsApiEnabled": (
        "API Enabled",
        "low",
        "Can reach org data through the API, outside the UI's guardrails.",
    ),
    "PermissionsPasswordNeverExpires": (
        "Password Never Expires",
        "medium",
        "Exempts the user from the org's password rotation policy.",
    ),
}

OBJECT_PERMISSION_FIELDS = {
    "PermissionsRead": "read",
    "PermissionsCreate": "create",
    "PermissionsEdit": "edit",
    "PermissionsDelete": "delete",
    "PermissionsViewAllRecords": "view_all",
    "PermissionsModifyAllRecords": "modify_all",
}

_SEVERITY_RANK = {"critical": 4, "high": 3, "medium": 2, "low": 1}


@dataclass
class AccessGrant:
    """One route by which a user holds a permission."""

    via: str  # 'Profile' | 'Permission Set' | 'Permission Set Group'
    name: str
    permissions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"via": self.via, "name": self.name, "permissions": self.permissions}


@dataclass
class Finding:
    severity: str
    title: str
    detail: str
    subjects: list[str] = field(default_factory=list)
    why_it_matters: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "finding": self.title,
            "detail": self.detail,
            "affected": self.subjects[:40],
            "affected_count": len(self.subjects),
            "why_it_matters": self.why_it_matters,
        }


def effective_object_access(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Union object permissions across every grant a user holds.

    A user with read on their profile and delete on one permission set has
    delete. Reporting the profile alone would say otherwise.
    """
    effective = {name: False for name in OBJECT_PERMISSION_FIELDS.values()}
    grants: list[AccessGrant] = []
    for row in rows:
        parent = row.get("Parent") or {}
        held = [
            label
            for api_field, label in OBJECT_PERMISSION_FIELDS.items()
            if row.get(api_field)
        ]
        for label in held:
            effective[label] = True
        grants.append(
            AccessGrant(
                via="Profile" if parent.get("IsOwnedByProfile") else "Permission Set",
                name=(parent.get("Profile") or {}).get("Name") or parent.get("Label") or "",
                permissions=held,
            )
        )
    return {
        "effective": effective,
        "granted_by": [g.to_dict() for g in grants],
        "summary": ", ".join(k for k, v in effective.items() if v) or "no access",
    }


def analyze_permission_sets(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Summarize profiles/permission sets by the dangerous permissions they carry."""
    out = []
    for row in rows:
        held = []
        for api_field, (label, severity, why) in DANGEROUS_PERMISSIONS.items():
            if row.get(api_field):
                held.append({"permission": label, "severity": severity, "why": why})
        if not held:
            continue
        held.sort(key=lambda p: -_SEVERITY_RANK.get(p["severity"], 0))
        profile = row.get("Profile") or {}
        out.append(
            {
                "id": row.get("Id"),
                "name": profile.get("Name") or row.get("Label") or row.get("Name"),
                "type": "Profile" if row.get("IsOwnedByProfile") else "Permission Set",
                "is_custom": bool(row.get("IsCustom", True)),
                "elevated_permissions": held,
                "max_severity": held[0]["severity"],
            }
        )
    out.sort(key=lambda r: -_SEVERITY_RANK.get(r["max_severity"], 0))
    return out


def security_findings(
    *,
    elevated: list[dict[str, Any]],
    assignment_counts: dict[str, int],
    inactive_with_access: list[dict[str, Any]],
    admin_users: list[dict[str, Any]],
    total_users: int,
) -> list[Finding]:
    """Turn permission facts into ranked, explained risk findings."""
    findings: list[Finding] = []

    for entry in elevated:
        assigned = assignment_counts.get(str(entry.get("id")), 0)
        critical = [p for p in entry["elevated_permissions"] if p["severity"] == "critical"]
        if not critical:
            continue
        if assigned == 0 and entry["type"] == "Permission Set":
            findings.append(
                Finding(
                    severity="low",
                    title=f"Unused permission set with elevated access: {entry['name']}",
                    detail=(
                        f"Grants {', '.join(p['permission'] for p in critical)} but is "
                        "assigned to nobody."
                    ),
                    why_it_matters=(
                        "An unassigned permission set is one click from being assigned. "
                        "Deleting unused ones shrinks what can go wrong."
                    ),
                )
            )
            continue
        findings.append(
            Finding(
                severity="critical" if assigned > 5 else "high",
                title=(
                    f"{entry['name']} grants "
                    f"{', '.join(p['permission'] for p in critical)}"
                ),
                detail=(
                    f"{assigned} user(s) hold this {entry['type'].lower()}."
                    if assigned
                    else "Assignment count unavailable."
                ),
                why_it_matters=critical[0]["why"],
            )
        )

    if inactive_with_access:
        findings.append(
            Finding(
                severity="medium",
                title="Inactive users still hold elevated permissions",
                detail=(
                    f"{len(inactive_with_access)} deactivated user(s) retain permission "
                    "set assignments granting elevated access."
                ),
                subjects=[str(u.get("name")) for u in inactive_with_access],
                why_it_matters=(
                    "Deactivation blocks login but leaves the grant in place. If the "
                    "account is ever reactivated — or its credentials are reused for an "
                    "integration — the access comes back with it."
                ),
            )
        )

    if admin_users and total_users:
        ratio = len(admin_users) / total_users
        if ratio > 0.1 and len(admin_users) > 3:
            findings.append(
                Finding(
                    severity="high",
                    title=f"{len(admin_users)} of {total_users} users have administrative access",
                    detail=(
                        f"{ratio * 100:.0f}% of active users hold Modify All Data or an "
                        "administrator profile."
                    ),
                    subjects=[str(u.get("name")) for u in admin_users],
                    why_it_matters=(
                        "Every administrator is a full-org blast radius. Most orgs need "
                        "far fewer than they have; the usual cause is granting the admin "
                        "profile to solve a narrow permission problem."
                    ),
                )
            )

    findings.sort(key=lambda f: -_SEVERITY_RANK.get(f.severity, 0))
    return findings
