"""
Tests for llm-sum/evaluator.py.

IN PLAIN ENGLISH: This file is an automated test suite for evaluator.py — a
set of small, self-checking exercises that each set up a scenario, run one
piece of evaluator.py's code, and check ("assert") that the result is what's
expected. Running `pytest tests/test_evaluator.py` runs every function below
whose name starts with `test_`; if any of its assert statements fails, pytest
reports exactly which one and why. None of these tests make real network
calls to OpenAI/Anthropic/Gemini — everything is either dry-run mode (fake,
deterministic responses) or a hand-built ("mocked"/"faked") stand-in for a
real API response, which is what makes it safe to run these automatically.

Critical assertions:
    Blind check: rendered judge prompt contains NONE of the forbidden tokens.
    JSON fallback: response with extra conversational text still yields a score.
    99 sentinel: completely malformed response triggers manual-review flag.
    requires_human_review: confidence < 3 triggers the flag.
    PHASE3_MODE=dev: evaluator honours the dev paper cap.
"""

# Lets type hints be treated as plain text instead of evaluated immediately —
# see the matching comment in evaluator.py for more detail.
from __future__ import annotations

# Standard Python toolkits used across these tests: `json` reads/writes JSON
# text, `Path` represents a file/folder location, and `SimpleNamespace` is a
# quick way to build a throwaway object with whatever attributes you want
# (used below to fake the shape of a real API response object).
import json
import sys
from pathlib import Path
from types import SimpleNamespace

# pytest is the testing framework that discovers and runs every `test_...`
# function in this file, and gives us `pytest.MonkeyPatch` — a tool for
# temporarily changing settings/environment variables/functions for the
# duration of one test, then automatically undoing the change afterward.
import pytest

# Import the module under test, plus the specific functions/constants from it
# that these tests call directly or check the value of.
import evaluator
from evaluator import (
    BLIND_FORBIDDEN_TOKENS,
    JURY_PANEL,
    MEDHELM_CRITERION_WEIGHTS,
    SCORE_SENTINEL_MALFORMED,
    UNWEIGHTED_CRITERION_WEIGHTS,
    build_judge_prompt,
    build_judge_prompt_segments,
    build_evaluation_row,
    aggregate_jury_scores,
    calculate_composite_score,
    calculate_jury_score,
    needs_human_review,
    parse_judge_response,
    resolve_judges,
)


# A "fixture" is pytest's mechanism for reusable test setup/teardown code.
# `autouse=True` means this fixture runs automatically before EVERY test in
# this file, without each test having to ask for it by name. This particular
# one forces dry-run mode on, so no test in this file can accidentally try to
# make a real, paid API call — a belt-and-suspenders safety measure on top of
# each test's own mocking.
@pytest.fixture(autouse=True)
def _force_dry_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """See test_summarizer.py for the rationale — Phase 2's
    test_pdf_validation.py sets DRY_RUN=false at import and leaks it."""
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setattr(evaluator, "DRY_RUN", True)


# ---------------------------------------------------------------------------
# Blind protocol
# These tests check the single most important safety rule in evaluator.py:
# the judge must never be told which AI wrote the summary it's grading.
# ---------------------------------------------------------------------------

# Checks that the rendered judge prompt (the actual text sent to the judge
# AI) never contains any of the forbidden provider-identifying words, no
# matter which reference/candidate text is plugged in.
def test_blind_judge_prompt_contains_no_model_identifiers() -> None:
    reference = "Reference text body about a clinical trial in dogs."
    candidate = "Some candidate summary text claiming X and Y."
    rendered = build_judge_prompt(reference, candidate).lower()
    for forbidden in BLIND_FORBIDDEN_TOKENS:
        assert forbidden not in rendered, (
            f"Judge prompt leaked the token '{forbidden}'. The blind protocol "
            "requires the prompt to be model-agnostic."
        )


# A structural (not just behavioural) guarantee of the blind protocol: this
# inspects build_judge_prompt's own function signature (its list of allowed
# parameter names) using Python's `inspect` module, to prove that there is no
# way to even pass a summariser name into it — the possibility is removed by
# the function's design, not just avoided by convention.
def test_build_judge_prompt_signature_excludes_summariser_name() -> None:
    """The signature must not accept a summariser identifier — that is the
    structural guarantee of the blind protocol."""
    import inspect
    sig = inspect.signature(build_judge_prompt)
    forbidden_params = {"summarizer", "summariser", "model_name", "provider"}
    # `&` between two sets gives their overlap (items in both). If that
    # overlap is empty, none of the forbidden names are accepted parameters.
    assert not (set(sig.parameters) & forbidden_params), (
        "build_judge_prompt must not take a summariser identifier parameter."
    )


# ---------------------------------------------------------------------------
# Phase A1: build_judge_prompt_segments — the segment invariant
# "".join(segments) must equal build_judge_prompt(...) byte-for-byte over
# both the real prompt file and a hand-built whitespace edge case.
# ---------------------------------------------------------------------------

def test_segments_join_to_build_judge_prompt_over_real_template() -> None:
    reference = "Reference text body about a clinical trial in dogs."
    candidate = "Some candidate summary text claiming X and Y."
    segments = build_judge_prompt_segments(reference, candidate)
    assert len(segments) == 3
    assert "".join(segments) == build_judge_prompt(reference, candidate)


def test_segments_join_to_build_judge_prompt_whitespace_edge_case() -> None:
    """No blank lines/whitespace around the placeholders — the split must
    still reproduce the flat render exactly even in a tight layout."""
    template = "RUBRIC{REFERENCE_TEXT}MIDDLE{CANDIDATE_SUMMARY}END"
    reference, candidate = "REF", "CAND"
    segments = build_judge_prompt_segments(reference, candidate, template)
    assert segments == ("RUBRIC", "REFMIDDLE", "CANDEND")
    assert "".join(segments) == build_judge_prompt(reference, candidate, template)


def test_segments_preserve_reference_before_candidate_order() -> None:
    """Finding A: the rubric already puts reference before candidate — the
    split must not reorder them."""
    segments = build_judge_prompt_segments("REF_MARKER", "CAND_MARKER")
    rubric_prefix, reference_block, candidate_block = segments
    assert "REF_MARKER" in reference_block
    assert "CAND_MARKER" in candidate_block
    assert "REF_MARKER" not in rubric_prefix and "REF_MARKER" not in candidate_block
    assert "CAND_MARKER" not in rubric_prefix and "CAND_MARKER" not in reference_block


# ---------------------------------------------------------------------------
# JSON parsing + fallback
# These tests exercise parse_judge_response()'s multi-step fallback chain:
# clean JSON, then the older schema versions, then regex, then the sentinel.
# ---------------------------------------------------------------------------

# Feeds parse_judge_response() a clean JSON response using the OLDER
# "Vet-Score v2.0" 4-dimension field names (factual_accuracy, etc.), to check
# that this older schema is still parsed correctly and the composite score
# formula runs end to end.
def test_clean_json_parsed_directly() -> None:
    """v2 schema: 4 dimension scores → composite computed, quality_score = round(composite)."""
    raw = json.dumps({
        "factual_accuracy": 3, "completeness": 2, "clinical_relevance": 3,
        "organization": 2,
        "hallucination": {"present": True, "count": 1, "claims": [
            {"claim": "X", "source_quote": "Y",
             "category": "omitted_caveat", "severity": "minor"},
        ]},
        "confidence_score": 4,
        "reasoning": "Mostly accurate.",
    })
    parsed = parse_judge_response(raw)
    assert parsed["factual_accuracy"] == 3
    assert parsed["completeness"] == 2
    assert parsed["clinical_relevance"] == 3
    assert parsed["organization"] == 2
    assert parsed["composite_score"] is not None
    assert parsed["quality_score"] == round(parsed["composite_score"])
    assert parsed["hallucination_count"] == 1
    assert parsed["confidence_score"] == 4
    assert parsed["parse_method"] == "json"


# Checks that when the AI wraps its JSON answer in chatty extra sentences
# (as AIs sometimes do despite being asked for pure JSON), the parser still
# finds and reads the embedded {...} block correctly.
def test_json_with_conversational_wrapper_still_parses() -> None:
    raw = ("Sure! Here is the evaluation: "
           '{"quality_score": 7, "hallucination_count": 0, '
           '"hallucination_categories": [], "confidence_score": 5, '
           '"reasoning": "Faithful summary."} '
           "Let me know if you need anything else.")
    parsed = parse_judge_response(raw)
    assert parsed["quality_score"] == 7
    assert parsed["parse_method"] == "json"


# Checks that when the response isn't valid JSON at all (missing braces and
# quotes), the regex fallback step still manages to pull out each named field
# by pattern-matching the raw text directly.
def test_regex_fallback_extracts_when_json_is_broken() -> None:
    # Missing braces / quotes but the named fields are still grep-able.
    raw = ("quality_score: 6, hallucination_count: 2, "
           "hallucination_categories: [fabricated statistics, contradiction], "
           "confidence_score: 3")
    parsed = parse_judge_response(raw)
    assert parsed["quality_score"] == 6
    assert parsed["hallucination_count"] == 2
    assert "fabricated statistics" in parsed["hallucination_categories"]
    assert parsed["confidence_score"] == 3
    assert parsed["parse_method"] == "regex"


