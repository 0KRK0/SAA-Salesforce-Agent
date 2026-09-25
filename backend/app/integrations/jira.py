"""Jira Cloud, over REST v3.

Implemented: read projects, search with JQL, read an issue, comment, transition
and create. That covers the workflow this product actually needs — a Salesforce
change that starts from a ticket and ends with the ticket updated — and nothing
beyond it is pretended.

Two details that are easy to get subtly wrong and are handled explicitly:

  * **Atlassian Document Format.** Jira Cloud v3 takes rich text as ADF, not a
    string. Sending a plain string produces a 400 that names nothing useful,
    so text is converted on the way in and flattened on the way out.
  * **Transitions are discovered, never guessed.** Every Jira project has its
    own workflow, so "move it to Done" means reading the available transitions
    for that issue and matching by name. A hardcoded transition id would work
    in one customer's project and silently fail in the next.

Everything Jira returns — summaries, descriptions, comments — is text a
stranger can write, and reaches the model inside an untrusted-data boundary.
"""

from __future__ import annotations

from typing import Any

from app.integrations.base import IntegrationClient, IntegrationError


class JiraClient(IntegrationClient):
    provider = "jira"

    def api_base(self) -> str:
        """Atlassian addresses a Cloud site by id through its OAuth gateway."""
        if self.config.extra.get("cloud_id"):
            return (
                f"https://api.atlassian.com/ex/jira/"
                f"{self.config.extra['cloud_id']}/rest/api/3"
            )
        return f"{(self.config.base_url or '').rstrip('/')}/rest/api/3"

    # ------------------------------------------------------------------ identity
    async def whoami(self) -> dict[str, Any]:
        me = await self.request("GET", "/myself")
        return {
            "success": True,
            "provider": self.provider,
            "account": me.get("accountId", ""),
            "display_name": me.get("displayName", ""),
            "email": me.get("emailAddress", ""),
        }

    async def list_projects(self, limit: int = 50) -> list[dict[str, Any]]:
        body = await self.request(
            "GET", "/project/search", params={"maxResults": min(100, limit)}
        )
        return [
            {
                "key": p.get("key", ""),
                "name": p.get("name", ""),
                "id": p.get("id", ""),
                "type": p.get("projectTypeKey", ""),
            }
            for p in (body.get("values") or [])
        ]

    # ------------------------------------------------------------------ reading
    async def search(self, jql: str, limit: int = 25) -> dict[str, Any]:
        body = await self.request(
            "POST",
            "/search",
            json={
                "jql": jql,
                "maxResults": min(100, limit),
                # Only the fields this product uses. Asking for everything drags
                # a great deal of a customer's data through a model's context.
                "fields": [
                    "summary",
                    "status",
                    "issuetype",
                    "priority",
                    "assignee",
                    "reporter",
                    "created",
                    "updated",
                    "labels",
                ],
            },
        )
        return {
            "total": body.get("total", 0),
            "issues": [_issue_summary(i) for i in (body.get("issues") or [])],
        }

    async def get_issue(self, key: str, with_comments: bool = False) -> dict[str, Any]:
        issue = await self.request("GET", f"/issue/{key}")
        fields = issue.get("fields") or {}
        out = {
            **_issue_summary(issue),
            # Untrusted: a description is free text written by whoever filed it.
            "description": adf_to_text(fields.get("description")),
        }
        if with_comments:
            comments = await self.request(
                "GET", f"/issue/{key}/comment", params={"maxResults": 20}
            )
            out["comments"] = [
                {
                    "author": (c.get("author") or {}).get("displayName", ""),
                    "created": c.get("created", ""),
                    "body": adf_to_text(c.get("body")),
                }
                for c in (comments.get("comments") or [])
            ]
        return out

    async def list_transitions(self, key: str) -> list[dict[str, Any]]:
        body = await self.request("GET", f"/issue/{key}/transitions")
        return [
            {
                "id": t.get("id", ""),
                "name": t.get("name", ""),
                "to": ((t.get("to") or {}).get("name") or ""),
            }
            for t in (body.get("transitions") or [])
        ]

    # ------------------------------------------------------------------ writing
    async def add_comment(self, key: str, text: str) -> dict[str, Any]:
        created = await self.request(
            "POST", f"/issue/{key}/comment", json={"body": text_to_adf(text)}
        )
        return {
            "id": created.get("id", ""),
            "issue": key,
            "created": created.get("created", ""),
        }

    async def transition(self, key: str, to_status: str) -> dict[str, Any]:
        """Move an issue by the *name* of the target status.

        Transitions are read from the issue rather than assumed: every Jira
        project has its own workflow, and a hardcoded id would work for one
        customer and quietly fail for the next.
        """
        available = await self.list_transitions(key)
        wanted = to_status.strip().lower()
        match = next(
            (
                t
                for t in available
                if t["name"].lower() == wanted or t["to"].lower() == wanted
            ),
            None,
        )
        if match is None:
            names = ", ".join(sorted({t["name"] for t in available})) or "(none)"
            raise IntegrationError(
                f"'{to_status}' is not an available transition for {key}.",
                error_type="TRANSITION_NOT_AVAILABLE",
                provider=self.provider,
                suggested_action=(
                    f"This issue can currently move to: {names}. Jira workflows "
                    "differ per project, so use one of those."
                ),
            )

        await self.request(
            "POST", f"/issue/{key}/transitions", json={"transition": {"id": match["id"]}}
        )
        # Read the issue back rather than reporting the transition we asked for.
        # A workflow rule can send an issue somewhere other than the named
        # target, and claiming otherwise would be a small, plausible lie.
        after = await self.request("GET", f"/issue/{key}", params={"fields": "status"})
        return {
            "issue": key,
            "transition": match["name"],
            "status": (
                ((after.get("fields") or {}).get("status") or {}).get("name", "")
            ),
        }

    async def create_issue(
        self,
        project_key: str,
        summary: str,
        description: str = "",
        issue_type: str = "Task",
        labels: list[str] | None = None,
    ) -> dict[str, Any]:
        fields: dict[str, Any] = {
            "project": {"key": project_key},
            "summary": summary,
            "issuetype": {"name": issue_type},
        }
        if description:
            fields["description"] = text_to_adf(description)
        if labels:
            fields["labels"] = labels
        created = await self.request("POST", "/issue", json={"fields": fields})
        return {
            "key": created.get("key", ""),
            "id": created.get("id", ""),
            "url": _browse_url(self.config, created.get("key", "")),
        }


