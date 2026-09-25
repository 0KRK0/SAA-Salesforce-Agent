"""Jira, GitHub and Bitbucket, against their real payload shapes.

`httpx.MockTransport` throughout — no network, no vendor SDKs. The bodies are
copied from the providers' documented responses, because the value of these
tests is entirely in whether the translation matches what the API actually
sends.

Two themes run through everything here:

  * **A claim requires a call.** Nothing reports success from a request that
    was not made, and nothing reports a state it did not read back.
  * **The guard rails are enforced, not described.** A write outside a
    repository's rules is refused before any HTTP request happens.
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.integrations.base import (
    IntegrationConfig,
    IntegrationError,
    IntegrationNotConnected,
    OAuthTokens,
    expires_at,
)
from app.integrations.bitbucket import BitbucketClient
from app.integrations.github import (
    DEFAULT_BRANCH_PATTERNS,
    GitHubClient,
    branch_allowed,
    path_allowed,
    refuse_write,
)
from app.integrations.jira import JiraClient, adf_to_text, text_to_adf


def _config(provider: str = "test", **kwargs) -> IntegrationConfig:
    return IntegrationConfig(
        provider=provider, access_token="tok", max_retries=1, **kwargs
    )


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class _Repo:
    """Stands in for a Repository row."""

    def __init__(self, **kwargs):
        self.full_name = kwargs.get("full_name", "acme/sfdx")
        self.default_branch = kwargs.get("default_branch", "main")
        self.allowed_branch_patterns = kwargs.get("allowed_branch_patterns")
        self.allowed_paths = kwargs.get("allowed_paths")
        self.require_pull_request = kwargs.get("require_pull_request", True)


# ---------------------------------------------------------------------------
# Guard rails
# ---------------------------------------------------------------------------
def test_the_default_branch_is_protected_when_pull_requests_are_required():
    """An agent that can push to main makes the review step decorative."""
    refusal = refuse_write("main", ["force-app/x.cls"], _Repo())
    assert "default branch" in refusal


def test_a_branch_outside_the_allowed_patterns_is_refused():
    refusal = refuse_write("hotfix/whatever", ["a.txt"], _Repo())
    assert "outside the patterns" in refusal
    # And it says what would work, so a person can decide.
    assert "agent/*" in refusal


def test_an_allowed_branch_passes():
    assert refuse_write("agent/add-tier", ["force-app/x.cls"], _Repo()) == ""


def test_a_repository_with_no_configured_patterns_still_refuses_main():
    """"I did not configure it" usually means "I did not think about it", so
    an unconfigured repository is not an unprotected one."""
    assert branch_allowed("agent/x", None) is True
    assert branch_allowed("main", None) is False
    assert DEFAULT_BRANCH_PATTERNS


def test_a_path_outside_the_allowlist_is_refused():
    repo = _Repo(allowed_paths=["force-app/"])
    assert refuse_write("agent/x", ["force-app/main/A.cls"], repo) == ""
    assert "outside the paths" in refuse_write("agent/x", [".github/workflows/ci.yml"], repo)


def test_path_traversal_is_refused_even_with_no_allowlist():
    """`../` in a repository path is never legitimate."""
    assert path_allowed("force-app/A.cls", None) is True
    assert path_allowed("../../etc/passwd", None) is False
    assert path_allowed("force-app/../../secrets", None) is False


def test_an_empty_path_allowlist_permits_the_whole_repository():
    """The right default for a repository connected specifically for this."""
    assert path_allowed("anything/at/all.xml", []) is True


def test_turning_off_pull_requests_allows_the_default_branch():
    """An explicit choice a project admin can make, not a hidden default."""
    repo = _Repo(require_pull_request=False, allowed_branch_patterns=["main"])
    assert refuse_write("main", ["a.txt"], repo) == ""


# ---------------------------------------------------------------------------
# GitHub
# ---------------------------------------------------------------------------
async def test_github_reads_a_file_and_decodes_it():
    import base64

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "sha": "blob123",
                "size": 11,
                "content": base64.b64encode(b"hello world").decode(),
                "encoding": "base64",
            },
        )

    async with _client(handler) as http:
        client = GitHubClient(_config("github"), http=http)
        result = await client.read_file("acme/sfdx", "README.md")
    assert result["exists"] is True
    assert result["content"] == "hello world"
    assert result["sha"] == "blob123"


async def test_a_missing_file_is_an_answer_not_an_exception():
    """A caller deciding create-or-update needs "not there" as a result."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Not Found"})

    async with _client(handler) as http:
        client = GitHubClient(_config("github"), http=http)
        result = await client.read_file("acme/sfdx", "missing.txt")
    assert result["exists"] is False