# Feeds a response that isn't JSON and doesn't match any regex pattern
# either — the final "we have no idea" case — and checks that it correctly
# produces the SCORE_SENTINEL_MALFORMED (99) flag rather than crashing or
# guessing at a score.
def test_completely_malformed_response_triggers_99_sentinel() -> None:
    raw = "Sorry, I cannot evaluate this summary."
    parsed = parse_judge_response(raw)
    assert parsed["quality_score"] == SCORE_SENTINEL_MALFORMED
    assert parsed["parse_method"] == "sentinel"


# ---------------------------------------------------------------------------
# Trailing-repetition-glitch repair (found live: ~half the "malformed" rows
# in one real Gemini judge batch were this exact pattern — a complete, valid
# MedHELM response corrupted only by a bare repeated fragment of the
# "reasoning" text right after its closing quote, before the final "}").
# Fixtures below are shortened/redacted real examples, not synthetic.
# ---------------------------------------------------------------------------

def _medhelm_json_prefix() -> str:
    """A complete, valid MedHELM object up through confidence_score — the
    part every fixture below shares, so each test only varies the corrupted
    tail after "reasoning"."""
    return (
        '{\n'
        '  "criteria_scores": {\n'
        '    "faithfulness": {"score": 5, "reasoning": "Fully supported."},\n'
        '    "completeness": {"score": 5, "reasoning": "All elements present."},\n'
        '    "clinical_usefulness": {"score": 5, "reasoning": "Clear takeaway."},\n'
        '    "clarity": {"score": 5, "reasoning": "Concise and organized."},\n'
        '    "safety": {"score": 5, "reasoning": "No misleading claims."}\n'
        '  },\n'
        '  "hallucination": {"present": false, "count": 0, "claims": []},\n'
        '  "confidence_score": 5,\n'
    )


def test_repairs_single_duplicated_trailing_fragment() -> None:
    # Real pattern: one echoed fragment of the reasoning text after its
    # closing quote, then the object closes normally.
    raw = (
        _medhelm_json_prefix()
        + '  "reasoning": "Faithful representation of the reference study."\n'
        + 'reference study."\n'
        + '}'
    )
    parsed = parse_judge_response(raw)
    # "json_repaired", not "json" — an audit trail distinguishing a row that
    # needed this repair from one that parsed cleanly on the first try.
    assert parsed["parse_method"] == "json_repaired"
    assert parsed["criteria_scores"]["faithfulness"]["score"] == 5
    assert parsed["confidence_score"] == 5
    assert parsed["reasoning"] == "Faithful representation of the reference study."


def test_repairs_multiple_cascading_duplicated_fragments() -> None:
    # Real pattern: several repeated/mutating echoes stacked before "}".
    raw = (
        _medhelm_json_prefix()
        + '  "reasoning": "Accurate summary with no hallucinations."\n'
        + 'without any hallucinations."\n'
        + 'any hallucinations."\n'
        + 'or overstatements."\n'
        + '}'
    )
    parsed = parse_judge_response(raw)
    assert parsed["parse_method"] == "json_repaired"
    assert parsed["reasoning"] == "Accurate summary with no hallucinations."


def test_repairs_truncated_response_missing_closing_brace() -> None:
    # Real pattern: the response trails off after the reasoning value's
    # closing quote with NO "}" at all — depth never reaches 0, so this
    # exercises the for/else "never balanced" repair path, not the
    # break-on-failed-candidate path the two tests above exercise.
    raw = (
        _medhelm_json_prefix()
        + '  "reasoning": "Excellent, complete representation with zero errors."\n'
        + 'of the reference study with zero errors."'
    )
    parsed = parse_judge_response(raw)
    assert parsed["parse_method"] == "json_repaired"
    assert parsed["reasoning"] == "Excellent, complete representation with zero errors."


def test_repair_does_not_mistake_nested_per_criterion_reasoning_key() -> None:
    # The regression this fix could easily reintroduce: "reasoning" appears
    # once per criterion too (five times) before the real top-level one.
    # Matching the FIRST occurrence instead of the LAST would truncate the
    # object right after "faithfulness"'s own reasoning, discarding every
    # other criterion silently instead of failing loudly.
    raw = (
        _medhelm_json_prefix()
        + '  "reasoning": "Overall assessment text."\n'
        + 'ssessment text."\n'
        + '}'
    )
    parsed = parse_judge_response(raw)
    assert parsed["parse_method"] == "json_repaired"
    # All five criteria must survive — not just the ones before the first
    # "reasoning" occurrence.
    assert set(parsed["criteria_scores"]) == {
        "faithfulness", "completeness", "clinical_usefulness", "clarity", "safety",
    }
    assert parsed["reasoning"] == "Overall assessment text."


def test_repair_returns_none_when_response_has_no_reasoning_key_at_all() -> None:
    # Guards the repair function's own None-return path: a response that's
    # broken for some OTHER reason (no "reasoning" key present) must still
    # fall through to the existing sentinel path, not raise.
    raw = '{"criteria_scores": {"faithfulness": {"score": 5' # truncated, no "reasoning" anywhere
    parsed = parse_judge_response(raw)
    assert parsed["parse_method"] == "sentinel"


# ---------------------------------------------------------------------------
# requires_human_review flag
# Each test below builds a small fake "parsed" dict by hand (rather than
# going through parse_judge_response) so it can check exactly one trigger
# condition of needs_human_review() in isolation.
# ---------------------------------------------------------------------------

def test_requires_human_review_on_low_confidence() -> None:
    parsed = {"quality_score": 7, "confidence_score": 2}
    assert needs_human_review(parsed) is True


def test_requires_human_review_on_sentinel_score() -> None:
    parsed = {"quality_score": SCORE_SENTINEL_MALFORMED, "confidence_score": 5}
    assert needs_human_review(parsed) is True


def test_does_not_require_review_on_good_response() -> None:
    parsed = {"quality_score": 9, "confidence_score": 5}
    assert needs_human_review(parsed) is False


# ---------------------------------------------------------------------------
# Evaluation row construction
# ---------------------------------------------------------------------------

# Builds a full evaluation row from a fake judge response and checks: the
# summariser name is kept as metadata on the row (for later joining/
# analysis) but never leaks into the actual judge prompt text — the same
# blind-protocol guarantee tested above, now checked at the full-row level.
def test_build_evaluation_row_keeps_summariser_metadata_only() -> None:
    judge_response = {
        "raw_text": json.dumps({
            "factual_accuracy": 3, "completeness": 3,
            "clinical_relevance": 3, "organization": 3,
            "hallucination": {"present": False, "count": 0, "claims": []},
            "confidence_score": 5, "reasoning": "ok",
        }),
        "input_tokens": 1500, "output_tokens": 200,
        "model_version": "gpt-5.4-test",
        "system_fingerprint": "fp-test",
    }
    row = build_evaluation_row(
        doi="10.1111/jvim.16872",
        summariser="anthropic",
        judge="openai",
        reference_text="Reference body.",
        candidate_summary="Candidate body.",
        judge_response=judge_response,
    )
    # Summariser is preserved as metadata for joining ...
    assert row["summarizer"] == "anthropic"
    assert row["system_fingerprint"] == "fp-test"
    # ... new v2 fields are present ...
    assert row["composite_score"] is not None
    assert row["rubric_version"] == "vet_medhelm_score_v1.0"
    assert "automatic_metrics" in row
    # ... but appears in NO other field of the row.
    #
    # This is the row-level half of the blind protocol. The template-level half
    # is covered above by test_blind_judge_prompt_contains_no_model_identifiers
    # (behavioural) and ..._signature_excludes_summariser_name (structural).
    # Scanning the row itself is what catches the remaining leak vector: a
    # summariser name copied into reasoning, strata, or an excerpt would ride
    # along into any downstream re-render of the judged text.
    #
    # Note this deliberately scans `row`, not a freshly built prompt from two
    # literals — a prompt built from strings that never contained the token
    # cannot fail, so it would assert nothing about build_evaluation_row.
    leaked = sorted(
        key for key, value in row.items()
        if key != "summarizer" and "anthropic" in json.dumps(value).lower()
    )
    assert not leaked, (
        f"Summariser identity leaked out of metadata into: {leaked}. "
        "Only row['summarizer'] may name the summariser."
    )


# Checks that the dry-run fake judge response is deterministic: calling it
# twice with the exact same inputs produces the exact same fake output, which
# is what makes dry-run tests repeatable instead of randomly flaky.
def test_mock_judge_response_is_stable() -> None:
    first = evaluator._mock_judge_response(
        "openai", "Reference body.", "Candidate summary."
    )
    second = evaluator._mock_judge_response(
        "openai", "Reference body.", "Candidate summary."
    )
    assert first["raw_text"] == second["raw_text"]


