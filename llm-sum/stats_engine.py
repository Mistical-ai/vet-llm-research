"""
llm-sum/stats_engine.py — Information density, Cohen's Kappa, subscription
economics, and provider x covariate "meat" tables
============================================================================

WHY THIS MODULE EXISTS
-----------------------
The rest of the Phase 3-6 stats layer (``reliability.py``, ``eval_report.py``,
``report_tables.py``, ``human_review.py``) already answers "which provider is
best," "do the judges agree," and "does the LLM jury track a human expert."
This module adds four narrower, specific lenses that OVC Pet Trust's study
design asks for and that nothing else in the codebase computes:

1. **Information density** — does an LLM summary carry as much information as
   the paper's own abstract? This directly re-runs Appleby et al. (2023)'s
   finding (AI summaries carried less information than the source abstract
   79.7% of the time) against this study's three providers, using word count
   plus TF-IDF/cosine similarity as the two measuring sticks.
2. **Cohen's Kappa + percent agreement** — a chance-corrected, *categorical*
   inter-rater statistic between the LLM judge and a human reviewer. This is
   deliberately different from ``reliability.py``'s Krippendorff's alpha
   (interval-scale, handles missing raters) and Pearson/Spearman (rank/linear
   correlation on continuous scores): Cohen's Kappa asks "how often do the two
   raters land on the *exact same category*, correcting for chance," which is
   the metric this study's human-validation design specifically asks for.
3. **Subscription-based cost-per-quality** — a consumer-economics answer
   ("is a $20/month subscription worth it to a practicing vet?") that is
   deliberately kept separate from ``report_tables.py``'s real per-token API
   cost-per-quality-point (a research-budget question). Both are useful; they
   are not the same question and neither replaces the other.
4. **Covariate "research meat"** — one table per (provider x species /
   study_design / journal) cell carrying hallucination rate, quality, AND
   Cohen's Kappa together. Today hallucination-by-stratum
   (``eval_report.summarize_rows``) and quality-by-stratum
   (``report_tables.build_stratum_crosstab``) live in different modules and
   are never joined with kappa; this is the join.

DEPENDENCY CHOICE (validated library, not hand-rolled)
-------------------------------------------------------
``reliability.py``'s own rule is: prefer a validated, peer-reviewed
implementation whenever one exists, and hand-roll only when it doesn't (that
is explicitly why Krippendorff's alpha there is hand-rolled — scipy has no
implementation — while Pearson/Spearman/bootstrap CIs all go through scipy).
Cohen's Kappa and TF-IDF/cosine similarity both have standard, validated
implementations, so per that same rule this module uses
``sklearn.metrics.cohen_kappa_score`` and
``sklearn.feature_extraction.text.TfidfVectorizer`` /
``sklearn.metrics.pairwise.cosine_similarity`` rather than reimplementing
either formula. scikit-learn rides on the numpy/scipy stack already pinned in
requirements.txt.

DESIGN CONSTRAINTS (from CLAUDE.md)
-------------------------------------
- Offline only. Never calls a paid API; consumes rows ``evaluator.py`` and
  ``human_review.py`` already wrote, plus manifest metadata.
- Lives in ``llm-sum/`` (not ``src/``) per CLAUDE.md's Phase 3 Additions rule:
  all Phase 3+ code lives here; ``src/`` is Phase 2 (corpus collection).
"""

from __future__ import annotations

from _bootstrap import *  # noqa: F401,F403

import argparse
import json
import os
import re
import sys
import warnings
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics import cohen_kappa_score
from sklearn.metrics.pairwise import cosine_similarity as _sk_cosine_similarity

from eval_instances import load_manifest_index
from eval_report import iter_evaluation_rows

EVALUATIONS_PATH = DATA_DIR / "evaluations.jsonl"
SUMMARIES_PATH = DATA_DIR / "summaries.jsonl"
HUMAN_REVIEWS_PATH = DATA_DIR / "human_reviews.jsonl"
RESULTS_DIR = DATA_DIR / "results"

# The five MedHELM criteria, mirrored from reliability.CRITERIA/evaluator's
# weights, kept local so this module has no import-time coupling to either.
CRITERIA = ("faithfulness", "completeness", "clinical_usefulness", "clarity", "safety")

