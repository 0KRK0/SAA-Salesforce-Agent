"""Duplicate detection and merge planning.

The approach is the standard one for record linkage, chosen because it scales
and is explainable to the person who has to approve a merge:

  1. **Normalize** — strip case, punctuation, legal suffixes ("Inc", "Ltd"),
     and formatting noise from phones and emails.
  2. **Block** — group records by a cheap key (normalized name prefix, email
     domain + local part, phone digits). Comparing every record with every
     other is O(n²) and pointless; blocking makes it linear in practice.
  3. **Score** — inside each block, compare candidates with a similarity metric
     and keep pairs above a threshold.
  4. **Group** — union pairs into clusters via union-find.
  5. **Explain** — every group carries *why* it matched, because a merge nobody
     can justify is a merge nobody should approve.

Nothing here deletes or merges anything. It produces a plan.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any

#: Company suffixes that carry no identifying information.
LEGAL_SUFFIXES = {
    "inc", "incorporated", "llc", "ltd", "limited", "corp", "corporation",
    "co", "company", "plc", "gmbh", "sa", "sas", "bv", "nv", "ag", "pty",
    "pvt", "private", "holdings", "group", "the",
}

_PUNCT = re.compile(r"[^a-z0-9\s]")
_SPACE = re.compile(r"\s+")
_DIGITS = re.compile(r"\D")


def normalize_name(value: Any) -> str:
    """Reduce a company or person name to its identifying core."""
    text = _PUNCT.sub(" ", str(value or "").lower())
    words = [w for w in _SPACE.sub(" ", text).strip().split(" ") if w]
    kept = [w for w in words if w not in LEGAL_SUFFIXES]
    return " ".join(kept or words)


def normalize_email(value: Any) -> str:
    email = str(value or "").strip().lower()
    if "@" not in email:
        return ""
    local, _, domain = email.partition("@")
    # Gmail-style tags and dots are not distinct addresses.
    if domain in {"gmail.com", "googlemail.com"}:
        local = local.split("+")[0].replace(".", "")
    else:
        local = local.split("+")[0]
    return f"{local}@{domain}"


def normalize_phone(value: Any) -> str:
    digits = _DIGITS.sub("", str(value or ""))
    # Compare on the last 10 digits so country/trunk prefixes do not split pairs.
    return digits[-10:] if len(digits) >= 10 else digits


def similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


@dataclass
class MatchRule:
    """One way two records can be considered the same thing."""

    name: str
    fields: tuple[str, ...]
    normalizer: Any
    #: Exact-match rules need no scoring; fuzzy rules compare within a block.
    exact: bool = True
    threshold: float = 0.88
    weight: float = 1.0


DEFAULT_RULES: dict[str, list[MatchRule]] = {
    "account": [
        MatchRule("exact_name", ("Name",), normalize_name, exact=True, weight=0.8),
        MatchRule("similar_name", ("Name",), normalize_name, exact=False, weight=0.7),
        MatchRule("website", ("Website",), normalize_email, exact=True, weight=0.6),
        MatchRule("phone", ("Phone",), normalize_phone, exact=True, weight=0.5),
    ],
    "contact": [
        MatchRule("email", ("Email",), normalize_email, exact=True, weight=1.0),
        MatchRule(
            "name_and_account",
            ("FirstName", "LastName"),
            normalize_name,
            exact=True,
            weight=0.7,
        ),
        MatchRule("phone", ("Phone",), normalize_phone, exact=True, weight=0.5),
    ],
    "lead": [
        MatchRule("email", ("Email",), normalize_email, exact=True, weight=1.0),
        MatchRule("company_and_name", ("Company", "LastName"), normalize_name, weight=0.7),
    ],
}

GENERIC_RULES = [MatchRule("name", ("Name",), normalize_name, exact=True, weight=0.8)]


def rules_for(object_name: str, fields: set[str]) -> list[MatchRule]:
    """The applicable rules for an object, filtered to fields it actually has."""
    candidates = DEFAULT_RULES.get(object_name.lower(), GENERIC_RULES)
    available = {f.lower() for f in fields}
    return [r for r in candidates if all(f.lower() in available for f in r.fields)]


class _UnionFind:
    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def find(self, item: str) -> str:
        self.parent.setdefault(item, item)
        while self.parent[item] != item:
            self.parent[item] = self.parent[self.parent[item]]
            item = self.parent[item]
        return item

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra

    def groups(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = defaultdict(list)
        for item in self.parent:
            out[self.find(item)].append(item)
        return out


@dataclass
class DuplicateGroup:
    record_ids: list[str]
    matched_on: list[str]
    confidence: float
    sample: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_ids": self.record_ids,
            "count": len(self.record_ids),
            "matched_on": self.matched_on,
            "confidence": round(self.confidence, 3),
            "records": self.sample,
        }


def _key_of(record: dict[str, Any], rule: MatchRule) -> str:
    parts = [rule.normalizer(record.get(f)) for f in rule.fields]
    if any(not p for p in parts):
        return ""
    return "|".join(parts)


def find_duplicates(
    records: list[dict[str, Any]],
    object_name: str,
    *,
    fields: set[str] | None = None,
    min_confidence: float = 0.5,
    max_block_size: int = 400,
) -> tuple[list[DuplicateGroup], dict[str, Any]]:
    """Group records that appear to be the same real-world thing.

    Returns `(groups, stats)`. `stats` records how the analysis was done —
    which rules fired, how many comparisons were made, whether any block was
    too large to compare exhaustively — so the answer is auditable rather than
    a number to be taken on faith.
    """
    if not records:
        return [], {"records": 0, "rules": [], "note": "No records to analyze."}

    present = fields or {k for r in records for k in r if k != "attributes"}
    applicable = rules_for(object_name, present)
    if not applicable:
        return [], {
            "records": len(records),
            "rules": [],
            "note": (
                f"No duplicate-matching rule applies to {object_name} with the fields "
                f"queried ({', '.join(sorted(present))})."
            ),
        }

    by_id = {str(r.get("Id")): r for r in records if r.get("Id")}
    union = _UnionFind()
    reasons: dict[tuple[str, str], list[str]] = defaultdict(list)
    scores: dict[tuple[str, str], float] = defaultdict(float)
    comparisons = 0
    skipped_blocks: list[str] = []

    for rule in applicable:
        blocks: dict[str, list[str]] = defaultdict(list)
        for record_id, record in by_id.items():
            key = _key_of(record, rule)
            if key:
                blocks[key if rule.exact else key[:6]].append(record_id)

        for key, members in blocks.items():
            if len(members) < 2:
                continue
            if len(members) > max_block_size:
                skipped_blocks.append(f"{rule.name}:{key}({len(members)})")
                continue
            if rule.exact:
                first = members[0]
                for other in members[1:]:
                    union.union(first, other)
                    pair = tuple(sorted((first, other)))
                    reasons[pair].append(rule.name)
                    scores[pair] = max(scores[pair], rule.weight)
                comparisons += len(members) - 1
                continue
            for i, left in enumerate(members):
                for right in members[i + 1 :]:
                    comparisons += 1
                    score = similarity(
                        _key_of(by_id[left], rule), _key_of(by_id[right], rule)
                    )
                    if score >= rule.threshold:
                        union.union(left, right)
                        pair = tuple(sorted((left, right)))
                        reasons[pair].append(f"{rule.name}({score:.2f})")
                        scores[pair] = max(scores[pair], rule.weight * score)

    groups: list[DuplicateGroup] = []
    for members in union.groups().values():
        if len(members) < 2:
            continue
        member_reasons: list[str] = []
        member_scores: list[float] = []
        for i, left in enumerate(sorted(members)):
            for right in sorted(members)[i + 1 :]:
                pair = tuple(sorted((left, right)))
                member_reasons.extend(reasons.get(pair, []))
                if pair in scores:
                    member_scores.append(scores[pair])
        confidence = max(member_scores) if member_scores else 0.0
        if confidence < min_confidence:
            continue
        groups.append(
            DuplicateGroup(
                record_ids=sorted(members),
                matched_on=sorted(set(member_reasons)),
                confidence=confidence,
                sample=[
                    {k: v for k, v in by_id[m].items() if k != "attributes"}
                    for m in sorted(members)[:5]
                ],
            )
        )

    groups.sort(key=lambda g: (-g.confidence, -len(g.record_ids)))
    stats = {
        "records": len(by_id),
        "rules": [r.name for r in applicable],
        "comparisons": comparisons,
        "groups": len(groups),
        "duplicate_records": sum(len(g.record_ids) for g in groups),
        "skipped_blocks": skipped_blocks[:10],
    }
    if skipped_blocks:
        stats["note"] = (
            f"{len(skipped_blocks)} blocking group(s) were larger than {max_block_size} "
            "records and were not compared pairwise. They are usually a shared generic "
            "value (an empty name, a switchboard phone number) rather than duplicates."
        )
    return groups, stats


# ---------------------------------------------------------------------------
# Merge planning
# ---------------------------------------------------------------------------
#: How to pick the record that survives a merge. Each is defensible and stated
#: on the plan, because "which one is the master" is the decision a human
#: actually needs to make.
SURVIVOR_STRATEGIES = {
    "oldest": "The earliest-created record, so historical references keep resolving.",
    "newest": "The most recently created record, assuming it is the most current.",
    "most_complete": "The record with the fewest empty fields.",
    "most_activity": "The record with the most related records.",
}


def choose_survivor(
    records: list[dict[str, Any]], strategy: str = "most_complete"
) -> tuple[dict[str, Any], str]:
    """Pick the surviving record and say why, in words a human can check."""
    if not records:
        raise ValueError("No records supplied.")
    if len(records) == 1:
        return records[0], "Only one record in the group."

    if strategy == "oldest":
        winner = min(records, key=lambda r: str(r.get("CreatedDate") or "9999"))
        return winner, f"Created first ({winner.get('CreatedDate')})."
    if strategy == "newest":
        winner = max(records, key=lambda r: str(r.get("CreatedDate") or ""))
        return winner, f"Created most recently ({winner.get('CreatedDate')})."
    if strategy == "most_activity":
        def activity(record: dict[str, Any]) -> int:
            return sum(
                int(record.get(k) or 0)
                for k in record
                if k.endswith("Count") or k.endswith("__r")
            )

        winner = max(records, key=activity)
        return winner, "Has the most related activity."

    def filled(record: dict[str, Any]) -> int:
        return sum(
            1
            for k, v in record.items()
            if k not in {"attributes", "Id"} and v not in (None, "", [])
        )

    winner = max(records, key=filled)
    return winner, f"Most complete: {filled(winner)} populated fields."


def build_merge_plan(
    group: DuplicateGroup,
    *,
    strategy: str = "most_complete",
) -> dict[str, Any]:
    """A per-group merge proposal, including what would be lost.

    Salesforce merges are irreversible. The plan therefore names, field by
    field, the values that exist on a losing record and not on the survivor —
    the data that quietly disappears if someone approves without reading.
    """
    records = group.sample
    if len(records) < 2:
        return {"error": "A merge needs at least two records."}
    survivor, why = choose_survivor(records, strategy)
    survivor_id = str(survivor.get("Id"))
    losers = [r for r in records if str(r.get("Id")) != survivor_id]

    conflicts: list[dict[str, Any]] = []
    data_loss: list[dict[str, Any]] = []
    for loser in losers:
        for key, value in loser.items():
            if key in {"attributes", "Id"} or value in (None, "", []):
                continue
            survivor_value = survivor.get(key)
            if survivor_value in (None, "", []):
                data_loss.append(
                    {
                        "field": key,
                        "value": value,
                        "from_record": loser.get("Id"),
                        "note": "The survivor has no value here; this one is lost.",
                    }
                )
            elif str(survivor_value).strip() != str(value).strip():
                conflicts.append(
                    {
                        "field": key,
                        "survivor_value": survivor_value,
                        "losing_value": value,
                        "from_record": loser.get("Id"),
                    }
                )

    return {
        "survivor_id": survivor_id,
        "survivor_reason": why,
        "strategy": strategy,
        "strategy_explanation": SURVIVOR_STRATEGIES.get(strategy, ""),
        "losing_ids": [str(r.get("Id")) for r in losers],
        "matched_on": group.matched_on,
        "confidence": round(group.confidence, 3),
        "field_conflicts": conflicts[:25],
        "data_that_would_be_lost": data_loss[:25],
        "warning": (
            "Salesforce merges cannot be undone. Losing records are deleted and their "
            "related records are re-parented to the survivor."
        ),
    }