# Feeds the CURRENT MedHELM-style schema (five named criteria, each with its
# own score and reasoning) and checks that both the weighted and unweighted
# jury scores are computed correctly, and that the "primary" jury_score field
# matches whichever aggregation mode is active by default (unweighted).
def test_medhelm_schema_parses_criteria_scores() -> None:
    raw = json.dumps({
        "criteria_scores": {
            "faithfulness": {"score": 5, "reasoning": "Supported."},
            "completeness": {"score": 4, "reasoning": "One minor omission."},
            "clinical_usefulness": {"score": 5, "reasoning": "Useful."},
            "clarity": {"score": 4, "reasoning": "Readable."},
            "safety": {"score": 5, "reasoning": "No safety concern."},
        },
        "hallucination": {"present": False, "count": 0, "claims": []},
        "confidence_score": 5,
        "reasoning": "Strong summary.",
    })
    parsed = parse_judge_response(raw)
    assert parsed["criteria_scores"]["faithfulness"]["score"] == 5
    expected_weighted = calculate_jury_score(
        parsed["criteria_scores"], weights=MEDHELM_CRITERION_WEIGHTS
    )
    expected_unweighted = calculate_jury_score(
        parsed["criteria_scores"], weights=UNWEIGHTED_CRITERION_WEIGHTS
    )
    assert parsed["jury_score_weighted"] == expected_weighted
    assert parsed["jury_score_unweighted"] == expected_unweighted
    assert parsed["jury_score_unweighted"] == round((5 + 4 + 5 + 4 + 5) / 5, 2)
    # Default JURY_AGGREGATION_MODE is "unweighted" (stock MedHELM's flat mean).
    assert evaluator.JURY_AGGREGATION_MODE == "unweighted"
    assert parsed["jury_score"] == expected_unweighted
    assert parsed["jury_aggregation_mode"] == "unweighted"
    assert parsed["quality_score"] == 9
    assert parsed["parse_method"] == "json"


# `monkeypatch.setattr(...)` temporarily overwrites a module-level value
# (here, evaluator.JURY_AGGREGATION_MODE) for the duration of this one test
# only; pytest automatically restores the original value afterward, so this
# override can never leak into other tests.
def test_jury_aggregation_mode_selects_primary_score(monkeypatch: pytest.MonkeyPatch) -> None:
    """JURY_AGGREGATION_MODE=weighted makes the weighted score primary."""
    monkeypatch.setattr(evaluator, "JURY_AGGREGATION_MODE", "weighted")
    raw = json.dumps({
        "criteria_scores": {
            "faithfulness": {"score": 5, "reasoning": "x"},
            "completeness": {"score": 1, "reasoning": "x"},
            "clinical_usefulness": {"score": 5, "reasoning": "x"},
            "clarity": {"score": 1, "reasoning": "x"},
            "safety": {"score": 5, "reasoning": "x"},
        },
        "hallucination": {"present": False, "count": 0, "claims": []},
        "confidence_score": 5,
        "reasoning": "Mixed scores.",
    })
    parsed = parse_judge_response(raw)
    assert parsed["jury_aggregation_mode"] == "weighted"
    assert parsed["jury_score"] == parsed["jury_score_weighted"]
    assert parsed["jury_score"] != parsed["jury_score_unweighted"]


# `monkeypatch.setenv(...)` temporarily sets an environment variable (as if
# it were written in .env) for just this test, automatically undone
# afterward — the environment-variable equivalent of monkeypatch.setattr.
def test_jury_criterion_weights_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """A valid JURY_CRITERION_WEIGHTS JSON overrides the hardcoded defaults."""
    import importlib

    custom = {
        "faithfulness": 2.0, "completeness": 2.0, "clinical_usefulness": 2.0,
        "clarity": 2.0, "safety": 2.0,
    }
    monkeypatch.setenv("JURY_CRITERION_WEIGHTS", json.dumps(custom))
    weights = evaluator._load_criterion_weights()
    assert weights == custom


# Checks two separate broken-input cases in one test: a JSON object missing
# some of the five required criteria, and text that isn't valid JSON at all.
# Both should silently fall back to the safe defaults rather than crashing or
# producing a partially-broken weights dict.
def test_jury_criterion_weights_env_override_invalid_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Invalid or incomplete JSON falls back to the documented defaults."""
    monkeypatch.setenv("JURY_CRITERION_WEIGHTS", '{"faithfulness": 2.0}')
    weights = evaluator._load_criterion_weights()
    assert weights == evaluator._DEFAULT_MEDHELM_CRITERION_WEIGHTS

    monkeypatch.setenv("JURY_CRITERION_WEIGHTS", "not json")
    weights = evaluator._load_criterion_weights()
    assert weights == evaluator._DEFAULT_MEDHELM_CRITERION_WEIGHTS


# Three fake judge rows for the same paper (one with a missing score) — check
# that the average, judge count, and "valid" judge count (excluding the
# missing one) all come out correctly.
def test_aggregate_jury_scores_tracks_disagreement() -> None:
    rows = [{"jury_score": 4.5}, {"jury_score": 3.5}, {"jury_score": None}]
    aggregate = aggregate_jury_scores(rows)
    assert aggregate["jury_score"] == 4.0
    assert aggregate["judge_count"] == 3
    assert aggregate["valid_judge_count"] == 2
    assert aggregate["judge_disagreement"] == 1.0


def test_aggregate_jury_scores_preserves_both_modes() -> None:
    """Cross-judge aggregation keeps weighted AND unweighted means, each with
    its own disagreement spread, so switching JURY_AGGREGATION_MODE after a
    multi-judge run never needs the judges re-run."""
    rows = [
        {"jury_score": 4.0, "jury_score_weighted": 4.2, "jury_score_unweighted": 4.0},
        {"jury_score": 3.0, "jury_score_weighted": 2.8, "jury_score_unweighted": 3.0},
    ]
    aggregate = aggregate_jury_scores(rows)
    assert aggregate["jury_score"] == 3.5
    assert aggregate["jury_score_weighted_mean"] == 3.5
    assert aggregate["jury_score_unweighted_mean"] == 3.5
    # Weighted judges disagreed more (4.2 vs 2.8) than unweighted (4.0 vs 3.0).
    assert aggregate["judge_disagreement_weighted"] == 1.4
    assert aggregate["judge_disagreement_unweighted"] == 1.0


def test_aggregate_jury_scores_empty_rows_returns_nones() -> None:
    aggregate = aggregate_jury_scores([{"jury_score": None}])
    assert aggregate["jury_score"] is None
    assert aggregate["valid_judge_count"] == 0
    assert aggregate["judge_disagreement"] is None
    assert aggregate["jury_score_weighted_mean"] is None
    assert aggregate["judge_disagreement_unweighted"] is None


# ---------------------------------------------------------------------------
# Judge selection: --judges / --jury / JURY_PRESET / JUDGE_MODELS
# Each test below checks one rung of resolve_judges()'s priority ladder
# (explicit list > --jury flag > JURY_PRESET > JUDGE_MODELS default).
# ---------------------------------------------------------------------------

def test_resolve_judges_defaults_to_judge_models(monkeypatch: pytest.MonkeyPatch) -> None:
    """No CLI flag and no preset → whatever JUDGE_MODELS holds."""
    monkeypatch.delenv("JURY_PRESET", raising=False)
    monkeypatch.setattr(evaluator, "JUDGE_MODELS", ["openai"])
    assert resolve_judges(None, jury=False) == ["openai"]


def test_resolve_judges_default_is_panel(monkeypatch: pytest.MonkeyPatch) -> None:
    """The system default (no CLI flag, no preset) is the full 3-judge panel."""
    monkeypatch.delenv("JURY_PRESET", raising=False)
    monkeypatch.setattr(evaluator, "JUDGE_MODELS", list(JURY_PANEL))
    assert resolve_judges(None, jury=False) == ["openai", "anthropic", "gemini"]


def test_resolve_judges_jury_flag_expands_to_panel() -> None:
    assert resolve_judges(None, jury=True) == JURY_PANEL
    assert resolve_judges(None, jury=True) == ["openai", "anthropic", "gemini"]


def test_resolve_judges_explicit_list_overrides_everything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit --judges beats --jury and JURY_PRESET."""
    monkeypatch.setenv("JURY_PRESET", "panel")
    assert resolve_judges("anthropic", jury=True) == ["anthropic"]


