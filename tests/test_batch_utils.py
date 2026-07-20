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
    build_gemini_batch_request,
    parse_openai_result_line,
    parse_anthropic_result_line,
    parse_gemini_result_line,
    parse_evaluation_custom_id,
    write_batch_jsonl,
)
from file_paths import doi_to_slug
from models_config import get_model_spec
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
    # Regression guard: OpenAI's Structured Outputs "strict" mode requires
    # every key in `properties` to also appear in `required` — a live batch
    # job was rejected with "'required' is required to be supplied and to be
    # an array including every key in properties. Missing 'sample_size'"
    # because the schema came from the plain (non-strict) pydantic
    # model_json_schema(), which correctly omits Optional fields from
    # `required` under normal JSON Schema semantics. build_openai_request
    # must instead use openai.lib._pydantic.to_strict_json_schema(), which
    # adds every property to `required` and expresses optional fields as
    # nullable anyOf unions — this asserts that transform actually ran.
    schema = body["response_format"]["json_schema"]["schema"]
    assert set(schema["properties"].keys()) <= set(schema["required"])
    assert "sample_size" in schema["required"]


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


def test_build_gemini_batch_request_shape() -> None:
    row = build_gemini_batch_request("gemini_slug", "Gemini judge.", "gemini-3.5-flash")
    # "key" (not "custom_id") is Gemini's own field name for the JSONL
    # request identifier — see ai.google.dev/gemini-api/docs/batch-mode.
    assert row["key"] == "gemini_slug"
    request = row["request"]
    assert request["contents"][0]["parts"][0]["text"] == "Gemini judge."
    assert "config" not in request, (
        "the live batch endpoint rejects the SDK real-time wrapper key "
        "'config' with 'no such field: config' — generation settings must "
        "sit under 'generation_config' (snake_case) instead"
    )
    config = request["generation_config"]
    assert config["temperature"] == 0.0
    assert config["response_mime_type"] == "application/json"
    assert "response_schema" in config
    assert "max_output_tokens" in config
    assert config["thinking_config"] == {"thinking_budget": 0}


# ---------------------------------------------------------------------------
# for_judge=True must not force the VeterinarySummary schema — a judge needs
# to answer with quality_score/hallucination/criteria_scores, a completely
# different shape. See evaluator._call_judge_openai/_anthropic/_gemini for
# what the real-time judge calls actually send.
# ---------------------------------------------------------------------------

def test_build_openai_request_for_judge_uses_json_object_mode() -> None:
    row = build_openai_request("slug", "Judge this.", "gpt-5.4", for_judge=True)
    assert row["body"]["response_format"] == {"type": "json_object"}


def test_build_anthropic_request_for_judge_omits_tools() -> None:
    row = build_anthropic_request("slug", "Judge this.", "claude-sonnet-4-6", for_judge=True)
    params = row["params"]
    assert "tools" not in params, (
        "a forced tool_choice makes Anthropic return a tool_use block, not a "
        "text block — parse_anthropic_result_line(expect_summary_schema=False) "
        "(what the judge merge path uses) would read back an empty string"
    )
    assert "tool_choice" not in params


def test_build_gemini_batch_request_for_judge_omits_response_schema() -> None:
    row = build_gemini_batch_request("slug", "Judge this.", "gemini-3.5-flash", for_judge=True)
    config = row["request"]["generation_config"]
    assert "response_schema" not in config
    assert config["response_mime_type"] == "application/json"
    assert config["thinking_config"] == {"thinking_budget": 0}


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


# ---------------------------------------------------------------------------
# parse_gemini_result_line
# ---------------------------------------------------------------------------

def _gemini_success_line(key: str, text: str, model_version: str = "gemini-3.5-flash") -> str:
    return json.dumps({
        "key": key,
        "response": {
            "candidates": [{"content": {"parts": [{"text": text}]}}],
            "usageMetadata": {"promptTokenCount": 900, "candidatesTokenCount": 180},
            "modelVersion": model_version,
        },
    })


def _gemini_error_line(key: str) -> str:
    return json.dumps({
        "key": key,
        "status": {"code": 8, "message": "Resource exhausted"},
    })


def test_parse_gemini_result_line_success() -> None:
    line = _gemini_success_line("slug_e", json.dumps(_valid_summary_payload()))
    parsed = parse_gemini_result_line(line)
    assert parsed["status"] == "success"
    assert parsed["summary"] == "Objective: Assess treatment response in client-owned dogs."
    assert parsed["structured_summary"]["species"] == "Dogs"
    assert parsed["input_tokens"] == 900
    assert parsed["output_tokens"] == 180
    assert parsed["model_version"] == "gemini-3.5-flash"
    assert parsed["custom_id"] == "slug_e"


def test_parse_gemini_result_line_error() -> None:
    line = _gemini_error_line("slug_f")
    parsed = parse_gemini_result_line(line)
    assert parsed["status"] == "failed"
    assert parsed["summary"] is None


