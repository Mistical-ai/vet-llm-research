"""
llm-sum/reliability.py — Offline inter-judge reliability statistics
===================================================================

WHY THIS MODULE EXISTS
----------------------
A single blind judge gives a score, but a score with no measure of its own
reproducibility is hard to defend in a methods section. When a run uses two or
three judges (see ``JUDGE_MODELS`` / ``JURY_PRESET`` in ``.env``), this module
reads the append-only ``data/evaluations.jsonl`` rows and asks: *do the judges
agree with each other?* The answer — Krippendorff's alpha, pairwise absolute
agreement, and per-criterion spread — tells a reader how trustworthy the jury
scores are.

DESIGN CONSTRAINTS (from CLAUDE.md)
-----------------------------------
- Offline only. This module never calls a paid API; it consumes rows that
  ``evaluator.py`` already wrote.
- Krippendorff's alpha is hand-rolled in plain Python because ``scipy`` does not
  implement it. The correlation coefficients and p-values (the human-vs-jury
  validation below) come from ``scipy.stats`` — a peer-reviewed, citable source,
  so a claim like "the jury tracks veterinarian judgment (r=0.72, p=0.003)"
  rests on a validated implementation. ``scipy``/``numpy`` are pinned in
  requirements.txt.
- Reads the same item identity as the resume key: a "unit" (item) is one
  ``(doi, summarizer, input_source)`` triple, scored once per judge.

WHAT IS KRIPPENDORFF'S ALPHA?
-----------------------------
A single number for how much a set of raters agree, corrected for the agreement
you would expect by chance. 1.0 = perfect agreement, 0.0 = only chance-level
agreement, negative = systematic disagreement (worse than chance). It handles a
judge missing on some items (unbalanced data), which max-minus-min disagreement
cannot. We use the *interval* difference metric because the criterion scores
(1-5) and jury scores are ordered numeric values, not unordered categories.
"""

from __future__ import annotations

import math
import warnings
from collections import defaultdict
from typing import Any, Callable, Iterable

from scipy import stats as scipy_stats

# The five MedHELM-style criteria, mirrored from evaluator.MEDHELM_CRITERION_WEIGHTS.
# Kept as a local tuple so reliability reporting has no import-time dependency on
# the evaluator's runtime weight configuration.
CRITERIA = ("faithfulness", "completeness", "clinical_usefulness", "clarity", "safety")


# ---------------------------------------------------------------------------
# Row accessors
# ---------------------------------------------------------------------------

def _item_key(row: dict[str, Any]) -> tuple[str, str, str]:
    """Identity of the thing being judged: one (doi, summarizer, input_source).

    This matches the evaluator's resume key (minus judge + rubric_version), so
    the rows grouped here are exactly the per-judge opinions of one summary.
    """
    return (
        str(row.get("doi", "")),
        str(row.get("summarizer", "")),
        str(row.get("input_source") or "processed"),
    )


def _judge(row: dict[str, Any]) -> str:
    return str(row.get("judge", ""))


def _jury_value(row: dict[str, Any]) -> float | None:
    value = row.get("jury_score")
    return float(value) if isinstance(value, (int, float)) else None


def _criterion_value(row: dict[str, Any], criterion: str) -> float | None:
    """Read one criterion's integer score from the nested criteria_scores block."""
    block = row.get("criteria_scores")
    if not isinstance(block, dict):
        return None
    item = block.get(criterion)
    if isinstance(item, dict):
        item = item.get("score")
    return float(item) if isinstance(item, (int, float)) else None


def judges_present(rows: Iterable[dict[str, Any]]) -> list[str]:
    """Sorted list of distinct judge provider keys seen in the rows."""
    return sorted({_judge(row) for row in rows if _judge(row)})


# ---------------------------------------------------------------------------
# Krippendorff's alpha (interval metric), hand-rolled
# ---------------------------------------------------------------------------

