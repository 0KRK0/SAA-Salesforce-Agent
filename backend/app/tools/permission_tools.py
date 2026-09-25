"""Permissions and security agent.

Read-only analysis, deliberately. Reading permissions is how you find problems;
changing them is how you cause outages, so mutation lives behind
`modify_permissions`, which is HIGH risk, needs security-admin approval, and
does one narrow thing (permission set assignment) rather than offering a
general-purpose way to edit access.

The questions this answers:
  "Who can delete Accounts?"
  "Which users can export our customer data?"
  "Why can't this user edit this field?"
  "Where is our access excessive?"

Every answer resolves effective access as the union of profile and permission
sets — the thing that makes hand-checking unreliable.
"""

from __future__ import annotations

from typing import Any

from app.analysis.permissions import (
    DANGEROUS_PERMISSIONS,
    analyze_permission_sets,
    effective_object_access,
    security_findings,
)
from app.models import RiskLevel
from app.salesforce.errors import SalesforceError
from app.salesforce.inspect import soql_literal
from app.tools.base import Tool, ToolContext, ToolValidationError, fail, ok
from app.tools.registry import registry

_PERM_FIELDS = ", ".join(sorted(DANGEROUS_PERMISSIONS))


# ---------------------------------------------------------------------------
# inspect_permissions
# ---------------------------------------------------------------------------
INSPECT_DESCRIPTION = """Inspect Salesforce access: what a user can do, who can act on
an object, or what a profile/permission set grants.

Pick a mode:
  * `user_id`   — everything one person can do, resolved across their profile
    and every assigned permission set.
  * `object`    — who can read/create/edit/delete that object, and through what.
  * `permission_set` / `profile` — what one grant carries.

Effective access is always the union of every grant. A user with read on their
profile and delete on one permission set has delete; reporting the profile
alone would be wrong, which is why hand-checking this in Setup so often is.
"""


