"""Jira, GitHub and Bitbucket.

Everything these systems return is untrusted data — a ticket description and an
issue body are text a stranger can write — and reaches the model inside the
same boundary as Salesforce records, never as instructions.

Everything the agent writes to them passes the same guard rails: an explicitly
registered repository, an allowed branch pattern, an allowed path, and a pull
request rather than a push to a default branch.
"""

from app.integrations.base import (
    IntegrationClient,
    IntegrationConfig,
    IntegrationError,
    IntegrationNotConnected,
    IntegrationNotImplemented,
    OAuthTokens,
)
from app.integrations.bitbucket import BitbucketClient
from app.integrations.github import (
    GitHubClient,
    branch_allowed,
    path_allowed,
    refuse_write,
)
from app.integrations.jira import JiraClient, adf_to_text, text_to_adf
from app.integrations.service import build_client, catalog, connection_for

__all__ = [
    "BitbucketClient",
    "GitHubClient",
    "IntegrationClient",
    "IntegrationConfig",
    "IntegrationError",
    "IntegrationNotConnected",
    "IntegrationNotImplemented",
    "JiraClient",
    "OAuthTokens",
    "adf_to_text",
    "branch_allowed",
    "build_client",
    "catalog",
    "connection_for",
    "path_allowed",
    "refuse_write",
    "text_to_adf",
]
