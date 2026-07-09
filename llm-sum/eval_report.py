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
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from scenarios import VET_TAXONOMY_V1, VeterinarySummaryQualityScenario
from reliability import compute_reliability
from eval_instances import load_manifest_index

EVALUATIONS_PATH = DATA_DIR / "evaluations.jsonl"
HUMAN_REVIEWS_PATH = DATA_DIR / "human_reviews.jsonl"
RESULTS_DIR = DATA_DIR / "results"
SUMMARIES_TXT_DIR = DATA_DIR / "summaries_txt"
DEV_TESTS_SUMMARIES_TXT_DIR = DATA_DIR / "dev_tests" / "summaries_txt"
DEFAULT_GROUP_FIELDS = ("summarizer", "species", "study_design", "clinical_topic", "journal", "input_source")


def save_report(report: dict[str, Any], ts: str, out_dir: Path = RESULTS_DIR) -> Path:
    """Write the full report JSON to a timestamped file under data/results/.

    Every eval-report run is saved so past results survive a fresh
    'evaluate' run overwriting evaluations.jsonl aggregates.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"eval_report_{ts}.json"
    out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return out_path


def save_markdown_report(markdown: str, ts: str, out_dir: Path = RESULTS_DIR) -> Path:
    """Write the plain-English aggregate Markdown report (File 1)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"eval_report_{ts}.md"
    out_path.write_text(markdown, encoding="utf-8")
    return out_path


