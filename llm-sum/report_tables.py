"""
llm-sum/report_tables.py — Publication-grade quantitative reporting
====================================================================

WHY THIS MODULE EXISTS
----------------------
``eval_report.py`` answers "how did each model do, by clinical stratum?" for an
operator reading a terminal. This module answers the question a *paper* asks:
"which provider is best, is the difference statistically significant, and what
does the quality cost?" It reads the same append-only ``data/evaluations.jsonl``
rows (plus, for cost, ``data/summaries.jsonl``) and emits publication-ready
tables — provider comparison with confidence intervals, per-stratum breakdowns,
the processed-vs-PDF input comparison, cost-per-quality-point, and paired
significance tests — in machine-readable (JSON + CSV) and human-readable
(Markdown) form.

DESIGN CONSTRAINTS (from CLAUDE.md)
-----------------------------------
- Offline only. Never calls a paid API; consumes rows ``evaluator.py`` wrote.
- Research-grade statistics via ``scipy``. The significance tests (Wilcoxon
  signed-rank, Friedman) and the bootstrap confidence intervals come from
  ``scipy.stats`` — a peer-reviewed, citable library that computes EXACT
  small-sample p-values, which matters because a stratified veterinary study
  runs many comparisons at small per-cell n (exactly where a normal
  approximation is weakest). ``scipy`` and ``numpy`` are pinned in
  requirements.txt, so every machine reproduces the identical result — a pinned
  validated library is MORE reproducible than hand-rolled math, not less.
  (Krippendorff's alpha in ``reliability.py`` stays hand-rolled only because
  scipy does not implement it.)
- Reuses existing joins: ``eval_report.iter_evaluation_rows`` for the rows,
  ``reliability.compute_reliability`` for inter-judge agreement,
  ``models_config.compute_cost`` for pricing, and the taxonomy header from
  ``scenarios.VET_TAXONOMY_V1`` so a report names the versioned benchmark it
  belongs to.

THE UNIT OF ANALYSIS
--------------------
One "item" is a (doi, input_source) pair — a paper judged from one input
channel (cleaned text vs. direct PDF). Within an item, each provider's summary
may have been scored by several judges (the default 3-judge jury) or a single
judge; those judge scores are averaged first, so every provider contributes one score per
item. Significance tests then pair providers *by item* (a paper is its own
block), which is what makes "provider A beat provider B on the same papers"
a valid paired comparison rather than a comparison of two independent samples.
"""

from __future__ import annotations

from _bootstrap import *  # noqa: F401,F403

import argparse
import csv
import io
import json
import os
import sys
import warnings
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import numpy as np
from scipy import stats as scipy_stats

from models_config import all_providers, compute_cost
from reliability import compute_reliability
from scenarios import VET_TAXONOMY_V1, VeterinarySummaryQualityScenario
from eval_report import iter_evaluation_rows
from eval_instances import load_manifest_index
import stats_engine  # noqa: E402  (information density, subscription economics, covariates)
# calculate_jury_score is the single implementation of the composite formula,
# reused here to recompute composites from criteria at read time (see
# _apply_recomputed_composites) rather than reimplementing the weighting.
import evaluator  # noqa: E402

EVALUATIONS_PATH = DATA_DIR / "evaluations.jsonl"
SUMMARIES_PATH = DATA_DIR / "summaries.jsonl"
RESULTS_DIR = DATA_DIR / "results"

# The five stratification fields carried on every evaluation row's ``strata``
# block (see eval_instances.STRATIFICATION_FIELDS). input_source gets its own
# dedicated processed-vs-PDF comparison, so it is not repeated here.
STRATUM_FIELDS = ("species", "study_design", "clinical_topic", "journal")

# Default number of bootstrap resamples behind every confidence interval.
# 2000 is the conventional floor for a stable 95% percentile interval; tunable
# via PUBLICATION_BOOTSTRAP_RESAMPLES for a quick draft (fewer) or a final
# camera-ready run (more). The seed keeps every CI reproducible run-to-run.
DEFAULT_BOOTSTRAP_RESAMPLES = 2000
DEFAULT_BOOTSTRAP_SEED = 42
DEFAULT_CI_ALPHA = 0.05

# Below this many paired items a significance test's p-value is statistically
# unstable, so the reported verdict is caveated (the number is still shown for
# transparency). Mirrors reliability.MIN_CORRELATION_N's intent.
MIN_ITEMS_FOR_SIGNIFICANCE = 10

# Multiple-comparison correction applied to the family of pairwise Wilcoxon
# p-values within one score mode. Benjamini-Hochberg (false discovery rate) is
# used rather than Holm/Bonferroni because a stratified study runs many
# comparisons and FDR keeps power without inflating false positives; it comes
# from scipy.stats.false_discovery_control (no new dependency, no hand-rolled
# math — same rule as every other statistic here).
MULTIPLE_COMPARISON_METHOD = "benjamini-hochberg"


# ===========================================================================
# Statistics primitives (scipy-backed — see module docstring)
# ===========================================================================
# Thin wrappers around scipy.stats that normalize its results into the flat,
# JSON-serializable dicts the renderers and CSV writers below already consume.
# Keeping the wrappers here (rather than calling scipy inline) means the shape
# of a "significance result" is defined in exactly one place.

def wilcoxon_signed_rank(diffs: list[float]) -> dict[str, Any]:
    """Two-sided Wilcoxon signed-rank test on paired differences.

    ``diffs`` is one ``score_A - score_B`` per item both providers scored. The
    p-value comes from ``scipy.stats.wilcoxon`` with ``method="auto"``, which
    uses the EXACT distribution for small samples without ties/zeros and the
    normal approximation otherwise — the right behaviour for a study whose
    per-pair n is often small. Zero differences are dropped (a paper both
    providers scored identically carries no directional signal), matching the
    Wilcoxon convention.

    ``w_plus`` / ``w_minus`` (the signed rank sums) are computed here purely for
    transparency in the table. Returns ``available=False`` when no non-zero
    difference survives; ``n`` is the number of contributing pairs, and below
    :data:`MIN_ITEMS_FOR_SIGNIFICANCE` the caller treats the p-value as
    indicative only.
    """
    nonzero = [float(d) for d in diffs if d != 0]
    n = len(nonzero)
    if n == 0:
        return {"available": False, "reason": "No non-zero paired differences.",
                "n": 0, "n_pairs": len(diffs)}

    ranks = scipy_stats.rankdata([abs(d) for d in nonzero])
    w_plus = float(sum(r for r, d in zip(ranks, nonzero) if d > 0))
    w_minus = float(sum(r for r, d in zip(ranks, nonzero) if d < 0))

    try:
        # scipy warns when it falls back from exact to the normal approximation
        # (ties/zeros/large n); that is expected here, so silence it — the
        # method choice is scipy's and is the statistically correct one.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            result = scipy_stats.wilcoxon(
                nonzero, alternative="two-sided", zero_method="wilcox",
                correction=True, method="auto",
            )
        statistic = float(result.statistic)
        p_value = float(result.pvalue)
    except ValueError:
        # Degenerate input scipy refuses (e.g. every difference identical after
        # zero removal): report no significant difference rather than crash.
        statistic, p_value = min(w_plus, w_minus), 1.0

    return {
        "available": True,
        "n": n,
        "n_pairs": len(diffs),
        "w_plus": round(w_plus, 3),
        "w_minus": round(w_minus, 3),
        "statistic": round(statistic, 3),
        "p_value": round(p_value, 4),
        "mean_diff": round(sum(nonzero) / n, 3),
    }


