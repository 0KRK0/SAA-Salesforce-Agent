"""GitHub, over the REST API.

What is implemented is exactly what a Salesforce delivery workflow needs:
read a repository, branch from its default, commit metadata files, open a pull
request, and read issues. Everything else is deliberately absent rather than
half-present.

Two constraints shape the write path:

  * **Branch and path allowlists are enforced here**, not just described. A
    repository row says which branch names and which paths the agent may touch,
    and a write outside them is refused before any call is made.
  * **The agent does not push to a default branch.** `require_pull_request` is
    on by default, so a change arrives as a branch and a pull request that a
    human merges. An agent that could merge its own work would make the review
    step decorative.
"""

from __future__ import annotations

import base64
import fnmatch
from typing import Any

from app.integrations.base import IntegrationClient, IntegrationError

DEFAULT_API = "https://api.github.com"


class GitHubClient(IntegrationClient):
    provider = "github"

    def api_base(self) -> str:
        return self.config.base_url or DEFAULT_API

    def headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.config.access_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
        }

    # ------------------------------------------------------------------ identity
    async def whoami(self) -> dict[str, Any]:
        user = await self.request("GET", "/user")
        return {
            "success": True,
            "provider": self.provider,
            "account": user.get("login", ""),
            "display_name": user.get("name") or user.get("login", ""),
            "url": user.get("html_url", ""),
        }

    async def list_repositories(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = await self.request(
            "GET",
            "/user/repos",
            params={"per_page": min(100, limit), "sort": "updated"},
        )
        return [
            {
                "full_name": r.get("full_name", ""),
                "default_branch": r.get("default_branch", "main"),
                "private": r.get("private", True),
                "permissions": r.get("permissions") or {},
                "url": r.get("html_url", ""),
            }
            for r in (rows or [])
        ]

    # ------------------------------------------------------------------ reading
    async def get_repository(self, full_name: str) -> dict[str, Any]:
        repo = await self.request("GET", f"/repos/{full_name}")
        return {
            "full_name": repo.get("full_name", ""),
            "default_branch": repo.get("default_branch", "main"),
            "private": repo.get("private", True),
            "permissions": repo.get("permissions") or {},
            "url": repo.get("html_url", ""),
        }

    async def get_branch_sha(self, full_name: str, branch: str) -> str:
        ref = await self.request("GET", f"/repos/{full_name}/git/ref/heads/{branch}")
        sha = ((ref or {}).get("object") or {}).get("sha")
        if not sha:
            raise IntegrationError(
                f"GitHub returned no commit for branch '{branch}'.",
                error_type="NOT_FOUND",
                provider=self.provider,
            )
        return str(sha)

    async def read_file(
        self, full_name: str, path: str, ref: str | None = None
    ) -> dict[str, Any]:
        """Read one file. Returns `exists: False` rather than raising on 404.

        A caller deciding whether to create or update a file needs "not there"
        as an answer, not an exception to catch.
        """
        try:
            body = await self.request(
                "GET",
                f"/repos/{full_name}/contents/{path}",
                params={"ref": ref} if ref else None,
            )
        except IntegrationError as exc:
            if exc.error_type == "NOT_FOUND":
                return {"exists": False, "path": path}
            raise
        content = body.get("content") or ""
        try:
            decoded = base64.b64decode(content).decode("utf-8", errors="replace")
        except Exception:  # pragma: no cover - binary file
            decoded = ""
        return {
            "exists": True,
            "path": path,
            "sha": body.get("sha", ""),
            "size": body.get("size", 0),
            "content": decoded,
        }

    async def list_issues(
        self, full_name: str, state: str = "open", limit: int = 30
    ) -> list[dict[str, Any]]:
        rows = await self.request(
            "GET",
            f"/repos/{full_name}/issues",
            params={"state": state, "per_page": min(100, limit)},
        )
        return [
            {
                "number": r.get("number"),
                "title": r.get("title", ""),
                # Untrusted: anyone with access to the repository writes this.
                "body": r.get("body") or "",
                "state": r.get("state", ""),
                "author": (r.get("user") or {}).get("login", ""),
                "labels": [ln.get("name", "") for ln in (r.get("labels") or [])],
                "url": r.get("html_url", ""),
                "is_pull_request": "pull_request" in r,
            }
            for r in (rows or [])
        ]

    # ------------------------------------------------------------------ writing
    async def create_branch(
        self, full_name: str, branch: str, from_branch: str
    ) -> dict[str, Any]:
        sha = await self.get_branch_sha(full_name, from_branch)
        created = await self.request(
            "POST",
            f"/repos/{full_name}/git/refs",
            json={"ref": f"refs/heads/{branch}", "sha": sha},
        )
        return {
            "branch": branch,
            "from_branch": from_branch,
            "sha": ((created or {}).get("object") or {}).get("sha", sha),
        }

    async def put_file(
        self,
        full_name: str,
        path: str,
        content: str,
        message: str,
        branch: str,
        sha: str | None = None,
    ) -> dict[str, Any]:
        """Create or update one file on a branch.

        When `sha` is absent the current file is read first, so an update does
        not silently become a create-that-fails. GitHub requires the blob sha of
        the file being replaced, and omitting it on an existing file is a 422
        that reads like a validation error rather than a missing read.
        """
        if sha is None:
            existing = await self.read_file(full_name, path, ref=branch)
            sha = existing.get("sha") if existing.get("exists") else None

        payload: dict[str, Any] = {
            "message": message,
            "content": base64.b64encode(content.encode()).decode(),
            "branch": branch,
        }
        if sha:
            payload["sha"] = sha

        result = await self.request(
            "PUT", f"/repos/{full_name}/contents/{path}", json=payload
        )
        commit = (result or {}).get("commit") or {}
        return {
            "path": path,
            "branch": branch,
            "commit_sha": commit.get("sha", ""),
            "url": ((result or {}).get("content") or {}).get("html_url", ""),
            "created": sha is None,
        }

    async def create_pull_request(
        self,
        full_name: str,
        title: str,
        head: str,
        base: str,
        body: str = "",
        draft: bool = False,
    ) -> dict[str, Any]:
        pr = await self.request(
            "POST",
            f"/repos/{full_name}/pulls",
            json={
                "title": title,
                "head": head,
                "base": base,
                "body": body,
                "draft": draft,
            },
        )
        return {
            "number": pr.get("number"),
            "url": pr.get("html_url", ""),
            "state": pr.get("state", ""),
            "head": head,
            "base": base,
            "draft": pr.get("draft", draft),
        }

    async def get_pull_request(self, full_name: str, number: int) -> dict[str, Any]:
        pr = await self.request("GET", f"/repos/{full_name}/pulls/{number}")
        return {
            "number": pr.get("number"),
            "title": pr.get("title", ""),
            "state": pr.get("state", ""),
            "merged": pr.get("merged", False),
            "mergeable": pr.get("mergeable"),
            "url": pr.get("html_url", ""),
            "head": (pr.get("head") or {}).get("ref", ""),
            "base": (pr.get("base") or {}).get("ref", ""),
            "changed_files": pr.get("changed_files", 0),
            "additions": pr.get("additions", 0),
            "deletions": pr.get("deletions", 0),
        }

    async def comment_on_issue(
        self, full_name: str, number: int, body: str
    ) -> dict[str, Any]:
        created = await self.request(
            "POST", f"/repos/{full_name}/issues/{number}/comments", json={"body": body}
        )
        return {"id": created.get("id"), "url": created.get("html_url", "")}


# ---------------------------------------------------------------------------
# Guard rails, shared by GitHub and Bitbucket
# ---------------------------------------------------------------------------
#: Applied when a repository row names no patterns. Deliberately not "anything":
#: a repository connected without explicit rules should still refuse a write to
#: `main`, because the common case for "I did not configure it" is "I did not
#: think about it".
DEFAULT_BRANCH_PATTERNS = ("agent/*", "feature/*", "fix/*", "sfagent/*")


def branch_allowed(branch: str, patterns: list[str] | None) -> bool:
    return any(
        fnmatch.fnmatch(branch, pattern)
        for pattern in (patterns or DEFAULT_BRANCH_PATTERNS)
    )


def path_allowed(path: str, allowed: list[str] | None) -> bool:
    """Whether the agent may write this path.

    An empty allowlist means every path, which is the right default for a
    repository someone connected specifically for this. Path traversal is
    refused outright: `../` in a repository path is never legitimate.
    """
    normalized = path.lstrip("/")
    if ".." in normalized.split("/"):
        return False
    if not allowed:
        return True
    return any(
        normalized.startswith(prefix.lstrip("/").rstrip("*"))
        or fnmatch.fnmatch(normalized, prefix.lstrip("/"))
        for prefix in allowed
    )


def refuse_write(branch: str, paths: list[str], repository: Any) -> str:
    """Why this write is not allowed, or "" if it is.

    Returns a sentence for a person, because these refusals are read by someone
    who has to decide whether to widen the rule or change the request.
    """
    patterns = getattr(repository, "allowed_branch_patterns", None)
    default_branch = getattr(repository, "default_branch", "main")
    require_pr = getattr(repository, "require_pull_request", True)

    if require_pr and branch == default_branch:
        return (
            f"'{branch}' is this repository's default branch and it is protected. "
            "The agent works on a branch and opens a pull request; a person "
            "merges it."
        )
    if not branch_allowed(branch, patterns):
        allowed = ", ".join(patterns or DEFAULT_BRANCH_PATTERNS)
        return (
            f"Branch '{branch}' is outside the patterns this repository permits "
            f"({allowed}). Rename the branch, or widen the patterns in the "
            "repository's settings."
        )
    allowed_paths = getattr(repository, "allowed_paths", None)
    for path in paths:
        if not path_allowed(path, allowed_paths):
            return (
                f"'{path}' is outside the paths this repository permits "
                f"({', '.join(allowed_paths or ['(any)'])})."
            )
    return ""
