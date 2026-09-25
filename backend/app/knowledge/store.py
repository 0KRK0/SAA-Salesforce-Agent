"""Reading and writing org knowledge, with a strict context budget.

The retrieval model is deliberately simple and explainable rather than clever:
score rows by how well their key and summary match the terms in the request,
prefer recent and frequently-used entries, and stop at a character budget. No
embeddings, no vector store, no dependency that can be down — this runs on the
same database as everything else and is auditable by reading a table.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import KnowledgeKind, OrgKnowledge
from app.observability.logging import get_logger

log = get_logger("knowledge.store")

#: Characters of recalled knowledge that may enter a prompt. Small on purpose:
#: knowledge is a hint that saves a tool call, not a substitute for inspecting.
DEFAULT_BUDGET = 4000

#: Sources that may be written. 'model' is deliberately absent.
TRUSTED_SOURCES = {
    "describe", "tooling", "metadata", "deployment", "agent_run", "analysis",
    "diagnosis", "user",
}

_WORD = re.compile(r"[A-Za-z][A-Za-z0-9_]{2,}")

_STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "have", "has", "was",
    "are", "not", "all", "can", "you", "our", "your", "please", "salesforce",
    "record", "records", "field", "fields", "object", "objects", "show", "get",
    "make", "create", "update", "find", "why", "how", "what", "which", "when",
}


def terms_of(text: str) -> set[str]:
    return {
        w.lower()
        for w in _WORD.findall(text or "")
        if w.lower() not in _STOPWORDS
    }


async def remember(
    db: AsyncSession,
    *,
    company_id: str,
    project_id: str,
    salesforce_connection_id: str,
    kind: KnowledgeKind,
    key: str,
    summary: str,
    data: dict[str, Any] | None = None,
    source: str = "describe",
    ttl_days: int | None = None,
) -> OrgKnowledge:
    """Record or refresh one piece of knowledge about an org.

    Upserts on (connection, kind, key) so repeated observation refreshes rather
    than accumulating duplicates — an org's schema is observed constantly.
    """
    if source not in TRUSTED_SOURCES:
        raise ValueError(
            f"'{source}' is not a trusted knowledge source. Knowledge must come from "
            "the org or from an explicit human statement, never from the model's own "
            "output."
        )
    existing = (
        await db.execute(
            select(OrgKnowledge).where(
                OrgKnowledge.salesforce_connection_id == salesforce_connection_id,
                OrgKnowledge.kind == kind,
                OrgKnowledge.key == key,
            )
        )
    ).scalar_one_or_none()

    expires = (
        datetime.now(UTC) + timedelta(days=ttl_days) if ttl_days else None
    )
    if existing is not None:
        existing.summary = summary
        existing.data = data
        existing.source = source
        existing.observed_at = datetime.now(UTC)
        existing.expires_at = expires
        await db.flush()
        return existing

    row = OrgKnowledge(
        company_id=company_id,
        project_id=project_id,
        salesforce_connection_id=salesforce_connection_id,
        kind=kind,
        key=key,
        summary=summary,
        data=data,
        source=source,
        expires_at=expires,
    )
    db.add(row)
    await db.flush()
    return row


async def forget(
    db: AsyncSession, *, salesforce_connection_id: str, kind: KnowledgeKind, key: str
) -> bool:
    row = (
        await db.execute(
            select(OrgKnowledge).where(
                OrgKnowledge.salesforce_connection_id == salesforce_connection_id,
                OrgKnowledge.kind == kind,
                OrgKnowledge.key == key,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return False
    await db.delete(row)
    await db.flush()
    return True


def _fresh(row: OrgKnowledge, now: datetime) -> bool:
    if row.expires_at is None:
        return True
    expiry = row.expires_at
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=UTC)
    return expiry > now


def score(row: OrgKnowledge, wanted: set[str], now: datetime) -> float:
    """Relevance of one row to the current request.

    Key matches dominate — if someone says "Opportunity", the Opportunity rows
    are what matter. Summary matches contribute less, and age and use nudge the
    ordering between otherwise equal candidates.
    """
    key_terms = terms_of(row.key)
    summary_terms = terms_of(row.summary)
    key_hits = len(wanted & key_terms)
    summary_hits = len(wanted & summary_terms)
    if not key_hits and not summary_hits:
        return 0.0

    observed = row.observed_at
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=UTC)
    age_days = max((now - observed).total_seconds() / 86400, 0)
    recency = 1.0 / (1.0 + age_days / 30.0)
    usage = min(row.hit_count, 20) / 20.0

    # Failures and diagnoses are worth surfacing even on a weak match: knowing
    # a change failed here before is exactly what stops it failing again.
    kind_weight = 1.5 if row.kind in {KnowledgeKind.FAILURE, KnowledgeKind.PATTERN} else 1.0
    return (key_hits * 3.0 + summary_hits) * kind_weight * (0.6 + 0.3 * recency + 0.1 * usage)


async def recall(
    db: AsyncSession,
    *,
    salesforce_connection_id: str,
    query: str,
    kinds: list[KnowledgeKind] | None = None,
    budget: int = DEFAULT_BUDGET,
    limit: int = 25,
) -> list[dict[str, Any]]:
    """The most relevant knowledge about this org for this request.

    Returns summaries within a character budget. Callers get hints, not a
    database dump, and the budget is enforced here rather than trusted to the
    caller.
    """
    stmt = select(OrgKnowledge).where(
        OrgKnowledge.salesforce_connection_id == salesforce_connection_id
    )
    if kinds:
        stmt = stmt.where(OrgKnowledge.kind.in_(kinds))
    rows = (await db.execute(stmt.limit(2000))).scalars().all()

    now = datetime.now(UTC)
    wanted = terms_of(query)
    scored = [
        (score(row, wanted, now), row)
        for row in rows
        if _fresh(row, now)
    ]
    scored = [(s, r) for s, r in scored if s > 0]
    scored.sort(key=lambda pair: -pair[0])

    out: list[dict[str, Any]] = []
    used = 0
    for value, row in scored[:limit]:
        entry = {
            "kind": row.kind.value,
            "key": row.key,
            "summary": row.summary,
            "source": row.source,
            "observed_at": row.observed_at.isoformat(),
            "relevance": round(value, 2),
        }
        size = len(row.summary) + len(row.key) + 80
        if used + size > budget:
            break
        used += size
        out.append(entry)
        row.hit_count += 1
    if out:
        await db.flush()
    return out


async def recent(
    db: AsyncSession,
    *,
    salesforce_connection_id: str,
    kinds: list[KnowledgeKind] | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    stmt = select(OrgKnowledge).where(
        OrgKnowledge.salesforce_connection_id == salesforce_connection_id
    )
    if kinds:
        stmt = stmt.where(OrgKnowledge.kind.in_(kinds))
    rows = (
        await db.execute(stmt.order_by(OrgKnowledge.observed_at.desc()).limit(limit))
    ).scalars().all()
    return [
        {
            "kind": r.kind.value,
            "key": r.key,
            "summary": r.summary,
            "source": r.source,
            "observed_at": r.observed_at.isoformat(),
        }
        for r in rows
    ]


def render_for_prompt(entries: list[dict[str, Any]]) -> str:
    """Format recalled knowledge for the system prompt.

    Framed as prior observations that may be stale, because they may be. The
    agent must still inspect before acting; this only tells it where to look.
    """
    if not entries:
        return ""
    lines = [
        "## What is already known about this org",
        "These are observations recorded during earlier work on this org. They may be "
        "out of date — treat them as a starting point for where to look, never as a "
        "substitute for inspecting the org now.",
        "",
    ]
    for entry in entries:
        lines.append(f"- [{entry['kind']}] {entry['key']}: {entry['summary']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Recording helpers used by the runtime
# ---------------------------------------------------------------------------
async def record_deployment(
    db: AsyncSession,
    *,
    company_id: str,
    project_id: str,
    salesforce_connection_id: str,
    label: str,
    succeeded: bool,
    detail: dict[str, Any],
) -> None:
    """Remember that a deployment happened, and how it went.

    A failed deployment is the more valuable of the two: next time the agent
    proposes something similar, it can recall that this org rejected it and
    why.
    """
    await remember(
        db,
        company_id=company_id,
        project_id=project_id,
        salesforce_connection_id=salesforce_connection_id,
        kind=KnowledgeKind.DEPLOYMENT if succeeded else KnowledgeKind.FAILURE,
        key=label,
        summary=(
            f"Deployment '{label}' succeeded."
            if succeeded
            else f"Deployment '{label}' FAILED: {detail.get('message', 'no message')}"
        ),
        data=detail,
        source="deployment",
    )


async def record_schema(
    db: AsyncSession,
    *,
    company_id: str,
    project_id: str,
    salesforce_connection_id: str,
    object_name: str,
    describe: dict[str, Any],
) -> None:
    """Remember the shape of an object, not its contents."""
    fields = describe.get("fields") or []
    custom = [f["name"] for f in fields if f.get("custom")]
    await remember(
        db,
        company_id=company_id,
        project_id=project_id,
        salesforce_connection_id=salesforce_connection_id,
        kind=KnowledgeKind.OBJECT,
        key=str(describe.get("name") or object_name),
        summary=(
            f"{describe.get('label', object_name)}: {len(fields)} fields, "
            f"{len(custom)} custom. "
            + (f"Custom fields include {', '.join(custom[:8])}." if custom else "")
        ),
        data={
            "label": describe.get("label"),
            "custom_fields": custom[:60],
            "createable": describe.get("createable"),
            "updateable": describe.get("updateable"),
            "field_count": len(fields),
        },
        source="describe",
        # Schema changes; a month-old memory of it is a lie waiting to happen.
        ttl_days=30,
    )