def friedman(blocks: list[dict[str, float]], providers: list[str]) -> dict[str, Any]:
    """Friedman test across >=3 providers on complete blocks (items).

    Each block is one item's ``{provider: score}`` map; only blocks that scored
    *every* provider are passed in (an incomplete block cannot be ranked across
    all treatments). The statistic and p-value come from
    ``scipy.stats.friedmanchisquare`` (a chi-square with ``k-1`` df, tie-aware);
    ``mean_ranks`` are computed here so the table can show which provider ranked
    best on average.

    Returns ``available=False`` for fewer than three providers (use pairwise
    Wilcoxon instead) or fewer than two complete blocks.
    """
    k = len(providers)
    n = len(blocks)
    if k < 3:
        return {"available": False,
                "reason": "Friedman needs >=3 providers; see pairwise Wilcoxon.",
                "n_blocks": n, "k": k}
    if n < 2:
        return {"available": False,
                "reason": "Fewer than two items scored by all providers.",
                "n_blocks": n, "k": k}

    # Mean rank per provider (ascending ranks within each block, tie-averaged).
    rank_sums = {p: 0.0 for p in providers}
    for block in blocks:
        block_ranks = scipy_stats.rankdata([block[p] for p in providers])
        for p, r in zip(providers, block_ranks):
            rank_sums[p] += float(r)

    columns = [[block[p] for block in blocks] for p in providers]
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            result = scipy_stats.friedmanchisquare(*columns)
        statistic = float(result.statistic)
        p_value = float(result.pvalue)
    except ValueError:
        statistic, p_value = 0.0, 1.0
    # All-identical blocks make the statistic undefined (0/0 → nan); report no
    # difference rather than propagate a nan into the report.
    if statistic != statistic or p_value != p_value:  # NaN check
        statistic, p_value = 0.0, 1.0

    return {
        "available": True,
        "n_blocks": n,
        "k": k,
        "statistic": round(statistic, 3),
        "df": k - 1,
        "p_value": round(p_value, 4),
        "mean_ranks": {p: round(rank_sums[p] / n, 3) for p in providers},
    }


def bootstrap_ci(
    values: list[float], *, seed: int, n_resamples: int = DEFAULT_BOOTSTRAP_RESAMPLES,
    alpha: float = DEFAULT_CI_ALPHA,
) -> dict[str, Any]:
    """Percentile bootstrap confidence interval for the mean of ``values``.

    Wraps ``scipy.stats.bootstrap`` (percentile method) so the interval comes
    from a validated implementation. Deterministic for a given ``seed`` —
    ``numpy``'s seeded ``default_rng`` drives the resampling — so every CI in a
    paper reproduces exactly. Returns the mean with ``ci_low=ci_high=None`` when
    fewer than two values exist (a CI needs spread to estimate).
    """
    clean = [float(v) for v in values]
    n = len(clean)
    mean = round(sum(clean) / n, 3) if n else None
    if n < 2:
        return {"mean": mean, "ci_low": None, "ci_high": None, "n": n}
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        result = scipy_stats.bootstrap(
            (np.asarray(clean),), np.mean, n_resamples=n_resamples,
            confidence_level=1.0 - alpha, method="percentile",
            random_state=np.random.default_rng(seed),
        )
    ci = result.confidence_interval
    return {
        "mean": mean,
        "ci_low": round(float(ci.low), 3),
        "ci_high": round(float(ci.high), 3),
        "n": n,
    }


def benjamini_hochberg(p_values: list[float]) -> list[float]:
    """Benjamini-Hochberg FDR-adjusted p-values (q-values) for a family of tests.

    Wraps ``scipy.stats.false_discovery_control`` (method="bh") so the
    correction comes from a validated implementation, not hand-rolled ranking.
    Input order is preserved: the i-th returned q-value corresponds to the
    i-th input p-value. An empty input returns an empty list; a single p-value
    is returned unchanged (a family of one needs no correction). q-values are
    rounded to 4 decimals to match the raw p-value formatting elsewhere.
    """
    if not p_values:
        return []
    if len(p_values) == 1:
        return [round(float(p_values[0]), 4)]
    adjusted = scipy_stats.false_discovery_control(p_values, method="bh")
    return [round(float(q), 4) for q in adjusted]


# ===========================================================================
# Row → per-item, per-provider scores
# ===========================================================================

def _unweighted(row: dict[str, Any]) -> float | None:
    """Primary MedHELM score for a row: unweighted jury mean, else jury_score."""
    for field in ("jury_score_unweighted", "jury_score"):
        value = row.get(field)
        if isinstance(value, (int, float)):
            return float(value)
    return None


def _weighted(row: dict[str, Any]) -> float | None:
    value = row.get("jury_score_weighted")
    return float(value) if isinstance(value, (int, float)) else None


def _disagreement(row: dict[str, Any]) -> float | None:
    value = row.get("judge_disagreement")
    return float(value) if isinstance(value, (int, float)) else None


def _input_source(row: dict[str, Any]) -> str:
    return str(row.get("input_source") or (row.get("strata") or {}).get("input_source") or "processed")


def _strata_value(row: dict[str, Any], field: str) -> str:
    """Printable value of one stratum field from top-level or the strata block."""
    if row.get(field) not in (None, "", []):
        value = row[field]
    else:
        value = (row.get("strata") or {}).get(field, "unknown")
    if isinstance(value, list):
        return ", ".join(str(v) for v in value) if value else "unknown"
    return str(value or "unknown")


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _criterion_scores(row: dict[str, Any]) -> dict[str, float]:
    """One judge's numeric criterion scores from the nested criteria_scores block.

    A criterion the judge omitted, or returned unparseably, is simply absent
    from the result — never imputed. Imputing the floor (1) would be a strong
    claim the judge never made; see evaluator.calculate_jury_score for the
    matching rule on the composite side.
    """
    block = row.get("criteria_scores")
    if not isinstance(block, dict):
        return {}
    scores: dict[str, float] = {}
    for criterion in stats_engine.CRITERIA:
        item = block.get(criterion)
        value = item.get("score") if isinstance(item, dict) else item
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            scores[criterion] = float(value)
    return scores


def _hallucination_flag(row: dict[str, Any]) -> bool | None:
    """Did this judge report a hallucination? ``None`` when it never said.

    An absent or unparseable ``hallucination_count`` is missing data, NOT a
    clean bill of health. Counting it as clean biases the rate downward in the
    study's own favour — worse parse rates would read as fewer hallucinations.
    """
    value = row.get("hallucination_count")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value) > 0


def _major_hallucination_flag(row: dict[str, Any]) -> bool | None:
    """Same missing-vs-clean distinction for major-severity claims."""
    claims = row.get("hallucination_claims")
    if not isinstance(claims, list):
        return None
    return any(isinstance(c, dict) and c.get("severity") == "major" for c in claims)


def _low_confidence_flag(row: dict[str, Any]) -> bool | None:
    value = row.get("confidence_score")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value) < 3


def _collapse_flags(values: list[bool | None]) -> dict[str, Any]:
    """Collapse one item's per-judge booleans into a single panel verdict.

    ``value`` (the headline) is the MAJORITY verdict: the item counts as
    positive when more than half the judges that reported flagged it. With a
    3-judge panel that needs 2; with a single judge — normal on this corpus,
    since resume is per (doi, summariser, judge) — that judge's call stands
    alone, which must be stated wherever the rate is published.

    Majority rather than OR because inter-judge agreement on hallucination is
    low: on this project's corpus, "any judge flagged it" marks 80.6% of items
    while "most judges agreed" marks 6.5% and no item ever drew a unanimous
    flag. Under that much disagreement, OR reports "at least one judge was
    worried", which is a much weaker claim than "this summary contains a
    hallucination" and would dominate the headline on lone-judge calls.

    ``any_value`` and ``mean_rate`` are kept alongside as the upper bound and
    the per-judgement comparator (the latter is close to the pre-fix figure,
    which was judge-row-weighted). All three are published: the spread between
    them IS the inter-judge disagreement, and hiding it would be the mistake.

    The verdict is None ONLY when every judge on the item is missing data — a
    partially-missing item still yields a verdict from those that did report.
    ``n_missing`` counts the unusable rows so coverage stays visible.
    """
    usable = [v for v in values if v is not None]
    if not usable:
        return {"value": None, "any_value": None, "mean_rate": None,
                "n_missing": len(values), "n_reporting": 0}
    n_flagged = sum(1 for v in usable if v)
    return {
        "value": n_flagged * 2 > len(usable),
        "any_value": n_flagged > 0,
        "mean_rate": n_flagged / len(usable),
        "n_missing": len(values) - len(usable),
        "n_reporting": len(usable),
    }