# Stratum fields the covariate report breaks out, per the user's request
# (species, study_design, journal). clinical_topic is deliberately excluded
# here (it already has its own crosstab in report_tables.py) to keep this
# table focused on the three covariates OVC Pet Trust asked for by name.
COVARIATE_FIELDS = ("species", "study_design", "journal")

# Below this many human-reviewed items in a (provider, stratum-value) cell, a
# Cohen's Kappa figure is statistically unstable (one item swings it wildly) —
# flagged "underpowered" rather than hidden, the same transparency convention
# as reliability.MIN_CORRELATION_N and report_tables.MIN_ITEMS_FOR_SIGNIFICANCE.
MIN_ITEMS_FOR_KAPPA = 5

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z'-]*")


# ===========================================================================
# 1. Information density: word count + TF-IDF cosine similarity vs. abstract
# ===========================================================================
# A cosine similarity below this cutoff is treated as "lost key medical
# details" (per the user's own threshold); at/above 0.85 is "kept almost all
# technical meaning." The band in between is "moderate." The 0.50 cutoff is
# also the line used for the headline "similar-or-more information" percentage
# compared against Appleby et al.'s 60%/80% benchmarks below — Appleby's exact
# metric isn't reproducible from the numbers we have, so this is an explicit,
# named, tunable modeling choice, the same pattern as
# reliability.MIN_CORRELATION_N / report_tables.MIN_ITEMS_FOR_SIGNIFICANCE.
INFORMATION_DENSITY_HIGH_THRESHOLD = 0.85
INFORMATION_DENSITY_RETENTION_THRESHOLD = 0.50

# Benchmarks from Appleby et al. (2023): AI summaries carried less information
# than the source abstract 79.7% of the time. "Meaningful progress" and
# "problem unsolved" verdicts are reported against these two lines.
APPLEBY_2023_FAILURE_RATE = 0.797
PROGRESS_THRESHOLD = 0.60
UNSOLVED_THRESHOLD = 0.20  # i.e. still failing (<0.20 retained) >=80% of the time


def word_count_comparison(abstract: str, summary: str) -> dict[str, Any]:
    """Word counts for both texts, their ratio, and a shorter/same/longer label.

    "Same" allows a +/-2% band around 1.0 so near-identical lengths (a wash
    after rounding) aren't arbitrarily called "longer" by one extra word.
    """
    abstract_words = len(_WORD_RE.findall(abstract or ""))
    summary_words = len(_WORD_RE.findall(summary or ""))
    ratio = round(summary_words / abstract_words, 3) if abstract_words else None

    if ratio is None:
        label = "unknown"
    elif abs(ratio - 1.0) <= 0.02:
        label = "same"
    elif ratio < 1.0:
        label = "shorter"
    else:
        label = "longer"

    return {
        "abstract_words": abstract_words,
        "summary_words": summary_words,
        "ratio": ratio,
        "label": label,
    }


def fit_tfidf_corpus(documents: list[str]) -> TfidfVectorizer | None:
    """Fit one TF-IDF vectorizer over the WHOLE corpus (all abstracts + all
    candidate summaries in the run), not per-pair.

    A 2-document IDF is degenerate: any term shared by both documents in a
    single pair gets document-frequency 2/2 and collapses toward IDF=0,
    which washes out exactly the "meaningful veterinary term" signal this
    metric exists to capture. Fitting over the full corpus gives real
    document-frequency statistics. Returns None when there is no text to fit
    (empty corpus), so callers can degrade to "unavailable" rather than crash.
    """
    non_empty = [doc for doc in documents if doc and doc.strip()]
    if not non_empty:
        return None
    vectorizer = TfidfVectorizer(lowercase=True, stop_words="english", min_df=1)
    vectorizer.fit(non_empty)
    return vectorizer


def cosine_similarity_pair(vectorizer: TfidfVectorizer | None, abstract: str, summary: str) -> float | None:
    """TF-IDF cosine similarity between one abstract and one summary.

    Uses the already-fitted corpus vectorizer (see fit_tfidf_corpus) so IDF
    weights reflect the whole study, not just this one pair. Returns None
    when either text is empty/whitespace-only or no vectorizer is available
    (cosine similarity is undefined for an empty document).
    """
    if vectorizer is None or not (abstract or "").strip() or not (summary or "").strip():
        return None
    matrix = vectorizer.transform([abstract, summary])
    if matrix.nnz == 0:
        return None
    similarity = _sk_cosine_similarity(matrix[0], matrix[1])[0, 0]
    return round(float(similarity), 4)


