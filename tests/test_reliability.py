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
