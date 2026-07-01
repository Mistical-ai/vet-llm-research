"""
Tests for llm-sum/batch_utils.py and the merge path in check_batch_status.py.

Critical assertions:
    Request builders produce the exact shape the provider APIs expect.
    Result line parsers extract correct fields from success + error responses.
    parse_evaluation_custom_id round-trips the slug__summariser format.
    merge_evaluation_results routes through build_evaluation_row so the output
        has requires_human_review, parse_method, quality_score — not raw batch fields.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import batch_utils
import check_batch_status
from batch_utils import (
    build_openai_request,
    build_anthropic_request,
    parse_openai_result_line,
    parse_anthropic_result_line,
    parse_evaluation_custom_id,
    write_batch_jsonl,
)
from file_paths import doi_to_slug
import summarizer


# ---------------------------------------------------------------------------
# Request builders
# ---------------------------------------------------------------------------

def _valid_summary_payload(**overrides) -> dict:
    """Return a complete VeterinarySummary-shaped payload for batch parser tests."""
    payload = {
        "headline": "Clinical signs improved in dogs after treatment.",
        "objective": "Assess treatment response in client-owned dogs.",
        "study_design": "Retrospective cohort.",
        "species": "Dogs",
        "sample_size": 42,
        "key_methods": ["Reviewed medical records.", "Compared clinical outcomes."],
        "key_findings": ["Clinical signs improved in most dogs."],
        "clinical_significance": "The findings may help guide follow-up care.",
        "limitations": ["Single-center study."],
        "summary_text": "Objective: Assess treatment response in client-owned dogs.",
    }
    payload.update(overrides)
    return payload

def test_build_openai_request_shape() -> None:
    row = build_openai_request("my_slug", "Judge this please.", "gpt-5.4")
    assert row["custom_id"] == "my_slug"
    assert row["method"] == "POST"
    assert row["url"] == "/v1/chat/completions"
    body = row["body"]
    assert body["model"] == "gpt-5.4"
    assert body["messages"][0]["role"] == "user"
    assert body["messages"][0]["content"] == "Judge this please."
    assert body["temperature"] == 0.0
    assert "max_completion_tokens" in body
    assert body["max_completion_tokens"] == summarizer.MAX_OUTPUT_TOKENS
    assert "max_tokens" not in body
    assert body["seed"] == 42
    assert body["response_format"]["type"] == "json_schema"


def test_build_anthropic_request_shape() -> None:
    row = build_anthropic_request("another_slug", "Anthropic judge.", "claude-sonnet-4-6")
    assert row["custom_id"] == "another_slug"
    params = row["params"]
    assert params["model"] == "claude-sonnet-4-6"
    assert params["messages"][0]["content"] == "Anthropic judge."
    assert params["temperature"] == 0.0
    assert "max_tokens" in params
    assert params["max_tokens"] == summarizer.MAX_OUTPUT_TOKENS
    assert params["tool_choice"]["name"] == "VeterinarySummary"


# ---------------------------------------------------------------------------
# parse_evaluation_custom_id
# ---------------------------------------------------------------------------

def test_parse_evaluation_custom_id_valid() -> None:
    slug = doi_to_slug("10.1111/jvim.16872")
    cid = f"{slug}__openai"
    pair = parse_evaluation_custom_id(cid)
    assert pair is not None
    assert pair == (slug, "openai")


def test_parse_evaluation_custom_id_keeps_input_source_suffix() -> None:
    slug = f"{doi_to_slug('10.1111/jvim.16872')}__raw_text"
    cid = f"{slug}__anthropic"
    pair = parse_evaluation_custom_id(cid)
    assert pair == (slug, "anthropic")


def test_parse_evaluation_custom_id_invalid() -> None:
    assert parse_evaluation_custom_id("no_double_underscore") is None
    assert parse_evaluation_custom_id("") is None


# ---------------------------------------------------------------------------
# parse_openai_result_line
# ---------------------------------------------------------------------------

def _openai_success_line(custom_id: str, content: str,
                         model: str = "gpt-5.4-preview") -> str:
    return json.dumps({
        "custom_id": custom_id,
        "response": {
            "status_code": 200,
            "body": {
                "model": model,
                "choices": [{"message": {"content": content}}],
                "usage": {"prompt_tokens": 1000, "completion_tokens": 200},
            },
        },
    })


def _openai_error_line(custom_id: str) -> str:
    return json.dumps({
        "custom_id": custom_id,
        "error": {"code": "rate_limit_exceeded", "message": "Too many requests"},
    })


def test_parse_openai_result_line_success() -> None:
    line = _openai_success_line("slug_a", json.dumps(_valid_summary_payload()))
    parsed = parse_openai_result_line(line)
    assert parsed["status"] == "success"
    assert parsed["summary"] == "Objective: Assess treatment response in client-owned dogs."
    assert parsed["structured_summary"]["species"] == "Dogs"
    assert parsed["input_tokens"] == 1000
    assert parsed["output_tokens"] == 200
    assert parsed["model_version"] == "gpt-5.4-preview"
    assert parsed["custom_id"] == "slug_a"


def test_parse_openai_result_line_error() -> None:
    line = _openai_error_line("slug_b")
    parsed = parse_openai_result_line(line)
    assert parsed["status"] == "failed"
    assert parsed["summary"] is None


# ---------------------------------------------------------------------------
# parse_anthropic_result_line
# ---------------------------------------------------------------------------

def _anthropic_success_line(custom_id: str, content: str) -> str:
    return json.dumps({
        "custom_id": custom_id,
        "result": {
            "type": "succeeded",
            "message": {
                "model": "claude-sonnet-4-6-20260101",
                "content": [{"type": "text", "text": content}],
                "usage": {"input_tokens": 800, "output_tokens": 150},
            },
        },
    })


def _anthropic_error_line(custom_id: str) -> str:
    return json.dumps({
        "custom_id": custom_id,
        "result": {
            "type": "errored",
            "error": {"type": "server_error", "message": "Internal error"},
        },
    })


def test_parse_anthropic_result_line_success() -> None:
    line = json.dumps({
        "custom_id": "slug_c",
        "result": {
            "type": "succeeded",
            "message": {
                "model": "claude-sonnet-4-6-20260101",
                "content": [{
                    "type": "tool_use",
                    "name": "VeterinarySummary",
                    "input": _valid_summary_payload(
                        summary_text="Objective: Anthropic structured summary."
                    ),
                }],
                "usage": {"input_tokens": 800, "output_tokens": 150},
            },
        },
    })
    parsed = parse_anthropic_result_line(line)
    assert parsed["status"] == "success"
    assert parsed["summary"] == "Objective: Anthropic structured summary."
    assert parsed["structured_summary"]["species"] == "Dogs"
    assert parsed["input_tokens"] == 800
    assert parsed["output_tokens"] == 150
    assert parsed["model_version"] == "claude-sonnet-4-6-20260101"


def test_parse_anthropic_result_line_error() -> None:
    line = _anthropic_error_line("slug_d")
    parsed = parse_anthropic_result_line(line)
    assert parsed["status"] == "failed"
    assert parsed["summary"] is None


def test_merge_summarisation_results_uses_custom_id_not_bare_doi(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A raw_text batch result must update only the raw_text row for a DOI."""
    doi = "10.9999/batch.same.doi"
    slug = doi_to_slug(doi)
    summaries_path = tmp_path / "summaries.jsonl"
    processed_entry = {
        "doi": doi,
        "custom_id": slug,
        "input_source": "processed",
        "models": {"openai": {"status": "pending"}},
    }
    raw_entry = {
        "doi": doi,
        "custom_id": f"{slug}__raw_text",
        "input_source": "raw_text",
        "models": {"openai": {"status": "pending"}},
    }
    summaries_path.write_text(
        json.dumps(processed_entry) + "\n" + json.dumps(raw_entry) + "\n",
        encoding="utf-8",
    )
    result_path = tmp_path / "openai_sum_results.jsonl"
    result_path.write_text(
        _openai_success_line(
            f"{slug}__raw_text",
            json.dumps(_valid_summary_payload(summary_text="Raw text summary.")),
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(check_batch_status, "SUMMARIES_PATH", summaries_path)
    count = check_batch_status.merge_summarisation_results(
        result_path, provider="openai"
    )
    assert count == 1

    rows = [
        json.loads(line)
        for line in summaries_path.read_text(encoding="utf-8").splitlines()
    ]
    by_source = {row["input_source"]: row for row in rows}
    assert by_source["processed"]["models"]["openai"]["status"] == "pending"
    assert by_source["raw_text"]["models"]["openai"]["status"] == "success"
    assert by_source["raw_text"]["models"]["openai"]["summary"] == "Raw text summary."


# ---------------------------------------------------------------------------
# merge_evaluation_results routes through build_evaluation_row
# ---------------------------------------------------------------------------

def _make_judge_json_response(quality: int = 7) -> str:
    """Produce a valid judge JSON response string."""
    return json.dumps({
        "quality_score": quality,
        "hallucination_count": 0,
        "hallucination_categories": [],
        "confidence_score": 4,
        "reasoning": "Test evaluation.",
    })


def test_merge_evaluation_results_produces_correct_schema(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Verify that merge_evaluation_results routes through build_evaluation_row
    so every output row has the required evaluation schema fields — not raw
    batch parser output.
    """
    doi = "10.9999/batch.eval.0001"
    slug = doi_to_slug(doi)

    # --- Set up fake summaries.jsonl ---
    summaries_path = tmp_path / "summaries.jsonl"
    summaries_path.write_text(json.dumps({
        "doi": doi,
        "custom_id": slug,
        "models": {
            "anthropic": {
                "status": "success",
                "summary": "Candidate summary text.",
                "model_version": "claude-sonnet-4-6-test",
                "input_tokens": 400, "output_tokens": 80,
            }
        }
    }) + "\n", encoding="utf-8")

    # --- Set up fake batch result JSONL (OpenAI format) ---
    judge_raw = _make_judge_json_response(quality=8)
    result_path = tmp_path / "openai_eval_results.jsonl"
    result_path.write_text(
        _openai_success_line(f"{slug}__anthropic", judge_raw, model="gpt-5.4-eval"),
        encoding="utf-8",
    )

    # --- Redirect outputs ---
    evaluations_path = tmp_path / "evaluations.jsonl"
    monkeypatch.setattr(check_batch_status, "EVALUATIONS_PATH", evaluations_path)
    monkeypatch.setattr(check_batch_status, "SUMMARIES_PATH", summaries_path)

    # _load_summaries() in check_batch_status reads SUMMARIES_PATH, so patch
    # the module's path attribute that the function reads at call time.
    import check_batch_status as cbs
    original_load = cbs._load_summaries

    def _patched_load():
        # Load from our tmp path instead.
        out: dict = {}
        with open(summaries_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                entry = json.loads(line)
                d = entry.get("doi", "")
                if d:
                    out[d] = entry
        return out

    monkeypatch.setattr(cbs, "_load_summaries", _patched_load)

    import evaluator
    monkeypatch.setattr(evaluator, "EVALUATIONS_PATH", evaluations_path)

    count = check_batch_status.merge_evaluation_results(result_path, provider="openai", judge="openai")
    assert count == 1

    lines = evaluations_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])

    # Core schema fields that prove we went through build_evaluation_row
    assert row["doi"] == doi
    assert row["summarizer"] == "anthropic"
    assert row["judge"] == "openai"
    assert row["quality_score"] == 8
    assert row["confidence_score"] == 4
    assert "requires_human_review" in row, "requires_human_review missing — raw batch output not going through build_evaluation_row"
    assert "parse_method" in row, "parse_method missing — raw batch output not going through build_evaluation_row"
    assert row["parse_method"] == "json"
    assert row["requires_human_review"] is False
    assert "judge_model_version" in row
    assert row["judge_model_version"] == "gpt-5.4-eval"