def test_resolve_judges_preset_panel(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JURY_PRESET", "panel")
    assert resolve_judges(None, jury=False) == JURY_PANEL


def test_resolve_judges_preset_duo(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JURY_PRESET", "duo")
    assert resolve_judges(None, jury=False) == ["openai", "anthropic"]


def test_resolve_judges_preset_solo(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JURY_PRESET", "solo")
    assert resolve_judges(None, jury=False) == ["openai"]


def test_resolve_judges_unknown_preset_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JURY_PRESET", "quintet")
    monkeypatch.setattr(evaluator, "JUDGE_MODELS", ["openai"])
    assert resolve_judges(None, jury=False) == ["openai"]


# Replaces evaluator._call_judge_openai with a small stand-in function (a
# "fake"/"mock") that records what message it was called with and returns a
# canned response, instead of making a real network call. This lets the test
# verify call_judge()'s wiring (does it build the prompt and route to the
# right provider function?) without ever touching the real OpenAI API — the
# core technique used throughout this file to keep tests safe and fast.
def test_call_judge_uses_call_time_dry_run(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("PHASE3_MODE", "single")
    monkeypatch.setattr(evaluator, "DRY_RUN", True)

    captured = {}

    def _fake_openai(segments, *, article_id=None) -> dict:
        captured["segments"] = segments
        captured["article_id"] = article_id
        return {
            "raw_text": json.dumps({
                "quality_score": 8,
                "hallucination_count": 0,
                "hallucination_categories": [],
                "confidence_score": 4,
                "reasoning": "ok",
            }),
            "input_tokens": 10,
            "output_tokens": 5,
            "model_version": "gpt-test",
        }

    monkeypatch.setattr(evaluator, "_call_judge_openai", _fake_openai)
    response = evaluator.call_judge(
        "openai", "Reference body.", "Candidate body.", max_retries=1
    )
    assert response["model_version"] == "gpt-test"
    # Segmented on every path (Phase A1): the reference text lands in the
    # reference block, not glued into one flat string.
    rubric_prefix, reference_block, candidate_block = captured["segments"]
    assert "Reference body." in reference_block
    assert "Candidate body." in candidate_block


# A "class" is a blueprint for creating objects that bundle together data and
# the functions that act on that data — Python's core building block for
# "object-oriented" code. Builds small fake stand-in classes that mimic the
# shape of the real Google Gemini library's objects (Client, its
# .models.generate_content(...) method,
# and the config object), then swaps them in for the real library. This lets
# the test check that _call_judge_gemini() calls the library correctly
# (right API key, right settings) without needing the real google-genai
# package installed or a real network call. `**kwargs` here means "accept any
# number of named arguments and collect them into a dict called kwargs" —
# used so these fakes can accept whatever arguments the real call passes.
def test_gemini_judge_uses_google_genai(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}

    class _FakeGenerateContentConfig:
        def __init__(self, **kwargs):
            captured["config"] = kwargs

    class _FakeModels:
        def generate_content(self, **kwargs):
            captured["kwargs"] = kwargs
            return SimpleNamespace(
                text=json.dumps({
                    "quality_score": 7,
                    "hallucination_count": 0,
                    "hallucination_categories": [],
                    "confidence_score": 4,
                    "reasoning": "ok",
                }),
                usage_metadata=SimpleNamespace(
                    prompt_token_count=123,
                    candidates_token_count=45,
                ),
            )

    class _FakeClient:
        def __init__(self, api_key=None):
            captured["api_key"] = api_key
            self.models = _FakeModels()

    fake_genai = SimpleNamespace(Client=_FakeClient)
    fake_types = SimpleNamespace(GenerateContentConfig=_FakeGenerateContentConfig)

    def _fake_import(name: str):
        if name == "google.genai":
            return fake_genai
        if name == "google.genai.types":
            return fake_types
        raise ImportError(name)

    monkeypatch.setattr(evaluator.importlib, "import_module", _fake_import)
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")

    segments = ("Rubric.", "Reference text.", "Candidate summary.")
    result = evaluator._call_judge_gemini(segments)

    assert captured["api_key"] == "test-key"
    # Segmented on every path (Phase A1): three ordered parts, matching
    # batch_utils.build_gemini_batch_request's for_judge=True shape exactly.
    assert captured["kwargs"]["contents"] == [{"parts": [
        {"text": "Rubric."},
        {"text": "Reference text."},
        {"text": "Candidate summary."},
    ]}]
    assert captured["config"]["response_mime_type"] == "application/json"
    assert result["input_tokens"] == 123
    assert result["output_tokens"] == 45
    assert result["judge_prompt_shape"] == "segmented_v1"


# ---------------------------------------------------------------------------
# Phase A2/A3: Anthropic cache accounting + cache markers (flag-gated)
# ---------------------------------------------------------------------------

def _fake_anthropic_module(*, cache_creation: int = 0, cache_read: int = 0,
                           captured: dict) -> SimpleNamespace:
    """Build a fake `anthropic` module whose Messages.create() records its
    kwargs and returns a synthetic usage block with the given cache counts."""

    class _FakeContentBlock:
        def __init__(self, text: str):
            self.type = "text"
            self.text = text

    class _FakeMessages:
        def create(self, **kwargs):
            captured["kwargs"] = kwargs
            usage = SimpleNamespace(
                input_tokens=100,
                output_tokens=50,
                cache_creation_input_tokens=cache_creation,
                cache_read_input_tokens=cache_read,
            )
            return SimpleNamespace(
                content=[_FakeContentBlock(json.dumps({
                    "quality_score": 8, "hallucination_count": 0,
                    "hallucination_categories": [], "confidence_score": 4,
                    "reasoning": "ok",
                }))],
                usage=usage,
                model="claude-test",
            )

    class _FakeAnthropic:
        def __init__(self, *a, **kw):
            self.messages = _FakeMessages()

    return SimpleNamespace(Anthropic=_FakeAnthropic)


def test_anthropic_judge_cache_accounting_sums_all_three(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verification #6: synthetic usage {input:100, cache_creation:900,
    cache_read:8000} must record input_tokens == 9000 (the sum of all
    three) — Anthropic's usage.input_tokens alone is the UNCACHED remainder."""
    captured: dict = {}
    fake_anthropic = _fake_anthropic_module(
        cache_creation=900, cache_read=8000, captured=captured,
    )
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic)

    segments = ("Rubric.", "Reference text.", "Candidate summary.")
    result = evaluator._call_judge_anthropic(segments)

    assert result["input_tokens"] == 9000
    assert result["cache_creation_input_tokens"] == 900
    assert result["cache_read_input_tokens"] == 8000
    assert result["judge_prompt_shape"] == "segmented_v1"


def test_anthropic_judge_cache_markers_absent_when_flag_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Flag off (default): segmented blocks are sent, but with no
    cache_control marker anywhere — verification #4."""
    captured: dict = {}
    fake_anthropic = _fake_anthropic_module(captured=captured)
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic)
    monkeypatch.setattr(evaluator, "PROMPT_CACHE_ENABLED", False)

    segments = ("Rubric.", "Reference text.", "Candidate summary.")
    evaluator._call_judge_anthropic(segments)

    system_blocks = captured["kwargs"]["system"]
    content_blocks = captured["kwargs"]["messages"][0]["content"]
    assert system_blocks == [{"type": "text", "text": "Rubric."}]
    assert content_blocks == [
        {"type": "text", "text": "Reference text."},
        {"type": "text", "text": "Candidate summary."},
    ]
    assert "cache_control" not in system_blocks[0]
    assert "cache_control" not in content_blocks[0]
    assert "cache_control" not in content_blocks[1]


def test_anthropic_judge_cache_markers_land_only_on_system_and_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Flag on: exactly two cache_control markers (system, reference) —
    NEVER on the candidate block. Verification #5. Text and block
    partitioning are otherwise identical to the flag-off case (verification #4)."""
    captured: dict = {}
    fake_anthropic = _fake_anthropic_module(captured=captured)
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic)
    monkeypatch.setattr(evaluator, "PROMPT_CACHE_ENABLED", True)

    segments = ("Rubric.", "Reference text.", "Candidate summary.")
    evaluator._call_judge_anthropic(segments)

    system_blocks = captured["kwargs"]["system"]
    content_blocks = captured["kwargs"]["messages"][0]["content"]
    # Same text/blocks as the flag-off case — only markers differ.
    assert [b["text"] for b in system_blocks] == ["Rubric."]
    assert [b["text"] for b in content_blocks] == ["Reference text.", "Candidate summary."]

    markers = [b for b in (system_blocks + content_blocks) if "cache_control" in b]
    assert len(markers) == 2
    assert "cache_control" in system_blocks[0]
    assert "cache_control" in content_blocks[0]      # reference block
    assert "cache_control" not in content_blocks[1]  # candidate block — never cached


# ---------------------------------------------------------------------------
# Phase A1: extended blind-protocol scan — walks the ACTUAL structured
# request each provider receives (Anthropic system list + content blocks,
# OpenAI system+user messages, every Gemini part), not just a flat string.
# ---------------------------------------------------------------------------

def _scan_blocks_for_forbidden_tokens(value: object) -> list[str]:
    text = json.dumps(value).lower()
    return [token for token in BLIND_FORBIDDEN_TOKENS if token in text]


def test_anthropic_judge_request_blocks_carry_no_summariser_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}
    fake_anthropic = _fake_anthropic_module(captured=captured)
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic)

    segments = build_judge_prompt_segments(
        "Reference text body about a clinical trial in dogs.",
        "Some candidate summary text claiming X and Y.",
    )
    evaluator._call_judge_anthropic(segments)

    assert not _scan_blocks_for_forbidden_tokens(captured["kwargs"]["system"])
    assert not _scan_blocks_for_forbidden_tokens(captured["kwargs"]["messages"])