def _issue_summary(issue: dict[str, Any]) -> dict[str, Any]:
    fields = issue.get("fields") or {}
    return {
        "key": issue.get("key", ""),
        "summary": fields.get("summary", ""),
        "status": ((fields.get("status") or {}).get("name") or ""),
        "type": ((fields.get("issuetype") or {}).get("name") or ""),
        "priority": ((fields.get("priority") or {}).get("name") or ""),
        "assignee": ((fields.get("assignee") or {}).get("displayName") or ""),
        "reporter": ((fields.get("reporter") or {}).get("displayName") or ""),
        "labels": fields.get("labels") or [],
        "created": fields.get("created", ""),
        "updated": fields.get("updated", ""),
    }


def _browse_url(config: Any, key: str) -> str:
    base = (getattr(config, "base_url", "") or "").rstrip("/")
    return f"{base}/browse/{key}" if base and key else ""


# ---------------------------------------------------------------------------
# Atlassian Document Format
# ---------------------------------------------------------------------------
def text_to_adf(text: str) -> dict[str, Any]:
    """Plain text -> the minimal valid ADF document Jira Cloud v3 requires.

    One paragraph per line. Empty lines are dropped, because ADF rejects a
    paragraph with an empty text node and the resulting 400 names nothing.
    """
    paragraphs = (text or "").split("\n")
    content = [
        {"type": "paragraph", "content": [{"type": "text", "text": line}]}
        for line in paragraphs
        if line.strip()
    ] or [{"type": "paragraph", "content": []}]
    return {"type": "doc", "version": 1, "content": content}


def adf_to_text(node: Any) -> str:
    """ADF -> readable text.

    Lossy on purpose. The model needs what the ticket *says*; panels, colours
    and emoji shortcodes are formatting, and carrying them through would spend
    context on nothing. Links keep their URL, because a URL in a ticket is
    often the actual content.
    """
    if node is None:
        return ""
    if isinstance(node, str):
        return node

    if isinstance(node, list):
        return "".join(adf_to_text(child) for child in node)

    if not isinstance(node, dict):
        return str(node)

    kind = node.get("type")
    if kind == "text":
        text = str(node.get("text", ""))
        for mark in node.get("marks") or []:
            if mark.get("type") == "link":
                href = (mark.get("attrs") or {}).get("href")
                if href and href != text:
                    text = f"{text} ({href})"
        return text
    if kind == "hardBreak":
        return "\n"
    if kind == "mention":
        return str((node.get("attrs") or {}).get("text") or "@mention")
    if kind == "inlineCard":
        return str((node.get("attrs") or {}).get("url") or "")

    inner = adf_to_text(node.get("content"))
    if kind in {"paragraph", "heading", "listItem", "blockquote", "codeBlock"}:
        return inner + "\n"
    if kind in {"bulletList", "orderedList", "doc", "panel", "tableRow"}:
        return inner
    return inner
