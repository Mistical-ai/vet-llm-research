"""
Tests for llm-sum/evaluator.py.

Critical assertions:
    Blind check: rendered judge prompt contains NONE of the forbidden tokens.
    JSON fallback: response with extra conversational text still yields a score.
    99 sentinel: completely malformed response triggers manual-review flag.
    requires_human_review: confidence < 3 triggers the flag.
    PHASE3_MODE=dev: evaluator honours the dev paper cap.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import evaluator
from evaluator import (
    BLIND_FORBIDDEN_TOKENS,
    SCORE_SENTINEL_MALFORMED,
    build_judge_prompt,
    build_evaluation_row,
    aggregate_jury_scores,
    calculate_composite_score,
    calculate_jury_score,
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
    # ... but is NOT in any field that would be reconstructed into a judge prompt.
    rendered = build_judge_prompt("Reference body.", "Candidate body.").lower()
    assert "anthropic" not in rendered


def test_mock_judge_response_is_stable() -> None:
    first = evaluator._mock_judge_response(
        "openai", "Reference body.", "Candidate summary."
    )
    second = evaluator._mock_judge_response(
        "openai", "Reference body.", "Candidate summary."
    )
    assert first["raw_text"] == second["raw_text"]


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
    assert parsed["jury_score"] == calculate_jury_score(parsed["criteria_scores"])
    assert parsed["quality_score"] == 9
    assert parsed["parse_method"] == "json"


def test_aggregate_jury_scores_tracks_disagreement() -> None:
    rows = [{"jury_score": 4.5}, {"jury_score": 3.5}, {"jury_score": None}]
    aggregate = aggregate_jury_scores(rows)
    assert aggregate["jury_score"] == 4.0
    assert aggregate["judge_count"] == 3
    assert aggregate["valid_judge_count"] == 2
    assert aggregate["judge_disagreement"] == 1.0


def test_call_judge_uses_call_time_dry_run(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("PHASE3_MODE", "single")
    monkeypatch.setattr(evaluator, "DRY_RUN", True)

    captured = {}

    def _fake_openai(user_message: str) -> dict:
        captured["message"] = user_message
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
    assert "Reference body." in captured["message"]


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

    result = evaluator._call_judge_gemini("Judge this.")

    assert captured["api_key"] == "test-key"
    assert captured["kwargs"]["contents"] == "Judge this."
    assert captured["config"]["response_mime_type"] == "application/json"
    assert result["input_tokens"] == 123
    assert result["output_tokens"] == 45


# ---------------------------------------------------------------------------
# PHASE3_MODE=dev paper cap
# ---------------------------------------------------------------------------

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
    lines = evaluations_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2


# ---------------------------------------------------------------------------
# Composite score (Vet-Score v2.0)
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