async def test_github_updating_a_file_supplies_the_blob_sha():
    """GitHub requires the sha of the file being replaced. Omitting it is a 422
    that reads like a validation error rather than a missing read."""
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import base64

        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "sha": "existing-sha",
                    "size": 3,
                    "content": base64.b64encode(b"old").decode(),
                },
            )
        seen.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={"commit": {"sha": "commit-sha"}, "content": {"html_url": "u"}},
        )

    async with _client(handler) as http:
        client = GitHubClient(_config("github"), http=http)
        result = await client.put_file(
            "acme/sfdx", "A.cls", "new", "msg", "agent/x"
        )
    assert seen[0]["sha"] == "existing-sha"
    assert result["created"] is False
    assert result["commit_sha"] == "commit-sha"


async def test_github_creating_a_new_file_sends_no_sha():
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(404, json={"message": "Not Found"})
        seen.append(json.loads(request.content))
        return httpx.Response(201, json={"commit": {"sha": "c1"}, "content": {}})

    async with _client(handler) as http:
        client = GitHubClient(_config("github"), http=http)
        result = await client.put_file("acme/sfdx", "New.cls", "x", "msg", "agent/x")
    assert "sha" not in seen[0]
    assert result["created"] is True


async def test_github_creates_a_branch_from_the_named_base():
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"object": {"sha": "base-sha"}})
        seen.append(json.loads(request.content))
        return httpx.Response(201, json={"object": {"sha": "base-sha"}})

    async with _client(handler) as http:
        client = GitHubClient(_config("github"), http=http)
        result = await client.create_branch("acme/sfdx", "agent/x", "main")
    assert seen[0] == {"ref": "refs/heads/agent/x", "sha": "base-sha"}
    assert result["sha"] == "base-sha"


async def test_github_pull_requests_report_merge_state_honestly():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "number": 7,
                "title": "Add Tier field",
                "state": "open",
                "merged": False,
                "mergeable": True,
                "html_url": "https://github.com/acme/sfdx/pull/7",
                "head": {"ref": "agent/x"},
                "base": {"ref": "main"},
                "changed_files": 2,
                "additions": 40,
                "deletions": 1,
            },
        )

    async with _client(handler) as http:
        client = GitHubClient(_config("github"), http=http)
        pr = await client.get_pull_request("acme/sfdx", 7)
    assert pr["merged"] is False
    assert pr["state"] == "open"
    assert pr["changed_files"] == 2


