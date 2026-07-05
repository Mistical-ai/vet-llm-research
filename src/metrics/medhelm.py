"""MedHELM-style primary metrics."""

from __future__ import annotations

from typing import Any

DEFAULT_CRITERION_WEIGHTS = {
    "faithfulness": 1.5,
    "completeness": 1.0,
    "clinical_usefulness": 1.2,
    "clarity": 0.8,
    "safety": 1.3,
}


def clamp_score(value: Any, lo: int = 1, hi: int = 5) -> int:
    """Clamp judge-provided scores so malformed values do not skew metrics."""
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError):
        return lo


def calculate_jury_score(
    criteria_scores: dict[str, Any],
    weights: dict[str, float] | None = None,
) -> float:
    """Compute the deterministic weighted average used as the primary endpoint."""
    resolved = weights or DEFAULT_CRITERION_WEIGHTS
    weighted_sum = 0.0
    weight_total = 0.0
    for criterion, weight in resolved.items():
        raw = criteria_scores.get(criterion, {})
        if isinstance(raw, dict):
            raw = raw.get("score")
        score = clamp_score(raw)
        weighted_sum += score * weight
        weight_total += weight
    return round(weighted_sum / weight_total, 2) if weight_total else 0.0


def jury_to_quality_score(jury_score: float) -> int:
    """Map a 1-5 jury score to the legacy 1-10 compatibility score."""
    if jury_score <= 0:
        return 99
    return int(round(((jury_score - 1) / 4) * 9 + 1))


def hallucination_rate(rows: list[dict[str, Any]]) -> float:
    """Return the fraction of rows with at least one hallucination."""
    if not rows:
        return 0.0
    flagged = sum(1 for row in rows if int(row.get("hallucination_count") or 0) > 0)
    return round(flagged / len(rows), 4)


def flagged_for_review_rate(rows: list[dict[str, Any]]) -> float:
    """Return the fraction of rows that require human review."""
    if not rows:
        return 0.0
    flagged = sum(1 for row in rows if bool(row.get("requires_human_review")))
    return round(flagged / len(rows), 4)
