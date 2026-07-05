"""
llm-sum/eval_report.py — Stratified MedHELM-style evaluation summaries
======================================================================

WHY THIS MODULE EXISTS
----------------------
MedHELM's value is not just a score; it is a score organized by meaningful
clinical task groups. This module summarizes append-only evaluation JSONL rows
by model and veterinary strata so the researcher can see where performance is
strong or weak.
"""

from __future__ import annotations

from _bootstrap import *  # noqa: F401,F403

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

from reporting.exports import build_report_payload, write_standard_reports

EVALUATIONS_PATH = DATA_DIR / "evaluations.jsonl"
DEFAULT_GROUP_FIELDS = ("summarizer", "species", "study_design", "clinical_topic", "journal", "input_source")


def iter_evaluation_rows(path: Path | None = None) -> Iterable[dict[str, Any]]:
    """Yield valid evaluation rows from append-only JSONL."""
    resolved = path if path is not None else EVALUATIONS_PATH
    if not resolved.exists():
        return
    with open(resolved, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                yield row


def _score(row: dict[str, Any]) -> float | None:
    """Prefer MedHELM jury_score but fall back to legacy quality_score."""
    value = row.get("jury_score")
    if isinstance(value, (int, float)):
        return float(value)
    value = row.get("quality_score")
    if isinstance(value, (int, float)) and value != 99:
        return float(value)
    return None


def _major_hallucination(row: dict[str, Any]) -> bool:
    return any(
        claim.get("severity") == "major"
        for claim in (row.get("hallucination_claims") or [])
        if isinstance(claim, dict)
    )


def _strata_value(row: dict[str, Any], field: str) -> str:
    """Return a printable grouping value from top-level or strata fields."""
    if field in row and row[field] not in (None, "", []):
        value = row[field]
    else:
        value = (row.get("strata") or {}).get(field, "unknown")
    if isinstance(value, list):
        return ", ".join(str(v) for v in value) if value else "unknown"
    return str(value or "unknown")


def summarize_rows(rows: Iterable[dict[str, Any]], group_field: str = "summarizer") -> list[dict[str, Any]]:
    """Aggregate scores and reliability signals by one grouping field."""
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[_strata_value(row, group_field)].append(row)

    summary: list[dict[str, Any]] = []
    for group, items in sorted(groups.items()):
        scores = [score for row in items if (score := _score(row)) is not None]
        hallucination_rows = [row for row in items if int(row.get("hallucination_count") or 0) > 0]
        major_rows = [row for row in items if _major_hallucination(row)]
        low_confidence = [row for row in items if int(row.get("confidence_score") or 0) < 3]
        malformed = [row for row in items if row.get("parse_method") == "sentinel"]
        disagreements = [
            float(row["judge_disagreement"])
            for row in items
            if isinstance(row.get("judge_disagreement"), (int, float))
        ]
        summary.append({
            "group": group,
            "n_rows": len(items),
            "n_scored": len(scores),
            "mean_score": round(sum(scores) / len(scores), 3) if scores else None,
            "hallucination_rate": round(len(hallucination_rows) / len(items), 3) if items else 0.0,
            "major_hallucination_rate": round(len(major_rows) / len(items), 3) if items else 0.0,
            "low_confidence_rate": round(len(low_confidence) / len(items), 3) if items else 0.0,
            "parse_failure_rate": round(len(malformed) / len(items), 3) if items else 0.0,
            "mean_judge_disagreement": (
                round(sum(disagreements) / len(disagreements), 3)
                if disagreements else None
            ),
        })
    return summary


def build_report(
    rows: Iterable[dict[str, Any]],
    *,
    bootstrap_reps: int = 1000,
    seed: int = 42,
) -> dict[str, Any]:
    """Build a multi-stratum report from evaluation rows."""
    materialized = list(rows)
    return {
        "overall": summarize_rows(materialized, group_field="benchmark_name"),
        "by_summarizer": summarize_rows(materialized, group_field="summarizer"),
        "by_species": summarize_rows(materialized, group_field="species"),
        "by_study_design": summarize_rows(materialized, group_field="study_design"),
        "by_clinical_topic": summarize_rows(materialized, group_field="clinical_topic"),
        "by_journal": summarize_rows(materialized, group_field="journal"),
        "by_input_source": summarize_rows(materialized, group_field="input_source"),
        "uncertainty_aware": build_report_payload(
            materialized,
            bootstrap_reps=bootstrap_reps,
            seed=seed,
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Summarize MedHELM-style evaluation JSONL.")
    parser.add_argument("--evaluations", type=Path, default=EVALUATIONS_PATH)
    parser.add_argument("--json", action="store_true", help="Print full report JSON.")
    parser.add_argument("--output-dir", type=Path, default=None,
                        help="Optional directory for summary.json and CSV exports.")
    parser.add_argument("--bootstrap-reps", type=int, default=1000,
                        help="Bootstrap repetitions for uncertainty-aware reports.")
    parser.add_argument("--seed", type=int, default=42,
                        help="Deterministic seed for bootstrap resampling.")
    args = parser.parse_args(argv)

    rows = list(iter_evaluation_rows(args.evaluations))
    report = build_report(rows, bootstrap_reps=args.bootstrap_reps, seed=args.seed)
    if args.output_dir is not None:
        paths = write_standard_reports(
            args.output_dir,
            rows,
            bootstrap_reps=args.bootstrap_reps,
            seed=args.seed,
        )
        print(f"[eval-report] wrote machine-readable reports: {paths}")
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0

    for section, rows in report.items():
        if section == "uncertainty_aware":
            continue
        print(f"\n[{section}]")
        for row in rows:
            print(
                f"{row['group']}: n={row['n_rows']} mean={row['mean_score']} "
                f"halluc={row['hallucination_rate']} major={row['major_hallucination_rate']} "
                f"parse_fail={row['parse_failure_rate']}"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