async def test_github_sends_the_api_version_header():
    """Without it GitHub may serve an older representation than the one these
    translations were written against."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(request.headers)
        return httpx.Response(200, json={"login": "octocat"})

    async with _client(handler) as http:
        client = GitHubClient(_config("github"), http=http)
        await client.whoami()
    assert captured["x-github-api-version"] == "2022-11-28"


# ---------------------------------------------------------------------------
# Jira
# ---------------------------------------------------------------------------
def test_plain_text_becomes_valid_adf():
    """Jira Cloud v3 rejects a plain string with a 400 that names nothing."""
    doc = text_to_adf("First line\nSecond line")
    assert doc["type"] == "doc"
    assert doc["version"] == 1
    assert [p["content"][0]["text"] for p in doc["content"]] == [
        "First line",
        "Second line",
    ]


def test_empty_paragraphs_are_dropped_from_adf():
    """ADF rejects a paragraph with an empty text node."""
    doc = text_to_adf("One\n\n\nTwo")
    assert len(doc["content"]) == 2


def test_empty_text_still_produces_a_valid_document():
    doc = text_to_adf("")
    assert doc["type"] == "doc"
    assert doc["content"]


def test_adf_flattens_back_to_readable_text():
    doc = {
        "type": "doc",
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": "Hello"}]},
            {
                "type": "bulletList",
                "content": [
                    {
                        "type": "listItem",
                        "content": [
                            {
                                "type": "paragraph",
                                "content": [{"type": "text", "text": "One"}],
                            }
                        ],
                    }
                ],
            },
        ],
    }
    text = adf_to_text(doc)
    assert "Hello" in text
    assert "One" in text


def test_adf_links_keep_their_url():
    """A URL in a ticket is often the actual content."""
    node = {
        "type": "text",
        "text": "the doc",
        "marks": [{"type": "link", "attrs": {"href": "https://example.test/x"}}],
    }
    assert "https://example.test/x" in adf_to_text(node)


def test_adf_handles_none_and_strings_without_raising():
    assert adf_to_text(None) == ""
    assert adf_to_text("plain") == "plain"


async def test_jira_addresses_a_cloud_site_by_id():
    """Without the cloud_id every Atlassian API call 404s."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"accountId": "a1", "displayName": "Ann"})

    async with _client(handler) as http:
        client = JiraClient(_config("jira", extra={"cloud_id": "cloud-123"}), http=http)
        await client.whoami()
    assert "api.atlassian.com/ex/jira/cloud-123/rest/api/3/myself" in captured["url"]


async def test_jira_search_returns_summaries_not_whole_issues():
    """Asking for every field drags a lot of a customer's data through a
    model's context."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "total": 1,
                "issues": [
                    {
                        "key": "SF-1",
                        "fields": {
                            "summary": "Add a Tier field",
                            "status": {"name": "In Progress"},
                            "issuetype": {"name": "Story"},
                            "priority": {"name": "High"},
                            "assignee": {"displayName": "Ann"},
                            "labels": ["salesforce"],
                        },
                    }
                ],
            },
        )

    async with _client(handler) as http:
        client = JiraClient(_config("jira", extra={"cloud_id": "c"}), http=http)
        result = await client.search("project = SF")
    assert result["total"] == 1
    assert result["issues"][0]["status"] == "In Progress"
    assert "description" not in result["issues"][0]
    assert "summary" in captured["body"]["fields"]


async def test_jira_transitions_are_discovered_not_guessed():
    """Every Jira project has its own workflow. A hardcoded id would work for
    one customer and silently fail for the next."""
    posted: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/transitions") and request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "transitions": [
                        {"id": "31", "name": "Done", "to": {"name": "Done"}},
                        {"id": "11", "name": "To Do", "to": {"name": "To Do"}},
                    ]
                },
            )
        if path.endswith("/transitions"):
            posted.append(json.loads(request.content))
            return httpx.Response(204)
        return httpx.Response(200, json={"fields": {"status": {"name": "Done"}}})

    async with _client(handler) as http:
        client = JiraClient(_config("jira", extra={"cloud_id": "c"}), http=http)
        result = await client.transition("SF-1", "Done")
    assert posted[0] == {"transition": {"id": "31"}}
    assert result["status"] == "Done"


async def test_a_transition_reports_the_status_it_read_back():
    """A workflow rule can send an issue somewhere other than the named target.
    Reporting the requested one would be a small, plausible lie."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/transitions") and request.method == "GET":
            return httpx.Response(
                200,
                json={"transitions": [{"id": "31", "name": "Done", "to": {"name": "Done"}}]},
            )
        if path.endswith("/transitions"):
            return httpx.Response(204)
        # The workflow actually moved it somewhere else.
        return httpx.Response(200, json={"fields": {"status": {"name": "In Review"}}})

    async with _client(handler) as http:
        client = JiraClient(_config("jira", extra={"cloud_id": "c"}), http=http)
        result = await client.transition("SF-1", "Done")
    assert result["transition"] == "Done"
    assert result["status"] == "In Review"