def krippendorff_alpha_interval(units: Iterable[list[float]]) -> float | None:
    """Krippendorff's alpha for interval data.

    ``units`` is one list of numeric ratings per item — e.g. ``[4, 5]`` means two
    judges scored the same summary 4 and 5. Items with fewer than two ratings are
    ignored (a single opinion carries no agreement information).

    Returns ``None`` when there is nothing to measure (no item has >= 2 ratings).
    Returns ``1.0`` when every rating is identical (no variance at all), which is
    the conventional definition of perfect reliability.

    The formula is the standard coincidence-matrix definition
    (Krippendorff 2011) specialized to the squared-difference metric
    ``delta(a, b) = (a - b)^2``:

        alpha = 1 - D_observed / D_expected

    - ``D_observed`` averages within-item disagreement, weighting each item by
      ``1 / (m_u - 1)`` so items rated by more judges are not over-counted.
    - ``D_expected`` is the disagreement expected if all ratings were shuffled
      across items, i.e. chance agreement given the observed score distribution.
    """
    pairable = [list(u) for u in units if len(u) >= 2]
    if not pairable:
        return None

    n = sum(len(u) for u in pairable)  # total pairable values
    if n < 2:
        return None

    # Observed disagreement: within-item squared differences over ordered pairs,
    # each item normalized by (m_u - 1).
    observed_numerator = 0.0
    for u in pairable:
        m = len(u)
        within = 0.0
        for i in range(m):
            for j in range(m):
                if i != j:
                    diff = u[i] - u[j]
                    within += diff * diff
        observed_numerator += within / (m - 1)
    d_observed = observed_numerator / n

    # Expected disagreement: squared differences over every ordered pair of the
    # pooled ratings (diagonal pairs contribute 0, so they drop out naturally).
    pooled = [x for u in pairable for x in u]
    expected_numerator = 0.0
    for a in range(len(pooled)):
        for b in range(len(pooled)):
            if a != b:
                diff = pooled[a] - pooled[b]
                expected_numerator += diff * diff
    d_expected = expected_numerator / (n * (n - 1))

    if d_expected == 0:
        # No variance anywhere → all judges gave identical scores → perfect.
        return 1.0
    return 1.0 - d_observed / d_expected


# ---------------------------------------------------------------------------
# Per-value aggregation helpers
# ---------------------------------------------------------------------------

def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 3) if values else None


def _variance(values: list[float]) -> float | None:
    """Population variance — a plain spread measure, no sampling correction."""
    if len(values) < 2:
        return 0.0 if values else None
    mean = sum(values) / len(values)
    return round(sum((v - mean) ** 2 for v in values) / len(values), 3)


def _units_for(rows: list[dict[str, Any]],
               value_fn: Callable[[dict[str, Any]], float | None]) -> list[list[float]]:
    """Group rows into per-item rating lists using ``value_fn`` to read a score.

    One judge is allowed at most one rating per item; if the same judge appears
    twice for an item (e.g. a re-run wrote a second row), the later value wins so
    a duplicate never inflates the agreement count.
    """
    by_item: dict[tuple[str, str, str], dict[str, float]] = defaultdict(dict)
    for row in rows:
        value = value_fn(row)
        if value is None:
            continue
        by_item[_item_key(row)][_judge(row)] = value
    return [list(judge_values.values()) for judge_values in by_item.values()]


