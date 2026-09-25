"""Rate limiting.

Scoped to what it can honestly do. This is an **in-process** limiter: it holds
counters in memory, so N application processes allow roughly N times the
configured rate. That is stated here, in `describe()`, and in the docs, because
a limit that is quietly four times what an operator configured is worse than
no limit at all — they would stop looking for the real control.

What it is genuinely good for, and why it is here:

  * **Login attempts.** Slowing credential stuffing to a crawl needs only a
    coarse limit, and even a per-process one changes the economics.
  * **Run starts.** Each run spends money on a model and Salesforce API calls.
    A loop in someone's script should not be able to empty a budget before a
    human notices.
  * **Accidental floods.** A retry loop in a client is the common case, and it
    hits one process anyway.

For a deployment that needs an exact global limit, put it at the edge — that is
where it belongs, and this does not pretend otherwise.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from app.config import settings

#: Keep at most this many buckets. A bounded map is what stops a limiter from
#: becoming the memory leak it was added to prevent.
MAX_BUCKETS = 50_000


@dataclass
class _Bucket:
    hits: deque[float] = field(default_factory=deque)


@dataclass(frozen=True)
class Decision:
    allowed: bool
    limit: int
    remaining: int
    retry_after: int = 0

    def headers(self) -> dict[str, str]:
        out = {
            "X-RateLimit-Limit": str(self.limit),
            "X-RateLimit-Remaining": str(max(0, self.remaining)),
        }
        if not self.allowed:
            out["Retry-After"] = str(max(1, self.retry_after))
        return out


class RateLimiter:
    """Sliding-window counters, per key."""

    def __init__(self, window_seconds: float = 60.0) -> None:
        self.window = window_seconds
        self._buckets: dict[str, _Bucket] = {}

    def check(self, key: str, limit: int, *, now: float | None = None) -> Decision:
        if limit <= 0:
            return Decision(allowed=True, limit=limit, remaining=limit)

        moment = now if now is not None else time.monotonic()
        bucket = self._buckets.get(key)
        if bucket is None:
            if len(self._buckets) >= MAX_BUCKETS:
                self._evict(moment)
            bucket = self._buckets.setdefault(key, _Bucket())

        cutoff = moment - self.window
        while bucket.hits and bucket.hits[0] <= cutoff:
            bucket.hits.popleft()

        if len(bucket.hits) >= limit:
            oldest = bucket.hits[0]
            return Decision(
                allowed=False,
                limit=limit,
                remaining=0,
                retry_after=int(self.window - (moment - oldest)) + 1,
            )

        bucket.hits.append(moment)
        return Decision(
            allowed=True, limit=limit, remaining=limit - len(bucket.hits)
        )

    def _evict(self, now: float) -> None:
        """Drop buckets with nothing left in the window."""
        cutoff = now - self.window
        stale = [
            key
            for key, bucket in self._buckets.items()
            if not bucket.hits or bucket.hits[-1] <= cutoff
        ]
        for key in stale:
            self._buckets.pop(key, None)
        if not stale:
            # Everything is live. Drop the oldest half rather than grow without
            # bound; under this much pressure an edge limiter is the answer.
            for key in list(self._buckets)[: len(self._buckets) // 2]:
                self._buckets.pop(key, None)

    def reset(self) -> None:
        self._buckets.clear()


limiter = RateLimiter()


#: path marker -> (setting name, whether the caller's *session* may key it)
#:
#: The third field is the security-relevant one. An authenticated route can be
#: keyed on the session, which is fairer: one person's runaway script does not
#: rate-limit their colleague behind the same NAT.
#:
#: An **unauthenticated** route must not be. A login request is by definition
#: not authenticated yet, so keying it on whatever cookie the client happened
#: to send would let an attacker reset their own limit by rotating a junk
#: cookie value — the limiter would be trivially evadable by the exact traffic
#: it exists to stop.
ROUTE_LIMITS: tuple[tuple[str, str, bool], ...] = (
    ("/auth/login", "rate_limit_login_per_minute", False),
    ("/auth/invitations/redeem", "rate_limit_login_per_minute", False),
    ("/auth/oidc", "rate_limit_login_per_minute", False),
    ("/scim/", "rate_limit_login_per_minute", False),
    ("/messages", "rate_limit_run_start_per_minute", True),
    ("/resume", "rate_limit_run_start_per_minute", True),
)


def limit_for(path: str) -> tuple[str, int]:
    """Which limit applies to a path: (name, per-minute allowance)."""
    attr, _ = _rule_for(path)
    return attr, int(getattr(settings, attr, 0) or 0)


def keys_on_session(path: str) -> bool:
    """Whether this route may be rate-limited per session rather than per client."""
    _, session_keyed = _rule_for(path)
    return session_keyed


def _rule_for(path: str) -> tuple[str, bool]:
    for marker, attr, session_keyed in ROUTE_LIMITS:
        if marker in path:
            return attr, session_keyed
    return "rate_limit_api_per_minute", True


def describe() -> dict[str, Any]:
    """What the limiter is, stated so nobody over-trusts it."""
    return {
        "enabled": settings.rate_limit_enabled,
        "scope": "per application process",
        "limits": {
            "login_per_minute": settings.rate_limit_login_per_minute,
            "api_per_minute": settings.rate_limit_api_per_minute,
            "run_start_per_minute": settings.rate_limit_run_start_per_minute,
        },
        "note": (
            "Counters are held in memory, so a deployment running N processes "
            "allows roughly N times these rates. Use an edge rate limiter where "
            "an exact global limit is required."
        ),
    }