async def test_an_unavailable_transition_lists_what_is_available():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"transitions": [{"id": "11", "name": "To Do", "to": {"name": "To Do"}}]},
        )

    async with _client(handler) as http:
        client = JiraClient(_config("jira", extra={"cloud_id": "c"}), http=http)
        with pytest.raises(IntegrationError) as exc:
            await client.transition("SF-1", "Done")
    assert exc.value.error_type == "TRANSITION_NOT_AVAILABLE"
    assert "To Do" in exc.value.suggested_action


async def test_a_jira_comment_is_sent_as_adf():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(201, json={"id": "10001", "created": "2026-01-01"})

    async with _client(handler) as http:
        client = JiraClient(_config("jira", extra={"cloud_id": "c"}), http=http)
        await client.add_comment("SF-1", "Created Account.Tier__c in the sandbox.")
    assert captured["body"]["body"]["type"] == "doc"


async def test_jira_reads_descriptions_as_flattened_text():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "key": "SF-1",
                "fields": {
                    "summary": "Add Tier",
                    "status": {"name": "Open"},
                    "description": {
                        "type": "doc",
                        "version": 1,
                        "content": [
                            {
                                "type": "paragraph",
                                "content": [
                                    {"type": "text", "text": "We need a Tier field."}
                                ],
                            }
                        ],
                    },
                },
            },
        )

    async with _client(handler) as http:
        client = JiraClient(_config("jira", extra={"cloud_id": "c"}), http=http)
        issue = await client.get_issue("SF-1")
    assert "We need a Tier field." in issue["description"]


# ---------------------------------------------------------------------------
# Bitbucket
# ---------------------------------------------------------------------------
async def test_bitbucket_commits_several_files_in_one_commit():
    """A Salesforce metadata change is usually several files that belong
    together; splitting them into separate commits would misrepresent it."""
    posted: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/src") and request.method == "POST":
            posted.append(request.content)
            return httpx.Response(201)
        return httpx.Response(200, json={"target": {"hash": "abc123"}})

    async with _client(handler) as http:
        client = BitbucketClient(_config("bitbucket"), http=http)
        result = await client.commit_files(
            "acme/sfdx",
            {"force-app/A.cls": "class A {}", "force-app/A.cls-meta.xml": "<x/>"},
            "Add A",
            "agent/x",
        )
    from urllib.parse import parse_qs

    body = parse_qs(posted[0].decode())
    assert body["force-app/A.cls"] == ["class A {}"]
    assert body["force-app/A.cls-meta.xml"] == ["<x/>"]
    assert body["branch"] == ["agent/x"]
    assert result["files_committed"] == 2
    # The commit sha is read back, not assumed: Bitbucket answers 201 with no body.
    assert result["commit_sha"] == "abc123"


async def test_bitbucket_uses_a_form_not_json_for_commits():
    """Bitbucket's /src endpoint takes multipart form fields. Sending JSON is a
    400 that names nothing useful."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/src") and request.method == "POST":
            captured["content_type"] = request.headers.get("content-type", "")
            return httpx.Response(201)
        return httpx.Response(200, json={"target": {"hash": "h"}})

    async with _client(handler) as http:
        client = BitbucketClient(_config("bitbucket"), http=http)
        await client.commit_files("acme/sfdx", {"a.txt": "x"}, "m", "agent/x")
    assert "json" not in captured["content_type"]


async def test_a_bitbucket_repository_without_issues_returns_an_empty_list():
    """Bitbucket's issue tracker is opt-in. 404 means "they track issues
    elsewhere", not an error worth surfacing."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": {"message": "Repository has no issue tracker"}})

    async with _client(handler) as http:
        client = BitbucketClient(_config("bitbucket"), http=http)
        assert await client.list_issues("acme/sfdx") == []


