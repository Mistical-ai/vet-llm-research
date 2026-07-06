"""Paired model comparisons for shared benchmark instances."""

from __future__ import annotations

from collections import defaultdict
from statistics import mean
from typing import Any

from reporting.stratified import score_value


def _instance_key(row: dict[str, Any]) -> tuple[str, str]:
    return (str(row.get("instance_id") or row.get("doi") or ""), str(row.get("input_source") or ""))


def paired_deltas(
    rows: list[dict[str, Any]],
    *,
    model_a: str,
    model_b: str,
) -> list[dict[str, Any]]:
    """Return per-instance score deltas for two summarizer models."""
    by_instance: dict[tuple[str, str], dict[str, Any]] = defaultdict(lambda: {"scores": {}})
    for row in rows:
        summarizer = str(row.get("summarizer") or "")
        if summarizer not in {model_a, model_b}:
            continue
        score = score_value(row)
        if score is None:
            continue
        key = _instance_key(row)
        by_instance[key]["scores"][summarizer] = score
        by_instance[key].setdefault("doi", str(row.get("doi") or ""))

    deltas: list[dict[str, Any]] = []
    for (instance_id, input_source), payload in sorted(by_instance.items()):
        scores = payload["scores"]
        if model_a not in scores or model_b not in scores:
            continue
        deltas.append(
            {
                "instance_id": instance_id,
                "doi": payload.get("doi") or instance_id,
                "input_source": input_source,
                "model_a": model_a,
                "model_b": model_b,
                "score_a": scores[model_a],
                "score_b": scores[model_b],
                "delta_a_minus_b": round(scores[model_a] - scores[model_b], 4),
            }
        )
    return deltas


def paired_win_rate(deltas: list[dict[str, Any]]) -> dict[str, float | int]:
    """Summarize win/loss/tie rates from paired deltas."""
    if not deltas:
        return {
            "n": 0,
            "mean_delta": 0.0,
            "model_a_win_rate": 0.0,
            "model_b_win_rate": 0.0,
            "tie_rate": 0.0,
        }
    values = [float(row["delta_a_minus_b"]) for row in deltas]
    wins_a = sum(1 for value in values if value > 0)
    wins_b = sum(1 for value in values if value < 0)
    ties = sum(1 for value in values if value == 0)
    n = len(values)
    return {
        "n": n,
        "mean_delta": round(mean(values), 4),
        "model_a_win_rate": round(wins_a / n, 4),
        "model_b_win_rate": round(wins_b / n, 4),
        "tie_rate": round(ties / n, 4),
    }


def all_pairwise_comparisons(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build win-rate summaries for every model pair present in rows."""
    models = sorted({str(row.get("summarizer")) for row in rows if row.get("summarizer")})
    output: list[dict[str, Any]] = []
    for i, model_a in enumerate(models):
        for model_b in models[i + 1 :]:
            deltas = paired_deltas(rows, model_a=model_a, model_b=model_b)
            output.append({"model_a": model_a, "model_b": model_b, **paired_win_rate(deltas)})
    return output
