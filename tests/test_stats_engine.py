"""
tests/test_stats_engine.py — Information density, Cohen's Kappa, subscription
economics, and covariate "research meat" tables.

Exercises the offline stats layer in llm-sum/stats_engine.py: hand-computed
Cohen's Kappa fixtures (locking the sklearn wrapper's behavior the same way
test_reliability.py locks Krippendorff's alpha), TF-IDF/cosine similarity on a
small fixed corpus, word-count banding, subscription-economics arithmetic, and
the covariate report's underpowered-flagging. Everything here is pure/offline
— no mocks needed, no API calls, no real data/ files touched.
"""

from __future__ import annotations

import stats_engine
from stats_engine import (
    build_covariate_report,
    build_information_density_report,
    build_subscription_efficiency,
    categorical_agreement,
    cohen_kappa,
    cosine_similarity_pair,
    fit_tfidf_corpus,
    information_density_row,
    percent_agreement,
    subscription_cost_per_summary,
    word_count_comparison,
)


# ---------------------------------------------------------------------------
# Cohen's Kappa + percent agreement
# ---------------------------------------------------------------------------

def test_cohen_kappa_perfect_agreement_is_one() -> None:
    ratings = [1, 2, 3, 1, 2, 3, 1, 2, 3]
    assert cohen_kappa(ratings, ratings) == 1.0


def test_cohen_kappa_hand_computed_negative_value() -> None:
    """Hand-computed fixture locks the sklearn wrapper's formula.

    a = [1,1,1, 2,2,2, 3,3,3]; b = [3,3,3, 1,1,1, 2,2,2] (cyclic shift, 0 matches).
    Balanced marginals (3 of each category on both sides) ->
      p_o = 0
      p_e = sum_i (n_i/N) * (m_i/N) = 3 * (3/9 * 3/9) = 1/3
      kappa = (p_o - p_e) / (1 - p_e) = (0 - 1/3) / (2/3) = -0.5
    """
    a = [1, 1, 1, 2, 2, 2, 3, 3, 3]
    b = [3, 3, 3, 1, 1, 1, 2, 2, 2]
    assert cohen_kappa(a, b) == -0.5


def test_cohen_kappa_all_same_category_is_perfect() -> None:
    """Degenerate p_e=1 case (sklearn returns nan) converts to 1.0, matching
    Krippendorff's alpha's own 'no variance anywhere -> perfect' convention."""
    assert cohen_kappa([3, 3, 3], [3, 3, 3]) == 1.0


def test_cohen_kappa_none_for_fewer_than_two_ratings() -> None:
    assert cohen_kappa([], []) is None
    assert cohen_kappa([3], [3]) is None
    assert cohen_kappa([1, 2], [1]) is None


def test_cohen_kappa_quadratic_weights_accepted() -> None:
    a = [1, 2, 3, 4, 5]
    b = [1, 2, 3, 4, 4]
    unweighted = cohen_kappa(a, b)
    quadratic = cohen_kappa(a, b, weights="quadratic")
    assert unweighted is not None and quadratic is not None
    # A single off-by-one miss is penalized less under quadratic weighting.
    assert quadratic > unweighted


def test_percent_agreement_basic() -> None:
    assert percent_agreement([1, 2, 3], [1, 2, 4]) == round(2 / 3, 3)
    assert percent_agreement([1, 2, 3], [1, 2, 3]) == 1.0
    assert percent_agreement([], []) is None
    assert percent_agreement([1], [1, 2]) is None


def test_categorical_agreement_rounds_continuous_composites() -> None:
    """4.4 -> 4, 2.6 -> 3 on both sides -> perfect categorical agreement."""
    result = categorical_agreement([4.4, 2.6], [4.0, 3.0])
    assert result["n"] == 2
    assert result["cohen_kappa"] == 1.0
    assert result["percent_agreement"] == 1.0


def test_categorical_agreement_none_below_n_two() -> None:
    result = categorical_agreement([4.0], [4.0])
    assert result == {"n": 1, "cohen_kappa": None, "cohen_kappa_quadratic": None, "percent_agreement": None}


