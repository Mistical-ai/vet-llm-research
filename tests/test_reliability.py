"""
Tests for llm-sum/reliability.py — offline inter-judge reliability.

Critical assertions:
    Krippendorff's alpha matches hand-computed values on small fixtures.
    Perfect agreement → 1.0; a lone disagreeing item → 0.0.
    compute_reliability reports available=False for a single judge and
    available=True (with alpha + pairwise agreement) for a jury.
    No paid API is touched (pure functions over in-memory rows).
"""

from __future__ import annotations

import pytest

import reliability
from reliability import (
    compute_reliability,
    has_multiple_judges,
    krippendorff_alpha_interval,
    pairwise_absolute_agreement,
)


# ---------------------------------------------------------------------------
# Krippendorff's alpha (interval)
# ---------------------------------------------------------------------------

def test_alpha_none_without_pairable_units() -> None:
    """No item scored by >= 2 judges → nothing to measure → None."""
    assert krippendorff_alpha_interval([]) is None
    assert krippendorff_alpha_interval([[4], [5], [3]]) is None


def test_alpha_perfect_agreement_is_one() -> None:
    """Identical ratings on every item → perfect reliability."""
    assert krippendorff_alpha_interval([[3, 3], [5, 5], [4, 4]]) == 1.0


def test_alpha_single_disagreeing_item_is_zero() -> None:
    """One item, two judges differing by 1, no other data → alpha = 0.0.

    With a single item you cannot establish above-chance agreement, and the
    coincidence-matrix formula yields exactly 0 here (D_observed == D_expected).
    """
    assert krippendorff_alpha_interval([[1, 2]]) == 0.0


def test_alpha_known_value() -> None:
    """Hand-computed fixture locks the formula.

    units = [[4, 5], [4, 4], [3, 4]]
      D_observed = (2 + 0 + 2) / 6           = 0.6667
      pooled = [4, 5, 4, 4, 3, 4], N = 6
      sum_{a!=b}(x_a - x_b)^2 = 2*N*sum(x^2) - 2*(sum x)^2
                              = 2*6*98 - 2*24^2 = 24
      D_expected = 24 / (6 * 5)              = 0.8
      alpha = 1 - 0.6667/0.8                 = 0.16667
    """
    alpha = krippendorff_alpha_interval([[4, 5], [4, 4], [3, 4]])
    assert alpha is not None
    assert round(alpha, 5) == 0.16667


def test_alpha_negative_on_systematic_disagreement() -> None:
    """Judges that flip high/low across items disagree worse than chance."""
    alpha = krippendorff_alpha_interval([[1, 5], [5, 1], [1, 5], [5, 1]])
    assert alpha is not None
    assert alpha < 0


# ---------------------------------------------------------------------------
# Pairwise agreement
# ---------------------------------------------------------------------------

def test_pairwise_absolute_agreement_mean_diff() -> None:
    rows = [
        {"doi": "d1", "summarizer": "openai", "input_source": "processed",
         "judge": "openai", "jury_score": 4.0},
        {"doi": "d1", "summarizer": "openai", "input_source": "processed",
         "judge": "anthropic", "jury_score": 5.0},
        {"doi": "d2", "summarizer": "openai", "input_source": "processed",
         "judge": "openai", "jury_score": 3.0},
        {"doi": "d2", "summarizer": "openai", "input_source": "processed",
         "judge": "anthropic", "jury_score": 3.0},
    ]
    agreement = pairwise_absolute_agreement(rows, reliability._jury_value)
    pair = agreement["anthropic|openai"]
    assert pair["n_items"] == 2
    # |4-5| and |3-3| → mean 0.5, max 1.0
    assert pair["mean_abs_diff"] == 0.5
    assert pair["max_abs_diff"] == 1.0


# ---------------------------------------------------------------------------
# compute_reliability
# ---------------------------------------------------------------------------

def _jury_row(doi: str, judge: str, scores: dict[str, int], jury: float) -> dict:
    return {
        "doi": doi,
        "summarizer": "openai",
        "input_source": "processed",
        "judge": judge,
        "jury_score": jury,
        "criteria_scores": {k: {"score": v, "reasoning": ""} for k, v in scores.items()},
    }


def test_single_judge_not_available() -> None:
    rows = [_jury_row("d1", "openai", {"faithfulness": 4}, 4.0)]
    result = compute_reliability(rows)
    assert result["available"] is False
    assert result["n_judges"] == 1
    assert "at least two judges" in result["reason"]
    assert not has_multiple_judges(rows)


def test_two_judges_no_shared_item_not_available() -> None:
    """Two judges present, but each scored a different summary → not comparable."""
    rows = [
        _jury_row("d1", "openai", {"faithfulness": 4}, 4.0),
        _jury_row("d2", "anthropic", {"faithfulness": 3}, 3.0),
    ]
    result = compute_reliability(rows)
    assert result["available"] is False
    assert result["n_judges"] == 2
    assert result["n_comparable_items"] == 0
    assert "no single summary" in result["reason"].lower()