async def test_bitbucket_pull_request_state_maps_to_merged():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": 3,
                "title": "Add Tier",
                "state": "MERGED",
                "links": {"html": {"href": "https://bitbucket.org/x/3"}},
                "source": {"branch": {"name": "agent/x"}},
                "destination": {"branch": {"name": "main"}},
            },
        )

    async with _client(handler) as http:
        client = BitbucketClient(_config("bitbucket"), http=http)
        pr = await client.get_pull_request("acme/sfdx", 3)
    assert pr["merged"] is True


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------
async def test_a_revoked_token_says_what_to_do_about_it():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "Bad credentials"})

    async with _client(handler) as http:
        client = GitHubClient(_config("github"), http=http)
        with pytest.raises(IntegrationError) as exc:
            await client.whoami()
    assert exc.value.error_type == "UNAUTHORIZED"
    assert "Reconnect" in exc.value.suggested_action
    assert exc.value.retryable is False


async def test_a_permission_failure_is_distinguished_from_a_bad_token():
    """"Your token is fine but this account cannot do that" is a completely
    different fix from "reconnect"."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403, json={"message": "Resource not accessible by integration"}
        )

    async with _client(handler) as http:
        client = GitHubClient(_config("github"), http=http)
        with pytest.raises(IntegrationError) as exc:
            await client.whoami()
    assert exc.value.error_type == "FORBIDDEN"
    assert "permission" in exc.value.suggested_action


async def test_an_error_never_carries_the_token():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"message": "boom"})

    async with _client(handler) as http:
        client = GitHubClient(
            IntegrationConfig(
                provider="github", access_token="ghp_SECRETVALUE", max_retries=1
            ),
            http=http,
        )
        with pytest.raises(IntegrationError) as exc:
            await client.whoami()
    assert "ghp_SECRETVALUE" not in str(exc.value)
    assert "ghp_SECRETVALUE" not in json.dumps(exc.value.to_dict())


async def test_a_rate_limit_is_retryable_and_a_validation_error_is_not():
    for status, retryable in ((429, True), (422, False), (404, False), (503, True)):

        def handler(request: httpx.Request, code=status) -> httpx.Response:
            return httpx.Response(code, json={"message": "x"})

        async with _client(handler) as http:
            client = GitHubClient(_config("github"), http=http)
            with pytest.raises(IntegrationError) as exc:
                await client.whoami()
        assert exc.value.retryable is retryable, status


async def test_jira_nested_error_arrays_are_surfaced():
    """Jira puts its errors in `errorMessages`, not `message`."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400, json={"errorMessages": ["Field 'summary' is required."], "errors": {}}
        )

    async with _client(handler) as http:
        client = JiraClient(_config("jira", extra={"cloud_id": "c"}), http=http)
        with pytest.raises(IntegrationError) as exc:
            await client.list_projects()
    assert "summary" in exc.value.message


# ---------------------------------------------------------------------------
# Token handling
# ---------------------------------------------------------------------------
def test_a_token_with_no_expiry_is_not_treated_as_expired():
    """GitHub classic tokens never expire. Inventing an expiry would demand a
    re-connect that is not needed."""
    assert expires_at(None) is None
    assert expires_at(0) is None
    assert expires_at("not a number") is None


def test_an_expiry_is_shortened_so_a_token_does_not_die_mid_request():
    from datetime import UTC, datetime

    deadline = expires_at(3600)
    assert deadline is not None
    remaining = (deadline - datetime.now(UTC)).total_seconds()
    assert 3400 < remaining < 3600


def test_oauth_tokens_can_be_logged_without_logging_the_token():
    tokens = OAuthTokens(access_token="secret-value", refresh_token="also-secret")
    described = tokens.redacted()
    assert described["has_access_token"] is True
    assert "secret-value" not in json.dumps(described)
    assert "also-secret" not in json.dumps(described)


def test_not_connected_says_where_to_connect():
    error = IntegrationNotConnected("github")
    assert "Integrations page" in error.suggested_action