def pairwise_absolute_agreement(
    rows: list[dict[str, Any]],
    value_fn: Callable[[dict[str, Any]], float | None],
) -> dict[str, dict[str, Any]]:
    """Mean absolute score difference for each pair of judges on shared items.

    A simple, explainable companion to alpha: "on the summaries both judges
    scored, their scores differed by X on average." Keyed by ``"judgeA|judgeB"``
    with the judge names sorted so the key is stable.
    """
    by_item: dict[tuple[str, str, str], dict[str, float]] = defaultdict(dict)
    for row in rows:
        value = value_fn(row)
        if value is None:
            continue
        by_item[_item_key(row)][_judge(row)] = value

    pair_diffs: dict[tuple[str, str], list[float]] = defaultdict(list)
    for judge_values in by_item.values():
        judges = sorted(judge_values)
        for i in range(len(judges)):
            for j in range(i + 1, len(judges)):
                pair = (judges[i], judges[j])
                pair_diffs[pair].append(abs(judge_values[judges[i]] - judge_values[judges[j]]))

    result: dict[str, dict[str, Any]] = {}
    for (judge_a, judge_b), diffs in sorted(pair_diffs.items()):
        result[f"{judge_a}|{judge_b}"] = {
            "n_items": len(diffs),
            "mean_abs_diff": round(sum(diffs) / len(diffs), 3) if diffs else None,
            "max_abs_diff": round(max(diffs), 3) if diffs else None,
        }
    return result


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def has_multiple_judges(rows: Iterable[dict[str, Any]]) -> bool:
    """True when at least two distinct judges appear — the gate for a report."""
    seen: set[str] = set()
    for row in rows:
        judge = _judge(row)
        if judge:
            seen.add(judge)
        if len(seen) >= 2:
            return True
    return False


def _count_multi_judge_items(rows: list[dict[str, Any]],
                             value_fn: Callable[[dict[str, Any]], float | None]) -> int:
    return sum(1 for unit in _units_for(rows, value_fn) if len(unit) >= 2)