def _density_band(cosine: float | None) -> str:
    if cosine is None:
        return "unknown"
    if cosine >= INFORMATION_DENSITY_HIGH_THRESHOLD:
        return "high"
    if cosine < INFORMATION_DENSITY_RETENTION_THRESHOLD:
        return "low"
    return "moderate"


def information_density_row(vectorizer: TfidfVectorizer | None, abstract: str, summary: str) -> dict[str, Any]:
    """One summary's information-density row: word count + TF-IDF cosine verdict."""
    counts = word_count_comparison(abstract, summary)
    cosine = cosine_similarity_pair(vectorizer, abstract, summary)
    band = _density_band(cosine)
    return {
        **counts,
        "cosine_similarity": cosine,
        "band": band,
        # The headline retention criterion this whole feature reports against:
        # "similar or more information" = not in the "lost key details" band.
        "retained_or_more": band in ("moderate", "high") if band != "unknown" else None,
    }


def build_information_density_report(
    manifest_index: dict[str, dict[str, Any]],
    summaries_by_doi: dict[str, dict[str, str]],
) -> dict[str, Any]:
    """Full information-density report across every (doi, provider) pair.

    ``summaries_by_doi`` is ``{doi: {provider: candidate_summary}}`` (see
    ``_load_summaries_by_provider`` below). Fits one TF-IDF corpus over every
    abstract and every candidate summary in the run, then scores each pair
    and aggregates per provider.
    """
    corpus: list[str] = []
    pairs: list[tuple[str, str, str, str]] = []  # (doi, provider, abstract, summary)
    for doi, providers in summaries_by_doi.items():
        abstract = str((manifest_index.get(doi) or {}).get("abstract") or "").strip()
        if not abstract:
            continue
        corpus.append(abstract)
        for provider, summary in providers.items():
            if not summary or not summary.strip():
                continue
            corpus.append(summary)
            pairs.append((doi, provider, abstract, summary))

    vectorizer = fit_tfidf_corpus(corpus)
    if not pairs or vectorizer is None:
        return {
            "available": False,
            "reason": (
                "No (abstract, summary) pairs found. Needs data/manifest.jsonl "
                "abstracts and data/summaries.jsonl candidate summaries for the "
                "same DOIs."
            ),
            "by_provider": {},
        }

    by_provider: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for doi, provider, abstract, summary in pairs:
        row = information_density_row(vectorizer, abstract, summary)
        row["doi"] = doi
        row["provider"] = provider
        by_provider[provider].append(row)

    def _pct(rows: list[dict[str, Any]], predicate) -> float:
        scored = [r for r in rows if r["band"] != "unknown"]
        return round(sum(1 for r in scored if predicate(r)) / len(scored), 3) if scored else 0.0

    providers_summary: dict[str, Any] = {}
    for provider, rows in sorted(by_provider.items()):
        retained_pct = _pct(rows, lambda r: r["retained_or_more"])
        providers_summary[provider] = {
            "n": len(rows),
            "pct_high": _pct(rows, lambda r: r["band"] == "high"),
            "pct_moderate": _pct(rows, lambda r: r["band"] == "moderate"),
            "pct_low": _pct(rows, lambda r: r["band"] == "low"),
            "pct_retained_or_more": retained_pct,
            "pct_shorter": round(sum(1 for r in rows if r["label"] == "shorter") / len(rows), 3) if rows else 0.0,
            "pct_same": round(sum(1 for r in rows if r["label"] == "same") / len(rows), 3) if rows else 0.0,
            "pct_longer": round(sum(1 for r in rows if r["label"] == "longer") / len(rows), 3) if rows else 0.0,
            "meets_progress_threshold": retained_pct >= PROGRESS_THRESHOLD,
            "still_matches_2023_finding": retained_pct <= UNSOLVED_THRESHOLD,
        }

    all_rows = [row for rows in by_provider.values() for row in rows]
    overall_retained_pct = _pct(all_rows, lambda r: r["retained_or_more"])

    return {
        "available": True,
        "n_pairs": len(pairs),
        "appleby_2023_failure_rate": APPLEBY_2023_FAILURE_RATE,
        "progress_threshold": PROGRESS_THRESHOLD,
        "unsolved_threshold": UNSOLVED_THRESHOLD,
        "retention_cosine_threshold": INFORMATION_DENSITY_RETENTION_THRESHOLD,
        "high_fidelity_cosine_threshold": INFORMATION_DENSITY_HIGH_THRESHOLD,
        "overall": {
            "pct_retained_or_more": overall_retained_pct,
            "meets_progress_threshold": overall_retained_pct >= PROGRESS_THRESHOLD,
            "still_matches_2023_finding": overall_retained_pct <= UNSOLVED_THRESHOLD,
        },
        "by_provider": providers_summary,
        "rows": all_rows,
    }


