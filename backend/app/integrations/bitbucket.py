"""Bitbucket Cloud, over REST 2.0.

The same workflow as the GitHub client — branch, commit, pull request, read
issues — over a different API, and the differences are real rather than
cosmetic:

  * Commits go through a **form-encoded body**, not JSON. Bitbucket's `/src`
    endpoint takes each file path as a form field name, which is also how it
    supports committing several files in one commit — something GitHub's
    contents API cannot do.
  * There is **no blob sha to supply**. Bitbucket resolves the parent from the
    branch, so create and update are the same call.
  * Pagination is `values` + `next`, and `next` is an absolute URL.

The same branch and path allowlists apply, from the same repository row, using
the same helpers as GitHub. Guard rails that differed per provider would be
guard rails somebody eventually routes around.
"""

from __future__ import annotations

from typing import Any

import httpx

from app.integrations.base import IntegrationClient, IntegrationError

DEFAULT_API = "https://api.bitbucket.org/2.0"


class BitbucketClient(IntegrationClient):
    provider = "bitbucket"

    def api_base(self) -> str:
        return self.config.base_url or DEFAULT_API

    # ------------------------------------------------------------------ identity
    async def whoami(self) -> dict[str, Any]:
        user = await self.request("GET", "/user")
        return {
            "success": True,
            "provider": self.provider,
            "account": user.get("username") or user.get("uuid", ""),
            "display_name": user.get("display_name", ""),
            "url": ((user.get("links") or {}).get("html") or {}).get("href", ""),
        }

    async def list_repositories(
        self, workspace: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        path = f"/repositories/{workspace}" if workspace else "/repositories"
        params: dict[str, Any] = {"pagelen": min(100, limit), "sort": "-updated_on"}
        if not workspace:
            params["role"] = "contributor"
        body = await self.request("GET", path, params=params)
        return [
            {
                "full_name": r.get("full_name", ""),
                "default_branch": (
                    (r.get("mainbranch") or {}).get("name") or "main"
                ),
                "private": r.get("is_private", True),
                "url": ((r.get("links") or {}).get("html") or {}).get("href", ""),
            }
            for r in (body.get("values") or [])
        ]

    # ------------------------------------------------------------------ reading
    async def get_repository(self, full_name: str) -> dict[str, Any]:
        repo = await self.request("GET", f"/repositories/{full_name}")
        return {
            "full_name": repo.get("full_name", ""),
            "default_branch": (repo.get("mainbranch") or {}).get("name") or "main",
            "private": repo.get("is_private", True),
            "url": ((repo.get("links") or {}).get("html") or {}).get("href", ""),
        }

    async def get_branch_sha(self, full_name: str, branch: str) -> str:
        body = await self.request(
            "GET", f"/repositories/{full_name}/refs/branches/{branch}"
        )
        sha = ((body or {}).get("target") or {}).get("hash")
        if not sha:
            raise IntegrationError(
                f"Bitbucket returned no commit for branch '{branch}'.",
                error_type="NOT_FOUND",
                provider=self.provider,
            )
        return str(sha)

    async def read_file(
        self, full_name: str, path: str, ref: str = "HEAD"
    ) -> dict[str, Any]:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=self.config.timeout_seconds)
        url = f"{self.api_base().rstrip('/')}/repositories/{full_name}/src/{ref}/{path}"
        response = await self._http.get(url, headers=self.headers())
        if response.status_code == 404:
            return {"exists": False, "path": path}
        if response.status_code >= 400:
            raise self._error_for(response)
        return {
            "exists": True,
            "path": path,
            "content": response.text,
            "size": len(response.content),
        }

    async def list_issues(
        self, full_name: str, state: str = "new", limit: int = 30
    ) -> list[dict[str, Any]]:
        """Bitbucket's issue tracker is opt-in per repository.

        A repository with it disabled answers 404, which is not an error worth
        surfacing as one — it means "this team tracks issues elsewhere".
        """
        try:
            body = await self.request(
                "GET",
                f"/repositories/{full_name}/issues",
                params={"pagelen": min(50, limit), "q": f'state="{state}"'},
            )
        except IntegrationError as exc:
            if exc.error_type == "NOT_FOUND":
                return []
            raise
        return [
            {
                "id": r.get("id"),
                "title": r.get("title", ""),
                # Untrusted: free text from whoever filed it.
                "body": ((r.get("content") or {}).get("raw") or ""),
                "state": r.get("state", ""),
                "kind": r.get("kind", ""),
                "priority": r.get("priority", ""),
                "reporter": (r.get("reporter") or {}).get("display_name", ""),
                "url": ((r.get("links") or {}).get("html") or {}).get("href", ""),
            }
            for r in (body.get("values") or [])
        ]

    # ------------------------------------------------------------------ writing
    async def create_branch(
        self, full_name: str, branch: str, from_branch: str
    ) -> dict[str, Any]:
        sha = await self.get_branch_sha(full_name, from_branch)
        created = await self.request(
            "POST",
            f"/repositories/{full_name}/refs/branches",
            json={"name": branch, "target": {"hash": sha}},
        )
        return {
            "branch": branch,
            "from_branch": from_branch,
            "sha": ((created or {}).get("target") or {}).get("hash", sha),
        }

    async def commit_files(
        self,
        full_name: str,
        files: dict[str, str],
        message: str,
        branch: str,
    ) -> dict[str, Any]:
        """Commit one or more files in a single commit.

        Bitbucket's `/src` endpoint takes a form body where each file path is a
        field name. That is also why several files land in one commit here,
        which GitHub's contents API cannot do — a Salesforce metadata change is
        usually several files that belong together, and splitting them into
        separate commits would misrepresent it.
        """
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=self.config.timeout_seconds)

        data = {"message": message, "branch": branch}
        form: dict[str, Any] = {**data}
        for path, content in files.items():
            form[path] = content

        url = f"{self.api_base().rstrip('/')}/repositories/{full_name}/src"
        response = await self._http.post(
            url,
            data=form,
            headers={"Authorization": f"Bearer {self.config.access_token}"},
        )
        if response.status_code >= 400:
            raise self._error_for(response)

        # Bitbucket answers a commit with 201 and no body, so the resulting
        # commit is read back rather than assumed.
        sha = await self.get_branch_sha(full_name, branch)
        return {
            "branch": branch,
            "paths": sorted(files),
            "commit_sha": sha,
            "files_committed": len(files),
        }

    async def create_pull_request(
        self,
        full_name: str,
        title: str,
        source_branch: str,
        destination_branch: str,
        description: str = "",
    ) -> dict[str, Any]:
        pr = await self.request(
            "POST",
            f"/repositories/{full_name}/pullrequests",
            json={
                "title": title,
                "description": description,
                "source": {"branch": {"name": source_branch}},
                "destination": {"branch": {"name": destination_branch}},
            },
        )
        return {
            "id": pr.get("id"),
            "url": ((pr.get("links") or {}).get("html") or {}).get("href", ""),
            "state": pr.get("state", ""),
            "source": source_branch,
            "destination": destination_branch,
        }

    async def get_pull_request(self, full_name: str, pr_id: int) -> dict[str, Any]:
        pr = await self.request("GET", f"/repositories/{full_name}/pullrequests/{pr_id}")
        return {
            "id": pr.get("id"),
            "title": pr.get("title", ""),
            "state": pr.get("state", ""),
            "merged": pr.get("state") == "MERGED",
            "url": ((pr.get("links") or {}).get("html") or {}).get("href", ""),
            "source": ((pr.get("source") or {}).get("branch") or {}).get("name", ""),
            "destination": (
                ((pr.get("destination") or {}).get("branch") or {}).get("name", "")
            ),
        }
