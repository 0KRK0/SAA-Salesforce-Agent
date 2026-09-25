"""Field completeness, validity and anomaly analysis.

Same rule as duplicate detection: the dataset is analyzed here and only the
findings travel. A completeness scan over 50,000 Contacts returns twenty
findings, not 50,000 rows.

Validity checks are deliberately conservative. Flagging a real email address as
invalid sends an admin on a pointless hunt, so the patterns here catch things
that are unambiguously wrong (no @ in an email, letters in a phone number, a
close date decades in the future) rather than things that look unusual.
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import UTC, date, datetime
from typing import Any

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s.]+\.[^@\s]{2,}$")
URL_RE = re.compile(r"^(https?://)?[\w.-]+\.[a-z]{2,}(/.*)?$", re.IGNORECASE)
PHONE_ALLOWED = re.compile(r"^[\d\s()+\-.extEXT]*$")


def _is_blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def completeness(
    records: list[dict[str, Any]], fields: list[str]
) -> list[dict[str, Any]]:
    """Per-field fill rate, worst first."""
    total = len(records)
    if not total:
        return []
    out = []
    for field_name in fields:
        filled = sum(1 for r in records if not _is_blank(r.get(field_name)))
        out.append(
            {
                "field": field_name,
                "populated": filled,
                "empty": total - filled,
                "fill_rate": round(filled / total * 100, 1),
            }
        )
    out.sort(key=lambda f: f["fill_rate"])
    return out


def validity(
    records: list[dict[str, Any]], field_types: dict[str, str]
) -> list[dict[str, Any]]:
    """Values that are definitely wrong for their field type."""
    findings: list[dict[str, Any]] = []
    today = datetime.now(UTC).date()

    for field_name, field_type in field_types.items():
        kind = (field_type or "").lower()
        bad: list[dict[str, Any]] = []
        for record in records:
            value = record.get(field_name)
            if _is_blank(value):
                continue
            problem = _problem_with(value, kind, today)
            if problem:
                bad.append(
                    {"record_id": record.get("Id"), "value": value, "problem": problem}
                )
        if bad:
            findings.append(
                {
                    "field": field_name,
                    "type": field_type,
                    "invalid_count": len(bad),
                    "invalid_rate": round(len(bad) / max(len(records), 1) * 100, 2),
                    "examples": bad[:10],
                }
            )
    findings.sort(key=lambda f: -f["invalid_count"])
    return findings


def _problem_with(value: Any, kind: str, today: date) -> str | None:
    text = str(value).strip()
    if kind == "email":
        if not EMAIL_RE.match(text):
            return "Not a syntactically valid email address."
        return None
    if kind == "url":
        if not URL_RE.match(text):
            return "Not a valid URL."
        return None
    if kind == "phone":
        if not PHONE_ALLOWED.match(text):
            return "Contains characters that are not part of a phone number."
        if len(re.sub(r"\D", "", text)) < 7:
            return "Too few digits to be a dialable number."
        return None
    if kind in {"date", "datetime"}:
        parsed = _parse_date(text)
        if parsed is None:
            return None
        if parsed.year > today.year + 50:
            return f"More than 50 years in the future ({parsed.isoformat()})."
        if parsed.year < 1900:
            return f"Implausibly far in the past ({parsed.isoformat()})."
        return None
    if kind in {"double", "currency", "int", "percent"}:
        try:
            number = float(text)
        except ValueError:
            return "Not numeric."
        if kind == "percent" and not (0 <= number <= 100):
            return f"Percent value outside 0-100 ({number})."
        if kind == "currency" and number < 0:
            return f"Negative currency amount ({number})."
        return None
    return None


def _parse_date(text: str) -> date | None:
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.strptime(text[:26] if "T" in text else text, fmt).date()
        except ValueError:
            continue
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def distributions(
    records: list[dict[str, Any]], picklist_fields: list[str], top: int = 8
) -> list[dict[str, Any]]:
    """Value distribution for picklists — where standardization problems show."""
    out = []
    for field_name in picklist_fields:
        counter = Counter(
            str(r.get(field_name)).strip()
            for r in records
            if not _is_blank(r.get(field_name))
        )
        if not counter:
            continue
        out.append(
            {
                "field": field_name,
                "distinct_values": len(counter),
                "top_values": [
                    {"value": v, "count": c} for v, c in counter.most_common(top)
                ],
                "long_tail": max(0, len(counter) - top),
            }
        )
    return out


def anomalies(
    records: list[dict[str, Any]], numeric_fields: list[str]
) -> list[dict[str, Any]]:
    """Outliers by median absolute deviation.

    MAD rather than standard deviation because Salesforce numeric fields are
    routinely skewed — one enormous opportunity should not widen the band so
    far that nothing is ever flagged.
    """
    findings = []
    for field_name in numeric_fields:
        values: list[tuple[Any, float]] = []
        for record in records:
            raw = record.get(field_name)
            if _is_blank(raw):
                continue
            try:
                values.append((record.get("Id"), float(raw)))
            except (TypeError, ValueError):
                continue
        if len(values) < 12:
            continue
        numbers = sorted(v for _, v in values)
        median = numbers[len(numbers) // 2]
        deviations = sorted(abs(v - median) for v in numbers)
        mad = deviations[len(deviations) // 2]
        if mad == 0:
            continue
        outliers = [
            {"record_id": rid, "value": value, "deviation": round((value - median) / mad, 1)}
            for rid, value in values
            if abs(value - median) / mad > 6
        ]
        if outliers:
            findings.append(
                {
                    "field": field_name,
                    "median": median,
                    "outlier_count": len(outliers),
                    "examples": sorted(
                        outliers, key=lambda o: -abs(o["deviation"])
                    )[:10],
                    "method": "median absolute deviation, threshold 6x MAD",
                }
            )
    return findings


def summarize(
    *,
    object_name: str,
    analyzed: int,
    total_in_org: int | None,
    completeness_rows: list[dict[str, Any]],
    validity_rows: list[dict[str, Any]],
    anomaly_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """A short, ranked verdict — what an admin would actually act on."""
    issues: list[str] = []
    for row in completeness_rows[:3]:
        if row["fill_rate"] < 80:
            issues.append(
                f"{row['field']} is empty on {row['empty']} of {analyzed} records "
                f"({100 - row['fill_rate']:.0f}%)."
            )
    for row in validity_rows[:3]:
        issues.append(
            f"{row['field']} has {row['invalid_count']} value(s) that are not valid "
            f"for a {row['type']} field."
        )
    for row in anomaly_rows[:2]:
        issues.append(
            f"{row['field']} has {row['outlier_count']} extreme outlier(s) "
            f"(median {row['median']})."
        )
    sampled = total_in_org is not None and analyzed < total_in_org
    return {
        "object": object_name,
        "records_analyzed": analyzed,
        "records_in_org": total_in_org,
        "sampled": sampled,
        "sampling_note": (
            f"Findings are based on {analyzed:,} of {total_in_org:,} records. Rates are "
            "representative; absolute counts are not org-wide."
            if sampled
            else ""
        ),
        "top_issues": issues or ["No significant data-quality problems were found."],
    }