# ===========================================================================
# 2. Cohen's Kappa + percent agreement (categorical IRR)
# ===========================================================================

def cohen_kappa(ratings_a: list[int], ratings_b: list[int], weights: str | None = None) -> float | None:
    """Cohen's Kappa between two raters' paired categorical ratings.

    Thin wrapper over sklearn.metrics.cohen_kappa_score. ``weights=None`` is
    plain (unweighted) Cohen's Kappa — the literal statistic requested.
    ``weights="linear"``/``"quadratic"`` are also accepted: quadratic weighting
    is the statistically preferable variant for ordinal 1-5 rubric data (a
    miss by 4 points counts more than a miss by 1), reported as an optional
    companion the same way report_tables.py reports both weighted and
    unweighted jury scores rather than picking one.

    Returns None (rather than raising) when there are fewer than two paired
    ratings, or when sklearn's own agreement-by-definition edge case (every
    rating identical on both sides, so chance agreement p_e=1) makes the
    formula divide by zero — sklearn returns nan in that case, which is
    converted to 1.0 (perfect agreement), matching Krippendorff's alpha's own
    "all ratings identical -> 1.0" convention in reliability.py.
    """
    n = len(ratings_a)
    if n < 2 or len(ratings_b) != n:
        return None
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        value = cohen_kappa_score(ratings_a, ratings_b, weights=weights)
    if isinstance(value, float) and value != value:  # NaN check
        return 1.0
    return round(float(value), 3)


def percent_agreement(ratings_a: list[int], ratings_b: list[int]) -> float | None:
    """Share of paired ratings that are an EXACT match. None if empty/mismatched."""
    n = len(ratings_a)
    if n == 0 or len(ratings_b) != n:
        return None
    matches = sum(1 for a, b in zip(ratings_a, ratings_b) if a == b)
    return round(matches / n, 3)


def _round_to_category(value: float) -> int:
    """Round a continuous 1-5 composite to the nearest integer category.

    Cohen's Kappa needs discrete categories; Pearson/Spearman (reliability.py)
    do not need this step because they work on continuous scores directly.
    Clamped to [1, 5] so a rounding artifact at the boundary (e.g. 5.4) can't
    produce an out-of-range category.
    """
    return max(1, min(5, round(value)))


def categorical_agreement(
    human_values: list[float], jury_values: list[float], *, weights: str | None = None,
) -> dict[str, Any]:
    """Cohen's Kappa + percent agreement for one paired (human, jury) series.

    Values are rounded to the nearest 1-5 category first (see
    _round_to_category). Returns n alongside both statistics so a reader can
    judge how much a given kappa/agreement figure should be trusted — the
    same transparency convention reliability.py and report_tables.py already
    use for their own small-sample caveats.
    """
    n = len(human_values)
    if n < 2 or len(jury_values) != n:
        return {"n": n, "cohen_kappa": None, "cohen_kappa_quadratic": None, "percent_agreement": None}
    human_cats = [_round_to_category(v) for v in human_values]
    jury_cats = [_round_to_category(v) for v in jury_values]
    return {
        "n": n,
        "cohen_kappa": cohen_kappa(human_cats, jury_cats, weights=weights),
        "cohen_kappa_quadratic": cohen_kappa(human_cats, jury_cats, weights="quadratic"),
        "percent_agreement": percent_agreement(human_cats, jury_cats),
    }


# ===========================================================================
# 3. Subscription-based cost-per-quality
# ===========================================================================
# Deliberately NOT models_config.py pricing: this is a consumer-economics
# question ("is a $20/month subscription worth it to a practicing vet?"), not
# the research budget's real per-token API cost, which report_tables.py
# already tracks separately via BudgetGuard-logged token counts. Both numbers
# are kept side by side, never merged, so a reader can tell which question
# each column answers.

