"""
Tests for llm-sum/human_review.py — Phase 5 ingest + agreement/correlation.

Critical assertions:
    Ingest re-joins each item_id to the private un-blinding key and writes
    normalized, un-blinded rows to human_reviews.jsonl; it is idempotent
    (rewrite, not append); blank/invalid/unknown rows are dropped, not fatal.
    Analysis: inter-reviewer agreement appears only with >= 2 reviewers, but
    human-vs-jury correlation is reported even for a lone reviewer.
    All offline — no API is touched.

IN PLAIN ENGLISH
-----------------
This file tests the SECOND half of human_review.py: reading a reviewer's
filled-in scoresheet back in (ingest), and then computing whether reviewers
agreed with each other and with the AI jury (analysis). Like
test_human_review.py, every test here builds small fake data by hand, runs
real human_review.py functions against it, and checks the results with
``assert`` statements — no real reviewers, articles, or AI calls involved.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

import reliability
from human_review import (
    CRITERIA,
    SCORESHEET_FIELDS,
    analyze_human_reviews,
    ingest_human_reviews,
    ingest_main,
    iter_human_review_rows,
)


# ---------------------------------------------------------------------------
# Fixture: a completed export directory (key + filled reviewer scoresheets)
# ---------------------------------------------------------------------------
# In plain English: the three small helper functions below (not tests
# themselves — none start with "test_") build a fake, already-finished
# human-review export: a private unblinding_key.json plus one or more
# already-filled-in scoresheet CSVs, saved into a temporary folder. Every
# real test further down calls one of these to get a ready-made starting
# point instead of repeating the setup each time.

def _identity(doi: str, summarizer: str, *, unweighted: float, faithfulness: int) -> dict:
    """One un-blinding-key entry with the LLM jury's own scores for the item.

    In plain English: builds one fake entry of the "answer key" — as if
    export_human_review had already run and recorded that a given item is
    really (doi, summarizer), with the AI jury having given it some score.
    """
    return {
        "doi": doi,
        "summarizer": summarizer,
        "judge": "anthropic",
        "rubric_version": "vet_medhelm_score_v1.0",
        "input_source": "processed",
        "strata": {"journal": "JVIM", "input_source": "processed"},
        "requires_human_review": False,
        "llm_jury_score": unweighted,
        "llm_jury_score_weighted": unweighted,
        "llm_jury_score_unweighted": unweighted,
        "llm_criteria_scores": {c: {"score": faithfulness if c == "faithfulness" else 4,
                                    "reasoning": "ok"} for c in CRITERIA},
    }


# item_id -> (doi, summarizer, llm unweighted overall, llm faithfulness score)
# In plain English: three fake reviewable items, each already "un-blinded"
# to a different AI provider, each with a different jury score (4, 3, 5) —
# used throughout this file as the shared fake answer key.
_KEY_ITEMS = {
    "item_001": _identity("10.1/d1", "openai", unweighted=4.0, faithfulness=4),
    "item_002": _identity("10.1/d2", "anthropic", unweighted=3.0, faithfulness=3),
    "item_003": _identity("10.1/d3", "gemini", unweighted=5.0, faithfulness=5),
}


def _write_key(review_dir: Path) -> None:
    """Save _KEY_ITEMS to disk as unblinding_key.json, exactly like a real
    export would, so ingest_human_reviews() can find and load it.
    """
    review_dir.mkdir(parents=True, exist_ok=True)
    (review_dir / "unblinding_key.json").write_text(
        json.dumps({"reviewer_count": 2, "seed": 42, "items": _KEY_ITEMS}, indent=2),
        encoding="utf-8",
    )


def _write_scoresheet(review_dir: Path, reviewer_id: int, rows_by_item: dict[str, dict]) -> None:
    """Write a fake "filled-in" scoresheet CSV for one reviewer, as if they
    had opened their Excel file and typed the given scores/notes for each
    item_id. ``rows_by_item`` maps an item_id to a dict of column ->
    value overrides (e.g. {"faithfulness": 4, "comment": "..."})
    — any column not mentioned is left blank, matching a partially-filled
    real sheet.
    """
    reviewer_dir = review_dir / f"reviewer_{reviewer_id}"
    reviewer_dir.mkdir(parents=True, exist_ok=True)
    path = reviewer_dir / f"scoresheet_reviewer_{reviewer_id}.csv"
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=SCORESHEET_FIELDS)
        writer.writeheader()
        for item_id, values in rows_by_item.items():
            row = {field: "" for field in SCORESHEET_FIELDS}
            row["item_id"] = item_id
            row.update({k: str(v) for k, v in values.items()})
            writer.writerow(row)


def _uniform(score: int, **extra) -> dict:
    """A filled scoresheet cell block: the same 1-5 value for all five criteria."""
    row = {c: score for c in CRITERIA}
    row.update(extra)
    return row


def _full_export(tmp_path: Path, *, reviewers: int = 2) -> Path:
    """Two reviewers who both rank the items 4, 3, 5 (perfectly tracking the jury).

    In plain English: builds a complete, ready-to-ingest export folder — the
    key plus one (or more) reviewers' scoresheets, all scoring the three
    fake items exactly the same as the AI jury did. This "perfect agreement"
    setup is the baseline several tests below start from.
    """
    review_dir = tmp_path / "human_review"
    _write_key(review_dir)
    scores = {"item_001": _uniform(4), "item_002": _uniform(3), "item_003": _uniform(5)}
    for reviewer_id in range(1, reviewers + 1):
        _write_scoresheet(review_dir, reviewer_id, scores)
    return review_dir


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------
# In plain English: this section tests ingest_human_reviews() itself — does
# it correctly read scoresheets back in, un-blind them using the key, skip
# what should be skipped, and behave safely when run more than once?

def test_ingest_writes_unblinded_normalized_rows(tmp_path: Path) -> None:
    """After ingesting one reviewer's filled sheet, human_reviews.jsonl
    should contain one row per scored item, each carrying the real identity
    (doi, summarizer) looked up from the key, the reviewer's own average
    score, and the AI jury's own score for later comparison.
    """
    review_dir = _full_export(tmp_path, reviewers=1)
    output = tmp_path / "human_reviews.jsonl"

    result = ingest_human_reviews(review_dir=review_dir, output_path=output)

    assert result.rows_written == 3
    assert result.reviewers == ["reviewer_1"]
    rows = list(iter_human_review_rows(output))
    assert len(rows) == 3
    by_item = {r["item_id"]: r for r in rows}
    # Un-blinded: the normalized row carries the identity from the key.
    assert by_item["item_001"]["doi"] == "10.1/d1"
    assert by_item["item_001"]["summarizer"] == "openai"
    # Human overall is the mean of the filled criteria (all 4s -> 4.0).
    assert by_item["item_001"]["human_score_unweighted"] == 4.0
    # LLM jury's own scores are carried for the later correlation step.
    assert by_item["item_003"]["llm_jury_score_unweighted"] == 5.0


def test_ingest_is_idempotent(tmp_path: Path) -> None:
    """Running ingest twice on the exact same input files should produce a
    byte-for-byte identical output file both times — the file is fully
    rewritten each run, never appended to.
    """
    review_dir = _full_export(tmp_path, reviewers=2)
    output = tmp_path / "human_reviews.jsonl"

    first = ingest_human_reviews(review_dir=review_dir, output_path=output)
    first_text = output.read_text(encoding="utf-8")
    second = ingest_human_reviews(review_dir=review_dir, output_path=output)

    # Rewritten, not appended: same rows, identical file both times.
    assert first.rows_written == second.rows_written == 6
    assert output.read_text(encoding="utf-8") == first_text


def test_ingest_skips_blank_invalid_and_unknown_rows(tmp_path: Path) -> None:
    """Exercises all three "drop this row" cases in one go: a fully blank
    row (not-yet-scored, dropped silently), a row with one invalid score
    (9 is out of the 1-5 range — that one cell is dropped but the rest of
    the row is kept), and a row for an item_id that isn't in the key
    (dropped with a warning). Also checks that the expected warning
    messages were actually produced.
    """
    review_dir = tmp_path / "human_review"
    _write_key(review_dir)
    _write_scoresheet(review_dir, 1, {
        "item_001": _uniform(4),                       # good
        "item_002": {c: "" for c in CRITERIA},         # blank -> skipped
        "item_003": {**_uniform(5), "faithfulness": 9},  # 9 invalid -> ignored, rest kept
        "item_404": _uniform(3),                       # not in key -> skipped
    })
    output = tmp_path / "human_reviews.jsonl"

    result = ingest_human_reviews(review_dir=review_dir, output_path=output)

    by_item = {r["item_id"]: r for r in iter_human_review_rows(output)}
    assert set(by_item) == {"item_001", "item_003"}
    # The one invalid faithfulness cell was dropped; the other four kept.
    assert "faithfulness" not in by_item["item_003"]["criteria_scores"]
    assert len(by_item["item_003"]["criteria_scores"]) == 4
    assert any("not a 1-5 score" in w for w in result.warnings)
    assert any("no matching item" in w for w in result.warnings)


def test_ingest_missing_key_raises(tmp_path: Path) -> None:
    """Trying to ingest a folder with no unblinding_key.json at all should
    fail clearly (FileNotFoundError) rather than silently producing an
    empty or wrong result.
    """
    empty_dir = tmp_path / "human_review"
    empty_dir.mkdir()
    with pytest.raises(FileNotFoundError):
        ingest_human_reviews(review_dir=empty_dir, output_path=tmp_path / "out.jsonl")


def test_ingest_main_returns_error_without_key(tmp_path: Path) -> None:
    """Same missing-key scenario as above, but through the command-line
    entry point (ingest_main) — it should report failure via exit code 1
    instead of letting the exception escape uncaught.
    """
    empty_dir = tmp_path / "human_review"
    empty_dir.mkdir()
    exit_code = ingest_main(["--review-dir", str(empty_dir), "--output", str(tmp_path / "out.jsonl")])
    assert exit_code == 1


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------
# In plain English: this section tests analyze_human_reviews() — the
# function that turns the ingested rows into the actual "do humans agree
# with each other / with the AI jury?" statistics.

def test_analyze_single_reviewer_has_correlation_but_no_agreement(tmp_path: Path) -> None:
    """With only one reviewer, there's no second person to check agreement
    against (inter_reviewer_agreement stays unavailable), but human-vs-jury
    correlation should still be fully computed. Since this reviewer's scores
    (4,3,5) exactly match the jury's, every agreement statistic should come
    out "perfect" (1.0).
    """
    review_dir = _full_export(tmp_path, reviewers=1)
    output = tmp_path / "human_reviews.jsonl"
    ingest_human_reviews(review_dir=review_dir, output_path=output)

    analysis = analyze_human_reviews(iter_human_review_rows(output))

    assert analysis["available"] is True
    assert analysis["n_reviewers"] == 1
    assert analysis["n_items"] == 3
    # One reviewer has no one to agree with.
    assert analysis["inter_reviewer_agreement"]["available"] is False
    # Default mode is per_reviewer: correlation lives under by_reviewer, not
    # a single pooled block. Humans ranked 4,3,5; jury's overalls are 4,3,5.
    overall = analysis["by_reviewer"]["reviewer_1"]["overall"]
    assert overall["n"] == 3
    assert overall["spearman"] == 1.0
    # Cohen's Kappa + percent agreement are categorical companions to Spearman:
    # human and jury both rank 4,3,5 exactly -> perfect categorical agreement too.
    assert overall["cohen_kappa"] == 1.0
    assert overall["percent_agreement"] == 1.0


def test_analyze_two_reviewers_reports_inter_reviewer_alpha(tmp_path: Path) -> None:
    """With two reviewers who scored identically, inter-reviewer agreement
    (Krippendorff's alpha) should now be computable and come out perfect
    (1.0), and each reviewer should still get their own per-criterion
    breakdown.
    """
    review_dir = _full_export(tmp_path, reviewers=2)
    output = tmp_path / "human_reviews.jsonl"
    ingest_human_reviews(review_dir=review_dir, output_path=output)

    analysis = analyze_human_reviews(iter_human_review_rows(output))

    assert analysis["n_reviewers"] == 2
    agreement = analysis["inter_reviewer_agreement"]
    assert agreement["available"] is True
    assert agreement["n_raters"] == 2
    assert agreement["n_comparable_items"] == 3
    # Both reviewers gave identical scores -> perfect inter-reviewer agreement.
    assert agreement["overall"]["krippendorff_alpha"] == 1.0
    # Per-reviewer default: each reviewer has its own criterion breakdown.
    assert set(analysis["by_reviewer"]["reviewer_1"]["per_criterion"]) == set(CRITERIA)


def test_analyze_empty_is_unavailable() -> None:
    """Analyzing an empty list of rows (nothing ingested yet) should report
    itself as unavailable with a helpful, human-readable reason, rather than
    crashing.
    """
    analysis = analyze_human_reviews([])
    assert analysis["available"] is False
    assert "human_reviews.jsonl" in analysis["reason"]


# ---------------------------------------------------------------------------
# Validation mode switch: per_reviewer (default) vs pooled vs both
# ---------------------------------------------------------------------------
# In plain English: analyze_human_reviews() can report human-vs-jury
# correlation in different ways — one number per reviewer ("per_reviewer"),
# one number averaged across all reviewers ("pooled"), or both at once. This
# section proves those modes genuinely behave differently, using a
# deliberately disagreeing pair of fake reviewers so the difference is
# obvious in the numbers.

def _write_disagreeing_two_reviewers(tmp_path: Path) -> Path:
    """Reviewer 1 tracks the jury (4,3,5); reviewer 2 inverts it (2,5,1).

    In plain English: builds a fixture where the two reviewers actively
    disagree with each other — one matches the AI jury's ranking exactly,
    the other ranks the same three items in the opposite order. This is
    what makes it possible to prove, below, that "per_reviewer" mode keeps
    them separate while "pooled" mode blends them together.
    """
    review_dir = tmp_path / "human_review"
    _write_key(review_dir)
    _write_scoresheet(review_dir, 1, {"item_001": _uniform(4), "item_002": _uniform(3),
                                      "item_003": _uniform(5)})
    _write_scoresheet(review_dir, 2, {"item_001": _uniform(2), "item_002": _uniform(5),
                                      "item_003": _uniform(1)})
    return review_dir


def test_per_reviewer_mode_isolates_each_reviewer(tmp_path: Path) -> None:
    """The whole point: one reviewer's disagreement must not dilute the other's.

    Reviewer 1 perfectly tracks the jury; reviewer 2 inverts it. Per-reviewer,
    reviewer 1 still shows a clean +1.0 — it is NOT averaged away.
    """
    output = tmp_path / "human_reviews.jsonl"
    ingest_human_reviews(review_dir=_write_disagreeing_two_reviewers(tmp_path), output_path=output)

    analysis = analyze_human_reviews(iter_human_review_rows(output), mode="per_reviewer")

    assert analysis["human_vs_jury"] is None                 # no pooled block in this mode
    r1 = analysis["by_reviewer"]["reviewer_1"]["overall"]
    r2 = analysis["by_reviewer"]["reviewer_2"]["overall"]
    assert r1["spearman"] == 1.0                              # tracks jury
    assert r2["spearman"] == -1.0                             # inverts jury, undiluted


def test_pooled_mode_averages_reviewers(tmp_path: Path) -> None:
    """In "pooled" mode, the disagreeing reviewers' scores get averaged
    together per item BEFORE correlating with the jury — which washes out
    reviewer 1's perfect agreement, so the pooled correlation is no longer
    a perfect 1.0. There's also no by_reviewer breakdown in this mode.
    """
    output = tmp_path / "human_reviews.jsonl"
    ingest_human_reviews(review_dir=_write_disagreeing_two_reviewers(tmp_path), output_path=output)

    analysis = analyze_human_reviews(iter_human_review_rows(output), mode="pooled")

    assert analysis["by_reviewer"] == {}                     # no per-reviewer block
    # Means per item: item1 (4+2)/2=3, item2 (3+5)/2=4, item3 (5+1)/2=3 -> a
    # flat-ish series that no longer perfectly tracks the jury's 4,3,5.
    assert analysis["human_vs_jury"]["overall"]["spearman"] != 1.0


def test_both_mode_reports_pooled_and_per_reviewer(tmp_path: Path) -> None:
    """"both" mode should include BOTH the pooled human_vs_jury block AND a
    by_reviewer breakdown for every reviewer, at the same time.
    """
    output = tmp_path / "human_reviews.jsonl"
    ingest_human_reviews(review_dir=_full_export(tmp_path, reviewers=2), output_path=output)

    analysis = analyze_human_reviews(iter_human_review_rows(output), mode="both")

    assert analysis["human_vs_jury"] is not None
    assert set(analysis["by_reviewer"]) == {"reviewer_1", "reviewer_2"}


def test_invalid_mode_falls_back_to_default(tmp_path: Path) -> None:
    """Passing a made-up mode string ("bogus") should not crash — it should
    silently fall back to the safe default ("per_reviewer").
    """
    output = tmp_path / "human_reviews.jsonl"
    ingest_human_reviews(review_dir=_full_export(tmp_path, reviewers=1), output_path=output)

    analysis = analyze_human_reviews(iter_human_review_rows(output), mode="bogus")

    assert analysis["mode"] == "per_reviewer"                 # safe fallback, no crash


# ---------------------------------------------------------------------------
# Correlation primitives (reused from reliability.py)
# ---------------------------------------------------------------------------
# In plain English: these tests don't touch human_review.py directly at all
# — they check the underlying statistics building blocks (imported from
# reliability.py) that human_review.py's correlation code depends on, using
# simple hand-checkable number sequences.

def test_pearson_perfect_and_degenerate() -> None:
    """Pearson correlation of a perfectly straight-line relationship should
    be exactly 1.0; it should be undefined (None) when one list has no
    variance at all (every value the same) or when there are fewer than 2
    data points to compare.
    """
    assert reliability.pearson([1, 2, 3], [2, 4, 6]) == 1.0
    assert reliability.pearson([1, 1, 1], [2, 4, 6]) is None  # no variance
    assert reliability.pearson([1], [2]) is None              # < 2 points


def test_spearman_is_rank_based() -> None:
    """Spearman correlation only cares about RANK ORDER, not the exact
    numeric relationship — so a curved-but-always-increasing relationship
    (y = x^2) scores a perfect 1.0 under Spearman even though it's not a
    straight line (so Pearson scores less than 1.0 for the same data).
    """
    # Monotonic-but-nonlinear -> Pearson < 1 while Spearman == 1.
    xs = [1, 2, 3, 4]
    ys = [1, 4, 9, 16]
    assert reliability.spearman(xs, ys) == 1.0
    assert reliability.pearson(xs, ys) < 1.0


def test_correlation_p_values_from_scipy() -> None:
    """A "p-value" is a standard statistics measure of how likely a result
    is to have happened by pure chance — small is "probably not a fluke".
    Checks that a strong, real correlation gets a small p-value, and that
    the same degenerate cases that make the coefficient itself undefined
    (no variance, too few points) also make the p-value undefined.
    """
    # A strong correlation over enough points is significant (small p); the
    # degenerate cases that make the coefficient None make the p-value None too.
    strong_x = [1, 2, 3, 4, 5, 6, 7, 8]
    strong_y = [1, 2, 3, 4, 5, 6, 7, 9]
    p = reliability.pearson_p(strong_x, strong_y)
    assert isinstance(p, float) and 0.0 <= p < 0.05
    sp = reliability.spearman_p(strong_x, strong_y)
    assert isinstance(sp, float) and 0.0 <= sp < 0.05
    assert reliability.pearson_p([1, 1, 1], [2, 4, 6]) is None   # no variance
    assert reliability.pearson_p([1], [2]) is None               # < 2 points
    # Spearman's p-value is undefined at n=2 even though the coefficient is not.
    assert reliability.spearman([1, 2], [3, 4]) is not None
    assert reliability.spearman_p([1, 2], [3, 4]) is None


def test_correlate_dict_carries_p_values() -> None:
    # The human-vs-jury correlation block surfaces both coefficient and p-value.
    from human_review import _correlate
    block = _correlate([1.0, 2.0, 3.0, 4.0, 5.0], [1.0, 2.0, 3.0, 4.0, 5.0])
    assert block["spearman"] == 1.0
    assert block["spearman_p"] is not None
    assert "pearson_p" in block


def test_bland_altman_reports_bias() -> None:
    # Jury is uniformly 0.5 above the human -> mean bias (human - jury) = -0.5.
    result = reliability.bland_altman([4.0, 3.0, 5.0], [4.5, 3.5, 5.5])
    assert result["mean_bias"] == -0.5
    assert result["n"] == 3


# ---------------------------------------------------------------------------
# Research-grade guards: small-sample verdict + both jury modes validated
# ---------------------------------------------------------------------------
# In plain English: this last section checks two "don't fool yourself"
# safety nets that keep the study's conclusions honest — refusing to call a
# tiny sample "proof", and making sure the human-vs-jury check covers BOTH
# ways the jury's score can be computed (plain average and clinical-risk-
# weighted), not just one.

def test_small_sample_correlation_withholds_verdict() -> None:
    """A perfect r on too few items must NOT be reported as validation.

    In plain English: getting a "perfect" correlation from only 3 data
    points is easy to happen by pure luck — it doesn't actually prove
    anything. This checks that reliability.interpret_correlation() knows
    that and labels a too-small sample "Underpowered" instead of boasting
    about the number, even when the number itself (1.0) looks perfect.
    Once there are enough data points (reliability.MIN_CORRELATION_N), the
    same perfect score IS allowed to be called a genuinely strong result.
    """
    assert reliability.MIN_CORRELATION_N > 2
    underpowered = reliability.interpret_correlation(1.0, n=3)
    assert "Underpowered" in underpowered
    # With enough items the real band is used.
    powered = reliability.interpret_correlation(1.0, n=reliability.MIN_CORRELATION_N)
    assert "Strong correlation" in powered


def test_weighted_human_composite_matches_jury_formula(tmp_path: Path) -> None:
    """A fully-filled row's weighted composite equals the jury's own formula.

    In plain English: when a reviewer has filled in all five criteria, the
    human "weighted" overall score human_review.py computes should use the
    EXACT SAME formula (calculate_jury_score with MEDHELM_CRITERION_WEIGHTS)
    the AI jury itself uses — checked here by computing the expected value
    independently and comparing it to what ingest actually produced.
    """
    from evaluator import MEDHELM_CRITERION_WEIGHTS, calculate_jury_score

    review_dir = tmp_path / "human_review"
    _write_key(review_dir)
    scores = {"faithfulness": 5, "completeness": 3, "clinical_usefulness": 4,
              "clarity": 2, "safety": 5}
    _write_scoresheet(review_dir, 1, {"item_001": dict(scores)})
    output = tmp_path / "human_reviews.jsonl"

    ingest_human_reviews(review_dir=review_dir, output_path=output)
    row = next(r for r in iter_human_review_rows(output) if r["item_id"] == "item_001")

    expected = calculate_jury_score({k: float(v) for k, v in scores.items()},
                                    weights=MEDHELM_CRITERION_WEIGHTS)
    assert row["human_score_weighted"] == expected


def test_partial_row_has_no_weighted_composite(tmp_path: Path) -> None:
    """A missing criterion must not deflate the weighted composite -> None.

    In plain English: if a reviewer leaves even one of the five criteria
    blank, the weighted overall score must come back as ``None`` (unknown)
    rather than quietly averaging only the criteria that WERE filled in —
    which would unfairly make an incomplete row look worse (or better) than
    it really is. The plain (unweighted) average, by contrast, is fine to
    compute from just the criteria that were actually filled in.
    """
    review_dir = tmp_path / "human_review"
    _write_key(review_dir)
    partial = {c: 4 for c in CRITERIA if c != "safety"}  # safety left blank
    _write_scoresheet(review_dir, 1, {"item_001": partial})
    output = tmp_path / "human_reviews.jsonl"

    ingest_human_reviews(review_dir=review_dir, output_path=output)
    row = next(r for r in iter_human_review_rows(output) if r["item_id"] == "item_001")

    assert row["human_score_weighted"] is None       # partial -> withheld
    assert row["human_score_unweighted"] == 4.0        # unweighted still averages present ones


def test_analyze_reports_both_jury_modes(tmp_path: Path) -> None:
    """Both the unweighted (MedHELM) and weighted overall correlations appear.

    In plain English: analyze_human_reviews() should report correlation
    against BOTH ways the AI jury's overall score can be computed — the
    plain average ("overall") and the clinical-risk-weighted version
    ("overall_weighted") — so neither version of the jury score goes
    unchecked against the human reviewer.
    """
    review_dir = _full_export(tmp_path, reviewers=1)
    output = tmp_path / "human_reviews.jsonl"
    ingest_human_reviews(review_dir=review_dir, output_path=output)

    # Default per_reviewer mode: read the single reviewer's block.
    hv = analyze_human_reviews(iter_human_review_rows(output))["by_reviewer"]["reviewer_1"]

    assert "overall" in hv and "overall_weighted" in hv
    # All-4/3/5 uniform rows -> weighted and unweighted composites both track the
    # jury's 4/3/5, so both overalls have three comparable items.
    assert hv["overall"]["n"] == 3
    assert hv["overall_weighted"]["n"] == 3