def compute_reliability(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Compute the full inter-judge reliability view for a set of evaluation rows.

    Returns a dict that is always shaped the same way. ``available`` is ``False``
    (with a human-readable ``reason``) when the data cannot support a reliability
    estimate — fewer than two judges, or no summary scored by two of them — so
    callers can render the section without special-casing missing keys.
    """
    materialized = list(rows)
    judges = judges_present(materialized)

    base: dict[str, Any] = {
        "judges": judges,
        "n_judges": len(judges),
    }

    if len(judges) < 2:
        base.update({
            "available": False,
            "reason": (
                "Reliability statistics need at least two judges. The default is "
                "a single judge; set JUDGE_MODELS=openai,anthropic,gemini or "
                "JURY_PRESET=panel to enable a jury."
            ),
            "n_comparable_items": 0,
            "jury_score": None,
            "per_criterion": {},
            "pairwise_agreement": {},
        })
        return base

    comparable_items = _count_multi_judge_items(materialized, _jury_value)
    if comparable_items == 0:
        base.update({
            "available": False,
            "reason": (
                "Two or more judges are present, but no single summary was scored "
                "by two of them, so agreement cannot be measured yet."
            ),
            "n_comparable_items": 0,
            "jury_score": None,
            "per_criterion": {},
            "pairwise_agreement": {},
        })
        return base

    # Overall reliability on the primary jury_score.
    jury_units = _units_for(materialized, _jury_value)
    jury_values = [v for row in materialized if (v := _jury_value(row)) is not None]
    jury_block = {
        "krippendorff_alpha": _round_or_none(krippendorff_alpha_interval(jury_units)),
        "mean": _mean(jury_values),
        "variance": _variance(jury_values),
        "n_comparable_items": comparable_items,
    }

    # Per-criterion reliability so a reader can see which criteria judges argue
    # about most (e.g. "safety" is often the least reproducible).
    per_criterion: dict[str, dict[str, Any]] = {}
    for criterion in CRITERIA:
        value_fn = _make_criterion_fn(criterion)
        units = _units_for(materialized, value_fn)
        values = [v for row in materialized if (v := value_fn(row)) is not None]
        per_criterion[criterion] = {
            "krippendorff_alpha": _round_or_none(krippendorff_alpha_interval(units)),
            "mean": _mean(values),
            "variance": _variance(values),
            "n_comparable_items": sum(1 for unit in units if len(unit) >= 2),
        }

    base.update({
        "available": True,
        "n_comparable_items": comparable_items,
        "jury_score": jury_block,
        "per_criterion": per_criterion,
        "pairwise_agreement": pairwise_absolute_agreement(materialized, _jury_value),
        "interpretation": _interpret_alpha(jury_block["krippendorff_alpha"]),
    })
    return base


def _make_criterion_fn(criterion: str) -> Callable[[dict[str, Any]], float | None]:
    return lambda row: _criterion_value(row, criterion)


def _round_or_none(value: float | None) -> float | None:
    return round(value, 3) if isinstance(value, (int, float)) else None


def _interpret_alpha(alpha: float | None) -> str:
    """Plain-language reading of an alpha value using Krippendorff's own cutoffs.

    Krippendorff suggests alpha >= 0.80 for firm conclusions and 0.667 as the
    lowest value at which tentative conclusions are still defensible.
    """
    if alpha is None:
        return "No agreement estimate available."
    if alpha >= 0.80:
        return "Strong agreement (alpha >= 0.80): jury scores are reliable."
    if alpha >= 0.667:
        return "Acceptable agreement (0.667 <= alpha < 0.80): tentative conclusions only."
    if alpha >= 0:
        return "Weak agreement (0 <= alpha < 0.667): interpret jury scores with caution."
    return "Systematic disagreement (alpha < 0): judges disagree more than chance."


# ---------------------------------------------------------------------------
# Correlation (human-vs-jury validation) — scipy-backed
# ===========================================================================
# Phase 5's human-validation step (human_review.py) uses these to ask the
# complementary question to alpha: not "do the raters agree with each other?"
# but "does the LLM jury track a human expert?" The coefficients AND their
# p-values come from scipy.stats (pearsonr / spearmanr), a peer-reviewed and
# citable source, so a "the jury tracks veterinarian judgment (r=0.72, p=0.003)"
# claim is defensible. Krippendorff's alpha above stays hand-rolled only because
# scipy does not implement it.
# ---------------------------------------------------------------------------

def _pearsonr(xs: list[float], ys: list[float]) -> tuple[float, float] | None:
    """(coefficient, two-sided p-value) from scipy, or ``None`` when undefined.

    Undefined = fewer than two paired points, mismatched lengths, or a flat
    series (zero variance, where scipy returns NaN). Returning ``None`` in those
    cases preserves the pre-scipy contract every caller already handles.
    """
    n = len(xs)
    if n < 2 or len(ys) != n:
        return None
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # scipy warns on constant/near-constant input
        result = scipy_stats.pearsonr(xs, ys)
    if math.isnan(result.statistic):
        return None
    return float(result.statistic), float(result.pvalue)


def _spearmanr(xs: list[float], ys: list[float]) -> tuple[float, float | None] | None:
    """(coefficient, p-value) from scipy spearmanr, or ``None`` when undefined.

    The p-value can be ``None`` even when the coefficient is defined — notably at
    n=2, where the rank-correlation significance is undefined (scipy returns NaN
    for the p-value but a valid ±1 coefficient).
    """
    n = len(xs)
    if n < 2 or len(ys) != n:
        return None
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        result = scipy_stats.spearmanr(xs, ys)
    if math.isnan(result.statistic):
        return None
    p = None if math.isnan(result.pvalue) else float(result.pvalue)
    return float(result.statistic), p


def pearson(xs: list[float], ys: list[float]) -> float | None:
    """Pearson product-moment correlation coefficient, or ``None`` when undefined.

    Thin accessor over ``scipy.stats.pearsonr``; see :func:`_pearsonr` for the
    ``None`` cases (< 2 points, mismatched lengths, or a flat series).
    """
    result = _pearsonr(xs, ys)
    return round(result[0], 3) if result else None


def pearson_p(xs: list[float], ys: list[float]) -> float | None:
    """Two-sided p-value for the Pearson correlation, or ``None`` when undefined."""
    result = _pearsonr(xs, ys)
    return round(result[1], 4) if result else None


def spearman(xs: list[float], ys: list[float]) -> float | None:
    """Spearman rank correlation coefficient, or ``None`` when undefined.

    More appropriate than Pearson for validating an LLM jury against human
    scores, because it measures whether the two rank summaries in the same order
    without assuming the relationship is linear. Thin accessor over
    ``scipy.stats.spearmanr``; returns ``None`` under the same conditions as
    :func:`pearson` (< 2 points, or no rank variance).
    """
    result = _spearmanr(xs, ys)
    return round(result[0], 3) if result else None


def spearman_p(xs: list[float], ys: list[float]) -> float | None:
    """Two-sided p-value for the Spearman correlation, or ``None`` when undefined.

    ``None`` includes the n=2 case, where the rank-correlation p-value is not
    defined even though a ±1 coefficient is.
    """
    result = _spearmanr(xs, ys)
    if not result or result[1] is None:
        return None
    return round(result[1], 4)


def bland_altman(primary: list[float], reference: list[float]) -> dict[str, Any] | None:
    """Bland-Altman bias of ``primary`` relative to ``reference``.

    Correlation says whether two measures move together; it does *not* reveal a
    constant offset (an LLM jury that is always 0.5 points more generous than a
    human is perfectly correlated yet biased). ``mean_bias`` is the average
    ``primary - reference`` difference, and the 95% limits of agreement
    (``loa_lower``/``loa_upper``) bound where individual differences fall.
    Returns ``None`` when the series are empty or mismatched in length.
    """
    n = len(primary)
    if n < 1 or len(reference) != n:
        return None
    diffs = [p - r for p, r in zip(primary, reference)]
    bias = sum(diffs) / n
    sd = (sum((d - bias) ** 2 for d in diffs) / (n - 1)) ** 0.5 if n >= 2 else 0.0
    return {
        "n": n,
        "mean_bias": round(bias, 3),
        "sd_diff": round(sd, 3),
        "loa_lower": round(bias - 1.96 * sd, 3),
        "loa_upper": round(bias + 1.96 * sd, 3),
    }


# Minimum comparable items before a correlation coefficient is treated as
# interpretable. Below this, a coefficient (even r=1.0) is statistically
# unstable — one item added or removed can swing it wildly — so the report must
# NOT present it as validation. Set to 30, the conventional small-sample
# threshold: a research-grade claim that "the LLM jury tracks veterinarian
# judgment" should not rest on fewer. Callers still see the coefficient and n;
# only the plain-language verdict is withheld below this. Tunable here in one
# place if a study's validation sample dictates otherwise.
MIN_CORRELATION_N = 30


def interpret_correlation(coefficient: float | None, n: int | None = None) -> str:
    """Plain-language reading of a human-vs-jury correlation coefficient.

    Uses conventional |r| bands so a reader can see at a glance whether the LLM
    jury tracks expert judgment. Sign matters: a strong *negative* correlation
    means the jury ranks summaries opposite to the human.

    ``n`` is the number of comparable items behind the coefficient. When it is
    below :data:`MIN_CORRELATION_N`, the verdict is withheld and replaced with
    an underpowered-sample caveat regardless of the coefficient's value — a
    safety guard so a spurious small-sample correlation is never reported as
    validation.
    """
    if coefficient is None:
        return "No correlation estimate available (need at least two comparable items)."
    if n is not None and n < MIN_CORRELATION_N:
        return (
            f"Underpowered: only {n} comparable item(s) (< {MIN_CORRELATION_N}). "
            f"The coefficient (r={coefficient}) is shown for transparency but is too "
            "unstable to treat as validation -- sample more items before concluding."
        )
    if coefficient < 0:
        return ("Negative correlation: the LLM jury tends to rank summaries opposite "
                "to the human reviewer(s) — investigate before trusting the jury.")
    if coefficient >= 0.7:
        return "Strong correlation (r >= 0.70): the LLM jury tracks expert judgment well."
    if coefficient >= 0.4:
        return "Moderate correlation (0.40 <= r < 0.70): the jury broadly tracks experts."
    return "Weak correlation (r < 0.40): the LLM jury is a poor proxy for expert judgment."