DEFAULT_SUBSCRIPTION_PRICE_USD = 20.0
DEFAULT_PAPERS_PER_MONTH = 500  # 20 papers/day x 25 days


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        print(f"[stats_engine] WARNING: invalid {name}={raw!r}; using {default}.")
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        print(f"[stats_engine] WARNING: invalid {name}={raw!r}; using {default}.")
        return default


def subscription_cost_per_summary(
    *, subscription_price_usd: float | None = None, papers_per_month: int | None = None,
) -> float:
    """USD cost to produce ONE summary under a flat monthly subscription.

    Defaults: $20/month, 500 papers/month (20 papers/day x 25 days) -> $0.04
    per summary, identical across all three providers since a subscription
    price does not vary by token usage. Overridable via
    SUBSCRIPTION_COST_PER_MONTH_USD / SUBSCRIPTION_PAPERS_PER_MONTH in .env.
    """
    price = (
        subscription_price_usd if subscription_price_usd is not None
        else _env_float("SUBSCRIPTION_COST_PER_MONTH_USD", DEFAULT_SUBSCRIPTION_PRICE_USD)
    )
    papers = (
        papers_per_month if papers_per_month is not None
        else _env_int("SUBSCRIPTION_PAPERS_PER_MONTH", DEFAULT_PAPERS_PER_MONTH)
    )
    if papers <= 0:
        raise ValueError(f"papers_per_month must be > 0, got {papers}")
    return round(price / papers, 6)


def build_subscription_efficiency(
    provider_mean_quality: dict[str, float | None],
    *, subscription_price_usd: float | None = None, papers_per_month: int | None = None,
) -> list[dict[str, Any]]:
    """Per-provider subscription cost-per-summary and quality/cost efficiency.

    ``provider_mean_quality`` is ``{provider: mean_unweighted_quality}`` —
    callers pass in report_tables.py's already-computed per-provider means
    (via collect_item_scores) rather than re-deriving that join here.
    ``efficiency = mean_quality / subscription_cost_per_summary``: higher is
    better value for a vet paying the subscription price rather than using a
    free alternative.
    """
    cost = subscription_cost_per_summary(
        subscription_price_usd=subscription_price_usd, papers_per_month=papers_per_month,
    )
    rows: list[dict[str, Any]] = []
    for provider, quality in sorted(provider_mean_quality.items()):
        efficiency = round(quality / cost, 3) if isinstance(quality, (int, float)) and cost else None
        rows.append({
            "provider": provider,
            "mean_quality": quality,
            "subscription_cost_per_summary_usd": cost,
            "efficiency": efficiency,
        })
    rows.sort(key=lambda r: (r["efficiency"] is None, -(r["efficiency"] or 0.0)))
    return rows


# ===========================================================================
# 4. Covariate "research meat": provider x (species/study_design/journal)
# ===========================================================================

def _strata_value(row: dict[str, Any], field: str) -> str:
    if row.get(field) not in (None, "", []):
        value = row[field]
    else:
        value = (row.get("strata") or {}).get(field, "unknown")
    if isinstance(value, list):
        return ", ".join(str(v) for v in value) if value else "unknown"
    return str(value or "unknown")


def _has_hallucination(row: dict[str, Any]) -> bool:
    return int(row.get("hallucination_count") or 0) > 0