def test_parse_gemini_result_line_truncated_surfaces_finish_reason() -> None:
    """A truncated/safety-filtered row must fail with an actionable message,
    not a bare JSON-decode error — mirrors the real-time path's
    finish_reason-aware error (summarizer._gemini_finish_reason)."""
    line = json.dumps({
        "key": "slug_g",
        "response": {
            "candidates": [{
                "content": {"parts": [{"text": "{\"headline\": \"incomple"}]},
                "finishReason": "MAX_TOKENS",
            }],
            "usageMetadata": {"promptTokenCount": 900, "candidatesTokenCount": 180},
        },
    })
    parsed = parse_gemini_result_line(line)
    assert parsed["status"] == "failed"
    assert "MAX_TOKENS" in parsed["error"]


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
    monkeypatch.setattr(
        check_batch_status, "BATCH_SUMMARIES_JSONL_DIR", tmp_path / "batch_summaries_jsonl",
    )
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
# Batch spend must flow through BudgetGuard
# ---------------------------------------------------------------------------

# In plain English: CLAUDE.md requires every paid API call to be charged to
# BudgetGuard. Batch was the exception — it spent real money with no tracking at
# all, in the mode that processes the entire corpus. These tests pin that shut.

def test_merge_summarisation_results_charges_real_batch_cost(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from utils import BudgetGuard
    from models_config import compute_cost

    doi = "10.9999/budget.paper"
    slug = doi_to_slug(doi)
    summaries_path = tmp_path / "summaries.jsonl"
    summaries_path.write_text(
        json.dumps({
            "doi": doi, "custom_id": slug, "input_source": "processed",
            "journal": "jvim", "models": {"openai": {"status": "pending"}},
        }) + "\n",
        encoding="utf-8",
    )
    result_path = tmp_path / "openai_sum_results.jsonl"
    result_path.write_text(
        _openai_success_line(slug, json.dumps(_valid_summary_payload())),
        encoding="utf-8",
    )
    monkeypatch.setattr(check_batch_status, "SUMMARIES_PATH", summaries_path)
    monkeypatch.setattr(
        check_batch_status, "BATCH_SUMMARIES_JSONL_DIR", tmp_path / "readable")
    monkeypatch.setattr(summarizer, "SUMMARIES_PATH", summaries_path)

    guard = BudgetGuard(hard_stop=100.0)
    check_batch_status.merge_summarisation_results(
        result_path, provider="openai", guard=guard)

    # _openai_success_line reports 1000 input / 200 output tokens.
    expected = compute_cost("openai", 1000, 200, batched=True)
    assert guard.total_spent == pytest.approx(expected)
    assert guard.total_spent > 0, "batch merge recorded no spend at all"
    # Batch pricing is discounted, so the batch charge must be strictly cheaper
    # than the real-time charge for the identical token counts.
    assert guard.total_spent < compute_cost("openai", 1000, 200, batched=False)


def test_merge_summarisation_results_charges_real_gemini_batch_cost(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Gemini batch merges must flow through BudgetGuard too, same as OpenAI/Anthropic."""
    from utils import BudgetGuard
    from models_config import compute_cost

    doi = "10.9999/gemini.budget.paper"
    slug = doi_to_slug(doi)
    summaries_path = tmp_path / "summaries.jsonl"
    summaries_path.write_text(
        json.dumps({
            "doi": doi, "custom_id": slug, "input_source": "processed",
            "journal": "jvim", "models": {"gemini": {"status": "pending"}},
        }) + "\n",
        encoding="utf-8",
    )
    result_path = tmp_path / "gemini_sum_results.jsonl"
    result_path.write_text(
        _gemini_success_line(slug, json.dumps(_valid_summary_payload())),
        encoding="utf-8",
    )
    monkeypatch.setattr(check_batch_status, "SUMMARIES_PATH", summaries_path)
    monkeypatch.setattr(
        check_batch_status, "BATCH_SUMMARIES_JSONL_DIR", tmp_path / "readable")
    monkeypatch.setattr(summarizer, "SUMMARIES_PATH", summaries_path)

    guard = BudgetGuard(hard_stop=100.0)
    check_batch_status.merge_summarisation_results(
        result_path, provider="gemini", guard=guard)

    # _gemini_success_line reports 900 input / 180 output tokens.
    expected = compute_cost("gemini", 900, 180, batched=True)
    assert guard.total_spent == pytest.approx(expected)
    assert guard.total_spent > 0, "gemini batch merge recorded no spend at all"
    assert guard.total_spent < compute_cost("gemini", 900, 180, batched=False)


def test_merge_persists_results_even_when_the_budget_stop_fires(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A hard stop must not cost us results that were already paid for.

    Batch money is spent when the job runs, long before the merge. If add_cost
    exited before the write, the run would lose summaries it had already been
    billed for — the guard would be destroying value, not protecting it.
    """
    from utils import BudgetGuard

    doi = "10.9999/overbudget.paper"
    slug = doi_to_slug(doi)
    summaries_path = tmp_path / "summaries.jsonl"
    summaries_path.write_text(
        json.dumps({
            "doi": doi, "custom_id": slug, "input_source": "processed",
            "journal": "jvim", "models": {"openai": {"status": "pending"}},
        }) + "\n",
        encoding="utf-8",
    )
    result_path = tmp_path / "openai_sum_results.jsonl"
    result_path.write_text(
        _openai_success_line(slug, json.dumps(_valid_summary_payload())),
        encoding="utf-8",
    )
    monkeypatch.setattr(check_batch_status, "SUMMARIES_PATH", summaries_path)
    monkeypatch.setattr(
        check_batch_status, "BATCH_SUMMARIES_JSONL_DIR", tmp_path / "readable")
    monkeypatch.setattr(summarizer, "SUMMARIES_PATH", summaries_path)

    # A limit so low that any real result blows through it.
    guard = BudgetGuard(hard_stop=0.0000001)
    with pytest.raises(SystemExit):
        check_batch_status.merge_summarisation_results(
            result_path, provider="openai", guard=guard)

    rows = [json.loads(l) for l in
            summaries_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert rows[0]["models"]["openai"]["status"] == "success", (
        "the budget stop fired before the merged result was written to disk, "
        "discarding a summary that had already been paid for"
    )


def test_batch_submission_refuses_without_an_authorised_budget(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """BUDGET_HARD_STOP=0.00 is the fail-safe default; batch must honour it."""
    monkeypatch.setattr(batch_utils, "SUMMARIES_PATH", tmp_path / "summaries.jsonl")
    monkeypatch.setattr(summarizer, "SUMMARIES_PATH", tmp_path / "summaries.jsonl")
    monkeypatch.setattr(batch_utils, "BATCH_DIR", tmp_path / "batch")
    monkeypatch.setattr(batch_utils, "BATCH_JOBS_PATH", tmp_path / "batch_jobs.jsonl")
    monkeypatch.setattr(
        batch_utils, "_iter_manifest",
        lambda _p: iter([{"doi": "10.9999/x", "journal": "JVIM", "title": "T"}]))
    monkeypatch.setattr(batch_utils, "_read_cached_text", lambda _r: "Body.")

    submitted: list[str] = []
    monkeypatch.setattr(
        batch_utils, "submit_openai_batch",
        lambda p: submitted.append(str(p)) or {"job_id": "j", "provider": "openai"})
    monkeypatch.setattr(batch_utils, "record_batch_job", lambda _m: None)
    # BUDGET_HARD_STOP is read from the environment once at import time, so the
    # module attribute — not the env var — is what BudgetGuard() actually reads.
    import utils
    monkeypatch.setattr(utils, "BUDGET_HARD_STOP", 0.0)

    with pytest.raises(SystemExit, match="BUDGET_HARD_STOP"):
        batch_utils.run_batch_summarisation(
            manifest_path=tmp_path / "manifest.jsonl",
            resume=False, providers=["openai"])

    assert submitted == [], "submitted a paid batch with no budget authorised"


# In plain English: `summarize --mode batch` with no --providers flag used to
# pass ["openai", "anthropic", "gemini"] straight into run_batch_summarisation,
# which raised a ValueError the moment it reached "gemini" — there was no
# request builder or submitter for it. This is the regression test for that
# exact call shape, now that build_gemini_batch_request/submit_gemini_batch
# exist and are wired into the provider dispatch.
def test_run_batch_summarisation_handles_all_three_providers(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(summarizer, "SUMMARIES_PATH", tmp_path / "summaries.jsonl")
    monkeypatch.setattr(batch_utils, "SUMMARIES_PATH", tmp_path / "summaries.jsonl")
    monkeypatch.setattr(batch_utils, "BATCH_DIR", tmp_path / "batch")
    monkeypatch.setattr(batch_utils, "BATCH_JOBS_PATH", tmp_path / "batch_jobs.jsonl")
    monkeypatch.setattr(
        batch_utils, "_iter_manifest",
        lambda _p: iter([{"doi": "10.9999/x", "journal": "JVIM", "title": "T"}]))
    monkeypatch.setattr(batch_utils, "_read_cached_text", lambda _r: "Body.")

    submitted_gemini: list[str] = []
    monkeypatch.setattr(
        batch_utils, "submit_openai_batch",
        lambda p: {"job_id": "j-openai", "provider": "openai"})
    monkeypatch.setattr(
        batch_utils, "submit_anthropic_batch",
        lambda p: {"job_id": "j-anthropic", "provider": "anthropic"})
    monkeypatch.setattr(
        batch_utils, "submit_gemini_batch",
        lambda p, model_id: submitted_gemini.append(model_id)
        or {"job_id": "j-gemini", "provider": "gemini"})
    monkeypatch.setattr(batch_utils, "record_batch_job", lambda _m: None)

    result = batch_utils.run_batch_summarisation(
        manifest_path=tmp_path / "manifest.jsonl",
        resume=False, providers=["openai", "anthropic", "gemini"])

    job_ids = {job["provider"]: job["job_id"] for job in result["submitted"]}
    assert job_ids == {"openai": "j-openai", "anthropic": "j-anthropic", "gemini": "j-gemini"}
    assert submitted_gemini == [get_model_spec("gemini").model_id]


# ---------------------------------------------------------------------------
# _refuse_duplicate_submission: no double-submitting a batch job
# ---------------------------------------------------------------------------

# In plain English: re-running the submission step while an earlier job for
# the same stage/provider is still unresolved (not yet merged) must not
# silently submit a second, duplicate paid batch job.

def test_refuse_duplicate_submission_blocks_when_unresolved_job_exists(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(batch_utils, "BATCH_DIR", tmp_path / "batch")
    monkeypatch.setattr(
        batch_utils, "load_batch_jobs",
        lambda: [{"provider": "openai", "job_id": "job-1", "stage": "summarize"}],
    )
    # No .merged marker on disk for job-1 -> counts as unresolved.
    with pytest.raises(SystemExit, match="REFUSING"):
        batch_utils._refuse_duplicate_submission("summarize", ["openai"], force=False)


def test_refuse_duplicate_submission_ignores_merged_jobs(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    batch_dir = tmp_path / "batch"
    batch_dir.mkdir()
    monkeypatch.setattr(batch_utils, "BATCH_DIR", batch_dir)
    monkeypatch.setattr(
        batch_utils, "load_batch_jobs",
        lambda: [{"provider": "openai", "job_id": "job-1", "stage": "summarize"}],
    )
    # .with_suffix(".merged") REPLACES the .jsonl suffix, not appends to it.
    (batch_dir / "openai_job-1_results.merged").write_text("merged\n", encoding="utf-8")

    batch_utils._refuse_duplicate_submission("summarize", ["openai"], force=False)  # must not raise


def test_refuse_duplicate_submission_bypassed_with_force(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(batch_utils, "BATCH_DIR", tmp_path / "batch")
    monkeypatch.setattr(
        batch_utils, "load_batch_jobs",
        lambda: [{"provider": "openai", "job_id": "job-1", "stage": "summarize"}],
    )
    batch_utils._refuse_duplicate_submission("summarize", ["openai"], force=True)  # must not raise


def test_refuse_duplicate_submission_ignores_other_stage_and_provider(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(batch_utils, "BATCH_DIR", tmp_path / "batch")
    monkeypatch.setattr(
        batch_utils, "load_batch_jobs",
        lambda: [{"provider": "anthropic", "job_id": "job-1", "stage": "evaluate"}],
    )
    # Different stage AND different provider than what's being submitted now.
    batch_utils._refuse_duplicate_submission("summarize", ["openai"], force=False)  # must not raise


def test_safe_job_id_for_path_strips_slashes() -> None:
    assert batch_utils.safe_job_id_for_path("batches/abc123") == "batches_abc123"
    assert batch_utils.safe_job_id_for_path("plain-id") == "plain-id"


# In plain English: a slot already `pending` (submitted, awaiting merge) must
# not be resubmitted under --resume — direct regression test for the
# double-submission bug (previously only `success` was skipped).
def test_run_batch_summarisation_resume_skips_pending_slot(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    doi = "10.9999/pending.paper"
    summaries_path = tmp_path / "summaries.jsonl"
    summaries_path.write_text(json.dumps({
        "doi": doi, "custom_id": doi_to_slug(doi), "input_source": "processed",
        "models": {"openai": {"status": "pending"}},
    }) + "\n", encoding="utf-8")

    monkeypatch.setattr(summarizer, "SUMMARIES_PATH", summaries_path)
    monkeypatch.setattr(batch_utils, "SUMMARIES_PATH", summaries_path)
    monkeypatch.setattr(batch_utils, "BATCH_DIR", tmp_path / "batch")
    monkeypatch.setattr(batch_utils, "BATCH_JOBS_PATH", tmp_path / "batch_jobs.jsonl")
    monkeypatch.setattr(
        batch_utils, "_iter_manifest",
        lambda _p: iter([{"doi": doi, "journal": "JVIM", "title": "T"}]))
    monkeypatch.setattr(batch_utils, "_read_cached_text", lambda _r: "Body.")
    submitted: list[str] = []
    monkeypatch.setattr(
        batch_utils, "submit_openai_batch",
        lambda p: submitted.append(str(p)) or {"job_id": "j", "provider": "openai"})
    monkeypatch.setattr(batch_utils, "record_batch_job", lambda _m: None)

    batch_utils.run_batch_summarisation(
        manifest_path=tmp_path / "manifest.jsonl",
        resume=True, providers=["openai"])

    assert submitted == [], "resubmitted a paper whose slot was already 'pending'"


# ---------------------------------------------------------------------------
# --limit on batch mode (Q2): a real, safe way to smoke-test before the full
# corpus. Caps at the first N manifest rows with cached text.
# ---------------------------------------------------------------------------

def test_run_batch_summarisation_respects_paper_limit(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(summarizer, "SUMMARIES_PATH", tmp_path / "summaries.jsonl")
    monkeypatch.setattr(batch_utils, "SUMMARIES_PATH", tmp_path / "summaries.jsonl")
    monkeypatch.setattr(batch_utils, "BATCH_DIR", tmp_path / "batch")
    monkeypatch.setattr(batch_utils, "BATCH_JOBS_PATH", tmp_path / "batch_jobs.jsonl")
    records = [
        {"doi": f"10.9999/paper.{i}", "journal": "JVIM", "title": f"Paper {i}"}
        for i in range(5)
    ]
    monkeypatch.setattr(batch_utils, "_iter_manifest", lambda _p: iter(records))
    monkeypatch.setattr(batch_utils, "_read_cached_text", lambda _r: "Body.")
    monkeypatch.setattr(
        batch_utils, "submit_openai_batch",
        lambda p: {"job_id": "j", "provider": "openai"})
    monkeypatch.setattr(batch_utils, "record_batch_job", lambda _m: None)

    result = batch_utils.run_batch_summarisation(
        manifest_path=tmp_path / "manifest.jsonl",
        resume=False, providers=["openai"], paper_limit=2)

    assert len(result["slug_to_doi"]) == 2, "paper_limit=2 should have capped the run at 2 papers"


# ---------------------------------------------------------------------------
# run_batch_evaluation honors doi_filter (Fix 8)
# ---------------------------------------------------------------------------

def test_run_batch_evaluation_honors_doi_filter(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    in_doi = "10.9999/in.filter"
    out_doi = "10.9999/out.of.filter"
    summaries_path = tmp_path / "summaries.jsonl"
    summaries_path.write_text(
        "\n".join(json.dumps({
            "doi": doi, "custom_id": doi_to_slug(doi), "input_source": "processed",
            "models": {"anthropic": {"status": "success", "summary": "Candidate."}},
        }) for doi in (in_doi, out_doi)) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(batch_utils, "SUMMARIES_PATH", summaries_path)
    monkeypatch.setattr(batch_utils, "BATCH_DIR", tmp_path / "batch")
    monkeypatch.setattr(batch_utils, "BATCH_JOBS_PATH", tmp_path / "batch_jobs.jsonl")
    monkeypatch.setattr(batch_utils, "_read_cached_text", lambda _r: "Reference body.")
    monkeypatch.setattr(
        batch_utils, "submit_openai_batch",
        lambda p: {"job_id": "j", "provider": "openai"})
    monkeypatch.setattr(batch_utils, "record_batch_job", lambda _m: None)

    result = batch_utils.run_batch_evaluation(
        judges=["openai"], resume=False, doi_filter={in_doi},
    )

    assert result["submitted"], "no job submitted for the in-filter DOI"
    jsonl_files = sorted((tmp_path / "batch").glob("openai_eval_*.jsonl"))
    assert len(jsonl_files) == 1
    rows = [json.loads(l) for l in jsonl_files[0].read_text(encoding="utf-8").splitlines() if l.strip()]
    submitted_slugs = {row["custom_id"].split("__")[0] for row in rows}
    assert submitted_slugs == {doi_to_slug(in_doi)}, (
        "run_batch_evaluation submitted a row for a DOI outside doi_filter"
    )


# ---------------------------------------------------------------------------
# A failed judge result must not abort the whole merge
# ---------------------------------------------------------------------------

# In plain English: a batch of judge results can contain individual failures.
# Hitting one must log it and move on to the remaining good results — not throw
# away the entire merge. This used to raise UnboundLocalError on the first
# failure because the error branch named a variable bound further down.
def test_merge_evaluation_results_survives_a_failed_result(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    good_doi = "10.9999/good.paper"
    bad_doi = "10.9999/bad.paper"
    good_slug, bad_slug = doi_to_slug(good_doi), doi_to_slug(bad_doi)
    evaluations_path = tmp_path / "evaluations.jsonl"

    judge_payload = json.dumps({
        "factual_accuracy": 4, "completeness": 4,
        "clinical_relevance": 4, "organization": 4,
        "hallucination": {"present": False, "count": 0, "claims": []},
        "confidence_score": 4, "reasoning": "fine",
    })
    # The failure comes FIRST, so if it aborts the loop the good row is lost.
    result_path = tmp_path / "openai_eval_results.jsonl"
    result_path.write_text(
        _openai_error_line(f"{bad_slug}__anthropic") + "\n"
        + _openai_success_line(f"{good_slug}__anthropic", judge_payload) + "\n",
        encoding="utf-8",
    )

    def _patched_load(*_args, **_kwargs):
        return {
            f"{good_doi}::processed": {
                "doi": good_doi, "custom_id": good_slug, "input_source": "processed",
                "models": {"anthropic": {"status": "success", "summary": "Candidate."}},
            },
            f"{bad_doi}::processed": {
                "doi": bad_doi, "custom_id": bad_slug, "input_source": "processed",
                "models": {"anthropic": {"status": "success", "summary": "Candidate."}},
            },
        }

    monkeypatch.setattr(check_batch_status, "_load_summaries", _patched_load)
    # merge_evaluation_results passes check_batch_status.EVALUATIONS_PATH to
    # append_evaluation explicitly, so THIS is the name that must be redirected.
    # Patching evaluator.EVALUATIONS_PATH alone appends to the real
    # data/evaluations.jsonl — an append-only research ledger.
    monkeypatch.setattr(check_batch_status, "EVALUATIONS_PATH", evaluations_path)

    merged = check_batch_status.merge_evaluation_results(
        result_path, provider="openai", judge="openai",
    )

    assert merged == 1, "the good result after the failed one was not merged"
    rows = [json.loads(l) for l in
            evaluations_path.read_text(encoding="utf-8").strip().splitlines()]
    assert [row["doi"] for row in rows] == [good_doi]


# ---------------------------------------------------------------------------
# poll_all: download gating and merge-retry
# ---------------------------------------------------------------------------

def _poll_harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *,
                  status: str, stage: str = "evaluate") -> dict:
    """Wire poll_all up to a single fake job and record what it does."""
    batch_dir = tmp_path / "batch"
    batch_dir.mkdir()
    calls = {"downloaded": 0, "merged": 0}

    monkeypatch.setattr(check_batch_status, "BATCH_DIR", batch_dir)
    monkeypatch.setattr(
        check_batch_status, "load_batch_jobs",
        lambda: [{"provider": "openai", "job_id": "job-1", "stage": stage, "judge": "openai"}],
    )
    monkeypatch.setattr(
        check_batch_status, "fetch_openai_status",
        lambda _job_id: {"status": status, "output_file_id": "file-1"},
    )

    def _fake_download(_file_id, path: Path) -> None:
        calls["downloaded"] += 1
        path.write_text("{}\n", encoding="utf-8")

    def _fake_merge(path, **_kwargs) -> int:
        calls["merged"] += 1
        return 1

    monkeypatch.setattr(check_batch_status, "download_openai_results", _fake_download)
    monkeypatch.setattr(check_batch_status, "merge_evaluation_results", _fake_merge)
    monkeypatch.setattr(check_batch_status, "merge_summarisation_results", _fake_merge)
    calls["result_path"] = batch_dir / "openai_job-1_results.jsonl"
    return calls


# In plain English: if a batch job failed or expired, there is nothing to
# download. Asking anyway wastes a request and logs a confusing error.
@pytest.mark.parametrize("status", ["failed", "expired", "cancelled"])
def test_poll_all_does_not_download_unsuccessful_jobs(
        status: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _poll_harness(tmp_path, monkeypatch, status=status)
    check_batch_status.poll_all()
    assert calls["downloaded"] == 0, f"downloaded results for a '{status}' job"
    assert calls["merged"] == 0


# In plain English: if a previous run downloaded the results but crashed before
# merging them, the next run must finish the job. The downloaded file must not
# be what stops the retry — that would strand paid results on disk forever.
def test_poll_all_merges_previously_downloaded_but_unmerged_results(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _poll_harness(tmp_path, monkeypatch, status="completed")
    # Simulate the crashed run: results on disk, no merge marker.
    calls["result_path"].write_text("{}\n", encoding="utf-8")

    check_batch_status.poll_all()

    assert calls["downloaded"] == 0, "re-downloaded a file that was already present"
    assert calls["merged"] == 1, (
        "results already on disk were never merged — a crash between download "
        "and merge would strand them permanently."
    )


# In plain English: merging evaluations APPENDS to an append-only ledger, so
# polling twice must not merge twice or every judge row would be duplicated.
def test_poll_all_does_not_merge_twice(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _poll_harness(tmp_path, monkeypatch, status="completed")

    check_batch_status.poll_all()
    check_batch_status.poll_all()

    assert calls["downloaded"] == 1
    assert calls["merged"] == 1, (
        "merged the same results twice — evaluations.jsonl is append-only, so "
        "this would duplicate every row and inflate the judge panel."
    )


# In plain English: Gemini batch job names are resource paths like
# "batches/abc123", not bare ids — poll_all must sanitise the "/" out of the
# result filename (so it doesn't create a stray subdirectory) while still
# passing the REAL job_id (with the slash) to fetch/download.
def test_poll_all_handles_a_gemini_job(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    batch_dir = tmp_path / "batch"
    batch_dir.mkdir()
    calls = {"downloaded_with": None, "merged_provider": None}

    monkeypatch.setattr(check_batch_status, "BATCH_DIR", batch_dir)
    monkeypatch.setattr(
        check_batch_status, "load_batch_jobs",
        lambda: [{"provider": "gemini", "job_id": "batches/abc123", "stage": "summarize"}],
    )
    monkeypatch.setattr(
        check_batch_status, "fetch_gemini_status",
        lambda _job_id: {"status": "completed", "output_file_name": "files/xyz789"},
    )

    def _fake_download(file_name, path: Path) -> None:
        calls["downloaded_with"] = file_name
        path.write_text("{}\n", encoding="utf-8")

    def _fake_merge(_path, *, provider, **_kwargs) -> int:
        calls["merged_provider"] = provider
        return 1

    monkeypatch.setattr(check_batch_status, "download_gemini_results", _fake_download)
    monkeypatch.setattr(check_batch_status, "merge_summarisation_results", _fake_merge)

    check_batch_status.poll_all()

    assert calls["downloaded_with"] == "files/xyz789"
    assert calls["merged_provider"] == "gemini"
    result_path = batch_dir / "gemini_batches_abc123_results.jsonl"
    assert result_path.exists(), "job_id slash was not sanitised out of the result filename"
    assert result_path.with_suffix(".merged").exists()


# In plain English: an OpenAI batch job can be terminal-success (status
# "completed") while every request in it failed — output_file_id is then
# None and the failure detail lives in error_file_id instead. Before this
# fix, poll_all's download branch only checked output_file_id for OpenAI, so
# this case fell through to a silent `else: continue`: no .merged marker was
# ever written, which meant _refuse_duplicate_submission treated the job as
# still unresolved and blocked every future OpenAI batch submission. poll_all
# must instead log each failed row and still mark the job resolved.
def test_poll_all_handles_an_all_failed_openai_job(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    batch_dir = tmp_path / "batch"
    batch_dir.mkdir()
    logged: list[tuple[str, str, str]] = []
    merge_calls = {"count": 0}

    monkeypatch.setattr(check_batch_status, "BATCH_DIR", batch_dir)
    monkeypatch.setattr(
        check_batch_status, "load_batch_jobs",
        lambda: [{"provider": "openai", "job_id": "job-failed", "stage": "summarize"}],
    )
    monkeypatch.setattr(
        check_batch_status, "fetch_openai_status",
        lambda _job_id: {
            "status": "completed",
            "output_file_id": None,
            "error_file_id": "file-err-1",
            "request_counts": {"total": 2, "completed": 0, "failed": 2},
        },
    )

    error_jsonl = "\n".join([
        json.dumps({
            "id": "batch-req-1",
            "custom_id": "10_1111_jvim_16872",
            "response": {"status_code": 400, "request_id": "req-1",
                         "body": {"error": {"message": "Invalid schema for response_format "
                                                        "'VeterinarySummary': ... Missing "
                                                        "'sample_size'."}}},
            "error": None,
        }),
        json.dumps({
            "id": "batch-req-2",
            "custom_id": "10_2222_other_99999",
            "response": {"status_code": 400, "request_id": "req-2",
                         "body": {"error": {"message": "Invalid schema for response_format "
                                                        "'VeterinarySummary': ... Missing "
                                                        "'sample_size'."}}},
            "error": None,
        }),
    ])

    monkeypatch.setattr(
        check_batch_status, "download_openai_error_file",
        lambda _error_file_id: error_jsonl,
    )
    monkeypatch.setattr(
        check_batch_status, "log_error",
        lambda doi, stage, message: logged.append((doi, stage, message)),
    )

    def _fake_merge(*_args, **_kwargs) -> int:
        merge_calls["count"] += 1
        return 0

    monkeypatch.setattr(check_batch_status, "merge_summarisation_results", _fake_merge)
    monkeypatch.setattr(check_batch_status, "merge_evaluation_results", _fake_merge)

    check_batch_status.poll_all()

    assert len(logged) == 2
    assert logged[0][0] == "10_1111_jvim_16872"
    assert logged[0][1] == "batch_merge"
    assert "sample_size" in logged[0][2]
    assert logged[1][0] == "10_2222_other_99999"

    assert merge_calls["count"] == 0, "nothing to merge for an all-failed job"

    result_path = batch_dir / "openai_job-failed_results.jsonl"
    merged_marker = result_path.with_suffix(".merged")
    assert merged_marker.exists(), (
        ".merged marker must be written so _refuse_duplicate_submission treats "
        "this job as resolved instead of blocking future submissions forever"
    )
    assert "all_failed" in merged_marker.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Batch submission must never clobber rows it did not produce
# ---------------------------------------------------------------------------

# In plain English: `summarize --mode batch` rewrites the whole summaries.jsonl
# file from an in-memory dict before it submits anything. If that dict didn't
# start from what was already on disk, the rewrite would silently delete every
# summary the batch path doesn't produce — the raw_text/pdf comparison rows,
# and (with GEMINI_BATCH_ENABLED=false, the default) every gemini result,
# since those exist ONLY in the file being rewritten in that configuration.
# That is unrecoverable loss of paid work, so it gets a test.
def test_run_batch_summarisation_preserves_rows_it_does_not_produce(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    batch_doi = "10.9999/batch.target"
    other_doi = "10.9999/other.paper"
    summaries_path = tmp_path / "summaries.jsonl"

    # Three rows the batch run must leave intact: a gemini success (never
    # batched), a raw_text row for the very DOI being batched (different input
    # source, so a different key), and an unrelated paper.
    gemini_row = {
        "doi": other_doi,
        "custom_id": doi_to_slug(other_doi),
        "input_source": "processed",
        "models": {"gemini": {"status": "success", "summary": "Gemini prose."}},
    }
    raw_text_row = {
        "doi": batch_doi,
        "custom_id": f"{doi_to_slug(batch_doi)}__raw_text",
        "input_source": "raw_text",
        "models": {"openai": {"status": "success", "summary": "Raw-text prose."}},
    }
    summaries_path.write_text(
        json.dumps(gemini_row) + "\n" + json.dumps(raw_text_row) + "\n",
        encoding="utf-8",
    )

    # `load_existing_summaries` reads summarizer's own module-level path, while
    # `_write_all_summaries` is called with batch_utils'. Both must point here.
    monkeypatch.setattr(summarizer, "SUMMARIES_PATH", summaries_path)
    monkeypatch.setattr(batch_utils, "SUMMARIES_PATH", summaries_path)
    monkeypatch.setattr(batch_utils, "BATCH_DIR", tmp_path / "batch")
    monkeypatch.setattr(batch_utils, "BATCH_JOBS_PATH", tmp_path / "batch_jobs.jsonl")

    monkeypatch.setattr(
        batch_utils, "_iter_manifest",
        lambda _path: iter([{"doi": batch_doi, "journal": "JVIM", "title": "Batch paper"}]),
    )
    monkeypatch.setattr(
        batch_utils, "_read_cached_text", lambda _record: "Article body text.",
    )
    # Stop before the network: submission is not what this test is about.
    monkeypatch.setattr(
        batch_utils, "submit_openai_batch",
        lambda _path: {"job_id": "job-test", "provider": "openai"},
    )
    monkeypatch.setattr(batch_utils, "record_batch_job", lambda _meta: None)

    # resume=False is the case that used to wipe the file.
    batch_utils.run_batch_summarisation(
        manifest_path=tmp_path / "manifest.jsonl",
        resume=False,
        providers=["openai"],
    )

    rows = [
        json.loads(line)
        for line in summaries_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    by_key = {(row["doi"], row["input_source"]): row for row in rows}

    assert (other_doi, "processed") in by_key, (
        "gemini's row was deleted by a batch submission — gemini is never "
        "batched, so this summary cannot be regenerated by re-running batch."
    )
    assert by_key[(other_doi, "processed")]["models"]["gemini"]["summary"] == "Gemini prose."
    assert (batch_doi, "raw_text") in by_key, (
        "the raw_text row for the batched DOI was deleted; batch only owns "
        "the processed input source."
    )
    assert by_key[(batch_doi, "raw_text")]["models"]["openai"]["summary"] == "Raw-text prose."
    # ...and the row the run actually owns is present with a pending slot.
    assert by_key[(batch_doi, "processed")]["models"]["openai"]["status"] == "pending"


# ---------------------------------------------------------------------------
# Batch merges write the same readable pair dev/single runs produce
# ---------------------------------------------------------------------------

# In plain English: once a batch job's results are merged back, the batch run
# should leave behind the same human-readable files a dev or single run does —
# a bulleted .txt per paper at the top level, plus the flowing-prose version in
# prose/. That's what makes the finished 250-paper corpus ready for
# `evaluate --mode dev --source-dir data/batch_summaries_jsonl` and for human
# validation without any extra step.
def test_merge_summarisation_results_writes_readable_batch_outputs(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    doi = "10.9999/batch.readable"
    slug = doi_to_slug(doi)
    summaries_path = tmp_path / "summaries.jsonl"
    summaries_path.write_text(
        json.dumps({
            "doi": doi,
            "custom_id": slug,
            "input_source": "processed",
            "journal": "jvim",
            "models": {"openai": {"status": "pending"}},
        }) + "\n",
        encoding="utf-8",
    )
    result_path = tmp_path / "openai_sum_results.jsonl"
    result_path.write_text(
        _openai_success_line(
            slug,
            json.dumps(_valid_summary_payload(
                summary_text="Background This is the flowing prose summary text.",
            )),
        ),
        encoding="utf-8",
    )
    batch_dir = tmp_path / "batch_summaries_jsonl"

    monkeypatch.setattr(check_batch_status, "SUMMARIES_PATH", summaries_path)
    monkeypatch.setattr(check_batch_status, "BATCH_SUMMARIES_JSONL_DIR", batch_dir)
    monkeypatch.setattr(summarizer, "SUMMARIES_PATH", summaries_path)

    assert check_batch_status.merge_summarisation_results(result_path, provider="openai") == 1

    bullets = sorted(batch_dir.glob("*.txt"))
    prose = sorted((batch_dir / "prose").glob("*.txt"))
    assert len(bullets) == 1
    assert len(prose) == 1

    # The DOI header is what every downstream folder reader keys off.
    bullet_text = bullets[0].read_text(encoding="utf-8")
    assert bullet_text.startswith(f"DOI: {doi}")
    assert "batch" in bullet_text  # run-kind intro names the batch run

    # The prose file carries the judged summary_text and its word count.
    prose_text = prose[0].read_text(encoding="utf-8")
    assert prose_text.startswith(f"DOI: {doi}")
    assert "Summary (prose):" in prose_text
    assert "flowing prose summary text" in prose_text


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
