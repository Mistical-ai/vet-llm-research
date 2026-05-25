"""
Tests for llm-sum/evaluator.py.

Critical assertions:
    Blind check: rendered judge prompt contains NONE of the forbidden tokens.
    JSON fallback: response with extra conversational text still yields a score.
    99 sentinel: completely malformed response triggers manual-review flag.
    requires_human_review: confidence < 3 triggers the flag.
    DEVELOPMENT_MODE: evaluator also caps at 2 papers.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import evaluator
from evaluator import (
    BLIND_FORBIDDEN_TOKENS,
    SCORE_SENTINEL_MALFORMED,
    build_judge_prompt,
    build_evaluation_row,
    needs_human_review,
    parse_judge_response,
)


@pytest.fixture(autouse=True)
def _force_dry_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """See test_summarizer.py for the rationale — Phase 2's
    test_pdf_validation.py sets DRY_RUN=false at import and leaks it."""
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setattr(evaluator, "DRY_RUN", True)


# ---------------------------------------------------------------------------
# Blind protocol
# ---------------------------------------------------------------------------

def test_blind_judge_prompt_contains_no_model_identifiers() -> None:
    reference = "Reference text body about a clinical trial in dogs."
    candidate = "Some candidate summary text claiming X and Y."
    rendered = build_judge_prompt(reference, candidate).lower()
    for forbidden in BLIND_FORBIDDEN_TOKENS:
        assert forbidden not in rendered, (
            f"Judge prompt leaked the token '{forbidden}'. The blind protocol "
            "requires the prompt to be model-agnostic."
        )


def test_build_judge_prompt_signature_excludes_summariser_name() -> None:
    """The signature must not accept a summariser identifier — that is the
    structural guarantee of the blind protocol."""
    import inspect
    sig = inspect.signature(build_judge_prompt)
    forbidden_params = {"summarizer", "summariser", "model_name", "provider"}
    assert not (set(sig.parameters) & forbidden_params), (
        "build_judge_prompt must not take a summariser identifier parameter."
    )


# ---------------------------------------------------------------------------
# JSON parsing + fallback
# ---------------------------------------------------------------------------

def test_clean_json_parsed_directly() -> None:
    raw = json.dumps({
        "quality_score": 8,
        "hallucination_count": 1,
        "hallucination_categories": ["omitted caveat"],
        "confidence_score": 4,
        "reasoning": "Mostly accurate.",
    })
    parsed = parse_judge_response(raw)
    assert parsed["quality_score"] == 8
    assert parsed["hallucination_count"] == 1
    assert parsed["hallucination_categories"] == ["omitted caveat"]
    assert parsed["confidence_score"] == 4
    assert parsed["parse_method"] == "json"


def test_json_with_conversational_wrapper_still_parses() -> None:
    raw = ("Sure! Here is the evaluation: "
           '{"quality_score": 7, "hallucination_count": 0, '
           '"hallucination_categories": [], "confidence_score": 5, '
           '"reasoning": "Faithful summary."} '
           "Let me know if you need anything else.")
    parsed = parse_judge_response(raw)
    assert parsed["quality_score"] == 7
    assert parsed["parse_method"] == "json"


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


def test_completely_malformed_response_triggers_99_sentinel() -> None:
    raw = "Sorry, I cannot evaluate this summary."
    parsed = parse_judge_response(raw)
    assert parsed["quality_score"] == SCORE_SENTINEL_MALFORMED
    assert parsed["parse_method"] == "sentinel"


# ---------------------------------------------------------------------------
# requires_human_review flag
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

def test_build_evaluation_row_keeps_summariser_metadata_only() -> None:
    judge_response = {
        "raw_text": json.dumps({
            "quality_score": 8, "hallucination_count": 0,
            "hallucination_categories": [], "confidence_score": 5,
            "reasoning": "ok",
        }),
        "input_tokens": 1500, "output_tokens": 200,
        "model_version": "gpt-5.5-test",
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
    # ... but is NOT in any field that would be reconstructed into a judge prompt.
    rendered = build_judge_prompt("Reference body.", "Candidate body.").lower()
    assert "anthropic" not in rendered


# ---------------------------------------------------------------------------
# DEVELOPMENT_MODE cap
# ---------------------------------------------------------------------------

def test_development_mode_caps_at_two_papers(tmp_path: Path,
                                              monkeypatch: pytest.MonkeyPatch) -> None:
    """Build fake summaries with 5 papers; evaluator should only process 2."""
    summaries_path = tmp_path / "summaries.jsonl"
    evaluations_path = tmp_path / "evaluations.jsonl"
    processed_dir = tmp_path / "processed"
    processed_dir.mkdir()

    from file_paths import doi_to_slug

    with open(summaries_path, "w", encoding="utf-8") as f:
        for i in range(5):
            doi = f"10.9999/eval.{i:04d}"
            slug = doi_to_slug(doi)
            entry = {"doi": doi, "slug": slug, "text": "Reference body content. " * 30}
            (processed_dir / f"{slug}.jsonl").write_text(
                json.dumps(entry) + "\n", encoding="utf-8")
            f.write(json.dumps({
                "doi": doi,
                "custom_id": slug,
                "models": {
                    "openai": {
                        "status": "success",
                        "summary": f"summary for paper {i}",
                        "model_version": "gpt-5.5-test",
                        "input_tokens": 100, "output_tokens": 50,
                    }
                }
            }) + "\n")

    monkeypatch.setattr(evaluator, "SUMMARIES_PATH", summaries_path)
    monkeypatch.setattr(evaluator, "EVALUATIONS_PATH", evaluations_path)
    monkeypatch.setattr(evaluator, "PROCESSED_DIR", processed_dir)

    counts = evaluator.run_evaluation(
        judges=["openai"], resume=False,
        paper_limit=evaluator.DEV_MODE_PAPER_LIMIT,
    )

    assert counts["evaluated"] == 2  # 2 papers × 1 summariser × 1 judge
    lines = evaluations_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