def build_covariate_report(
    evaluation_rows: Iterable[dict[str, Any]],
    human_review_rows: Iterable[dict[str, Any]],
    item_scores: dict[tuple[str, str], dict[str, dict[str, Any]]],
    providers: list[str],
    *, fields: tuple[str, ...] = COVARIATE_FIELDS, min_items_for_kappa: int = MIN_ITEMS_FOR_KAPPA,
) -> dict[str, Any]:
    """One (provider x stratum-value) cell per field: hallucination rate,
    mean quality, and Cohen's Kappa (LLM judge vs. human), together.

    ``item_scores`` is report_tables.collect_item_scores()'s output (reused,
    not re-derived) for quality. Hallucination rate is computed directly from
    the evaluation rows (broken out by provider AND stratum together, which
    neither eval_report.py nor report_tables.py currently does). Cohen's Kappa
    comes from data/human_reviews.jsonl, filtered to each provider+stratum
    cell; because the human-review sample is small, every cell below
    ``min_items_for_kappa`` is flagged "underpowered" rather than hidden.
    """
    materialized_eval = [r for r in evaluation_rows if isinstance(r, dict)]
    materialized_human = [r for r in human_review_rows if isinstance(r, dict)]

    result: dict[str, Any] = {"fields": list(fields), "min_items_for_kappa": min_items_for_kappa, "by_field": {}}

    for field in fields:
        # Hallucination rate + n, per (provider, stratum value).
        halluc_buckets: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for row in materialized_eval:
            provider = str(row.get("summarizer", "")).strip()
            if not provider:
                continue
            value = _strata_value(row, field)
            halluc_buckets[(provider, value)].append(row)

        # Quality, per (provider, stratum value), from item_scores.
        quality_buckets: dict[tuple[str, str], list[float]] = defaultdict(list)
        for (_doi, _input_source), by_provider in item_scores.items():
            for provider, agg in by_provider.items():
                value = _strata_value({"strata": agg.get("strata") or {}}, field)
                if agg.get("unweighted") is not None:
                    quality_buckets[(provider, value)].append(agg["unweighted"])

        # Cohen's Kappa, per (provider, stratum value), from human_reviews.jsonl.
        kappa_human: dict[tuple[str, str], list[float]] = defaultdict(list)
        kappa_jury: dict[tuple[str, str], list[float]] = defaultdict(list)
        for row in materialized_human:
            provider = str(row.get("summarizer", "")).strip()
            human_score = row.get("human_score_unweighted")
            jury_score = row.get("llm_jury_score_unweighted")
            if jury_score is None:
                jury_score = row.get("llm_jury_score")
            if not provider or human_score is None or not isinstance(jury_score, (int, float)):
                continue
            value = _strata_value(row, field)
            kappa_human[(provider, value)].append(float(human_score))
            kappa_jury[(provider, value)].append(float(jury_score))

        values = sorted({v for (_p, v) in halluc_buckets} | {v for (_p, v) in quality_buckets})
        field_rows: list[dict[str, Any]] = []
        for value in values:
            cell: dict[str, Any] = {"value": value, "providers": {}}
            for provider in providers:
                key = (provider, value)
                halluc_rows = halluc_buckets.get(key, [])
                quality_scores = quality_buckets.get(key, [])
                agreement = categorical_agreement(kappa_human.get(key, []), kappa_jury.get(key, []))
                cell["providers"][provider] = {
                    "n_items": len(halluc_rows),
                    "hallucination_rate": (
                        round(sum(1 for r in halluc_rows if _has_hallucination(r)) / len(halluc_rows), 3)
                        if halluc_rows else None
                    ),
                    "mean_quality": (
                        round(sum(quality_scores) / len(quality_scores), 3) if quality_scores else None
                    ),
                    "cohen_kappa": agreement["cohen_kappa"],
                    "kappa_n": agreement["n"],
                    "kappa_underpowered": agreement["n"] < min_items_for_kappa,
                }
            field_rows.append(cell)
        result["by_field"][field] = field_rows

    return result


# ===========================================================================
# Data loading helpers (shared across the four sections above)
# ===========================================================================

def _iter_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    if not path.exists():
        return
    with open(path, encoding="utf-8") as f:
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


def load_summaries_by_provider(summaries_path: Path | None = None) -> dict[str, dict[str, str]]:
    """``{doi: {provider: candidate_summary}}`` from data/summaries.jsonl.

    Mirrors the successful-slot shape src/evaluation/rubric_scoring.py already
    reads (models.<provider>.summary with status success), so information
    density scores exactly the summaries the study actually judged.
    """
    resolved = summaries_path if summaries_path is not None else SUMMARIES_PATH
    result: dict[str, dict[str, str]] = defaultdict(dict)
    for record in _iter_jsonl(resolved):
        doi = str(record.get("doi", "")).strip()
        if not doi:
            continue
        for provider, slot in (record.get("models") or {}).items():
            if not isinstance(slot, dict) or slot.get("status") not in (None, "success"):
                continue
            summary = str(slot.get("summary") or "").strip()
            if summary:
                result[doi][str(provider)] = summary
    return result


def iter_human_review_rows(path: Path | None = None) -> Iterable[dict[str, Any]]:
    resolved = path if path is not None else HUMAN_REVIEWS_PATH
    return _iter_jsonl(resolved)


# ===========================================================================
# Full report + persistence + CLI
# ===========================================================================