# ---------------------------------------------------------------------------
# Word count comparison
# ---------------------------------------------------------------------------

def test_word_count_comparison_bands() -> None:
    abstract = "one two three four five"
    assert word_count_comparison(abstract, "one two three four five")["label"] == "same"
    assert word_count_comparison(abstract, "one two three")["label"] == "shorter"
    assert word_count_comparison(abstract, "one two three four five six seven eight nine ten")["label"] == "longer"
    unknown = word_count_comparison("", "some summary text")
    assert unknown["ratio"] is None
    assert unknown["label"] == "unknown"


# ---------------------------------------------------------------------------
# TF-IDF + cosine similarity
# ---------------------------------------------------------------------------

_CORPUS = [
    "the dog has osteosarcoma in the femur bone",
    "the dog has osteosarcoma in the femur causing a bone tumor",
    "weather forecast sunny tomorrow afternoon clouds",
    "completely different topic about baking bread recipes at home",
]


def test_fit_tfidf_corpus_none_for_empty_documents() -> None:
    assert fit_tfidf_corpus([]) is None
    assert fit_tfidf_corpus(["", "   "]) is None


def test_cosine_similarity_pair_high_for_shared_vocabulary() -> None:
    vectorizer = fit_tfidf_corpus(_CORPUS)
    similarity = cosine_similarity_pair(vectorizer, _CORPUS[0], _CORPUS[1])
    assert similarity is not None
    assert similarity > 0.5


def test_cosine_similarity_pair_low_for_disjoint_vocabulary() -> None:
    vectorizer = fit_tfidf_corpus(_CORPUS)
    similarity = cosine_similarity_pair(vectorizer, _CORPUS[2], _CORPUS[3])
    assert similarity is not None
    assert similarity < 0.1


def test_cosine_similarity_pair_none_for_empty_text() -> None:
    vectorizer = fit_tfidf_corpus(_CORPUS)
    assert cosine_similarity_pair(vectorizer, "", _CORPUS[0]) is None
    assert cosine_similarity_pair(None, _CORPUS[0], _CORPUS[1]) is None


def test_information_density_row_bands() -> None:
    vectorizer = fit_tfidf_corpus(_CORPUS)
    high = information_density_row(vectorizer, _CORPUS[0], _CORPUS[1])
    assert high["band"] in ("high", "moderate")
    assert high["retained_or_more"] is True

    low = information_density_row(vectorizer, _CORPUS[2], _CORPUS[3])
    assert low["band"] == "low"
    assert low["retained_or_more"] is False


# ---------------------------------------------------------------------------
# Information density report
# ---------------------------------------------------------------------------

def test_build_information_density_report_empty_is_unavailable() -> None:
    report = build_information_density_report({}, {})
    assert report["available"] is False


def test_build_information_density_report_basic_shape() -> None:
    manifest_index = {
        "10.1/one": {"abstract": _CORPUS[0]},
        "10.1/two": {"abstract": _CORPUS[2]},
    }
    summaries_by_doi = {
        "10.1/one": {"openai": _CORPUS[1]},
        "10.1/two": {"openai": _CORPUS[3]},
    }
    report = build_information_density_report(manifest_index, summaries_by_doi)
    assert report["available"] is True
    assert report["n_pairs"] == 2
    assert set(report["by_provider"]) == {"openai"}
    stats = report["by_provider"]["openai"]
    assert stats["n"] == 2
    # One retained-or-more pair (shared vocab), one lost (disjoint vocab).
    assert stats["pct_retained_or_more"] == 0.5


def test_build_information_density_report_skips_dois_without_abstract() -> None:
    manifest_index = {"10.1/one": {"abstract": ""}}
    summaries_by_doi = {"10.1/one": {"openai": _CORPUS[0]}}
    report = build_information_density_report(manifest_index, summaries_by_doi)
    assert report["available"] is False