def save_markdown_detail_report(markdown: str, ts: str, out_dir: Path = RESULTS_DIR) -> Path:
    """Write the per-article Markdown detail report (File 2)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"eval_report_{ts}_detail.md"
    out_path.write_text(markdown, encoding="utf-8")
    return out_path


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


def _load_human_reviews(path: Path | None) -> list[dict[str, Any]] | None:
    """Load normalized human-review rows, or None when none have been ingested.

    Lazy import of human_review (which imports this module) sidesteps the
    circular import. Returns None — not [] — when the file is absent so
    build_report renders a clear 'not ingested yet' reason instead of an empty,
    ambiguous 'available but no data' block.
    """
    resolved = path if path is not None else HUMAN_REVIEWS_PATH
    if not resolved.exists():
        return None
    from human_review import iter_human_review_rows  # noqa: E402  (lazy: avoids circular import)
    return list(iter_human_review_rows(resolved))


def _score(row: dict[str, Any]) -> float | None:
    """Prefer MedHELM jury_score but fall back to legacy quality_score."""
    value = row.get("jury_score")
    if isinstance(value, (int, float)):
        return float(value)
    value = row.get("quality_score")
    if isinstance(value, (int, float)) and value != 99:
        return float(value)
    return None


def _field_score(row: dict[str, Any], field: str) -> float | None:
    """Read a specific numeric aggregation field (weighted or unweighted)."""
    value = row.get(field)
    return float(value) if isinstance(value, (int, float)) else None


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
        weighted_scores = [
            s for row in items if (s := _field_score(row, "jury_score_weighted")) is not None
        ]
        unweighted_scores = [
            s for row in items if (s := _field_score(row, "jury_score_unweighted")) is not None
        ]
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
            # Both aggregation methods, so weighted vs. unweighted can be
            # compared in the same report without re-running judges.
            "mean_score_weighted": (
                round(sum(weighted_scores) / len(weighted_scores), 3) if weighted_scores else None
            ),
            "mean_score_unweighted": (
                round(sum(unweighted_scores) / len(unweighted_scores), 3) if unweighted_scores else None
            ),
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


def _build_human_validation(
    human_reviews: Iterable[dict[str, Any]] | None,
    mode: str | None = None,
) -> dict[str, Any]:
    """Human-validation block, or a self-describing 'unavailable' placeholder.

    Imported lazily because human_review imports THIS module
    (iter_evaluation_rows); a top-level import would be circular. When no
    human_reviews.jsonl has been ingested, returns available=False with a
    reason so the report renders the section uniformly either way. ``mode``
    selects per_reviewer / pooled / both (see human_review.analyze_human_reviews).
    """
    if human_reviews is None:
        return {
            "available": False,
            "reason": (
                "No human_reviews.jsonl yet. Export packets with "
                "'export-human-review', have reviewers fill the scoresheets, then "
                "'ingest-human-review' to populate this section."
            ),
        }
    from human_review import analyze_human_reviews  # noqa: E402  (lazy: avoids circular import)
    return analyze_human_reviews(human_reviews, mode=mode)


def build_report(
    rows: Iterable[dict[str, Any]],
    human_reviews: Iterable[dict[str, Any]] | None = None,
    human_validation_mode: str | None = None,
) -> dict[str, Any]:
    """Build a multi-stratum report from evaluation rows.

    The "taxonomy" entry is a header, not a stratum: it names which versioned
    veterinary benchmark task these rows belong to (see
    src/scenarios/taxonomy.py), so a report is self-describing even without
    its run manifest alongside it.

    ``human_reviews`` (normalized rows from data/human_reviews.jsonl, when a
    human-validation ingest has been run) adds the "human_validation" block —
    inter-reviewer agreement and human-vs-jury correlation. Left None, that
    block self-reports as unavailable, exactly like "reliability" does for a
    single-judge run, so the report shape never changes.
    """
    materialized = list(rows)
    return {
        "taxonomy": VET_TAXONOMY_V1.describe(VeterinarySummaryQualityScenario.name),
        # Inter-judge reliability. Like "taxonomy" this is a header block, not a
        # stratum: it is a single dict, so main() prints it separately from the
        # per-stratum tables. It self-reports available=False (with a reason)
        # when the run used a single judge, so the report never needs a jury.
        "reliability": compute_reliability(materialized),
        # Human validation — inter-reviewer agreement + human-vs-jury
        # correlation. Also a header block; self-reports available=False until
        # scoresheets are ingested.
        "human_validation": _build_human_validation(human_reviews, human_validation_mode),
        "overall": summarize_rows(materialized, group_field="benchmark_name"),
        "by_summarizer": summarize_rows(materialized, group_field="summarizer"),
        "by_species": summarize_rows(materialized, group_field="species"),
        "by_study_design": summarize_rows(materialized, group_field="study_design"),
        "by_clinical_topic": summarize_rows(materialized, group_field="clinical_topic"),
        "by_journal": summarize_rows(materialized, group_field="journal"),
        "by_input_source": summarize_rows(materialized, group_field="input_source"),
    }


# Section key -> plain-English title, in the order the report is printed.
_SECTION_TITLES = {
    "overall": "Overall",
    "by_summarizer": "By Provider",
    "by_species": "By Species",
    "by_study_design": "By Study Design",
    "by_clinical_topic": "By Clinical Topic",
    "by_journal": "By Journal",
    "by_input_source": "By Input Source",
}


def _fmt_score(value: float | None) -> str:
    return "-" if value is None else f"{value:.2f}"


def _fmt_pct(value: float | None) -> str:
    return "-" if value is None else f"{value * 100:.0f}%"


def _print_table(title: str, rows: list[dict[str, Any]]) -> bool:
    """Print one stratum as an aligned, easy-to-scan table.

    Returns False without printing anything when the section carries no real
    information — exactly one group, and that group is 'unknown' (case
    insensitive, since manifest.jsonl sometimes stores that literally as a
    curated value rather than a code default). A single-group "Unknown"
    table has nothing to compare against, so main() tracks it as skipped and
    explains why in one line at the end instead of showing empty noise.
    """
    if not rows:
        return False
    if len(rows) == 1 and str(rows[0]["group"]).strip().lower() == "unknown":
        return False

    headers = ["Group", "N", "Weighted", "Unweighted", "Halluc%", "Major%", "LowConf%", "ParseFail%"]
    body = [
        [
            str(row["group"]),
            str(row["n_rows"]),
            _fmt_score(row["mean_score_weighted"]),
            _fmt_score(row["mean_score_unweighted"]),
            _fmt_pct(row["hallucination_rate"]),
            _fmt_pct(row["major_hallucination_rate"]),
            _fmt_pct(row["low_confidence_rate"]),
            _fmt_pct(row["parse_failure_rate"]),
        ]
        for row in rows
    ]
    widths = [max(len(headers[i]), *(len(r[i]) for r in body)) for i in range(len(headers))]

    print(f"\n[{title}]")
    print("  " + "  ".join(h.ljust(w) for h, w in zip(headers, widths)))
    print("  " + "  ".join("-" * w for w in widths))
    for r in body:
        print("  " + "  ".join(cell.ljust(w) for cell, w in zip(r, widths)))
    return True


def _bold_top(cell: str, value: float | None, top: float | None) -> str:
    """Bold a formatted cell when its value ties the section's top value.

    Every group tied for the top value is bolded (not just the first one
    alphabetically), so the rendering never implies a false single winner.
    """
    if value is not None and top is not None and value == top:
        return f"**{cell}**"
    return cell


def _markdown_stratum_table(rows: list[dict[str, Any]]) -> list[str]:
    """Render one stratum's rows as a Markdown table sorted by Unweighted
    score descending (None last), with every top-tied Unweighted score bolded.
    """
    def sort_key(row: dict[str, Any]) -> tuple[bool, float]:
        value = row["mean_score_unweighted"]
        return (value is None, -(value if value is not None else 0.0))

    ordered = sorted(rows, key=sort_key)
    top = max(
        (r["mean_score_unweighted"] for r in rows if r["mean_score_unweighted"] is not None),
        default=None,
    )

    lines = [
        "| Group | N | Unweighted | Weighted | Hallucination % | Major % |",
        "|---|---|---|---|---|---|",
    ]
    for row in ordered:
        group = str(row["group"])
        label = "(no metadata)" if group.strip().lower() == "unknown" else group
        unweighted_cell = _bold_top(_fmt_score(row["mean_score_unweighted"]), row["mean_score_unweighted"], top)
        lines.append(
            f"| {label} | {row['n_rows']} | {unweighted_cell} | {_fmt_score(row['mean_score_weighted'])} | "
            f"{_fmt_pct(row['hallucination_rate'])} | {_fmt_pct(row['major_hallucination_rate'])} |"
        )
    return lines


def _render_markdown_reliability(reliability: dict[str, Any] | None) -> list[str]:
    """Render the inter-judge reliability block for the Markdown report."""
    if not reliability:
        return []
    lines = ["## Reliability (inter-judge agreement)", ""]
    if not reliability.get("available"):
        reason = reliability.get("reason", "not available")
        n_judges = reliability.get("n_judges", 0)
        lines.append(f"{reason} (judges seen: {n_judges}).")
        lines.append("")
        return lines

    jury = reliability.get("jury_score") or {}
    judges = ", ".join(reliability.get("judges", []))
    n_items = reliability.get("n_comparable_items", 0)
    lines.append(
        f"{n_items} article(s) were scored by more than one judge ({judges}). "
        f"Overall jury score agreement (Krippendorff's alpha): "
        f"**{jury.get('krippendorff_alpha')}** (mean={jury.get('mean')}, variance={jury.get('variance')})."
    )
    if reliability.get("interpretation"):
        lines.append(f"> {reliability['interpretation']}")
    lines.append("")

    per_criterion = reliability.get("per_criterion") or {}
    if per_criterion:
        lines.append("| Criterion | Alpha | Mean | Variance |")
        lines.append("|---|---|---|---|")
        for criterion, stats in per_criterion.items():
            lines.append(
                f"| {criterion} | {stats.get('krippendorff_alpha')} | {stats.get('mean')} | "
                f"{stats.get('variance')} |"
            )
        lines.append("")

    return lines


def _fmt_stat(value: float | None) -> str:
    """Format a correlation/bias number, or '-' when it could not be computed."""
    return "-" if value is None else f"{value:.2f}"


def _fmt_corr(coef: float | None, p: float | None) -> str:
    """Format a correlation as 'coef (p=..)', or just 'coef' when p is undefined.

    p comes from scipy (reliability.pearson_p / spearman_p); it is None when the
    significance is undefined (e.g. n=2), in which case only the coefficient
    shows so the table never prints a bare '(p=None)'.
    """
    if coef is None:
        return "-"
    if p is None:
        return f"{coef:.2f}"
    return f"{coef:.2f} (p<0.001)" if p < 0.001 else f"{coef:.2f} (p={p:.3f})"


def _corr_row_md(label: str, stats: dict[str, Any]) -> str:
    ba = stats.get("bland_altman") or {}
    return (
        f"| {label} | {stats.get('n', 0)} | "
        f"{_fmt_corr(stats.get('spearman'), stats.get('spearman_p'))} | "
        f"{_fmt_corr(stats.get('pearson'), stats.get('pearson_p'))} | "
        f"{_fmt_stat(ba.get('mean_bias'))} |"
    )


def _render_hvj_markdown(hvj: dict[str, Any] | None, heading: str) -> list[str]:
    """Render one human-vs-jury correlation block (pooled or one reviewer)."""
    if not hvj:
        return []
    overall = hvj.get("overall") or {}
    overall_w = hvj.get("overall_weighted") or {}
    lines = [f"**{heading}** (Spearman is the headline; bias is mean human − jury):", ""]
    lines.append("| Score | N | Spearman | Pearson | Bias |")
    lines.append("|---|---|---|---|---|")
    lines.append(_corr_row_md("**Overall (unweighted, MedHELM-comparable)**", overall))
    if overall_w:
        lines.append(_corr_row_md("Overall (weighted, clinical-risk)", overall_w))
    lines.append("")
    if overall.get("interpretation"):
        lines.append(f"> {overall['interpretation']}")
        lines.append("")
    per_criterion = hvj.get("per_criterion") or {}
    if per_criterion:
        lines.append("<details><summary>Per-criterion diagnostics</summary>")
        lines.append("")
        lines.append("| Criterion | N | Spearman | Pearson | Bias |")
        lines.append("|---|---|---|---|---|")
        for criterion, stats in per_criterion.items():
            lines.append(_corr_row_md(criterion, stats))
        lines.append("")
        lines.append("</details>")
        lines.append("")
    return lines


def _render_markdown_human_validation(human: dict[str, Any] | None) -> list[str]:
    """Render the human-validation block (agreement + correlation) for Markdown."""
    if not human:
        return []
    lines = ["## Human Validation (does the jury track experts?)", ""]
    if not human.get("available"):
        lines.append(human.get("reason", "not available"))
        lines.append("")
        return lines

    reviewers = ", ".join(human.get("reviewers", []))
    lines.append(
        f"{human.get('n_items', 0)} item(s) scored by "
        f"{human.get('n_reviewers', 0)} reviewer(s) ({reviewers})."
    )
    lines.append("")

    agreement = human.get("inter_reviewer_agreement") or {}
    if agreement.get("available"):
        overall = agreement.get("overall") or {}
        lines.append(
            "**Inter-reviewer agreement (Krippendorff's alpha):** "
            f"**{overall.get('krippendorff_alpha')}** on "
            f"{agreement.get('n_comparable_items', 0)} shared item(s)."
        )
        if agreement.get("interpretation"):
            lines.append(f"> {agreement['interpretation']}")
    else:
        lines.append(f"**Inter-reviewer agreement:** {agreement.get('reason', 'not available')}")
    lines.append("")

    # Per-reviewer blocks (each reviewer validated on their own scores) and/or
    # the pooled block, depending on the analysis mode.
    for reviewer_id, block in (human.get("by_reviewer") or {}).items():
        lines.extend(_render_hvj_markdown(block, f"Reviewer {reviewer_id} vs LLM jury"))
    if human.get("human_vs_jury"):
        lines.extend(_render_hvj_markdown(
            human["human_vs_jury"], "Pooled human vs LLM jury (all reviewers averaged)"))
    return lines


def _render_markdown(report: dict[str, Any], rows: list[dict[str, Any]]) -> str:
    """Render the plain-English aggregate Markdown report (File 1).

    Leads with Unweighted (the plain MedHELM-style average) as the primary
    score, keeping Weighted (this project's clinical-risk-weighted score)
    visible alongside it as a secondary column -- see docs/phase3/plan notes
    on why mean_score (a third, redundant field) is dropped from this view.
    """
    lines = ["# Veterinary Summary Evaluation Report", ""]

    taxonomy = report.get("taxonomy") or {}
    if taxonomy:
        benchmark = (
            f"{taxonomy.get('taxonomy_id')} v{taxonomy.get('taxonomy_version')} "
            f"({taxonomy.get('category_display_name')} / {taxonomy.get('task_display_name')})"
        )
    else:
        benchmark = "an unnamed benchmark"

    n_rows = len(rows)
    summarizers = sorted({str(r["summarizer"]) for r in rows if r.get("summarizer")})
    provider_note = (
        f" across {len(summarizers)} provider{'s' if len(summarizers) != 1 else ''} "
        f"({', '.join(summarizers)})"
        if summarizers else ""
    )
    lines.append(
        f"**{n_rows} summar{'y' if n_rows == 1 else 'ies'} evaluated**{provider_note} "
        f"using the {benchmark} benchmark."
    )
    lines.append("")

    if not rows:
        lines.append("No evaluation rows found yet. Run `evaluate` first, then re-run this report.")
        return "\n".join(lines) + "\n"

    overall_list = report.get("overall") or []
    overall = overall_list[0] if overall_list else None
    if overall:
        lines.append("## Headline")
        lines.append("")
        lines.append("| Metric | Value |")
        lines.append("|---|---|")
        lines.append(f"| Unweighted score (primary) | {_fmt_score(overall['mean_score_unweighted'])} / 5 |")
        lines.append(f"| Weighted score (secondary) | {_fmt_score(overall['mean_score_weighted'])} / 5 |")
        lines.append(
            f"| Hallucination rate | {_fmt_pct(overall['hallucination_rate'])} "
            f"({_fmt_pct(overall['major_hallucination_rate'])} major) |"
        )
        lines.append(
            f"| Needed human review (low judge confidence) | {_fmt_pct(overall['low_confidence_rate'])} |"
        )
        lines.append("")

    lines.extend(_render_markdown_reliability(report.get("reliability")))
    lines.extend(_render_markdown_human_validation(report.get("human_validation")))

    skipped: list[str] = []
    any_parse_failures = False
    for section, title in _SECTION_TITLES.items():
        if section == "overall":
            continue
        section_rows = report.get(section, [])
        if any((r.get("parse_failure_rate") or 0) > 0 for r in section_rows):
            any_parse_failures = True
        if not section_rows or (len(section_rows) == 1 and str(section_rows[0]["group"]).strip().lower() == "unknown"):
            skipped.append(title)
            continue
        lines.append(f"## {title}")
        lines.append("")
        lines.extend(_markdown_stratum_table(section_rows))
        lines.append("")

    if any_parse_failures:
        lines.append(
            "*Note: some summaries in this run had judge output that could not be "
            "parsed cleanly. See the JSON report's `parse_failure_rate` field for detail.*"
        )
        lines.append("")

    if skipped:
        lines.append(
            f"*No breakdown for: {', '.join(skipped)} -- that metadata isn't on these "
            "rows yet. This is common for rows judged from summarize-all's .txt files; "
            "species/study design/clinical topic normally come from manifest.jsonl.*"
        )
        lines.append("")

    lines.append("## Glossary")
    lines.append("")
    lines.append(
        "- **Unweighted score** -- plain average across the five rubric criteria "
        "(faithfulness, completeness, clinical usefulness, clarity, safety), 1-5 scale. "
        "Matches standard MedHELM scoring."
    )
    lines.append(
        "- **Weighted score** -- this project's clinical-risk-weighted average of the "
        "same five criteria (faithfulness and safety count more), 1-5 scale."
    )
    lines.append(
        "- **Hallucination rate / Major hallucination rate** -- share of summaries with "
        "at least one unsupported claim / at least one *major*-severity unsupported claim "
        "(one that could mislead a clinician)."
    )
    lines.append(
        "- **Low-confidence rate** -- share of summaries where the judge's own confidence "
        "was below 3/5, flagging them for human review."
    )
    lines.append("")

    return "\n".join(lines) + "\n"


def _dedupe_by_doi_summarizer(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep one row per (doi, summarizer): the one with the latest timestamp.

    A --no-resume rerun or a multi-judge panel can leave more than one
    evaluations.jsonl row for the same (doi, summarizer). ISO-8601
    timestamps sort correctly as plain strings, so string comparison is
    enough -- no datetime parsing needed. In jury mode this may pick one
    judge's row over another's; cross-judge agreement is reported
    separately in the "reliability" section, so nothing about jury
    agreement is lost, only the per-judge breakdown in this per-article view.
    """
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        doi = str(row.get("doi", "")).strip()
        summarizer = str(row.get("summarizer", "")).strip()
        if not doi or not summarizer:
            continue
        key = (doi, summarizer)
        existing = latest.get(key)
        if existing is None or str(row.get("timestamp") or "") >= str(existing.get("timestamp") or ""):
            latest[key] = row
    return list(latest.values())


def _load_candidate_summary_lookups() -> tuple[dict[tuple[str, str, str], str], dict[tuple[str, str], str]]:
    """Best-effort candidate-summary text lookups for the detail report.

    Imported lazily so a plain `eval_report.py` invocation never pulls in
    summarizer.py's/summarize_all_ingest.py's provider-related setup.
    Returns (jsonl_lookup keyed by (doi, input_source, summarizer),
    txt_lookup keyed by (doi, summarizer) from summarize-all's .txt files).
    """
    from summarizer import load_existing_summaries  # noqa: E402  (lazy import)
    from summarize_all_ingest import iter_summarize_all_instances  # noqa: E402  (lazy import)

    jsonl_lookup: dict[tuple[str, str, str], str] = {}
    for entry in load_existing_summaries().values():
        doi = str(entry.get("doi", "")).strip()
        input_source = str(entry.get("input_source") or "processed")
        for provider, slot in (entry.get("models") or {}).items():
            if isinstance(slot, dict) and slot.get("status") == "success" and slot.get("summary"):
                jsonl_lookup[(doi, input_source, str(provider))] = str(slot["summary"])

    txt_lookup: dict[tuple[str, str], str] = {}
    for folder in (SUMMARIES_TXT_DIR, DEV_TESTS_SUMMARIES_TXT_DIR):
        for instance in iter_summarize_all_instances(folder):
            txt_lookup.setdefault((instance.doi, instance.summarizer), instance.candidate_summary)

    return jsonl_lookup, txt_lookup


def _resolve_candidate_summary(
    row: dict[str, Any],
    jsonl_lookup: dict[tuple[str, str, str], str],
    txt_lookup: dict[tuple[str, str], str],
) -> str | None:
    """Three-step lookup: summaries.jsonl by (doi, input_source, summarizer),
    then summarize-all's .txt files by (doi, summarizer), else None."""
    doi = str(row.get("doi", "")).strip()
    summarizer = str(row.get("summarizer", "")).strip()
    input_source = str(row.get("input_source") or (row.get("strata") or {}).get("input_source") or "processed")
    text = jsonl_lookup.get((doi, input_source, summarizer))
    if text:
        return text
    return txt_lookup.get((doi, summarizer))


def _clean_text(value: Any) -> str:
    """Collapse whitespace (including embedded newlines) into single spaces.

    Some manifest.jsonl titles carry raw publisher-page whitespace/newlines
    (e.g. around inline <i> tags); left as-is, a multi-line title would
    break a Markdown "## " heading across several lines.
    """
    return " ".join(str(value).split())


def _render_markdown_detail(rows: list[dict[str, Any]], manifest_index: dict[str, dict[str, Any]]) -> str:
    """Render the per-article Markdown detail report (File 2).

    One section per article (grouped by doi, sorted by title), each with
    every model's score breakdown, judge reasoning, and hallucination
    claims -- everything needed to manually cross-check the judge's verdict
    against the real article without re-deriving anything.
    """
    deduped = _dedupe_by_doi_summarizer(rows)
    if not deduped:
        return (
            "# Veterinary Summary Evaluation -- Per-Article Detail\n\n"
            "No evaluation rows found yet. Run `evaluate` first, then re-run this report.\n"
        )

    jsonl_lookup, txt_lookup = _load_candidate_summary_lookups()

    by_doi: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in deduped:
        by_doi[str(row.get("doi", "")).strip()].append(row)

    def article_sort_key(doi: str) -> tuple[bool, str, str]:
        title = _clean_text((manifest_index.get(doi) or {}).get("title") or "")
        return (not title, title.lower(), doi)

    lines = [
        "# Veterinary Summary Evaluation -- Per-Article Detail",
        "",
        "Companion to the aggregate `eval_report_<timestamp>.md` in this same "
        "folder. One section per article, with every model's judged summary "
        "and score breakdown, so you can open the article and manually check "
        "the judge's scoring.",
        "",
    ]

    for doi in sorted(by_doi, key=article_sort_key):
        manifest_row = manifest_index.get(doi) or {}
        title = _clean_text(manifest_row.get("title") or "")
        heading = title if title else f"Untitled -- {doi or 'no DOI'}"
        lines.append(f"## {heading}")
        lines.append("")
        lines.append(f"**DOI:** [{doi}](https://doi.org/{doi})" if doi else "**DOI:** unknown")

        journal = manifest_row.get("journal")
        if journal:
            lines.append(f"**Journal:** {_clean_text(journal)}")

        strata = by_doi[doi][0].get("strata") or {}
        species = strata.get("species") or manifest_row.get("species")
        descriptors = [
            ", ".join(species) if isinstance(species, list) else species,
            strata.get("study_design") or manifest_row.get("study_design"),
            strata.get("clinical_topic") or manifest_row.get("clinical_topic"),
        ]
        descriptors = [_clean_text(d) for d in descriptors if d]
        if descriptors:
            lines.append(f"**Species / Topic / Study design:** {' · '.join(descriptors)}")
        lines.append("")

        article_rows = sorted(by_doi[doi], key=lambda r: str(r.get("summarizer") or ""))
        top_unweighted = max(
            (r.get("jury_score_unweighted") for r in article_rows
             if isinstance(r.get("jury_score_unweighted"), (int, float))),
            default=None,
        )

        lines.append("| Model | Unweighted | Weighted | Confidence | Hallucinations |")
        lines.append("|---|---|---|---|---|")
        for row in article_rows:
            unweighted = row.get("jury_score_unweighted")
            weighted = row.get("jury_score_weighted")
            confidence = row.get("confidence_score")
            claims = [c for c in (row.get("hallucination_claims") or []) if isinstance(c, dict)]
            major_count = sum(1 for c in claims if c.get("severity") == "major")
            unweighted_cell = _bold_top(
                "-" if unweighted is None else f"{float(unweighted):.2f}", unweighted, top_unweighted
            )
            weighted_cell = "-" if weighted is None else f"{float(weighted):.2f}"
            confidence_cell = "-" if confidence is None else f"{confidence}/5"
            lines.append(
                f"| {row.get('summarizer', 'unknown')} | {unweighted_cell} | {weighted_cell} | "
                f"{confidence_cell} | {len(claims)} ({major_count} major) |"
            )
        lines.append("")

        for row in article_rows:
            lines.append(f"#### {row.get('summarizer', 'unknown')}")
            for criterion, detail in (row.get("criteria_scores") or {}).items():
                if not isinstance(detail, dict):
                    continue
                label = str(criterion).replace("_", " ").capitalize()
                lines.append(f"- {label} {detail.get('score')}/5 -- \"{detail.get('reasoning', '')}\"")
            for claim in (row.get("hallucination_claims") or []):
                if not isinstance(claim, dict):
                    continue
                lines.append(
                    f"- Hallucination ({claim.get('severity', 'unknown')}): claimed "
                    f"\"{claim.get('claim', '')}\" -- article only supports: "
                    f"\"{claim.get('source_quote', '')}\""
                )
            summary_text = _resolve_candidate_summary(row, jsonl_lookup, txt_lookup)
            if summary_text:
                lines.append("- Candidate summary:")
                lines.append("")
                lines.append("  > " + summary_text.replace("\n", "\n  > "))
            else:
                lines.append(
                    "- Summary text not found in data/summaries.jsonl or "
                    "data/*summaries_txt/ -- the source file may have been moved "
                    "or deleted since evaluation."
                )
            lines.append("")

    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Summarize MedHELM-style evaluation JSONL.")
    parser.add_argument("--evaluations", type=Path, default=EVALUATIONS_PATH)
    parser.add_argument("--human-reviews", type=Path, default=HUMAN_REVIEWS_PATH,
                        help="Normalized human-validation rows from ingest-human-review "
                             "(default: data/human_reviews.jsonl). Adds the Human "
                             "Validation section when present.")
    parser.add_argument("--human-validation-mode", choices=("per_reviewer", "pooled", "both"),
                        default=None,
                        help="How to report human-vs-jury correlation. 'per_reviewer' "
                             "(default): one correlation per reviewer, so an expert is "
                             "validated on their own scores (not diluted by averaging "
                             "with other reviewers). 'pooled': one correlation over the "
                             "mean human score per item. 'both': show both. Overrides "
                             "HUMAN_VALIDATION_MODE from .env.")
    output_group = parser.add_mutually_exclusive_group()
    output_group.add_argument("--json", action="store_true", help="Print full report JSON.")
    output_group.add_argument("--markdown", action="store_true",
                        help="Print a plain-English Markdown report; also saves it and a "
                             "companion per-article detail file to --results-dir.")
    parser.add_argument("--no-detail", action="store_true",
                        help="With --markdown, skip the per-article detail file.")
    parser.add_argument("--results-dir", type=Path, default=RESULTS_DIR,
                        help="Where to save the report JSON (default: data/results/).")
    parser.add_argument("--no-save", action="store_true",
                        help="Print only; don't write a report file to --results-dir.")
    args = parser.parse_args(argv)

    rows = list(iter_evaluation_rows(args.evaluations))
    human_reviews = _load_human_reviews(args.human_reviews)
    # Precedence: --human-validation-mode (CLI) > HUMAN_VALIDATION_MODE (.env) >
    # the module default (per_reviewer). human_review validates the value.
    validation_mode = args.human_validation_mode or os.getenv("HUMAN_VALIDATION_MODE")
    report = build_report(rows, human_reviews, validation_mode)

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    should_save = not (args.no_save or not rows)

    # Nothing scored yet -- skip writing an empty placeholder file.
    saved_path = save_report(report, ts, args.results_dir) if should_save else None

    if args.markdown:
        markdown_text = _render_markdown(report, rows)
        saved_md_path = save_markdown_report(markdown_text, ts, args.results_dir) if should_save else None
        saved_detail_path = None
        if should_save and not args.no_detail:
            manifest_index = load_manifest_index()
            saved_detail_path = save_markdown_detail_report(
                _render_markdown_detail(rows, manifest_index), ts, args.results_dir
            )
        print(markdown_text)
        for path in (saved_path, saved_md_path, saved_detail_path):
            if path:
                print(f"Saved to: {path}", file=sys.stderr)
        return 0

    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
        if saved_path:
            print(f"\nSaved to: {saved_path}", file=sys.stderr)
        return 0

    print("=" * 72)
    print("VETERINARY SUMMARY EVALUATION REPORT")
    print("=" * 72)
    print(f"Source:      {args.evaluations}")
    print(f"Rows scored: {len(rows)}")

    taxonomy = report.get("taxonomy")
    if taxonomy:
        print(f"Benchmark:   {taxonomy['taxonomy_id']} v{taxonomy['taxonomy_version']} "
              f"- {taxonomy['category_display_name']} / {taxonomy['task_display_name']}")

    if not rows:
        print("\nNo evaluation rows found yet. Run 'evaluate' first, then re-run this report.")
        return 0

    if saved_path:
        print(f"Saved to:    {saved_path}")

    print(
        "\nColumns: N = evaluation rows | Weighted/Unweighted = the two jury_score\n"
        "aggregations, 1-5 scale (see docs/phase3/medhelm_evaluation.md) | Halluc%/\n"
        "Major% = rows with any / major-severity hallucination claims | LowConf% =\n"
        "judge confidence below 3 | ParseFail% = judge response could not be parsed."
    )

    _print_reliability(report.get("reliability"))
    _print_human_validation(report.get("human_validation"))

    skipped: list[str] = []
    for section, title in _SECTION_TITLES.items():
        if not _print_table(title, report.get(section, [])):
            skipped.append(title)

    if skipped:
        print(f"\n(No breakdown for: {', '.join(skipped)} -- that metadata isn't on "
              "these rows yet. This is common for rows judged from summarize-all's "
              ".txt files; species/study design/clinical topic normally come from "
              "manifest.jsonl.)")

    return 0


def _print_reliability(reliability: dict[str, Any] | None) -> None:
    """Render the inter-judge reliability block for the text report.

    Prints a single explanatory line when a jury is not present (the default
    single-judge run) so the reader knows *why* there is no agreement estimate,
    then the alpha/agreement numbers when two or more judges scored the data.
    """
    if not reliability:
        return
    print("\n[Reliability]")
    if not reliability.get("available"):
        n_judges = reliability.get("n_judges", 0)
        print(f"  {reliability.get('reason', 'not available')} (judges seen: {n_judges})")
        return

    jury = reliability.get("jury_score") or {}
    print(f"  Judges: {', '.join(reliability.get('judges', []))}  "
          f"(agreement measured on {reliability.get('n_comparable_items', 0)} "
          "article(s) scored by more than one judge)")
    print(f"  Overall jury score agreement (Krippendorff's alpha): "
          f"{jury.get('krippendorff_alpha')}  [mean={jury.get('mean')}, "
          f"variance={jury.get('variance')}]")
    if reliability.get("interpretation"):
        print(f"    -> {reliability['interpretation']}")

    per_criterion = reliability.get("per_criterion") or {}
    if per_criterion:
        print("  Per-criterion alpha:")
        for criterion, stats in per_criterion.items():
            print(f"    {criterion:<22} alpha={stats.get('krippendorff_alpha')}  "
                  f"mean={stats.get('mean')}  variance={stats.get('variance')}")

    pairwise = reliability.get("pairwise_agreement") or {}
    if pairwise:
        print("  Pairwise score agreement (mean/max absolute difference):")
        for pair, stats in pairwise.items():
            print(f"    {pair:<22} n={stats.get('n_items')}  "
                  f"mean_diff={stats.get('mean_abs_diff')}  "
                  f"max_diff={stats.get('max_abs_diff')}")


def _print_human_validation(human: dict[str, Any] | None) -> None:
    """Render the human-validation block for the plain text report.

    Prints one explanatory line when no scoresheets have been ingested (the
    common case), then inter-reviewer agreement and human-vs-jury correlation
    once data/human_reviews.jsonl exists.
    """
    if not human:
        return
    print("\n[Human validation]")
    if not human.get("available"):
        print(f"  {human.get('reason', 'not available')}")
        return

    reviewers = ", ".join(human.get("reviewers", []))
    print(f"  {human.get('n_items', 0)} item(s) scored by "
          f"{human.get('n_reviewers', 0)} reviewer(s): {reviewers}")

    agreement = human.get("inter_reviewer_agreement") or {}
    if agreement.get("available"):
        overall = agreement.get("overall") or {}
        print(f"  Inter-reviewer agreement (Krippendorff's alpha): "
              f"{overall.get('krippendorff_alpha')} "
              f"on {agreement.get('n_comparable_items', 0)} shared item(s)")
        if agreement.get("interpretation"):
            print(f"    -> {agreement['interpretation']}")
    else:
        print(f"  Inter-reviewer agreement: {agreement.get('reason', 'not available')}")

    for reviewer_id, block in (human.get("by_reviewer") or {}).items():
        _print_hvj(block, f"Reviewer {reviewer_id} vs LLM jury")
    if human.get("human_vs_jury"):
        _print_hvj(human["human_vs_jury"], "Pooled human vs LLM jury (all reviewers averaged)")


def _print_hvj(hvj: dict[str, Any] | None, heading: str) -> None:
    """Print one human-vs-jury correlation block (pooled or one reviewer)."""
    if not hvj:
        return
    overall = hvj.get("overall") or {}
    overall_w = hvj.get("overall_weighted") or {}

    def _corr_line(label: str, stats: dict[str, Any]) -> str:
        ba = stats.get("bland_altman") or {}
        return (f"    {label:<28} "
                f"spearman={_fmt_corr(stats.get('spearman'), stats.get('spearman_p'))}  "
                f"pearson={_fmt_corr(stats.get('pearson'), stats.get('pearson_p'))}  "
                f"bias={_fmt_stat(ba.get('mean_bias'))}  (n={stats.get('n', 0)})")

    print(f"  {heading} (Spearman | Pearson | bias human-jury):")
    print(_corr_line("overall (unweighted, MedHELM)", overall))
    if overall_w:
        print(_corr_line("overall (weighted, clinical)", overall_w))
    if overall.get("interpretation"):
        print(f"    -> {overall['interpretation']}")
    per_criterion = hvj.get("per_criterion") or {}
    if per_criterion:
        print("    per-criterion diagnostics:")
        for criterion, stats in per_criterion.items():
            print(_corr_line(criterion, stats))


if __name__ == "__main__":
    sys.exit(main())
