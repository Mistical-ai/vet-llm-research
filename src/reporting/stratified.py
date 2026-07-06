"""Stratified evaluation summaries with uncertainty."""

from __future__ import annotations

from collections import defaultdict
from statistics import mean
from typing import Any

from core.constants import get_random_seed
from metrics.medhelm import flagged_for_review_rate, hallucination_rate
from reporting.bootstrap import bootstrap_mean_ci

DEFAULT_STRATA = ("summarizer", "journal", "year", "article_type", "input_source")


def score_value(row: dict[str, Any]) -> float | None:
    """Return the primary score for a row, preferring jury_score."""
    raw = row.get("jury_score", row.get("quality_score"))
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def strata_value(row: dict[str, Any], field: str) -> str:
    """Read a grouping field from the row or nested strata dict."""
    if field in row and row.get(field) not in (None, "", []):
        value = row[field]
        if isinstance(value, list):
            return "|".join(str(item) for item in value) if value else "unknown"
        return str(value)
    strata = row.get("strata") or {}
    value = strata.get(field)
    if isinstance(value, list):
        return "|".join(str(item) for item in value) if value else "unknown"
    return str(value) if value not in (None, "") else "unknown"


def summarize_group(
    rows: list[dict[str, Any]],
    *,
    bootstrap_reps: int = 1000,
    seed: int | None = None,
) -> dict[str, Any]:
    """Summarize one group of evaluation rows."""
    scores = [score for row in rows if (score := score_value(row)) is not None]
    return {
        "n": len(rows),
        "valid_scores": len(scores),
        "mean_score": round(mean(scores), 4) if scores else 0.0,
        "score_ci95": bootstrap_mean_ci(scores, reps=bootstrap_reps, seed=seed),
        "hallucination_rate": hallucination_rate(rows),
        "flagged_for_review_rate": flagged_for_review_rate(rows),
    }


def stratified_summary(
    rows: list[dict[str, Any]],
    *,
    strata_fields: tuple[str, ...] = DEFAULT_STRATA,
    bootstrap_reps: int = 1000,
    seed: int | None = None,
) -> list[dict[str, Any]]:
    """Return summaries for each requested stratum."""
    resolved_seed = get_random_seed() if seed is None else seed
    output: list[dict[str, Any]] = []
    for field in strata_fields:
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            grouped[strata_value(row, field)].append(row)
        for value, group_rows in sorted(grouped.items()):
            output.append(
                {
                    "stratum": field,
                    "value": value,
                    **summarize_group(
                        group_rows, bootstrap_reps=bootstrap_reps, seed=resolved_seed
                    ),
                }
            )
    return output