def _collapse_duplicate_judge_rows(judge_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Collapse repeat judgements from ONE judge on ONE item into one rating.

    A re-run (``--no-resume``, or a crashed run retried) appends a second row
    for the same (item, provider, judge). Both are complete judgements, so both
    are treated as observations and averaged, rather than one being declared to
    supersede the other — see reliability._units_for for the full rationale.

    Averaging happens PER CRITERION, and the composites are then recomputed from
    those averaged criteria rather than averaging the stored composites. The two
    differ whenever a criterion is missing from one row but not the other: only
    recomputation applies the current renormalize-over-present rule to the
    merged result. This is also what makes the missing-criterion fix retroactive
    to rows written before it, since the stored jury_score_* on those rows was
    computed with the old impute-to-1 behaviour.
    """
    if len(judge_rows) == 1:
        row = dict(judge_rows[0])
        row["criteria_scores"] = _normalize_criteria_block(row.get("criteria_scores"))
        _apply_recomputed_composites(row)
        return row

    merged = dict(judge_rows[-1])  # non-score metadata (strata, doi, ...) from any row

    # Per-criterion mean across the duplicates, skipping rows that omitted it.
    per_criterion: dict[str, list[float]] = defaultdict(list)
    for row in judge_rows:
        for criterion, value in _criterion_scores(row).items():
            per_criterion[criterion].append(value)
    merged["criteria_scores"] = {
        criterion: {"score": sum(values) / len(values), "reasoning": ""}
        for criterion, values in per_criterion.items()
    }

    # Hallucination flags are booleans — OR them across the duplicate pair
    # rather than averaging. Missing only when EVERY duplicate lacked data.
    counts = [c for row in judge_rows
              if isinstance(c := row.get("hallucination_count"), (int, float))
              and not isinstance(c, bool)]
    merged["hallucination_count"] = max(counts) if counts else None
    claims: list[Any] = []
    for row in judge_rows:
        if isinstance(row.get("hallucination_claims"), list):
            claims.extend(row["hallucination_claims"])
    merged["hallucination_claims"] = claims if any(
        isinstance(r.get("hallucination_claims"), list) for r in judge_rows
    ) else None

    _apply_recomputed_composites(merged)
    return merged


def _normalize_criteria_block(block: Any) -> dict[str, dict[str, Any]]:
    """Keep only criteria carrying a usable numeric score."""
    if not isinstance(block, dict):
        return {}
    return {
        criterion: {"score": value}
        for criterion, value in _criterion_scores({"criteria_scores": block}).items()
    }


def _apply_recomputed_composites(row: dict[str, Any]) -> None:
    """Recompute jury_score_* from criteria_scores, in place, when possible.

    The reporting layer never rewrites data/evaluations.jsonl — it is
    append-only — so retroactivity has to happen at read time. Rows written
    before the missing-criterion fix carry composites computed with the old
    impute-to-1 rule; recomputing here applies the current renormalization to
    historical data without touching the file.

    Rows whose criteria_scores block is empty (the sentinel/malformed parse
    paths) keep their stored composites: there is nothing to recompute from,
    and dropping them silently would be worse than reporting them as-is.
    """
    criteria = row.get("criteria_scores")
    if not isinstance(criteria, dict) or not criteria:
        return
    unweighted = evaluator.calculate_jury_score(
        criteria, weights=evaluator.UNWEIGHTED_CRITERION_WEIGHTS,
    )
    weighted = evaluator.calculate_jury_score(
        criteria, weights=evaluator.MEDHELM_CRITERION_WEIGHTS,
    )
    if unweighted is not None:
        row["jury_score_unweighted"] = unweighted
    if weighted is not None:
        row["jury_score_weighted"] = weighted


def collect_item_scores(rows: Iterable[dict[str, Any]]) -> dict[tuple[str, str], dict[str, dict[str, Any]]]:
    """Collapse rows into ``{(doi, input_source): {provider: aggregate}}``.

    Multiple judges scoring the same summary are averaged first, so every
    provider contributes one unweighted score, one weighted score, one mean
    judge-disagreement, one per-criterion mean, and one hallucination verdict
    per item. This is the shared substrate every table, figure and significance
    test is built from — computing it once keeps the pairing identical across
    the whole report, and is what stops two reports printing different means
    from the same evaluations.jsonl.

    One judge gets at most one opinion per (item, provider). If a --no-resume
    rerun appended a second row for the same judge, the two rows are AVERAGED
    into one panelist rating rather than one superseding the other — see
    ``_collapse_duplicate_judge_rows``. Without any collapsing a rerun would be
    counted as an extra panelist and inflate n_judges.
    Mirrors reliability._units_for, which applies the identical rule.
    """
    # (doi, input_source) -> provider -> judge -> [rows from that judge]
    deduped: dict[tuple[str, str], dict[str, dict[str, list[dict[str, Any]]]]] = defaultdict(
        lambda: defaultdict(lambda: defaultdict(list))
    )
    # Keep one representative strata block per item for later stratum grouping.
    strata_by_item: dict[tuple[str, str], dict[str, Any]] = {}

    for index, row in enumerate(rows):
        doi = str(row.get("doi", "")).strip()
        summarizer = str(row.get("summarizer", "")).strip()
        if not doi or not summarizer:
            continue
        item = (doi, _input_source(row))
        strata_by_item.setdefault(item, row.get("strata") or {})
        # A row with no judge identity cannot be deduped against anything, so
        # it keeps a unique key rather than silently displacing another row.
        judge = str(row.get("judge", "")).strip() or f"__unkeyed_{index}"
        deduped[item][summarizer][judge].append(row)

    result: dict[tuple[str, str], dict[str, dict[str, Any]]] = {}
    for item, providers in deduped.items():
        result[item] = {}
        for provider, by_judge in providers.items():
            judge_rows = [_collapse_duplicate_judge_rows(rs) for rs in by_judge.values()]
            unweighted = [v for r in judge_rows if (v := _unweighted(r)) is not None]
            weighted = [v for r in judge_rows if (v := _weighted(r)) is not None]
            disagreement = [v for r in judge_rows if (v := _disagreement(r)) is not None]

            # Per-criterion: average the judges that scored each criterion,
            # independently per criterion, so one judge omitting `safety` does
            # not discard its opinion on the other four.
            criterion_values: dict[str, list[float]] = defaultdict(list)
            for r in judge_rows:
                for criterion, value in _criterion_scores(r).items():
                    criterion_values[criterion].append(value)

            result[item][provider] = {
                "unweighted": _mean(unweighted),
                "weighted": _mean(weighted),
                "disagreement": _mean(disagreement),
                "n_judges": max(len(unweighted), len(weighted)),
                "criteria": {c: _mean(v) for c, v in criterion_values.items()},
                "hallucination": _collapse_flags([_hallucination_flag(r) for r in judge_rows]),
                "major_hallucination": _collapse_flags(
                    [_major_hallucination_flag(r) for r in judge_rows]
                ),
                "low_confidence": _collapse_flags([_low_confidence_flag(r) for r in judge_rows]),
                "parse_failure": _collapse_flags(
                    [r.get("parse_method") == "sentinel" for r in judge_rows]
                ),
                "strata": strata_by_item.get(item, {}),
                "doi": item[0],
                "input_source": item[1],
            }
    return result


def _providers_in_order(item_scores: dict[tuple[str, str], dict[str, dict[str, Any]]]) -> list[str]:
    """Providers present, in the canonical models_config order then any extras."""
    seen = {provider for providers in item_scores.values() for provider in providers}
    ordered = [p for p in all_providers() if p in seen]
    ordered.extend(sorted(seen - set(ordered)))
    return ordered


# ===========================================================================
# Cost index (from summaries.jsonl)
# ===========================================================================

def build_summary_cost_index(
    summaries_path: Path | None = None, *, batched: bool = False,
) -> dict[tuple[str, str, str], float]:
    """Map ``(doi, provider, input_source) -> USD cost`` to produce that summary.

    Reads token counts recorded on each successful ``models`` slot in
    ``data/summaries.jsonl`` and prices them with ``models_config.compute_cost``
    — the same single-source-of-truth pricing the live pipeline bills against.
    ``batched`` selects the 50%-off batch rates; it defaults to False
    (real-time) because a summary slot does not record which API path produced
    it (see the cost-assumption note in the report). Summaries judged from the
    summarize-all .txt folders have no token record here and are simply absent,
    which surfaces as an unavailable cost rather than a wrong one.
    """
    resolved = summaries_path if summaries_path is not None else SUMMARIES_PATH
    index: dict[tuple[str, str, str], float] = {}
    if not resolved.exists():
        return index
    with open(resolved, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            doi = str(record.get("doi", "")).strip()
            input_source = str(record.get("input_source") or "processed")
            for provider, slot in (record.get("models") or {}).items():
                if not isinstance(slot, dict) or slot.get("status") != "success":
                    continue
                in_tok = slot.get("input_tokens")
                out_tok = slot.get("output_tokens")
                if not isinstance(in_tok, (int, float)) or not isinstance(out_tok, (int, float)):
                    continue
                try:
                    cost = compute_cost(str(provider), int(in_tok), int(out_tok), batched=batched)
                except KeyError:
                    continue
                index[(doi, str(provider), input_source)] = cost
    return index


# ===========================================================================
# Table builders
# ===========================================================================

def build_provider_comparison(
    item_scores: dict[tuple[str, str], dict[str, dict[str, Any]]],
    providers: list[str],
    cost_index: dict[tuple[str, str, str], float] | None,
    *, seed: int, n_resamples: int,
) -> list[dict[str, Any]]:
    """Per-provider quality (both jury modes, with bootstrap CIs), cost, and
    reliability, plus cost-per-quality-point. One row per provider."""
    rows: list[dict[str, Any]] = []
    for offset, provider in enumerate(providers):
        unweighted: list[float] = []
        weighted: list[float] = []
        disagreements: list[float] = []
        costs: list[float] = []
        for (doi, input_source), by_provider in item_scores.items():
            agg = by_provider.get(provider)
            if agg is None:
                continue
            if agg["unweighted"] is not None:
                unweighted.append(agg["unweighted"])
            if agg["weighted"] is not None:
                weighted.append(agg["weighted"])
            if agg["disagreement"] is not None:
                disagreements.append(agg["disagreement"])
            if cost_index is not None:
                cost = cost_index.get((doi, provider, input_source))
                if cost is not None:
                    costs.append(cost)

        # Each provider's bootstrap seed is offset so their CIs are not drawn
        # from an identical resample sequence (deterministic, but independent).
        unweighted_ci = bootstrap_ci(unweighted, seed=seed + offset, n_resamples=n_resamples)
        weighted_ci = bootstrap_ci(weighted, seed=seed + 1000 + offset, n_resamples=n_resamples)
        mean_cost = _mean(costs)
        mean_unweighted = unweighted_ci["mean"]
        cost_per_quality = (
            round(mean_cost / mean_unweighted, 6)
            if mean_cost is not None and mean_unweighted not in (None, 0)
            else None
        )
        subscription_cost = stats_engine.subscription_cost_per_summary()
        subscription_cost_per_quality = (
            round(subscription_cost / mean_unweighted, 6) if mean_unweighted else None
        )
        rows.append({
            "provider": provider,
            "n_items": len(unweighted),
            "quality_unweighted": unweighted_ci,
            "quality_weighted": weighted_ci,
            "cost": {
                "available": bool(costs),
                "mean_usd": round(mean_cost, 6) if mean_cost is not None else None,
                "total_usd": round(sum(costs), 6) if costs else None,
                "n_priced": len(costs),
            },
            "cost_per_quality_point": cost_per_quality,
            # Consumer-economics companion to the real-API cost above: a flat
            # monthly subscription price ($20/mo by default — see
            # stats_engine.subscription_cost_per_summary), NOT the logged API
            # spend. Answers "is the subscription worth it to a practicing
            # vet?" rather than "what did this research run cost."
            "subscription_cost_per_quality_point": subscription_cost_per_quality,
            "mean_judge_disagreement": (
                round(_mean(disagreements), 3) if disagreements else None
            ),
        })
    # Best unweighted mean first (None last) so the table reads as a leaderboard.
    rows.sort(key=lambda r: (r["quality_unweighted"]["mean"] is None,
                             -(r["quality_unweighted"]["mean"] or 0.0)))
    return rows


def build_significance(
    item_scores: dict[tuple[str, str], dict[str, dict[str, Any]]],
    providers: list[str],
    *, score_key: str, seed: int, n_resamples: int,
) -> dict[str, Any]:
    """Friedman (omnibus) + pairwise Wilcoxon signed-rank on one score mode.

    ``score_key`` is ``"unweighted"`` or ``"weighted"``. Providers are paired
    *by item*: a paper both providers scored contributes one difference. The
    Friedman test runs over items every provider scored (complete blocks); each
    pairwise Wilcoxon runs over items that pair of providers share, which is
    usually a larger sample than the complete-block set.
    """
    # Per-provider score maps keyed by item, for pairing.
    by_provider_item: dict[str, dict[tuple[str, str], float]] = {p: {} for p in providers}
    for item, by_provider in item_scores.items():
        for provider in providers:
            agg = by_provider.get(provider)
            if agg is not None and agg.get(score_key) is not None:
                by_provider_item[provider][item] = agg[score_key]

    # Friedman over complete blocks (items scored by every provider).
    complete_blocks: list[dict[str, float]] = []
    all_items = set().union(*(set(m) for m in by_provider_item.values())) if providers else set()
    for item in all_items:
        if all(item in by_provider_item[p] for p in providers):
            complete_blocks.append({p: by_provider_item[p][item] for p in providers})
    friedman_result = friedman(complete_blocks, providers)

    # Pairwise Wilcoxon over each pair's shared items.
    pairwise: list[dict[str, Any]] = []
    for i in range(len(providers)):
        for j in range(i + 1, len(providers)):
            a, b = providers[i], providers[j]
            shared = sorted(set(by_provider_item[a]) & set(by_provider_item[b]))
            diffs = [by_provider_item[a][item] - by_provider_item[b][item] for item in shared]
            test = wilcoxon_signed_rank(diffs)
            diff_ci = bootstrap_ci(
                diffs, seed=seed + 7 * (i + 1) + j, n_resamples=n_resamples,
            ) if diffs else {"mean": None, "ci_low": None, "ci_high": None, "n": 0}
            entry = {
                "provider_a": a,
                "provider_b": b,
                "n_shared": len(shared),
                "wilcoxon": test,
                "mean_diff": diff_ci["mean"],
                "diff_ci_low": diff_ci["ci_low"],
                "diff_ci_high": diff_ci["ci_high"],
                "underpowered": len(shared) < MIN_ITEMS_FOR_SIGNIFICANCE,
                # Filled in below once the whole family's p-values are known.
                "p_adjusted": None,
            }
            pairwise.append(entry)

    # Benjamini-Hochberg correction across the family of pairwise p-values in
    # THIS score mode (unweighted and weighted are separate families, each with
    # its own Friedman gate). Only entries with an available Wilcoxon p-value
    # enter the family; unavailable tests keep p_adjusted=None.
    testable = [e for e in pairwise if e["wilcoxon"].get("available") and e["wilcoxon"].get("p_value") is not None]
    adjusted = benjamini_hochberg([e["wilcoxon"]["p_value"] for e in testable])
    for entry, q in zip(testable, adjusted):
        entry["p_adjusted"] = q

    return {
        "score_key": score_key,
        "friedman": friedman_result,
        "pairwise_wilcoxon": pairwise,
        "min_items_for_significance": MIN_ITEMS_FOR_SIGNIFICANCE,
        "multiple_comparison_method": MULTIPLE_COMPARISON_METHOD,
        "n_comparisons_corrected": len(testable),
    }


def build_stratum_crosstab(
    item_scores: dict[tuple[str, str], dict[str, dict[str, Any]]],
    providers: list[str],
    field: str,
) -> list[dict[str, Any]]:
    """Provider × one stratum value cross-tab of mean unweighted score.

    Rows are stratum values (e.g. each species), each carrying every provider's
    mean unweighted score and item count within that value — the breakdown a
    paper uses to show where a provider's advantage concentrates.
    """
    # value -> provider -> list of unweighted scores
    grouped: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for item, by_provider in item_scores.items():
        # Any provider's strata block for this item carries the same labels.
        strata_row = next(iter(by_provider.values()))["strata"]
        proxy = {"strata": strata_row}
        value = _strata_value(proxy, field)
        for provider in providers:
            agg = by_provider.get(provider)
            if agg is not None and agg["unweighted"] is not None:
                grouped[value][provider].append(agg["unweighted"])

    table: list[dict[str, Any]] = []
    for value in sorted(grouped):
        cells = {
            provider: {
                "mean_unweighted": round(_mean(scores), 3) if scores else None,
                "n_items": len(scores),
            }
            for provider, scores in grouped[value].items()
        }
        table.append({
            "value": value,
            "n_items": sum(len(s) for s in grouped[value].values()),
            "providers": cells,
        })
    return table


def build_input_source_comparison(
    item_scores: dict[tuple[str, str], dict[str, dict[str, Any]]],
    providers: list[str],
) -> list[dict[str, Any]]:
    """Provider × input_source (processed vs. PDF) mean unweighted score.

    Isolates the cleaned-text-vs-direct-PDF question the study asks separately
    from the clinical strata: rows are input sources, cells are per-provider
    means, so a reader sees whether an input channel systematically helps or
    hurts each provider.
    """
    grouped: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for (doi, input_source), by_provider in item_scores.items():
        for provider in providers:
            agg = by_provider.get(provider)
            if agg is not None and agg["unweighted"] is not None:
                grouped[input_source][provider].append(agg["unweighted"])

    table: list[dict[str, Any]] = []
    for input_source in sorted(grouped):
        cells = {
            provider: {
                "mean_unweighted": round(_mean(scores), 3) if scores else None,
                "n_items": len(scores),
            }
            for provider, scores in grouped[input_source].items()
        }
        table.append({
            "input_source": input_source,
            "n_items": sum(len(s) for s in grouped[input_source].values()),
            "providers": cells,
        })
    return table


def build_publication_report(
    rows: Iterable[dict[str, Any]],
    *,
    cost_index: dict[tuple[str, str, str], float] | None = None,
    seed: int = DEFAULT_BOOTSTRAP_SEED,
    n_resamples: int = DEFAULT_BOOTSTRAP_RESAMPLES,
    cost_batched: bool = False,
    manifest_index: dict[str, dict[str, Any]] | None = None,
    summaries_by_doi: dict[str, dict[str, str]] | None = None,
    human_review_rows: Iterable[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Assemble the full publication report dict from evaluation rows.

    The shape is always the same so callers (Markdown/CSV renderers, tests) can
    read it without special-casing an empty corpus. ``cost_index``,
    ``manifest_index``, ``summaries_by_doi``, and ``human_review_rows`` are all
    injectable for tests (each defaults to reading its real data/ file only
    when left None — the CLI's ``main()`` builds them); this keeps the pure
    builder testable against in-memory fixtures without touching disk. All
    statistics are offline and hand-rolled or scikit-learn/scipy-backed.
    """
    materialized = list(rows)
    item_scores = collect_item_scores(materialized)
    providers = _providers_in_order(item_scores)

    # Computed once and reused for both the "provider_comparison" table below
    # and the subscription-economics section, so the bootstrap resampling
    # behind quality_unweighted only runs once per report.
    provider_comparison_rows = build_provider_comparison(
        item_scores, providers, cost_index, seed=seed, n_resamples=n_resamples,
    )

    resolved_manifest_index = manifest_index if manifest_index is not None else load_manifest_index()
    resolved_summaries_by_doi = (
        summaries_by_doi if summaries_by_doi is not None else stats_engine.load_summaries_by_provider()
    )
    information_density = stats_engine.build_information_density_report(
        resolved_manifest_index, resolved_summaries_by_doi,
    )

    provider_mean_quality = {
        row["provider"]: row["quality_unweighted"]["mean"] for row in provider_comparison_rows
    }
    subscription_economics = stats_engine.build_subscription_efficiency(provider_mean_quality)

    resolved_human_review_rows = list(
        human_review_rows if human_review_rows is not None else stats_engine.iter_human_review_rows()
    )
    covariate_analysis = stats_engine.build_covariate_report(
        materialized, resolved_human_review_rows, item_scores, providers,
    )

    notes = [
        "Significance tests use scipy.stats: Wilcoxon signed-rank (method='auto', "
        "so exact for small samples) and Friedman (chi-square). Confidence "
        f"intervals are scipy.stats percentile bootstraps ({n_resamples} resamples, "
        f"seed {seed}).",
        "Providers are paired by item (a paper judged from one input channel), so "
        "significance tests are paired/blocked, not two-independent-sample tests.",
        "Multiple judges scoring the same summary are averaged before any provider "
        "comparison, so a jury run and a single-judge run are reported the same way.",
    ]
    if cost_index is not None:
        notes.append(
            "Cost is the price to GENERATE each summary (summariser tokens × "
            f"models_config pricing, {'batch' if cost_batched else 'real-time'} "
            "rates); judge cost is a separate shared overhead and is not attributed "
            "to a provider here."
        )
    notes.append(
        "Subscription-based cost-per-quality is a separate consumer-economics view "
        "(flat monthly subscription price / papers-per-month), not derived from "
        "logged API token costs — see stats_engine.py."
    )
    notes.append(
        "Information density's 'retained-or-more' cutoff (TF-IDF cosine similarity "
        f">= {stats_engine.INFORMATION_DENSITY_RETENTION_THRESHOLD}) is a named, "
        "tunable modeling choice (stats_engine.INFORMATION_DENSITY_RETENTION_THRESHOLD), "
        "not a reproduction of Appleby et al. (2023)'s exact metric."
    )
    notes.append(
        "Covariate Cohen's Kappa cells are computed on a small human-review sample "
        f"and are flagged 'underpowered' below n={stats_engine.MIN_ITEMS_FOR_KAPPA} "
        "(stats_engine.MIN_ITEMS_FOR_KAPPA); treat low-n cells as illustrative, not conclusive."
    )
    notes.append(
        "Pairwise Wilcoxon p-values carry a Benjamini-Hochberg FDR-adjusted "
        "companion (p_adjusted, scipy.stats.false_discovery_control), corrected "
        "across the family of pairwise comparisons within each score mode "
        "(unweighted/weighted are separate families). Read the adjusted value "
        "when calling a pair significant; the raw p-value is kept alongside for "
        "transparency."
    )

    return {
        "taxonomy": VET_TAXONOMY_V1.describe(VeterinarySummaryQualityScenario.name),
        "generated_at": None,  # stamped by the CLI (kept out of the pure builder)
        "n_evaluation_rows": len(materialized),
        "n_items": len(item_scores),
        "providers": providers,
        "bootstrap": {"seed": seed, "n_resamples": n_resamples, "ci_alpha": DEFAULT_CI_ALPHA},
        "cost_basis": (
            {"available": True, "batched": cost_batched} if cost_index is not None
            else {"available": False, "reason": "No cost index supplied."}
        ),
        "provider_comparison": provider_comparison_rows,
        "information_density": information_density,
        "subscription_economics": subscription_economics,
        "covariate_analysis": covariate_analysis,
        "significance_unweighted": build_significance(
            item_scores, providers, score_key="unweighted", seed=seed, n_resamples=n_resamples,
        ),
        "significance_weighted": build_significance(
            item_scores, providers, score_key="weighted", seed=seed, n_resamples=n_resamples,
        ),
        "by_stratum": {
            field: build_stratum_crosstab(item_scores, providers, field)
            for field in STRATUM_FIELDS
        },
        "input_source_comparison": build_input_source_comparison(item_scores, providers),
        "reliability": compute_reliability(materialized),
        "notes": notes,
    }


# ===========================================================================
# Markdown rendering
# ===========================================================================

def _fmt(value: Any, digits: int = 2) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def _fmt_ci(block: dict[str, Any]) -> str:
    """'4.10 [3.90, 4.30]' or '4.10 (n<2)' when no interval could be formed."""
    mean = block.get("mean")
    if mean is None:
        return "-"
    low, high = block.get("ci_low"), block.get("ci_high")
    if low is None or high is None:
        return f"{mean:.2f} (n<2)"
    return f"{mean:.2f} [{low:.2f}, {high:.2f}]"


def _fmt_pval(p: float | None) -> str:
    """Format a bare p/q-value, or '-' when it could not be computed."""
    if p is None:
        return "-"
    return "<0.001" if p < 0.001 else f"{p:.3f}"


def _fmt_p(test: dict[str, Any]) -> str:
    if not test.get("available"):
        return "-"
    return _fmt_pval(test.get("p_value"))


def render_markdown(report: dict[str, Any]) -> str:
    taxonomy = report.get("taxonomy") or {}
    benchmark = (
        f"{taxonomy.get('taxonomy_id')} v{taxonomy.get('taxonomy_version')} "
        f"({taxonomy.get('category_display_name')} / {taxonomy.get('task_display_name')})"
        if taxonomy else "an unnamed benchmark"
    )

    lines = ["# Veterinary Summarization — Publication Report", ""]
    lines.append(
        f"Benchmark: **{benchmark}**. "
        f"{report['n_evaluation_rows']} evaluation row(s) across "
        f"{report['n_items']} item(s) (paper × input channel), "
        f"{len(report['providers'])} provider(s)."
    )
    lines.append("")

    if not report["providers"]:
        lines.append("No scored evaluation rows found. Run `evaluate` first, then re-run.")
        return "\n".join(lines) + "\n"

    # --- Provider comparison ---
    lines.append("## Provider comparison")
    lines.append("")
    lines.append(
        "Quality is the mean jury score (1-5) with a 95% bootstrap CI. Cost is the "
        "mean USD to generate one summary; cost/quality is USD per unweighted point."
    )
    lines.append("")
    lines.append("| Provider | N | Unweighted (95% CI) | Weighted (95% CI) | Mean cost (USD) | Cost/quality pt | Subscription cost/quality pt | Mean judge disagreement |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for row in report["provider_comparison"]:
        cost = row["cost"]
        lines.append(
            f"| {row['provider']} | {row['n_items']} | {_fmt_ci(row['quality_unweighted'])} | "
            f"{_fmt_ci(row['quality_weighted'])} | "
            f"{_fmt(cost['mean_usd'], 5) if cost['available'] else '-'} | "
            f"{_fmt(row['cost_per_quality_point'], 5)} | "
            f"{_fmt(row.get('subscription_cost_per_quality_point'), 5)} | "
            f"{_fmt(row['mean_judge_disagreement'])} |"
        )
    lines.append("")
    lines.append(
        "*Cost/quality pt is the real API cost this study paid. Subscription "
        "cost/quality pt is a separate consumer-economics view: a flat "
        "$20/month subscription split across 500 papers/month "
        "($0.04/summary) — see \"Subscription economics\" below.*"
    )
    lines.append("")

    # --- Significance ---
    for mode_key, title in (("significance_unweighted", "unweighted"),
                            ("significance_weighted", "weighted")):
        sig = report[mode_key]
        lines.append(f"## Significance ({title} score)")
        lines.append("")
        fr = sig["friedman"]
        if fr.get("available"):
            lines.append(
                f"**Friedman** (all providers, paired by item): "
                f"chi2({fr['df']}) = {_fmt(fr['statistic'], 3)}, "
                f"p = {_fmt_p(fr)}, on {fr['n_blocks']} complete block(s). "
                f"A significant result means at least one provider differs overall."
            )
        else:
            lines.append(f"**Friedman:** {fr.get('reason', 'not available')}")
        lines.append("")
        lines.append("| Pair | Shared items | Mean score diff (A−B) | 95% CI | Wilcoxon p | p (BH-adj) | Note |")
        lines.append("|---|---|---|---|---|---|---|")
        for pair in sig["pairwise_wilcoxon"]:
            note = "underpowered (n<%d)" % sig["min_items_for_significance"] if pair["underpowered"] else ""
            ci = (f"[{_fmt(pair['diff_ci_low'])}, {_fmt(pair['diff_ci_high'])}]"
                  if pair["diff_ci_low"] is not None else "-")
            lines.append(
                f"| {pair['provider_a']} vs {pair['provider_b']} | {pair['n_shared']} | "
                f"{_fmt(pair['mean_diff'])} | {ci} | {_fmt_p(pair['wilcoxon'])} | "
                f"{_fmt_pval(pair.get('p_adjusted'))} | {note} |"
            )
        method = sig.get("multiple_comparison_method", MULTIPLE_COMPARISON_METHOD)
        n_corrected = sig.get("n_comparisons_corrected", 0)
        lines.append("")
        lines.append(
            f"*p (BH-adj): Benjamini-Hochberg FDR-adjusted across the "
            f"{n_corrected} testable pair(s) in this score mode ({method}). "
            "Prefer it over the raw Wilcoxon p when declaring a pair significant.*"
        )
        lines.append("")

    # --- Per-stratum ---
    for field in STRATUM_FIELDS:
        table = report["by_stratum"][field]
        if not table or (len(table) == 1 and table[0]["value"].strip().lower() == "unknown"):
            continue
        lines.append(f"## By {field.replace('_', ' ')}")
        lines.append("")
        header = "| " + field.replace("_", " ") + " | " + " | ".join(report["providers"]) + " |"
        lines.append(header)
        lines.append("|---" * (len(report["providers"]) + 1) + "|")
        for entry in table:
            cells = []
            for provider in report["providers"]:
                cell = entry["providers"].get(provider)
                if cell and cell["mean_unweighted"] is not None:
                    cells.append(f"{cell['mean_unweighted']:.2f} (n={cell['n_items']})")
                else:
                    cells.append("-")
            lines.append(f"| {entry['value']} | " + " | ".join(cells) + " |")
        lines.append("")

    # --- Input source comparison ---
    iso = report["input_source_comparison"]
    if iso:
        lines.append("## Processed text vs. direct PDF")
        lines.append("")
        lines.append("| Input source | " + " | ".join(report["providers"]) + " |")
        lines.append("|---" * (len(report["providers"]) + 1) + "|")
        for entry in iso:
            cells = []
            for provider in report["providers"]:
                cell = entry["providers"].get(provider)
                if cell and cell["mean_unweighted"] is not None:
                    cells.append(f"{cell['mean_unweighted']:.2f} (n={cell['n_items']})")
                else:
                    cells.append("-")
            lines.append(f"| {entry['input_source']} | " + " | ".join(cells) + " |")
        lines.append("")

    # --- Information density (vs. Appleby et al. 2023) ---
    density = report.get("information_density") or {}
    lines.append("## Information density (summary vs. abstract)")
    lines.append("")
    if not density.get("available"):
        lines.append(density.get("reason", "Not available."))
        lines.append("")
    else:
        overall = density["overall"]
        lines.append(
            f"Appleby et al. (2023) found AI summaries carried less information than "
            f"the source abstract **{density['appleby_2023_failure_rate']*100:.1f}%** of the time "
            "(word count + TF-IDF/cosine similarity of the summary against the paper's own abstract). "
            f"Across {density['n_pairs']} summary-abstract pair(s) here, "
            f"**{overall['pct_retained_or_more']*100:.1f}%** retained similar-or-more information "
            f"(TF-IDF cosine similarity >= {density['retention_cosine_threshold']})."
        )
        lines.append("")
        lines.append("| Provider | N | High fidelity % | Moderate % | Lost details % | Retained-or-more % |")
        lines.append("|---|---|---|---|---|---|")
        for provider, stats in density["by_provider"].items():
            lines.append(
                f"| {provider} | {stats['n']} | {stats['pct_high']*100:.0f}% | "
                f"{stats['pct_moderate']*100:.0f}% | {stats['pct_low']*100:.0f}% | "
                f"{stats['pct_retained_or_more']*100:.0f}% |"
            )
        lines.append("")

    # --- Subscription economics ---
    lines.append("## Subscription-based cost-per-quality")
    lines.append("")
    lines.append(
        "Consumer-economics view — what a practicing vet would actually pay "
        "($20/month flat subscription, 500 papers/month), separate from the "
        "real API cost tracked above."
    )
    lines.append("")
    lines.append("| Provider | Mean quality | Cost/summary (USD) | Efficiency (quality / cost) |")
    lines.append("|---|---|---|---|")
    for row in report.get("subscription_economics") or []:
        quality = "-" if row["mean_quality"] is None else f"{row['mean_quality']:.2f}"
        efficiency = "-" if row["efficiency"] is None else f"{row['efficiency']:.1f}"
        lines.append(
            f"| {row['provider']} | {quality} | {row['subscription_cost_per_summary_usd']:.4f} | {efficiency} |"
        )
    lines.append("")

    # --- Covariate analysis (research meat) ---
    covariate = report.get("covariate_analysis") or {}
    for field in covariate.get("fields", []):
        field_rows = covariate.get("by_field", {}).get(field, [])
        if not field_rows:
            continue
        lines.append(f"## Covariate analysis — by {field.replace('_', ' ')}")
        lines.append("")
        lines.append(
            f"Hallucination rate, mean quality, and Cohen's Kappa (LLM judge vs. human), together. "
            f"Kappa cells below n={covariate['min_items_for_kappa']} human-reviewed items are flagged "
            "underpowered — shown for transparency, not a firm conclusion."
        )
        lines.append("")
        for provider in report["providers"]:
            lines.append(f"**{provider}**")
            lines.append("")
            # Quadratic kappa leads (ordinal ratings), exact-match kappa follows
            # as the sensitivity check. Reporting only the unweighted figure
            # understated agreement by treating 4-vs-5 like 1-vs-5.
            lines.append(
                f"| {field.replace('_', ' ').title()} | N | Hallucination rate | Mean quality | "
                f"Kappa quadratic (n) | Kappa exact |"
            )
            lines.append("|---|---|---|---|---|---|")
            for cell in field_rows:
                cell_stats = cell["providers"].get(provider, {})
                halluc = cell_stats.get("hallucination_rate")
                n_missing = cell_stats.get("hallucination_n_missing", 0)
                quality = cell_stats.get("mean_quality")
                kappa_q = cell_stats.get("cohen_kappa_quadratic")
                kappa = cell_stats.get("cohen_kappa")
                kappa_n = cell_stats.get("kappa_n", 0)
                notes = " (underpowered)" if cell_stats.get("kappa_underpowered") else ""
                if cell_stats.get("kappa_undefined"):
                    notes += " (undefined)"
                halluc_cell = "-" if halluc is None else f"{halluc*100:.0f}%"
                if n_missing:
                    halluc_cell += f" ({n_missing} no data)"
                lines.append(
                    f"| {cell['value']} | {cell_stats.get('n_items', 0)} | "
                    f"{halluc_cell} | "
                    f"{'-' if quality is None else f'{quality:.2f}'} | "
                    f"{'-' if kappa_q is None else f'{kappa_q:.2f}'} (n={kappa_n}){notes} | "
                    f"{'-' if kappa is None else f'{kappa:.2f}'} |"
                )
            lines.append("")

    # --- Reliability (brief) ---
    reliability = report.get("reliability") or {}
    lines.append("## Inter-judge reliability")
    lines.append("")
    if reliability.get("available"):
        jury = reliability.get("jury_score") or {}
        lines.append(
            f"Krippendorff's alpha on the jury score: **{jury.get('krippendorff_alpha')}** "
            f"across judges {', '.join(reliability.get('judges', []))} "
            f"({reliability.get('n_comparable_items', 0)} multi-judge item(s)). "
            f"{reliability.get('interpretation', '')}"
        )
    else:
        lines.append(reliability.get("reason", "Single-judge run: no inter-judge agreement to report."))
    lines.append("")

    # --- Notes ---
    lines.append("## Notes on method")
    lines.append("")
    for note in report.get("notes", []):
        lines.append(f"- {note}")
    lines.append("")
    return "\n".join(lines) + "\n"


# ===========================================================================
# CSV rendering
# ===========================================================================

def _csv_string(header: list[str], rows: list[list[Any]]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    for row in rows:
        writer.writerow(["" if cell is None else cell for cell in row])
    return buf.getvalue()


def render_csvs(report: dict[str, Any]) -> dict[str, str]:
    """Return a ``{filename: csv_text}`` map of every machine-readable table."""
    providers = report["providers"]
    files: dict[str, str] = {}

    # Provider comparison.
    files["provider_comparison.csv"] = _csv_string(
        ["provider", "n_items", "unweighted_mean", "unweighted_ci_low", "unweighted_ci_high",
         "weighted_mean", "weighted_ci_low", "weighted_ci_high", "mean_cost_usd",
         "total_cost_usd", "cost_per_quality_point", "subscription_cost_per_quality_point",
         "mean_judge_disagreement"],
        [[
            r["provider"], r["n_items"],
            r["quality_unweighted"]["mean"], r["quality_unweighted"]["ci_low"], r["quality_unweighted"]["ci_high"],
            r["quality_weighted"]["mean"], r["quality_weighted"]["ci_low"], r["quality_weighted"]["ci_high"],
            r["cost"]["mean_usd"], r["cost"]["total_usd"], r["cost_per_quality_point"],
            r.get("subscription_cost_per_quality_point"),
            r["mean_judge_disagreement"],
        ] for r in report["provider_comparison"]],
    )

    # Information density (per (doi, provider) row — the raw data behind the
    # per-provider percentages in the Markdown report).
    density = report.get("information_density") or {}
    files["information_density.csv"] = _csv_string(
        ["doi", "provider", "abstract_words", "summary_words", "word_count_ratio",
         "word_count_label", "cosine_similarity", "band", "retained_or_more"],
        [[
            r["doi"], r["provider"], r["abstract_words"], r["summary_words"], r["ratio"],
            r["label"], r["cosine_similarity"], r["band"], r["retained_or_more"],
        ] for r in (density.get("rows") or [])],
    )

    # Subscription economics.
    files["subscription_economics.csv"] = _csv_string(
        ["provider", "mean_quality", "subscription_cost_per_summary_usd", "efficiency"],
        [[
            r["provider"], r["mean_quality"], r["subscription_cost_per_summary_usd"], r["efficiency"],
        ] for r in (report.get("subscription_economics") or [])],
    )

    # Covariate analysis (long form: field, value, provider, n, hallucination
    # rate, quality, kappa).
    covariate_rows: list[list[Any]] = []
    covariate = report.get("covariate_analysis") or {}
    for field in covariate.get("fields", []):
        for cell in covariate.get("by_field", {}).get(field, []):
            for provider in providers:
                stats = cell["providers"].get(provider, {})
                covariate_rows.append([
                    field, cell["value"], provider, stats.get("n_items", 0),
                    stats.get("hallucination_rate"), stats.get("hallucination_n_missing", 0),
                    stats.get("mean_quality"),
                    stats.get("cohen_kappa_quadratic"), stats.get("cohen_kappa"),
                    stats.get("percent_agreement"),
                    stats.get("kappa_n", 0), stats.get("kappa_underpowered", False),
                    stats.get("kappa_undefined", False),
                ])
    files["covariate_analysis.csv"] = _csv_string(
        ["field", "value", "provider", "n_items", "hallucination_rate", "hallucination_n_missing",
         "mean_quality", "cohen_kappa_quadratic", "cohen_kappa", "percent_agreement",
         "kappa_n", "kappa_underpowered", "kappa_undefined"],
        covariate_rows,
    )

    # Pairwise significance (both modes stacked).
    sig_rows: list[list[Any]] = []
    for mode_key, mode in (("significance_unweighted", "unweighted"),
                           ("significance_weighted", "weighted")):
        for pair in report[mode_key]["pairwise_wilcoxon"]:
            test = pair["wilcoxon"]
            sig_rows.append([
                mode, pair["provider_a"], pair["provider_b"], pair["n_shared"],
                test.get("statistic"), test.get("p_value"), pair.get("p_adjusted"),
                pair["mean_diff"], pair["diff_ci_low"], pair["diff_ci_high"], pair["underpowered"],
            ])
    files["significance_pairwise.csv"] = _csv_string(
        ["score_mode", "provider_a", "provider_b", "n_shared", "wilcoxon_statistic",
         "wilcoxon_p", "wilcoxon_p_bh_adjusted", "mean_diff", "diff_ci_low", "diff_ci_high",
         "underpowered"],
        sig_rows,
    )

    # Friedman omnibus (both modes).
    friedman_rows: list[list[Any]] = []
    for mode_key, mode in (("significance_unweighted", "unweighted"),
                           ("significance_weighted", "weighted")):
        fr = report[mode_key]["friedman"]
        friedman_rows.append([
            mode, fr.get("available"), fr.get("n_blocks"), fr.get("k"),
            fr.get("statistic"), fr.get("df"), fr.get("p_value"),
        ])
    files["significance_friedman.csv"] = _csv_string(
        ["score_mode", "available", "n_blocks", "k", "statistic", "df", "p_value"],
        friedman_rows,
    )

    # Per-stratum cross-tabs (long form: value, provider, mean, n).
    for field in STRATUM_FIELDS:
        rows: list[list[Any]] = []
        for entry in report["by_stratum"][field]:
            for provider in providers:
                cell = entry["providers"].get(provider)
                rows.append([
                    entry["value"], provider,
                    cell["mean_unweighted"] if cell else None,
                    cell["n_items"] if cell else 0,
                ])
        files[f"by_{field}.csv"] = _csv_string(
            [field, "provider", "mean_unweighted", "n_items"], rows,
        )

    # Input source comparison (long form).
    iso_rows: list[list[Any]] = []
    for entry in report["input_source_comparison"]:
        for provider in providers:
            cell = entry["providers"].get(provider)
            iso_rows.append([
                entry["input_source"], provider,
                cell["mean_unweighted"] if cell else None,
                cell["n_items"] if cell else 0,
            ])
    files["input_source_comparison.csv"] = _csv_string(
        ["input_source", "provider", "mean_unweighted", "n_items"], iso_rows,
    )

    return files


# ===========================================================================
# Persistence + CLI
# ===========================================================================

def save_publication_report(
    report: dict[str, Any], markdown: str, csvs: dict[str, str], ts: str,
    out_dir: Path = RESULTS_DIR,
) -> dict[str, Path]:
    """Write the JSON, Markdown, and a folder of CSVs; return their paths."""
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / f"publication_report_{ts}.json"
    json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    md_path = out_dir / f"publication_report_{ts}.md"
    md_path.write_text(markdown, encoding="utf-8")

    csv_dir = out_dir / f"publication_report_{ts}_tables"
    csv_dir.mkdir(parents=True, exist_ok=True)
    csv_paths: list[Path] = []
    for name, text in csvs.items():
        path = csv_dir / name
        path.write_text(text, encoding="utf-8")
        csv_paths.append(path)
    return {"json": json_path, "markdown": md_path, "csv_dir": csv_dir}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        print(f"[report_tables] WARNING: invalid {name}={raw!r}; using {default}.")
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Publication-grade provider comparison, significance tests, "
                    "and cost tables from data/evaluations.jsonl.",
    )
    parser.add_argument("--evaluations", type=Path, default=EVALUATIONS_PATH)
    parser.add_argument("--summaries", type=Path, default=SUMMARIES_PATH,
                        help="Source of summariser token counts for the cost columns, and "
                             "candidate summaries for information density "
                             "(default: data/summaries.jsonl). Cost is omitted for items "
                             "with no token record here.")
    parser.add_argument("--human-reviews", type=Path, default=None,
                        help="Normalized human-review rows for the covariate analysis's "
                             "Cohen's Kappa (default: data/human_reviews.jsonl; missing is fine).")
    parser.add_argument("--results-dir", type=Path, default=RESULTS_DIR,
                        help="Where to save the report (default: data/results/).")
    parser.add_argument("--seed", type=int, default=None,
                        help="Bootstrap seed (default: PUBLICATION_BOOTSTRAP_SEED or 42).")
    parser.add_argument("--bootstrap-resamples", type=int, default=None,
                        help="Bootstrap resamples per CI "
                             "(default: PUBLICATION_BOOTSTRAP_RESAMPLES or 2000).")
    parser.add_argument("--cost-batched", action="store_true",
                        help="Price summaries at batch (50%%-off) rates instead of "
                             "real-time. Overrides PUBLICATION_COST_BATCHED.")
    output_group = parser.add_mutually_exclusive_group()
    output_group.add_argument("--json", action="store_true", help="Print the full report JSON.")
    output_group.add_argument("--markdown", action="store_true", help="Print the Markdown report.")
    parser.add_argument("--no-save", action="store_true",
                        help="Print only; do not write files to --results-dir.")
    args = parser.parse_args(argv)

    seed = args.seed if args.seed is not None else _env_int("PUBLICATION_BOOTSTRAP_SEED", DEFAULT_BOOTSTRAP_SEED)
    n_resamples = (
        args.bootstrap_resamples if args.bootstrap_resamples is not None
        else _env_int("PUBLICATION_BOOTSTRAP_RESAMPLES", DEFAULT_BOOTSTRAP_RESAMPLES)
    )
    cost_batched = args.cost_batched or _env_bool("PUBLICATION_COST_BATCHED", False)

    rows = list(iter_evaluation_rows(args.evaluations))
    cost_index = build_summary_cost_index(args.summaries, batched=cost_batched)
    summaries_by_doi = stats_engine.load_summaries_by_provider(args.summaries)
    human_review_rows = list(stats_engine.iter_human_review_rows(args.human_reviews))
    report = build_publication_report(
        rows, cost_index=cost_index, seed=seed, n_resamples=n_resamples,
        cost_batched=cost_batched, summaries_by_doi=summaries_by_doi,
        human_review_rows=human_review_rows,
    )
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report["generated_at"] = ts
    markdown = render_markdown(report)
    csvs = render_csvs(report)

    should_save = not (args.no_save or not rows)
    saved: dict[str, Path] = {}
    if should_save:
        saved = save_publication_report(report, markdown, csvs, ts, args.results_dir)

    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    elif args.markdown:
        print(markdown)
    else:
        print("=" * 72)
        print("VETERINARY SUMMARIZATION — PUBLICATION REPORT")
        print("=" * 72)
        print(f"Evaluations: {args.evaluations}")
        print(f"Rows: {report['n_evaluation_rows']}  Items: {report['n_items']}  "
              f"Providers: {', '.join(report['providers']) or 'none'}")
        if not rows:
            print("\nNo evaluation rows found yet. Run 'evaluate' first, then re-run.")
            return 0
        print("\nProvider comparison (unweighted mean [95% CI]):")
        for r in report["provider_comparison"]:
            print(f"  {r['provider']:<12} {_fmt_ci(r['quality_unweighted']):<24} "
                  f"cost/pt={_fmt(r['cost_per_quality_point'], 5)}")
        fr = report["significance_unweighted"]["friedman"]
        if fr.get("available"):
            print(f"\nFriedman (unweighted): chi2({fr['df']})={fr['statistic']} p={_fmt_p(fr)}")

    for label, path in saved.items():
        print(f"Saved {label}: {path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
