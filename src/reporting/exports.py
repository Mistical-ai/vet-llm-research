"""Machine-readable report exports."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from reporting.paired import all_pairwise_comparisons
from reporting.stratified import stratified_summary, summarize_group


def build_report_payload(
    rows: list[dict[str, Any]],
    *,
    bootstrap_reps: int = 1000,
    seed: int = 42,
) -> dict[str, Any]:
    """Build the standard report JSON payload."""
    return {
        "schema_version": "report_v1",
        "overall": summarize_group(rows, bootstrap_reps=bootstrap_reps, seed=seed),
        "strata": stratified_summary(rows, bootstrap_reps=bootstrap_reps, seed=seed),
        "paired_model_comparisons": all_pairwise_comparisons(rows),
    }


def write_json_report(path: Path, payload: dict[str, Any]) -> None:
    """Write an indented JSON report."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    """Write a list of dictionaries as CSV."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = sorted({key for row in rows for key in row})
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_standard_reports(
    output_dir: Path,
    rows: list[dict[str, Any]],
    *,
    bootstrap_reps: int = 1000,
    seed: int = 42,
) -> dict[str, str]:
    """Write JSON/CSV report artifacts and return their paths."""
    payload = build_report_payload(rows, bootstrap_reps=bootstrap_reps, seed=seed)
    output_dir.mkdir(parents=True, exist_ok=True)
    summary_json = output_dir / "summary.json"
    strata_csv = output_dir / "strata_summary.csv"
    paired_csv = output_dir / "paired_model_comparison.csv"
    write_json_report(summary_json, payload)
    write_csv(strata_csv, payload["strata"])
    write_csv(paired_csv, payload["paired_model_comparisons"])
    return {
        "summary_json": str(summary_json),
        "strata_csv": str(strata_csv),
        "paired_csv": str(paired_csv),
    }