def test_multi_judge_reports_alpha_and_pairwise() -> None:
    full = {"faithfulness": 5, "completeness": 4, "clinical_usefulness": 5,
            "clarity": 4, "safety": 5}
    rows = [
        _jury_row("d1", "openai", full, 4.6),
        _jury_row("d1", "anthropic", full, 4.6),
        _jury_row("d2", "openai", {**full, "safety": 3}, 4.2),
        _jury_row("d2", "anthropic", {**full, "safety": 4}, 4.4),
    ]
    result = compute_reliability(rows)
    assert result["available"] is True
    assert result["n_judges"] == 2
    assert result["judges"] == ["anthropic", "openai"]
    assert result["n_comparable_items"] == 2
    # Overall jury alpha is present and numeric.
    assert isinstance(result["jury_score"]["krippendorff_alpha"], float)
    # Every criterion has its own reliability sub-report.
    assert set(result["per_criterion"]) == set(reliability.CRITERIA)
    # Faithfulness is identical across judges/items → perfect agreement.
    assert result["per_criterion"]["faithfulness"]["krippendorff_alpha"] == 1.0
    # Pairwise agreement is keyed by the sorted judge pair.
    assert "anthropic|openai" in result["pairwise_agreement"]
    assert result["interpretation"]


def test_multi_judge_handles_missing_judge_on_some_items() -> None:
    """Unbalanced data (one judge skips an item) still yields a report."""
    rows = [
        _jury_row("d1", "openai", {"faithfulness": 4}, 4.0),
        _jury_row("d1", "anthropic", {"faithfulness": 4}, 4.0),
        _jury_row("d1", "gemini", {"faithfulness": 4}, 4.0),
        _jury_row("d2", "openai", {"faithfulness": 3}, 3.0),
        _jury_row("d2", "anthropic", {"faithfulness": 3}, 3.0),
        # gemini did not score d2 — that's allowed.
    ]
    result = compute_reliability(rows)
    assert result["available"] is True
    assert result["n_judges"] == 3
    assert result["n_comparable_items"] == 2
    assert has_multiple_judges(rows)


# ---------------------------------------------------------------------------
# Tier 1b.8 — the alpha verdict needs a minimum-n gate, like its sibling
# ---------------------------------------------------------------------------

def test_interpret_alpha_bands() -> None:
    """Krippendorff's own cutoffs, at and around each boundary (n large enough
    that the underpowered gate never fires)."""
    from reliability import _interpret_alpha

    n = 50
    assert "Strong agreement" in _interpret_alpha(0.80, n)
    assert "Strong agreement" in _interpret_alpha(0.95, n)
    assert "Acceptable agreement" in _interpret_alpha(0.667, n)
    assert "Acceptable agreement" in _interpret_alpha(0.79, n)
    assert "Weak agreement" in _interpret_alpha(0.0, n)
    assert "Weak agreement" in _interpret_alpha(0.5, n)
    assert "Systematic disagreement" in _interpret_alpha(-0.1, n)
    assert "No agreement estimate" in _interpret_alpha(None, n)


def test_interpret_alpha_withholds_verdict_below_min_n() -> None:
    """A 3-paper pilot must not headline 'jury scores are reliable'."""
    from reliability import MIN_ALPHA_N, _interpret_alpha

    verdict = _interpret_alpha(0.9, 3)
    assert "Underpowered" in verdict
    assert "Strong agreement" not in verdict
    assert str(MIN_ALPHA_N) in verdict
    # The coefficient itself stays visible for transparency.
    assert "0.9" in verdict


def test_interpret_alpha_ungated_without_n() -> None:
    """Omitting n keeps the old behaviour for callers that have no count."""
    from reliability import _interpret_alpha

    assert "Strong agreement" in _interpret_alpha(0.9)


def test_compute_reliability_withholds_verdict_on_small_sample() -> None:
    """The gate must be wired through compute_reliability, not just available."""
    from reliability import compute_reliability

    scores = {c: 4 for c in
              ("faithfulness", "completeness", "clinical_usefulness", "clarity", "safety")}
    rows = [_jury_row("10.1/x", judge, scores, 4.0) for judge in ("openai", "anthropic")]
    result = compute_reliability(rows)
    assert result["available"] is True
    assert result["n_comparable_items"] == 1
    assert "Underpowered" in result["interpretation"]


# ---------------------------------------------------------------------------
# External validation of the one hand-rolled statistic
# ---------------------------------------------------------------------------

def test_krippendorff_alpha_matches_published_reference_value() -> None:
    """Reproduce Krippendorff's own published worked example.

    krippendorff_alpha_interval is the ONLY statistic in this codebase that is
    hand-rolled rather than delegated to scipy/sklearn — legitimately, since
    scipy has no implementation. Every other test of it checks self-consistency
    or edge cases (all-identical -> 1.0, no variance -> None), which would all
    still pass if the formula itself were subtly wrong.

    This one pins it to an externally published number: the canonical
    3-observer x 15-unit reliability matrix from Krippendorff (2011),
    "Computing Krippendorff's Alpha-Reliability", for which the paper reports
    alpha_interval = 0.811. Twelve units carry two or more observations; the
    three singly-observed units drop out, as alpha requires.

    If this fails, the estimator is wrong — not the test data.
    """
    observer_a = [None, None, None, None, None, 3, 4, 1, 2, 1, 1, 3, 3, None, 3]
    observer_b = [1, None, 2, 1, 3, 3, 4, 3, None, None, None, None, None, None, None]
    observer_c = [None, None, 2, 1, 3, 4, 4, None, 2, 1, 1, 3, 3, None, 4]

    units = [
        [v for v in triple if v is not None]
        for triple in zip(observer_a, observer_b, observer_c)
    ]
    assert sum(1 for u in units if len(u) >= 2) == 12

    alpha = krippendorff_alpha_interval(units)
    assert alpha == pytest.approx(0.811, abs=0.001)