def build_stats_engine_report(
    *,
    evaluations_path: Path | None = None,
    summaries_path: Path | None = None,
    human_reviews_path: Path | None = None,
    subscription_price_usd: float | None = None,
    papers_per_month: int | None = None,
) -> dict[str, Any]:
    """Assemble the full stats_engine report: all four sections, one dict.

    Lazily imports report_tables to reuse its item/provider join
    (collect_item_scores, _providers_in_order) instead of re-deriving it —
    avoids a circular import since report_tables.py also imports this module.
    """
    from report_tables import collect_item_scores, _providers_in_order  # noqa: E402 (lazy: avoid circular import)

    evaluation_rows = list(iter_evaluation_rows(evaluations_path))
    human_review_rows = list(iter_human_review_rows(human_reviews_path))
    item_scores = collect_item_scores(evaluation_rows)
    providers = _providers_in_order(item_scores)

    manifest_index = load_manifest_index()
    summaries_by_doi = load_summaries_by_provider(summaries_path)
    information_density = build_information_density_report(manifest_index, summaries_by_doi)

    provider_mean_quality: dict[str, float | None] = {}
    for provider in providers:
        values = [
            agg["unweighted"]
            for by_provider in item_scores.values()
            for p, agg in by_provider.items()
            if p == provider and agg.get("unweighted") is not None
        ]
        provider_mean_quality[provider] = round(sum(values) / len(values), 3) if values else None
    subscription_economics = build_subscription_efficiency(
        provider_mean_quality, subscription_price_usd=subscription_price_usd, papers_per_month=papers_per_month,
    )

    covariate_analysis = build_covariate_report(evaluation_rows, human_review_rows, item_scores, providers)

    return {
        "generated_at": None,  # stamped by the CLI
        "providers": providers,
        "information_density": information_density,
        "subscription_economics": subscription_economics,
        "covariate_analysis": covariate_analysis,
    }


def render_markdown(report: dict[str, Any]) -> str:
    lines = ["# Statistics Engine — Information Density, IRR, Economics, Covariates", ""]

    density = report.get("information_density") or {}
    lines.append("## Information density (vs. Appleby et al. 2023)")
    lines.append("")
    if not density.get("available"):
        lines.append(density.get("reason", "Not available."))
        lines.append("")
    else:
        overall = density["overall"]
        lines.append(
            f"Appleby et al. (2023) found AI summaries carried less information than "
            f"the source abstract **{density['appleby_2023_failure_rate']*100:.1f}%** of the time. "
            f"Across {density['n_pairs']} summary-abstract pair(s) in this study, "
            f"**{overall['pct_retained_or_more']*100:.1f}%** retained similar-or-more information "
            f"(TF-IDF cosine similarity >= {density['retention_cosine_threshold']})."
        )
        lines.append("")
        verdict = (
            "Meaningful progress since 2023." if overall["meets_progress_threshold"]
            else "Still matches the 2023 finding — the problem is not solved."
            if overall["still_matches_2023_finding"] else "Mixed result — neither threshold met."
        )
        lines.append(f"> {verdict}")
        lines.append("")
        lines.append("| Provider | N | High fidelity % | Moderate % | Lost details % | Retained-or-more % | Shorter % | Same % | Longer % |")
        lines.append("|---|---|---|---|---|---|---|---|---|")
        for provider, stats in density["by_provider"].items():
            lines.append(
                f"| {provider} | {stats['n']} | {stats['pct_high']*100:.0f}% | {stats['pct_moderate']*100:.0f}% | "
                f"{stats['pct_low']*100:.0f}% | {stats['pct_retained_or_more']*100:.0f}% | "
                f"{stats['pct_shorter']*100:.0f}% | {stats['pct_same']*100:.0f}% | {stats['pct_longer']*100:.0f}% |"
            )
        lines.append("")

    lines.append("## Subscription-based cost-per-quality")
    lines.append("")
    lines.append(
        "Consumer-economics view: assumes a flat monthly subscription "
        "($20/month, 500 papers/month), NOT the research budget's real "
        "per-token API cost (see report_tables.py's provider comparison for that)."
    )
    lines.append("")
    lines.append("| Provider | Mean quality | Cost/summary (USD) | Efficiency (quality / cost) |")
    lines.append("|---|---|---|---|")
    for row in report.get("subscription_economics") or []:
        quality = "-" if row["mean_quality"] is None else f"{row['mean_quality']:.2f}"
        efficiency = "-" if row["efficiency"] is None else f"{row['efficiency']:.1f}"
        lines.append(f"| {row['provider']} | {quality} | {row['subscription_cost_per_summary_usd']:.4f} | {efficiency} |")
    lines.append("")

    covariate = report.get("covariate_analysis") or {}
    for field in covariate.get("fields", []):
        rows = covariate["by_field"].get(field, [])
        if not rows:
            continue
        lines.append(f"## Covariate analysis — by {field.replace('_', ' ')}")
        lines.append("")
        lines.append(
            f"Per-cell Kappa is flagged underpowered below n={covariate['min_items_for_kappa']} "
            "human-reviewed items — shown for transparency, not treated as a firm conclusion."
        )
        lines.append("")
        for provider in report.get("providers", []):
            lines.append(f"**{provider}**")
            lines.append("")
            lines.append(f"| {field.replace('_', ' ').title()} | N | Hallucination rate | Mean quality | Cohen's Kappa (n) |")
            lines.append("|---|---|---|---|---|")
            for cell in rows:
                stats = cell["providers"].get(provider, {})
                n_items = stats.get("n_items", 0)
                halluc = stats.get("hallucination_rate")
                quality = stats.get("mean_quality")
                kappa = stats.get("cohen_kappa")
                kappa_n = stats.get("kappa_n", 0)
                underpowered = " (underpowered)" if stats.get("kappa_underpowered") else ""
                lines.append(
                    f"| {cell['value']} | {n_items} | "
                    f"{'-' if halluc is None else f'{halluc*100:.0f}%'} | "
                    f"{'-' if quality is None else f'{quality:.2f}'} | "
                    f"{'-' if kappa is None else f'{kappa:.2f}'} (n={kappa_n}){underpowered} |"
                )
            lines.append("")

    return "\n".join(lines) + "\n"