# ---------------------------------------------------------------------------
# Subscription-based cost-per-quality
# ---------------------------------------------------------------------------

def test_subscription_cost_per_summary_default() -> None:
    assert subscription_cost_per_summary() == 0.04


def test_subscription_cost_per_summary_override() -> None:
    assert subscription_cost_per_summary(subscription_price_usd=10.0, papers_per_month=100) == 0.1


def test_subscription_cost_per_summary_rejects_non_positive_papers() -> None:
    try:
        subscription_cost_per_summary(papers_per_month=0)
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_build_subscription_efficiency_ranks_by_efficiency() -> None:
    rows = build_subscription_efficiency({"openai": 4.0, "anthropic": 2.0, "gemini": None})
    assert [r["provider"] for r in rows] == ["openai", "anthropic", "gemini"]
    assert rows[0]["efficiency"] == 100.0
    assert rows[1]["efficiency"] == 50.0
    assert rows[2]["efficiency"] is None
    assert rows[0]["subscription_cost_per_summary_usd"] == 0.04


# ---------------------------------------------------------------------------
# Covariate "research meat" report
# ---------------------------------------------------------------------------

def _eval_row(doi: str, provider: str, species: str, hallucinations: int) -> dict:
    return {
        "doi": doi,
        "summarizer": provider,
        "hallucination_count": hallucinations,
        "strata": {"species": [species], "study_design": "RCT", "journal": "JVIM"},
    }


def _item_scores_entry(doi: str, provider: str, species: str, unweighted: float) -> tuple:
    key = (doi, "processed")
    strata = {"species": [species], "study_design": "RCT", "journal": "JVIM"}
    return key, {provider: {"unweighted": unweighted, "strata": strata}}


def test_build_covariate_report_hallucination_and_quality() -> None:
    evaluation_rows = [
        _eval_row("10.1/a", "openai", "Canine", 0),
        _eval_row("10.1/b", "openai", "Canine", 1),
        _eval_row("10.1/c", "openai", "Feline", 0),
    ]
    item_scores = dict([
        _item_scores_entry("10.1/a", "openai", "Canine", 5.0),
        _item_scores_entry("10.1/b", "openai", "Canine", 3.0),
        _item_scores_entry("10.1/c", "openai", "Feline", 4.0),
    ])
    report = build_covariate_report(evaluation_rows, [], item_scores, ["openai"])
    canine_cell = next(c for c in report["by_field"]["species"] if c["value"] == "Canine")
    stats = canine_cell["providers"]["openai"]
    assert stats["n_items"] == 2
    assert stats["hallucination_rate"] == 0.5
    assert stats["mean_quality"] == 4.0


def test_build_covariate_report_kappa_underpowered_below_min_items() -> None:
    evaluation_rows = [_eval_row("10.1/a", "openai", "Canine", 0)]
    item_scores = dict([_item_scores_entry("10.1/a", "openai", "Canine", 5.0)])
    human_review_rows = [
        {
            "summarizer": "openai", "strata": {"species": ["Canine"]},
            "human_score_unweighted": 4.0, "llm_jury_score_unweighted": 4.0,
        },
        {
            "summarizer": "openai", "strata": {"species": ["Canine"]},
            "human_score_unweighted": 3.0, "llm_jury_score_unweighted": 3.0,
        },
    ]
    report = build_covariate_report(evaluation_rows, human_review_rows, item_scores, ["openai"])
    canine_cell = next(c for c in report["by_field"]["species"] if c["value"] == "Canine")
    stats = canine_cell["providers"]["openai"]
    assert stats["kappa_n"] == 2
    assert stats["kappa_underpowered"] is True  # 2 < stats_engine.MIN_ITEMS_FOR_KAPPA (5)


def test_build_covariate_report_empty_inputs_yields_empty_tables() -> None:
    report = build_covariate_report([], [], {}, [])
    assert report["fields"] == list(stats_engine.COVARIATE_FIELDS)
    assert all(rows == [] for rows in report["by_field"].values())