def test_gemini_judge_request_parts_carry_no_summariser_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    class _FakeModels:
        def generate_content(self, **kwargs):
            captured["kwargs"] = kwargs
            return SimpleNamespace(
                text=json.dumps({
                    "quality_score": 7, "hallucination_count": 0,
                    "hallucination_categories": [], "confidence_score": 4,
                    "reasoning": "ok",
                }),
                usage_metadata=SimpleNamespace(prompt_token_count=1, candidates_token_count=1),
            )

    class _FakeClient:
        def __init__(self, api_key=None):
            self.models = _FakeModels()

    fake_genai = SimpleNamespace(Client=_FakeClient)
    fake_types = SimpleNamespace(GenerateContentConfig=lambda **kw: SimpleNamespace(**kw))

    def _fake_import(name: str):
        if name == "google.genai":
            return fake_genai
        if name == "google.genai.types":
            return fake_types
        raise ImportError(name)

    monkeypatch.setattr(evaluator.importlib, "import_module", _fake_import)

    segments = build_judge_prompt_segments(
        "Reference text body about a clinical trial in dogs.",
        "Some candidate summary text claiming X and Y.",
    )
    evaluator._call_judge_gemini(segments)

    assert not _scan_blocks_for_forbidden_tokens(captured["kwargs"]["contents"])


# ---------------------------------------------------------------------------
# Phase A2: absent-vs-zero cache fields on build_evaluation_row
# ---------------------------------------------------------------------------

def test_build_evaluation_row_cache_fields_absent_when_judge_response_lacks_them() -> None:
    """Verification #7: a judge_response with no cache keys at all (as every
    pre-Phase-A2 caller's response shape looked) must load as None on the
    row, not 0 — None means 'this predates cache-token accounting'."""
    judge_response = {
        "raw_text": json.dumps({
            "quality_score": 8, "hallucination_count": 0,
            "hallucination_categories": [], "confidence_score": 4, "reasoning": "ok",
        }),
        "input_tokens": 100, "output_tokens": 20, "model_version": "gpt-test",
    }
    row = build_evaluation_row(
        doi="10.1111/example", summariser="openai", judge="anthropic",
        reference_text="Reference.", candidate_summary="Candidate.",
        judge_response=judge_response,
    )
    assert row["cache_read_input_tokens"] is None
    assert row["cache_creation_input_tokens"] is None
    assert row["judge_prompt_shape"] is None


def test_build_evaluation_row_cache_fields_zero_when_attempted_and_missed() -> None:
    """A judge_response carrying explicit 0s (caching existed, this call
    missed) must stay 0 on the row — distinct from the absent case above."""
    judge_response = {
        "raw_text": json.dumps({
            "quality_score": 8, "hallucination_count": 0,
            "hallucination_categories": [], "confidence_score": 4, "reasoning": "ok",
        }),
        "input_tokens": 100, "output_tokens": 20, "model_version": "gpt-test",
        "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
        "judge_prompt_shape": "segmented_v1",
    }
    row = build_evaluation_row(
        doi="10.1111/example", summariser="openai", judge="anthropic",
        reference_text="Reference.", candidate_summary="Candidate.",
        judge_response=judge_response,
    )
    assert row["cache_read_input_tokens"] == 0
    assert row["cache_creation_input_tokens"] == 0
    assert row["judge_prompt_shape"] == "segmented_v1"


# ---------------------------------------------------------------------------
# PHASE3_MODE=dev paper cap
# ---------------------------------------------------------------------------

