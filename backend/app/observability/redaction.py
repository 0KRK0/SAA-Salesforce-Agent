"""Keeping credentials out of logs, audit rows and error messages.

Key-based redaction — "this dict key is called `access_token`, hide it" — is
the easy half, and it is not enough. A credential does not only arrive as a
named field:

    {"message": "401 for Bearer sk-ant-api03-Xy9..."}
    {"detail": "curl -H 'Authorization: Bearer ghp_16C7e...' failed"}
    {"stderr": "SFDX_AUTH_URL=force://PlatformCLI::5Aep861..."}

Every one of those is a live credential in a place nothing is guarding, and
every one gets past a key-name check. So this module does both: it hides values
under known key names **and** scrubs credential-shaped substrings out of the
values it keeps.

The patterns are deliberately anchored on issuer prefixes rather than on
entropy. An entropy heuristic redacts Salesforce record ids, SOQL and Apex —
which would make the audit trail useless in exactly the cases someone needs to
read it. A pattern that misses an unknown token shape is a gap; a heuristic
that eats the evidence is worse.
"""

from __future__ import annotations

import re
from typing import Any

#: Dict keys whose value is a credential whatever it looks like.
SECRET_KEYS: frozenset[str] = frozenset(
    {
        "access_token",
        "refresh_token",
        "id_token",
        "client_secret",
        "authorization",
        "api_key",
        "apikey",
        "anthropic_api_key",
        "openai_api_key",
        "google_api_key",
        "secret",
        "secrets",
        "password",
        "passwd",
        "session",
        "session_token",
        "encryption_key",
        "private_key",
        "scim_token",
        "bearer",
        "credentials",
        "code_verifier",
        "sfdx_auth_url",
    }
)

#: Keys that merely *contain* one of these are also secrets. Catches
#: `salesforce_access_token`, `jira_client_secret`, `x-api-key`.
SECRET_KEY_MARKERS: tuple[str, ...] = (
    "_token",
    "token_",
    "secret",
    "password",
    "api_key",
    "apikey",
    "private_key",
    "credential",
)

REDACTED = "<redacted>"

#: Credential shapes, by issuer. Each is anchored on a prefix that no
#: legitimate Salesforce identifier or SOQL fragment starts with.
_VALUE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # Anthropic
    ("anthropic", re.compile(r"sk-ant-[A-Za-z0-9_\-]{16,}")),
    # OpenAI, including project and service-account forms
    ("openai", re.compile(r"sk-(?:proj-|svcacct-)?[A-Za-z0-9_\-]{20,}")),
    # GitHub: personal, OAuth, user-to-server, server-to-server, refresh
    ("github", re.compile(r"gh[pousr]_[A-Za-z0-9]{16,}")),
    ("github_fine_grained", re.compile(r"github_pat_[A-Za-z0-9_]{20,}")),
    # Google
    ("google", re.compile(r"AIza[A-Za-z0-9_\-]{30,}")),
    # Slack
    ("slack", re.compile(r"xox[baprs]-[A-Za-z0-9\-]{10,}")),
    # Atlassian
    ("atlassian", re.compile(r"ATATT[A-Za-z0-9_\-=]{20,}")),
    # AWS access key id, and a secret access key when it is labelled
    ("aws_key_id", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    # Salesforce session id. The 00D org prefix plus a long body is
    # unmistakable, and unlike a record id it is 60+ characters with a '!'.
    ("salesforce_session", re.compile(r"\b00D[A-Za-z0-9]{12,15}![A-Za-z0-9._\-]{20,}")),
    # Salesforce CLI auth URL — a refresh token in a URL, and people paste it.
    ("sfdx_auth_url", re.compile(r"force://[^\s\"']+")),
    # A bearer token in a header string, whatever its shape.
    (
        "bearer_header",
        re.compile(r"(?i)\b(?:authorization|bearer)\s*[:=]?\s*(?:bearer\s+)?[A-Za-z0-9._\-]{20,}"),
    ),
    # A JWT anywhere. Three base64url segments is not a shape anything else has.
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}")),
    # Private key blocks.
    (
        "private_key",
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    ),
    # The secret store's own references. Not credentials, but a reference in a
    # log is one lookup away from being one, and nothing needs to read them.
    ("secret_reference", re.compile(r"\b(?:local|env|ext):v1:[A-Za-z0-9+/=_\-]{16,}")),
)

#: Anything longer than this in a single string value is truncated. A log line
#: with a megabyte of Apex in it is not a log line.
MAX_STRING = 4_000
MAX_DEPTH = 10


def is_secret_key(key: str) -> bool:
    """Whether a key's *name* means its value is a credential.

    Hyphens are normalised to underscores first. Real header names are
    `x-api-key`, `x-auth-token`, `private-token` — matching only the
    underscored forms would let every HTTP header through, which is the exact
    place these appear.
    """
    lowered = str(key).lower().replace("-", "_").replace(" ", "_")
    if lowered in SECRET_KEYS:
        return True
    return any(marker in lowered for marker in SECRET_KEY_MARKERS)


def scrub(text: str) -> str:
    """Remove credential-shaped substrings from a string.

    Applied to values that are *kept* — error messages, stack traces, command
    output — which is where a credential most often ends up when nobody meant
    it to.
    """
    if not text or len(text) < 12:
        return text
    for name, pattern in _VALUE_PATTERNS:
        text = pattern.sub(f"<redacted:{name}>", text)
    return text


def redact(value: Any, _depth: int = 0) -> Any:
    """Recursively redact a structure for logging or audit.

    Both halves run: keys named like secrets are replaced wholesale, and every
    surviving string is scrubbed for credential shapes.
    """
    if _depth > MAX_DEPTH:
        return "<truncated:depth>"

    if isinstance(value, dict):
        out: dict[Any, Any] = {}
        for key, item in value.items():
            if is_secret_key(key):
                out[key] = REDACTED
            else:
                out[key] = redact(item, _depth + 1)
        return out

    if isinstance(value, list | tuple):
        return [redact(item, _depth + 1) for item in value]

    if isinstance(value, str):
        cleaned = scrub(value)
        if len(cleaned) > MAX_STRING:
            return cleaned[:MAX_STRING] + f"…<truncated:{len(cleaned)} chars>"
        return cleaned

    return value


def describe() -> dict[str, Any]:
    """What redaction covers, so nobody assumes it covers everything."""
    return {
        "key_names": sorted(SECRET_KEYS),
        "key_markers": list(SECRET_KEY_MARKERS),
        "value_patterns": [name for name, _ in _VALUE_PATTERNS],
        "max_string_chars": MAX_STRING,
        "note": (
            "Values are matched on issuer prefixes, not on entropy. An entropy "
            "heuristic would redact Salesforce record ids, SOQL and Apex, which "
            "is precisely the evidence someone reads an audit trail for. A token "
            "shape not listed here is a gap; a heuristic that eats the evidence "
            "is worse."
        ),
    }