async def _inspect_execute(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    sf = ctx.require_sf()
    try:
        if args.get("user_id"):
            return await _user_access(sf, str(args["user_id"]), args.get("object"))
        if args.get("object"):
            return await _object_access(sf, str(args["object"]))
        if args.get("permission_set") or args.get("profile"):
            return await _grant_detail(
                sf,
                str(args.get("permission_set") or ""),
                str(args.get("profile") or ""),
            )
    except SalesforceError as exc:
        return exc.to_dict()
    return fail(
        "MISSING_ARGUMENT",
        "Give one of: `user_id`, `object`, `permission_set` or `profile`.",
    )


async def _user_access(
    sf: Any, user_id: str, object_name: str | None
) -> dict[str, Any]:
    users = await sf.query(
        "SELECT Id, Name, Username, IsActive, UserType, Profile.Name, "
        "Profile.PermissionsModifyAllData, Profile.PermissionsViewAllData, "
        f"UserRole.Name FROM User WHERE Id = '{soql_literal(user_id)}' LIMIT 1"
    )
    rows = users.get("records") or []
    if not rows:
        return fail("USER_NOT_FOUND", f"No user with id {user_id} exists in this org.")
    user = rows[0]
    profile = user.get("Profile") or {}

    assignments = await sf.query(
        "SELECT PermissionSet.Id, PermissionSet.Label, PermissionSet.Name, "
        "PermissionSet.IsOwnedByProfile, PermissionSetGroup.DeveloperName "
        f"FROM PermissionSetAssignment WHERE AssigneeId = '{soql_literal(user_id)}' "
        "LIMIT 200"
    )
    sets = [
        {
            "id": (a.get("PermissionSet") or {}).get("Id"),
            "label": (a.get("PermissionSet") or {}).get("Label"),
            "name": (a.get("PermissionSet") or {}).get("Name"),
            "is_profile": (a.get("PermissionSet") or {}).get("IsOwnedByProfile"),
            "group": (a.get("PermissionSetGroup") or {}).get("DeveloperName"),
        }
        for a in assignments.get("records") or []
    ]

    elevated = await sf.query(
        f"SELECT Id, Label, Name, IsOwnedByProfile, Profile.Name, {_PERM_FIELDS} "
        "FROM PermissionSet WHERE Id IN (SELECT PermissionSetId FROM "
        f"PermissionSetAssignment WHERE AssigneeId = '{soql_literal(user_id)}') LIMIT 200"
    )
    elevated_rows = analyze_permission_sets(elevated.get("records") or [])

    payload: dict[str, Any] = {
        "user": {
            "id": user.get("Id"),
            "name": user.get("Name"),
            "username": user.get("Username"),
            "active": user.get("IsActive"),
            "user_type": user.get("UserType"),
            "profile": profile.get("Name"),
            "role": (user.get("UserRole") or {}).get("Name"),
        },
        "permission_sets": [s for s in sets if not s["is_profile"]],
        "elevated_permissions": elevated_rows,
        "administrative": bool(profile.get("PermissionsModifyAllData"))
        or any(
            p["permission"] == "Modify All Data"
            for row in elevated_rows
            for p in row["elevated_permissions"]
        ),
    }

    if object_name:
        perms = await sf.query(
            "SELECT PermissionsRead, PermissionsCreate, PermissionsEdit, "
            "PermissionsDelete, PermissionsViewAllRecords, PermissionsModifyAllRecords, "
            "Parent.Label, Parent.IsOwnedByProfile, Parent.Profile.Name "
            f"FROM ObjectPermissions WHERE SobjectType = '{soql_literal(object_name)}' "
            "AND ParentId IN (SELECT PermissionSetId FROM PermissionSetAssignment "
            f"WHERE AssigneeId = '{soql_literal(user_id)}') LIMIT 200"
        )
        payload["object_access"] = {
            "object": object_name,
            **effective_object_access(perms.get("records") or []),
        }
    return ok(**payload)


async def _object_access(sf: Any, object_name: str) -> dict[str, Any]:
    perms = await sf.query(
        "SELECT PermissionsRead, PermissionsCreate, PermissionsEdit, "
        "PermissionsDelete, PermissionsViewAllRecords, PermissionsModifyAllRecords, "
        "Parent.Label, Parent.Name, Parent.IsOwnedByProfile, Parent.Profile.Name "
        f"FROM ObjectPermissions WHERE SobjectType = '{soql_literal(object_name)}' "
        "LIMIT 500"
    )
    rows = perms.get("records") or []
    grouped: dict[str, list[str]] = {
        "read": [], "create": [], "edit": [], "delete": [],
        "view_all": [], "modify_all": [],
    }
    for row in rows:
        parent = row.get("Parent") or {}
        label = (parent.get("Profile") or {}).get("Name") or parent.get("Label") or ""
        kind = "profile" if parent.get("IsOwnedByProfile") else "permission set"
        who = f"{label} ({kind})"
        for api_field, key in (
            ("PermissionsRead", "read"),
            ("PermissionsCreate", "create"),
            ("PermissionsEdit", "edit"),
            ("PermissionsDelete", "delete"),
            ("PermissionsViewAllRecords", "view_all"),
            ("PermissionsModifyAllRecords", "modify_all"),
        ):
            if row.get(api_field):
                grouped[key].append(who)

    return ok(
        object=object_name,
        access_by_permission={k: sorted(set(v)) for k, v in grouped.items()},
        counts={k: len(set(v)) for k, v in grouped.items()},
        note=(
            "This lists profiles and permission sets, not individual users. Anyone "
            "holding one of these has the permission. Users with Modify All Data have "
            "full access regardless of what appears here."
        ),
    )


async def _grant_detail(sf: Any, permission_set: str, profile: str) -> dict[str, Any]:
    if profile:
        where = f"Profile.Name = '{soql_literal(profile)}' AND IsOwnedByProfile = true"
    else:
        where = (
            f"(Name = '{soql_literal(permission_set)}' OR "
            f"Label = '{soql_literal(permission_set)}')"
        )
    data = await sf.query(
        f"SELECT Id, Name, Label, IsOwnedByProfile, IsCustom, Profile.Name, {_PERM_FIELDS} "
        f"FROM PermissionSet WHERE {where} LIMIT 5"
    )
    rows = data.get("records") or []
    if not rows:
        return fail(
            "NOT_FOUND",
            f"No {'profile' if profile else 'permission set'} named "
            f"'{profile or permission_set}' exists in this org.",
        )
    analyzed = analyze_permission_sets(rows)
    row = rows[0]
    assignments = await sf.query(
        "SELECT COUNT() FROM PermissionSetAssignment "
        f"WHERE PermissionSetId = '{soql_literal(str(row['Id']))}'"
    )
    objects = await sf.query(
        "SELECT SobjectType, PermissionsRead, PermissionsCreate, PermissionsEdit, "
        "PermissionsDelete, PermissionsViewAllRecords, PermissionsModifyAllRecords "
        f"FROM ObjectPermissions WHERE ParentId = '{soql_literal(str(row['Id']))}' LIMIT 200"
    )
    return ok(
        name=(row.get("Profile") or {}).get("Name") or row.get("Label"),
        type="Profile" if row.get("IsOwnedByProfile") else "Permission Set",
        is_custom=row.get("IsCustom"),
        assigned_users=assignments.get("totalSize", 0),
        elevated_permissions=(analyzed[0]["elevated_permissions"] if analyzed else []),
        object_permissions=[
            {
                "object": o.get("SobjectType"),
                "permissions": [
                    label
                    for api_field, label in (
                        ("PermissionsRead", "read"),
                        ("PermissionsCreate", "create"),
                        ("PermissionsEdit", "edit"),
                        ("PermissionsDelete", "delete"),
                        ("PermissionsViewAllRecords", "view all"),
                        ("PermissionsModifyAllRecords", "modify all"),
                    )
                    if o.get(api_field)
                ],
            }
            for o in objects.get("records") or []
        ][:100],
    )


registry.register(
    Tool(
        name="inspect_permissions",
        description=INSPECT_DESCRIPTION,
        input_schema={
            "type": "object",
            "properties": {
                "user_id": {"type": "string"},
                "object": {"type": "string"},
                "permission_set": {"type": "string"},
                "profile": {"type": "string"},
            },
            "additionalProperties": False,
        },
        output_schema={"type": "object", "properties": {"success": {"type": "boolean"}}},
        risk=RiskLevel.LOW,
        requires_approval=False,
        execute=_inspect_execute,
        audit_action="salesforce.inspect_permissions",
        tags=["security", "permissions", "read"],
    )
)


# ---------------------------------------------------------------------------
# audit_security
# ---------------------------------------------------------------------------
AUDIT_DESCRIPTION = """Audit the org's access posture and rank what is risky.

Finds: profiles and permission sets carrying Modify All Data, View All Data,
Author Apex, Manage Users and other org-wide permissions; how many people hold
each; deactivated users who still carry elevated grants; and whether the
proportion of administrators is out of line.

Findings carry a severity and an explanation of why the permission matters.
They are risks to review, not verdicts — whether a given grant is wrong depends
on the business, and the tool does not pretend to know that.
"""


async def _audit_execute(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    sf = ctx.require_sf()
    try:
        permission_sets = await sf.query(
            f"SELECT Id, Name, Label, IsOwnedByProfile, IsCustom, Profile.Name, "
            f"{_PERM_FIELDS} FROM PermissionSet LIMIT 500"
        )
        elevated = analyze_permission_sets(permission_sets.get("records") or [])

        counts: dict[str, int] = {}
        for entry in elevated[:60]:
            result = await sf.query(
                "SELECT COUNT() FROM PermissionSetAssignment WHERE PermissionSetId = "
                f"'{soql_literal(str(entry['id']))}'"
            )
            counts[str(entry["id"])] = int(result.get("totalSize") or 0)

        total = await sf.query("SELECT COUNT() FROM User WHERE IsActive = true")
        total_users = int(total.get("totalSize") or 0)

        admins = await sf.query(
            "SELECT Id, Name, Username, Profile.Name FROM User WHERE IsActive = true "
            "AND Profile.PermissionsModifyAllData = true LIMIT 200"
        )
        admin_users = [
            {
                "id": u.get("Id"),
                "name": u.get("Name"),
                "profile": (u.get("Profile") or {}).get("Name"),
            }
            for u in admins.get("records") or []
        ]

        inactive = await sf.query(
            "SELECT Assignee.Id, Assignee.Name, PermissionSet.Label FROM "
            "PermissionSetAssignment WHERE Assignee.IsActive = false "
            "AND PermissionSet.IsOwnedByProfile = false LIMIT 200"
        )
        elevated_ids = {str(e["id"]) for e in elevated}
        inactive_with_access = [
            {
                "user_id": (r.get("Assignee") or {}).get("Id"),
                "name": (r.get("Assignee") or {}).get("Name"),
                "permission_set": (r.get("PermissionSet") or {}).get("Label"),
            }
            for r in inactive.get("records") or []
        ]
    except SalesforceError as exc:
        return exc.to_dict()

    findings = security_findings(
        elevated=elevated,
        assignment_counts=counts,
        inactive_with_access=inactive_with_access,
        admin_users=admin_users,
        total_users=total_users,
    )
    return ok(
        summary={
            "active_users": total_users,
            "administrators": len(admin_users),
            "grants_with_elevated_permissions": len(elevated),
            "inactive_users_with_permission_sets": len(inactive_with_access),
            "elevated_grant_ids_checked": len(elevated_ids),
        },
        findings=[f.to_dict() for f in findings],
        elevated_grants=elevated[:40],
        administrators=admin_users[:50],
        note=(
            "Findings are risks to review against how this business actually operates, "
            "not violations. Nothing was changed."
        ),
    )


registry.register(
    Tool(
        name="audit_security",
        description=AUDIT_DESCRIPTION,
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        output_schema={
            "type": "object",
            "properties": {"success": {"type": "boolean"}, "findings": {"type": "array"}},
        },
        risk=RiskLevel.LOW,
        requires_approval=False,
        execute=_audit_execute,
        audit_action="salesforce.audit_security",
        tags=["security", "permissions", "read"],
        long_running=True,
    )
)


# ---------------------------------------------------------------------------
# modify_permissions — narrow, high-risk, security-admin approval
# ---------------------------------------------------------------------------
MODIFY_DESCRIPTION = """Assign or remove a permission set for one user.

Deliberately narrow. This grants or revokes exactly one permission set for
exactly one user — it cannot edit a profile, change field-level security, or
alter what a permission set contains, because those are org-wide changes that
belong in a reviewed deployment, not in a chat turn.

Always HIGH risk and always requires approval from a security administrator.
The approval card shows what the permission set actually grants, so the person
approving sees the consequence rather than a name.
"""


async def _modify_validate(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    sf = ctx.require_sf()
    user_id = str(args.get("user_id") or "").strip()
    name = str(args.get("permission_set") or "").strip()
    if not user_id or not name:
        raise ToolValidationError(
            "`user_id` and `permission_set` are both required.", "MISSING_ARGUMENT"
        )

    users = await sf.query(
        f"SELECT Id, Name, IsActive FROM User WHERE Id = '{soql_literal(user_id)}' LIMIT 1"
    )
    if not (users.get("records") or []):
        raise ToolValidationError(
            f"No user with id {user_id} exists in this org.", "USER_NOT_FOUND"
        )

    data = await sf.query(
        f"SELECT Id, Label, Name, IsOwnedByProfile, {_PERM_FIELDS} FROM PermissionSet "
        f"WHERE (Name = '{soql_literal(name)}' OR Label = '{soql_literal(name)}') LIMIT 2"
    )
    rows = data.get("records") or []
    if not rows:
        raise ToolValidationError(
            f"No permission set named '{name}' exists in this org.",
            "PERMISSION_SET_NOT_FOUND",
            "Call inspect_permissions to see the permission sets that exist.",
        )
    if rows[0].get("IsOwnedByProfile"):
        raise ToolValidationError(
            f"'{name}' is a profile, not a permission set.",
            "CANNOT_MODIFY_PROFILE",
            "Profiles cannot be assigned this way. Use a permission set, or change the "
            "user's profile in Setup.",
        )
    return {"permission_set_id": rows[0]["Id"]}


async def _modify_plan(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    sf = ctx.require_sf()
    name = str(args["permission_set"])
    action = str(args.get("action") or "assign")
    data = await sf.query(
        f"SELECT Id, Label, {_PERM_FIELDS} FROM PermissionSet "
        f"WHERE (Name = '{soql_literal(name)}' OR Label = '{soql_literal(name)}') LIMIT 1"
    )
    rows = data.get("records") or []
    analyzed = analyze_permission_sets(rows)
    grants = analyzed[0]["elevated_permissions"] if analyzed else []
    users = await sf.query(
        "SELECT Id, Name, Username, Profile.Name FROM User WHERE Id = "
        f"'{soql_literal(str(args['user_id']))}' LIMIT 1"
    )
    who = (users.get("records") or [{}])[0]

    return {
        "title": f"{action.title()} permission set '{name}' "
        f"{'to' if action == 'assign' else 'from'} {who.get('Name')}",
        "change_type": "security.permission_assignment",
        "summary": (
            f"{'Grants' if action == 'assign' else 'Revokes'} the '{name}' permission "
            f"set for {who.get('Username')}."
        ),
        "details": [
            {"field": "User", "new_value": f"{who.get('Name')} ({who.get('Username')})"},
            {"field": "Current profile", "new_value": (who.get("Profile") or {}).get("Name")},
            {"field": "Permission set", "new_value": name},
            {"field": "Action", "new_value": action},
        ],
        "grants": grants,
        "impact": (
            (
                "This permission set carries org-wide permissions: "
                + "; ".join(f"{g['permission']} — {g['why']}" for g in grants)
            )
            if grants
            else "This permission set carries no org-wide administrative permissions."
        ),
        "reason": args.get("reason", ""),
    }


async def _modify_fingerprint(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    sf = ctx.require_sf()
    data = await sf.query(
        "SELECT Id FROM PermissionSetAssignment WHERE AssigneeId = "
        f"'{soql_literal(str(args['user_id']))}' AND PermissionSet.Name = "
        f"'{soql_literal(str(args['permission_set']))}' LIMIT 1"
    )
    return {"already_assigned": bool(data.get("records"))}


async def _modify_execute(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    sf = ctx.require_sf()
    user_id = str(args["user_id"])
    name = str(args["permission_set"])
    action = str(args.get("action") or "assign")

    data = await sf.query(
        "SELECT Id, Label FROM PermissionSet WHERE "
        f"(Name = '{soql_literal(name)}' OR Label = '{soql_literal(name)}') LIMIT 1"
    )
    rows = data.get("records") or []
    if not rows:
        return fail("PERMISSION_SET_NOT_FOUND", f"'{name}' no longer exists.")
    permission_set_id = rows[0]["Id"]

    existing = await sf.query(
        "SELECT Id FROM PermissionSetAssignment WHERE AssigneeId = "
        f"'{soql_literal(user_id)}' AND PermissionSetId = "
        f"'{soql_literal(str(permission_set_id))}' LIMIT 1"
    )
    assignment = (existing.get("records") or [None])[0]

    if action == "assign":
        if assignment:
            return ok(
                user_id=user_id,
                permission_set=name,
                action=action,
                already_assigned=True,
                message=f"{name} was already assigned to this user; nothing changed.",
            )
        created = await sf.create_record(
            "PermissionSetAssignment",
            {"AssigneeId": user_id, "PermissionSetId": permission_set_id},
        )
        return ok(
            user_id=user_id,
            permission_set=name,
            action=action,
            assignment_id=created.get("id"),
        )

    if not assignment:
        return ok(
            user_id=user_id,
            permission_set=name,
            action=action,
            already_removed=True,
            message=f"{name} was not assigned to this user; nothing changed.",
        )
    await sf.delete_record("PermissionSetAssignment", str(assignment["Id"]))
    return ok(user_id=user_id, permission_set=name, action=action, removed=True)


async def _modify_verify(
    ctx: ToolContext, args: dict[str, Any], result: dict[str, Any]
) -> dict[str, Any]:
    if not result.get("success"):
        return {"verified": False, "reason": "The change did not complete."}
    sf = ctx.require_sf()
    data = await sf.query(
        "SELECT Id FROM PermissionSetAssignment WHERE AssigneeId = "
        f"'{soql_literal(str(args['user_id']))}' AND PermissionSet.Name = "
        f"'{soql_literal(str(args['permission_set']))}' LIMIT 1"
    )
    assigned = bool(data.get("records"))
    wanted = str(args.get("action") or "assign") == "assign"
    if assigned != wanted:
        return {
            "verified": False,
            "reason": (
                f"After the change the permission set is "
                f"{'still not' if wanted else 'still'} assigned."
            ),
        }
    return {
        "verified": True,
        "method": "SOQL on PermissionSetAssignment after the change",
        "after": {"assigned": assigned},
    }


registry.register(
    Tool(
        name="modify_permissions",
        description=MODIFY_DESCRIPTION,
        input_schema={
            "type": "object",
            "properties": {
                "user_id": {"type": "string"},
                "permission_set": {"type": "string"},
                "action": {"type": "string", "enum": ["assign", "remove"], "default": "assign"},
                "reason": {"type": "string"},
            },
            "required": ["user_id", "permission_set"],
            "additionalProperties": False,
        },
        output_schema={"type": "object", "properties": {"success": {"type": "boolean"}}},
        risk=RiskLevel.HIGH,
        requires_approval=True,
        execute=_modify_execute,
        validate=_modify_validate,
        plan=_modify_plan,
        verify=_modify_verify,
        fingerprint=_modify_fingerprint,
        mutating=True,
        audit_action="salesforce.modify_permissions",
        tags=["security", "permissions", "write"],
    )
)