# Builds a fake summaries.jsonl file (in a temporary, throwaway test folder —
# `tmp_path`, a pytest built-in that gives each test its own private folder
# that's cleaned up afterward) with 5 fake papers, then runs the real
# run_evaluation() loop in dev mode with a cap of 2, and checks that only 2
# actually got evaluated and written out — proving the paper-count cap is
# respected end to end, not just checked in isolation.
def test_dev_mode_caps_paper_count(tmp_path: Path,
                                    monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Replaces the old DEVELOPMENT_MODE cap test. Build fake summaries with
    5 papers; with PHASE3_MODE=dev and PHASE3_DEV_LIMIT=2 the evaluator
    should only process 2.
    """
    import prepare_texts
    from phase3_mode import resolve_mode

    monkeypatch.setenv("PHASE3_MODE", "dev")
    monkeypatch.setenv("PHASE3_DEV_LIMIT", "2")

    profile = resolve_mode()
    assert profile.paper_limit == 2

    summaries_path = tmp_path / "summaries.jsonl"
    evaluations_path = tmp_path / "evaluations.jsonl"
    processed_dir = tmp_path / "processed"
    processed_dir.mkdir()

    from file_paths import doi_to_slug

    # Build 5 fake papers, each with a matching cached "reference text" file
    # and a fake OpenAI summary entry, written as JSONL (one JSON object per
    # line) so the real evaluator code can read them exactly as if they were
    # produced by a real summarizer.py run.
    with open(summaries_path, "w", encoding="utf-8") as f:
        for i in range(5):
            doi = f"10.9999/eval.{i:04d}"
            slug = doi_to_slug(doi)
            entry = {"doi": doi, "slug": slug, "text": "Reference body content. " * 30}
            # Cache file under legacy slug name. The summary entry below
            # has no journal/title, so the descriptive path resolves back
            # to the legacy slug naming and this file is found.
            (processed_dir / f"{slug}.jsonl").write_text(
                json.dumps(entry) + "\n", encoding="utf-8")
            f.write(json.dumps({
                "doi": doi,
                "custom_id": slug,
                "models": {
                    "openai": {
                        "status": "success",
                        "summary": f"summary for paper {i}",
                        "model_version": "gpt-5.4-test",
                        "input_tokens": 100, "output_tokens": 50,
                    }
                }
            }) + "\n")

    monkeypatch.setattr(evaluator, "SUMMARIES_PATH", summaries_path)
    monkeypatch.setattr(evaluator, "EVALUATIONS_PATH", evaluations_path)
    monkeypatch.setattr(evaluator, "PROCESSED_DIR", processed_dir)
    monkeypatch.setattr(prepare_texts, "PROCESSED_DIR", processed_dir)

    counts = evaluator.run_evaluation(
        judges=["openai"], resume=False,
        paper_limit=profile.paper_limit,
    )

    assert counts["evaluated"] == 2  # 2 papers × 1 summariser × 1 judge
    # This is a dry-run (PHASE3_MODE=test), so the mock rows are redirected
    # away from the production ledger and land in the _dryrun sibling. The
    # production file must stay untouched — that is the whole point of the
    # guard added after the 2026-07-09 contamination.
    assert not evaluations_path.exists()
    dry_run_path = evaluations_path.with_name("evaluations_dryrun.jsonl")
    lines = dry_run_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2


# Checks the `instances=` override path: passes a pre-built instance list
# straight into run_evaluation() and confirms it never even tries to read
# summaries.jsonl (the file is asserted not to exist) — proving that the
# judge loop's actual grading logic works the same regardless of where the
# list of things-to-judge came from.
def test_run_evaluation_with_explicit_instances_bypasses_summaries_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The `instances` override lets a caller (run_phase3.py's alternate
    EVAL_INPUT_MODE) judge a pre-built instance list without evaluator.py
    ever reading summaries.jsonl — proving the judge loop itself is unchanged
    regardless of where instances come from."""
    from eval_instances import EvaluationInstance

    missing_summaries_path = tmp_path / "does_not_exist.jsonl"
    evaluations_path = tmp_path / "evaluations.jsonl"
    monkeypatch.setattr(evaluator, "SUMMARIES_PATH", missing_summaries_path)
    monkeypatch.setattr(evaluator, "EVALUATIONS_PATH", evaluations_path)

    instances = [
        EvaluationInstance(
            doi="10.9999/txt.0001",
            summarizer="openai",
            reference_text="Reference body. " * 30,
            candidate_summary="A candidate summary.",
            input_source="processed",
            strata={"input_source": "processed"},
            summary_record={"doi": "10.9999/txt.0001"},
            manifest_record={},
        ),
    ]

    counts = evaluator.run_evaluation(judges=["openai"], resume=False, instances=instances)

    assert counts["evaluated"] == 1
    assert not missing_summaries_path.exists()  # never created or read
    # Dry-run rows go to the _dryrun sibling, never the production ledger.
    assert not evaluations_path.exists()
    evaluations_path = evaluations_path.with_name("evaluations_dryrun.jsonl")
    rows = [json.loads(line) for line in evaluations_path.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    assert rows[0]["doi"] == "10.9999/txt.0001"
    assert rows[0]["summarizer"] == "openai"


# ---------------------------------------------------------------------------
# Composite score (Vet-Score v2.0)
# These tests check the OLDER calculate_composite_score() formula (see the
# "IN PLAIN ENGLISH" note on that function in evaluator.py) against known,
# hand-worked-out expected results, including edge cases at both ends of the
# scale and out-of-range inputs.
# ---------------------------------------------------------------------------

def test_composite_score_all_threes() -> None:
    """Max possible scores → normalized to 10.0."""
    result = calculate_composite_score(
        {"factual_accuracy": 3, "completeness": 3, "clinical_relevance": 3, "organization": 3}
    )
    assert result == 10.0


def test_composite_score_all_ones() -> None:
    """Min possible dimension scores → normalized to 4.0.

    composite = (1×1.5)+(1×1.0)+(1×1.2)+(1×0.8) = 4.5
    normalized = (4.5/13.5) × 9 + 1 = 4.0
    Note: the formula range is [4.0, 10.0], not [1.0, 10.0].
    """
    result = calculate_composite_score(
        {"factual_accuracy": 1, "completeness": 1, "clinical_relevance": 1, "organization": 1}
    )
    assert result == 4.0


def test_composite_score_formula() -> None:
    """Verify the exact weighted formula with known values.

    FA=2, C=1, CR=3, O=2:
      composite = (2×1.5) + (1×1.0) + (3×1.2) + (2×0.8)
               = 3.0 + 1.0 + 3.6 + 1.6 = 9.2
      normalized = (9.2/13.5) × 9 + 1 ≈ 7.13
    """
    result = calculate_composite_score(
        {"factual_accuracy": 2, "completeness": 1, "clinical_relevance": 3, "organization": 2}
    )
    expected = round((9.2 / 13.5) * 9 + 1, 2)
    assert result == expected


def test_composite_score_clamps_out_of_range() -> None:
    """LLM returning 0 or 5 on a 1-3 scale should not corrupt the composite."""
    result_low  = calculate_composite_score(
        {"factual_accuracy": 0, "completeness": 0, "clinical_relevance": 0, "organization": 0}
    )
    result_high = calculate_composite_score(
        {"factual_accuracy": 5, "completeness": 5, "clinical_relevance": 5, "organization": 5}
    )
    # Clamped to 1 and 3 respectively → same as all-ones (4.0) and all-threes (10.0)
    assert result_low  == 4.0
    assert result_high == 10.0


# ---------------------------------------------------------------------------
# v2 schema parsing
# More end-to-end checks of parse_judge_response() on the older 4-dimension
# schema, including hallucination claim details and backward compatibility
# with the oldest (quality_score-only) schema.
# ---------------------------------------------------------------------------

def test_new_schema_parses_correctly() -> None:
    """New 4-dim JSON → all fields extracted, composite computed."""
    raw = json.dumps({
        "factual_accuracy": 2, "completeness": 3, "clinical_relevance": 2,
        "organization": 3,
        "hallucination": {
            "present": True, "count": 1,
            "claims": [{
                "claim": "Mortality was 20%",
                "source_quote": "The study did not report mortality",
                "category": "fabricated_statistics",
                "severity": "major",
            }],
        },
        "confidence_score": 4,
        "reasoning": "One fabricated statistic found.",
    })
    parsed = parse_judge_response(raw)
    assert parsed["factual_accuracy"] == 2
    assert parsed["completeness"] == 3
    assert parsed["clinical_relevance"] == 2
    assert parsed["organization"] == 3
    assert parsed["composite_score"] is not None
    assert 1.0 <= parsed["composite_score"] <= 10.0
    assert parsed["quality_score"] == round(parsed["composite_score"])
    assert parsed["hallucination_present"] is True
    assert len(parsed["hallucination_claims"]) == 1
    assert parsed["hallucination_claims"][0]["source_quote"] == "The study did not report mortality"
    assert parsed["parse_method"] == "json"


def test_species_penalty_schema_present() -> None:
    """Hallucination claims in v2 responses include source_quote field."""
    raw = json.dumps({
        "factual_accuracy": 1, "completeness": 2, "clinical_relevance": 2,
        "organization": 2,
        "hallucination": {
            "present": True, "count": 1,
            "claims": [{
                "claim": "The drug was tested in horses",
                "source_quote": "All subjects were domestic cats",
                "category": "contradiction",
                "severity": "major",
            }],
        },
        "confidence_score": 5,
        "reasoning": "Species mislabelled.",
    })
    parsed = parse_judge_response(raw)
    assert "source_quote" in parsed["hallucination_claims"][0]
    assert parsed["hallucination_claims"][0]["severity"] == "major"


def test_backward_compat_old_schema() -> None:
    """Legacy quality_score-only JSON still parses; composite_score is None."""
    raw = json.dumps({
        "quality_score": 8,
        "hallucination_count": 0,
        "hallucination_categories": [],
        "confidence_score": 5,
        "reasoning": "Good summary.",
    })
    parsed = parse_judge_response(raw)
    assert parsed["quality_score"] == 8
    assert parsed["composite_score"] is None
    assert parsed["factual_accuracy"] is None
    assert parsed["parse_method"] == "json"


def test_regex_fallback_new_fields() -> None:
    """Broken JSON with v2 dimension fields → regex path computes composite."""
    raw = ("factual_accuracy: 3, completeness: 2, clinical_relevance: 3, "
           "organization: 2, confidence_score: 4")
    parsed = parse_judge_response(raw)
    assert parsed["factual_accuracy"] == 3
    assert parsed["completeness"] == 2
    assert parsed["composite_score"] is not None
    assert parsed["quality_score"] == round(parsed["composite_score"])
    assert parsed["parse_method"] == "regex"


# ---------------------------------------------------------------------------
# Hallucination major-severity flag
# Checks the third trigger condition of needs_human_review() — a
# major-severity hallucination claim forces review even when confidence is
# high, but a minor one alone does not.
# ---------------------------------------------------------------------------

def test_hallucination_major_triggers_review() -> None:
    """A major-severity hallucination must flag the row for human review even
    when confidence is high and quality_score is not sentinel."""
    parsed = {
        "quality_score": 7,
        "confidence_score": 5,
        "hallucination_claims": [
            {"claim": "...", "source_quote": "...",
             "category": "contradiction", "severity": "major"},
        ],
    }
    assert needs_human_review(parsed) is True


def test_minor_hallucination_does_not_trigger_review_alone() -> None:
    """Minor hallucinations alone (with high confidence) should not force review."""
    parsed = {
        "quality_score": 7,
        "confidence_score": 4,
        "hallucination_claims": [
            {"claim": "...", "source_quote": "...",
             "category": "omitted_caveat", "severity": "minor"},
        ],
    }
    assert needs_human_review(parsed) is False


# ---------------------------------------------------------------------------
# Readable dev-eval mirrors (data/dev_evals_jsonl/, data/dev_detailEval_reports/)
# These tests check the human-readable report writers at the bottom of
# evaluator.py: write_dev_eval_jsonl_outputs (.txt) and
# write_dev_detail_eval_outputs (.md). They build fake evaluation rows
# directly (via the _fake_eval_row helper below) rather than running a full
# evaluation, since only the report-formatting logic is under test here.
# ---------------------------------------------------------------------------

# A helper (not itself a test — it doesn't start with `test_`, so pytest
# won't run it directly) that builds one realistic-looking fake evaluation
# row with sensible defaults for every field the report writers read.
# `**over` collects any extra keyword arguments the caller passes in (see
# the `**kwargs` explanation above) and `row.update(over)` overwrites the
# defaults with them — a compact way to let each test override just the one
# or two fields it cares about, e.g. `_fake_eval_row(..., requires_human_review=True)`.
def _fake_eval_row(doi: str, summarizer: str, judge: str, **over) -> dict:
    row = {
        "doi": doi,
        "summarizer": summarizer,
        "judge": judge,
        "input_source": "processed",
        "strata": {"journal": "jvim", "input_source": "processed"},
        "jury_score": 4,
        "jury_score_weighted": 4.1,
        "jury_score_unweighted": 4.0,
        "quality_score": 8,
        "hallucination_count": 0,
        "hallucination_claims": [
            {"claim": "The drug cured all cats.", "source_quote": "Some cats improved.",
             "category": "unsupported_inference", "severity": "major"},
        ],
        "requires_human_review": False,
        "confidence_score": 5,
        "parse_method": "json",
        "judge_model_version": "gpt-5.4-test",
        "reasoning": "Solid summary.",
        "criteria_scores": {
            "faithfulness": {"score": 4, "reasoning": "Mostly supported."},
            "completeness": {"score": 5, "reasoning": "Covers everything."},
            "clinical_usefulness": {"score": 4, "reasoning": "Useful takeaways."},
            "clarity": {"score": 5, "reasoning": "Easy to read."},
            "safety": {"score": 3, "reasoning": "Minor overstatement."},
        },
        "automatic_metrics": {
            "compression_ratio": 0.05,
            "extractive_coverage": 0.9,
            "section_coverage": {"covered_count": 5, "coverage_ratio": 0.83},
            "rouge_1": 0.4, "rouge_2": 0.2, "rouge_l": 0.3,
        },
    }
    row.update(over)
    return row


# A fake stand-in for the manifest index (normally loaded from
# data/manifest.jsonl) that supplies just one paper's title/journal — enough
# for the tests below to check that titles get looked up correctly, and that
# a DOI with no manifest entry (like "10.2/bbb" further down) falls back
# gracefully instead of crashing.
_FAKE_MANIFEST_INDEX = {
    "10.1/aaa": {"title": "A study of aaa in cats", "journal": "JVIM"},
}


# Writes fake evaluation rows for 3 DOIs but only requests reports for 2 of
# them, then checks: exactly 2 files were written, each contains the right
# provider sections and the DOI-derived header, the un-requested DOI produced
# no file, and a DOI missing from the manifest still produces a valid file
# with "Not recorded" instead of a title.
def test_write_dev_eval_jsonl_outputs_writes_one_file_per_doi(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from evaluator import write_dev_eval_jsonl_outputs
    from file_paths import doi_to_slug

    evaluations_path = tmp_path / "evaluations.jsonl"
    with open(evaluations_path, "w", encoding="utf-8") as f:
        for row in (
            _fake_eval_row("10.1/aaa", "openai", "openai"),
            _fake_eval_row("10.1/aaa", "anthropic", "openai", requires_human_review=True),
            _fake_eval_row("10.2/bbb", "gemini", "openai"),
            _fake_eval_row("10.3/ccc", "openai", "openai"),  # not requested
        ):
            f.write(json.dumps(row) + "\n")

    out_dir = tmp_path / "dev_evals_jsonl"
    written = write_dev_eval_jsonl_outputs(
        {"10.1/aaa", "10.2/bbb"}, output_dir=out_dir, evaluations_path=evaluations_path,
        manifest_index=_FAKE_MANIFEST_INDEX,
    )
    assert written == 2

    aaa = (out_dir / f"{doi_to_slug('10.1/aaa')}.txt").read_text(encoding="utf-8")
    # Header begins with DOI: so eval_instances.read_dois_from_dev_folder can parse it.
    assert aaa.startswith("DOI: 10.1/aaa")
    # Title line is sourced from the manifest index.
    assert "Title: A study of aaa in cats" in aaa
    # Both provider sections for that DOI are present.
    assert "SUMMARIZER: openai" in aaa
    assert "SUMMARIZER: anthropic" in aaa
    assert "Requires Human Review: Yes" in aaa
    # The un-requested DOI produced no file.
    assert not (out_dir / f"{doi_to_slug('10.3/ccc')}.txt").exists()

    # DOI with no manifest entry falls back to "Not recorded", not a crash.
    bbb = (out_dir / f"{doi_to_slug('10.2/bbb')}.txt").read_text(encoding="utf-8")
    assert "Title: Not recorded" in bbb


# Runs the writer twice in a row for the same DOI and checks there's still
# only 1 file afterward — proving a re-run overwrites the existing file
# (matched by DOI slug) instead of creating a second, timestamped duplicate.
def test_write_dev_eval_jsonl_outputs_overwrites_in_place(
    tmp_path: Path,
) -> None:
    """A re-judge writes one file per DOI keyed by slug (no timestamped dupes)."""
    from evaluator import write_dev_eval_jsonl_outputs

    evaluations_path = tmp_path / "evaluations.jsonl"
    evaluations_path.write_text(
        json.dumps(_fake_eval_row("10.1/aaa", "openai", "openai")) + "\n",
        encoding="utf-8",
    )
    out_dir = tmp_path / "dev_evals_jsonl"
    write_dev_eval_jsonl_outputs(
        {"10.1/aaa"}, output_dir=out_dir, evaluations_path=evaluations_path, manifest_index={},
    )
    write_dev_eval_jsonl_outputs(
        {"10.1/aaa"}, output_dir=out_dir, evaluations_path=evaluations_path, manifest_index={},
    )
    assert len(list(out_dir.glob("*.txt"))) == 1


# The Markdown-report equivalent of the .txt test above: checks the deep-dive
# report includes every judge's own scores and reasoning (not just the
# aggregate), the title heading, hallucination claim detail, and cross-judge
# agreement stats — plus the same "un-requested DOI produces no file" and
# "missing-manifest DOI still works" checks.
def test_write_dev_detail_eval_outputs_writes_one_file_per_doi(tmp_path: Path) -> None:
    from evaluator import write_dev_detail_eval_outputs
    from file_paths import doi_to_slug

    evaluations_path = tmp_path / "evaluations.jsonl"
    with open(evaluations_path, "w", encoding="utf-8") as f:
        for row in (
            _fake_eval_row("10.1/aaa", "openai", "openai"),
            _fake_eval_row("10.1/aaa", "openai", "anthropic", **{
                "criteria_scores": {
                    "faithfulness": {"score": 2, "reasoning": "Overstates one claim."},
                    "completeness": {"score": 4, "reasoning": "Missing limitations."},
                    "clinical_usefulness": {"score": 3, "reasoning": "Some value."},
                    "clarity": {"score": 4, "reasoning": "Readable."},
                    "safety": {"score": 3, "reasoning": "Moderate risk."},
                },
            }),
            _fake_eval_row("10.2/bbb", "gemini", "openai"),
            _fake_eval_row("10.3/ccc", "openai", "openai"),  # not requested
        ):
            f.write(json.dumps(row) + "\n")

    out_dir = tmp_path / "dev_detailEval_reports"
    written = write_dev_detail_eval_outputs(
        {"10.1/aaa", "10.2/bbb"}, output_dir=out_dir, evaluations_path=evaluations_path,
        manifest_index=_FAKE_MANIFEST_INDEX,
    )
    assert written == 2

    aaa = (out_dir / f"{doi_to_slug('10.1/aaa')}.md").read_text(encoding="utf-8")
    # Title is the top heading.
    assert aaa.startswith("# A study of aaa in cats")
    assert "[10.1/aaa](https://doi.org/10.1/aaa)" in aaa
    # All 5 plain-English criterion labels appear with their own reasoning.
    assert "Factual Accuracy" in aaa and "Mostly supported." in aaa
    assert "Practical Usefulness" in aaa
    assert "Overstates one claim." in aaa  # the second judge's own reasoning
    # Hallucination claim detail (not just a count).
    assert "The drug cured all cats." in aaa
    assert "Some cats improved." in aaa
    # Cross-judge stats block appears (2 judges scored openai's summary).
    assert "Cross-judge agreement" in aaa
    # Both judges get their own subsection.
    assert "### Judge: openai" in aaa
    assert "### Judge: anthropic" in aaa
    # The un-requested DOI produced no file.
    assert not (out_dir / f"{doi_to_slug('10.3/ccc')}.md").exists()

    # DOI with no manifest entry falls back to "Untitled -- {doi}".
    bbb = (out_dir / f"{doi_to_slug('10.2/bbb')}.md").read_text(encoding="utf-8")
    assert bbb.startswith("# Untitled -- 10.2/bbb")


# Same overwrite-in-place check as
# test_write_dev_eval_jsonl_outputs_overwrites_in_place above, but for the
# Markdown deep-dive report writer.
def test_write_dev_detail_eval_outputs_overwrites_in_place(tmp_path: Path) -> None:
    """A re-judge writes one file per DOI keyed by slug (no timestamped dupes)."""
    from evaluator import write_dev_detail_eval_outputs

    evaluations_path = tmp_path / "evaluations.jsonl"
    evaluations_path.write_text(
        json.dumps(_fake_eval_row("10.1/aaa", "openai", "openai")) + "\n",
        encoding="utf-8",
    )
    out_dir = tmp_path / "dev_detailEval_reports"
    write_dev_detail_eval_outputs(
        {"10.1/aaa"}, output_dir=out_dir, evaluations_path=evaluations_path, manifest_index={},
    )
    write_dev_detail_eval_outputs(
        {"10.1/aaa"}, output_dir=out_dir, evaluations_path=evaluations_path, manifest_index={},
    )
    assert len(list(out_dir.glob("*.md"))) == 1


# ---------------------------------------------------------------------------
# Tier 1b.1 — a criterion the judge never returned is not a score of 1
# ---------------------------------------------------------------------------

def test_jury_score_excludes_missing_criterion() -> None:
    """Imputing the floor is a strong claim the judge never made.

    faithfulness carries weight 1.5 of 5.5. Scoring it 1 by default drags the
    composite to (1*1.5 + 4*4.0)/5.5 = 3.18; renormalizing over the criteria
    actually present gives 4.00 — a 0.82 swing on a 1-5 scale.
    """
    from evaluator import MEDHELM_CRITERION_WEIGHTS, calculate_jury_score

    partial = {
        "completeness": {"score": 4},
        "clinical_usefulness": {"score": 4},
        "clarity": {"score": 4},
        "safety": {"score": 4},
    }
    assert calculate_jury_score(partial, weights=MEDHELM_CRITERION_WEIGHTS) == 4.0


def test_jury_score_unparseable_criterion_is_not_scored_one() -> None:
    """'4/5' is unparseable, not terrible."""
    from evaluator import UNWEIGHTED_CRITERION_WEIGHTS, calculate_jury_score

    scores = {
        "faithfulness": {"score": "4/5"},
        "completeness": {"score": 4},
        "clinical_usefulness": {"score": 4},
        "clarity": {"score": 4},
        "safety": {"score": 4},
    }
    assert calculate_jury_score(scores, weights=UNWEIGHTED_CRITERION_WEIGHTS) == 4.0


def test_jury_score_out_of_range_still_clamps() -> None:
    """Clamping a genuine out-of-range integer is correct and must survive."""
    from evaluator import UNWEIGHTED_CRITERION_WEIGHTS, calculate_jury_score

    scores = {c: {"score": 9} for c in
              ("faithfulness", "completeness", "clinical_usefulness", "clarity", "safety")}
    assert calculate_jury_score(scores, weights=UNWEIGHTED_CRITERION_WEIGHTS) == 5.0


def test_jury_score_all_criteria_missing_is_none() -> None:
    from evaluator import UNWEIGHTED_CRITERION_WEIGHTS, calculate_jury_score

    assert calculate_jury_score({}, weights=UNWEIGHTED_CRITERION_WEIGHTS) is None


def test_missing_criteria_helper_names_the_gaps() -> None:
    from evaluator import MEDHELM_CRITERION_WEIGHTS, missing_criteria

    scores = {"completeness": {"score": 4}, "faithfulness": {"score": "n/a"}}
    assert missing_criteria(scores, weights=MEDHELM_CRITERION_WEIGHTS) == [
        "faithfulness", "clinical_usefulness", "clarity", "safety",
    ]


def test_parse_judge_response_records_missing_criteria() -> None:
    """The row must carry which criteria were absent, so a consumer can filter."""
    import json as _json

    from evaluator import parse_judge_response

    payload = {
        "criteria_scores": {
            "completeness": {"score": 4, "reasoning": "ok"},
            "clinical_usefulness": {"score": 4, "reasoning": "ok"},
            "clarity": {"score": 4, "reasoning": "ok"},
            "safety": {"score": 4, "reasoning": "ok"},
        },
        "hallucination": {"present": False, "claims": []},
        "confidence": 5,
    }
    parsed = parse_judge_response(_json.dumps(payload))
    assert parsed["missing_criteria"] == ["faithfulness"]
    assert parsed["jury_score_unweighted"] == 4.0


def test_hallucination_count_survives_explicit_null() -> None:
    """`{"count": null}` must not crash the parser.

    dict.get's default never fires for a present-but-null key, so
    int(halluc_block.get("count", len(claims))) raised TypeError on a judge
    that answered `"count": null` — a whole judge response lost to a crash.
    """
    import json as _json

    from evaluator import parse_judge_response

    payload = {
        "criteria_scores": {c: {"score": 4, "reasoning": "ok"} for c in
                            ("faithfulness", "completeness", "clinical_usefulness",
                             "clarity", "safety")},
        "hallucination": {"present": True, "count": None,
                          "claims": [{"severity": "major", "category": "contradiction"}]},
        "confidence": 5,
    }
    parsed = parse_judge_response(_json.dumps(payload))
    # Falls back to the actual number of claims rather than crashing.
    assert parsed["hallucination_count"] == 1
    assert parsed["parse_method"] == "json"


# ---------------------------------------------------------------------------
# Dry-run contamination guards
# ---------------------------------------------------------------------------
#
# BACKGROUND: on 2026-07-09 a PHASE3_MODE=test run appended 16 mock rows to
# data/evaluations.jsonl. Nothing distinguished them from real judge output,
# so five days of reports averaged hash-derived scores in with real ones, and
# 10.1111/jvim.16872 was locked out of ever being judged because the resume
# check saw its mock row and skipped it. These tests pin both directions:
# mock rows must not be READ as real, and must not be WRITTEN to production.


def _mock_row(**overrides: object) -> dict:
    """A row shaped like what _mock_judge_response produces."""
    row = {
        "doi": "10.1111/jvim.16872",
        "summarizer": "anthropic",
        "judge": "openai",
        "input_source": "processed",
        "rubric_version": evaluator.RUBRIC_VERSION,
        "judge_model_version": "gpt-5.4-DRYRUN",
        "reasoning": "[MOCK] dry-run evaluation",
        "jury_score": 3.2,
    }
    row.update(overrides)
    return row


def _real_row(**overrides: object) -> dict:
    row = _mock_row()
    row.update({"judge_model_version": "gpt-5.4-2026-03-05",
                "reasoning": "The summary is well supported."})
    row.update(overrides)
    return row


def test_is_dry_run_row_matches_suffix_and_reasoning() -> None:
    """Either signal alone is enough; a real row trips neither."""
    assert evaluator.is_dry_run_row(_mock_row()) is True
    # Suffix alone, real-looking reasoning.
    assert evaluator.is_dry_run_row(_mock_row(reasoning="looks real")) is True
    # Reasoning alone, in case a row ever loses its model_version.
    assert evaluator.is_dry_run_row(
        _mock_row(judge_model_version="gpt-5.4-2026-03-05")) is True
    assert evaluator.is_dry_run_row(_real_row()) is False
    # Must not match a model that merely contains the word.
    assert evaluator.is_dry_run_row(
        _real_row(judge_model_version="gpt-DRYRUN-turbo")) is False
    assert evaluator.is_dry_run_row(None) is False


def test_is_dry_run_slot_reads_model_version() -> None:
    assert evaluator.is_dry_run_slot({"model_version": "gpt-5.4-DRYRUN"}) is True
    assert evaluator.is_dry_run_slot({"model_version": "gpt-5.4-2026-03-05"}) is False
    assert evaluator.is_dry_run_slot({}) is False
    assert evaluator.is_dry_run_slot(None) is False


def test_is_production_path_resolves_before_comparing() -> None:
    """A reconstructed path naming the same file must count as production.

    A bare `==` would miss this, and on a OneDrive-backed Windows path so
    would a case-sensitive compare.
    """
    reconstructed = evaluator.DATA_DIR / "evaluations.jsonl"
    assert evaluator.is_production_path(reconstructed, evaluator.EVALUATIONS_PATH)
    # None means "use the default", which IS the production file.
    assert evaluator.is_production_path(None, evaluator.EVALUATIONS_PATH)
    assert not evaluator.is_production_path(
        Path("somewhere_else.jsonl"), evaluator.EVALUATIONS_PATH)


def test_dry_run_sibling_stays_beside_target(tmp_path: Path) -> None:
    """Redirect target is derived from the path, never hardcoded to data/.

    A fixed constant would send a test writing to tmp into the real data/
    folder — the guard must not itself write somewhere unexpected.
    """
    sibling = evaluator.dry_run_sibling(tmp_path / "evaluations.jsonl")
    assert sibling == tmp_path / "evaluations_dryrun.jsonl"


def test_already_evaluated_ignores_mock_rows(tmp_path: Path,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    """THE REGRESSION: a mock row must not make a paper look already judged.

    The orphan mock for 10.1111/jvim.16872 matched the resume key exactly
    (same doi/summarizer/judge/rubric_version), so --resume skipped a paper
    that no judge had ever actually scored.
    """
    ledger = tmp_path / "evaluations.jsonl"
    ledger.write_text(json.dumps(_mock_row()) + "\n", encoding="utf-8")
    monkeypatch.setattr(evaluator, "EVALUATIONS_PATH", ledger)

    assert evaluator.already_evaluated(
        "10.1111/jvim.16872", "anthropic", "openai") is False

    # A real row for the same key still resumes correctly.
    ledger.write_text(json.dumps(_real_row()) + "\n", encoding="utf-8")
    assert evaluator.already_evaluated(
        "10.1111/jvim.16872", "anthropic", "openai") is True


def test_append_evaluation_redirects_mock_row_with_explicit_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Guard keys off the target file, not off `path is None`.

    check_batch_status calls append_evaluation(row, EVALUATIONS_PATH) with the
    path explicit, so a `path is None` test would let mock rows straight into
    production.
    """
    production = tmp_path / "evaluations.jsonl"
    monkeypatch.setattr(evaluator, "EVALUATIONS_PATH", production)

    evaluator.append_evaluation(_mock_row(), production)

    assert not production.exists(), "mock row reached the production ledger"
    redirected = tmp_path / "evaluations_dryrun.jsonl"
    assert redirected.exists()
    assert json.loads(redirected.read_text(encoding="utf-8").strip())["doi"] \
        == "10.1111/jvim.16872"


def test_append_evaluation_still_writes_real_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard must not block real judge output."""
    production = tmp_path / "evaluations.jsonl"
    monkeypatch.setattr(evaluator, "EVALUATIONS_PATH", production)

    evaluator.append_evaluation(_real_row(), production)

    assert production.exists()
    assert not (tmp_path / "evaluations_dryrun.jsonl").exists()