def save_stats_engine_report(report: dict[str, Any], markdown: str, ts: str, out_dir: Path = RESULTS_DIR) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / f"stats_engine_report_{ts}.json"
    json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    md_path = out_dir / f"stats_engine_report_{ts}.md"
    md_path.write_text(markdown, encoding="utf-8")
    return {"json": json_path, "markdown": md_path}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Information density, Cohen's Kappa IRR, subscription economics, "
                    "and covariate 'research meat' tables from data/evaluations.jsonl, "
                    "data/summaries.jsonl, and data/human_reviews.jsonl.",
    )
    parser.add_argument("--evaluations", type=Path, default=EVALUATIONS_PATH)
    parser.add_argument("--summaries", type=Path, default=SUMMARIES_PATH)
    parser.add_argument("--human-reviews", type=Path, default=HUMAN_REVIEWS_PATH)
    parser.add_argument("--results-dir", type=Path, default=RESULTS_DIR)
    parser.add_argument("--subscription-price-usd", type=float, default=None,
                        help="Override SUBSCRIPTION_COST_PER_MONTH_USD (default: $20/month).")
    parser.add_argument("--papers-per-month", type=int, default=None,
                        help="Override SUBSCRIPTION_PAPERS_PER_MONTH (default: 500).")
    output_group = parser.add_mutually_exclusive_group()
    output_group.add_argument("--json", action="store_true", help="Print the full report JSON.")
    output_group.add_argument("--markdown", action="store_true", help="Print the Markdown report.")
    parser.add_argument("--no-save", action="store_true", help="Print only; do not write files.")
    args = parser.parse_args(argv)

    report = build_stats_engine_report(
        evaluations_path=args.evaluations,
        summaries_path=args.summaries,
        human_reviews_path=args.human_reviews,
        subscription_price_usd=args.subscription_price_usd,
        papers_per_month=args.papers_per_month,
    )
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report["generated_at"] = ts
    markdown = render_markdown(report)

    has_data = bool(report["providers"]) or report["information_density"].get("available")
    saved: dict[str, Path] = {}
    if not args.no_save and has_data:
        saved = save_stats_engine_report(report, markdown, ts, args.results_dir)

    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    elif args.markdown:
        print(markdown)
    else:
        print("=" * 72)
        print("STATS ENGINE — INFORMATION DENSITY, IRR, ECONOMICS, COVARIATES")
        print("=" * 72)
        if not has_data:
            print("\nNo data found yet. Run 'evaluate' (and ideally 'ingest-human-review') first.")
        else:
            print(markdown)

    for label, path in saved.items():
        print(f"Saved {label}: {path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
