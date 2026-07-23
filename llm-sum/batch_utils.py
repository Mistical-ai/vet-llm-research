"""
llm-sum/batch_utils.py — Build & submit OpenAI / Anthropic batch jobs
======================================================================

Batch APIs accept a JSONL of requests, run them within 24h, and charge
50% of the real-time price. The trade-off: results come back out of order,
so we need a `custom_id` on every request to map responses to papers.

Our convention: `custom_id == doi_to_slug(doi)` for summarisation,
and `custom_id == f"{doi_slug}__{summariser}"` for evaluation.

This module:
    1. Builds one JSONL per provider per stage in data/batch/.
    2. Submits the job and persists the job_id to data/batch_jobs.jsonl
       (so a crash mid-run does NOT lose paid-for batch handles).
    3. Provides parsers for downloaded result files (used by
       check_batch_status.py).

Gemini batch support lives here too, gated behind GEMINI_BATCH_ENABLED (see
.env.template section 12). When that flag is off (the default), callers route
Gemini through summarizer.run_realtime() instead — see summarizer.main()'s
provider split in the `profile.use_batch` branch.
"""

from __future__ import annotations

from _bootstrap import *  # noqa: F401,F403

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from models_config import (  # noqa: E402
    anthropic_cache_control,
    get_model_spec,
    openai_prompt_cache_key,
    PROMPT_CACHE_ENABLED,
)
from file_paths import doi_to_slug  # noqa: E402
from utils import BudgetGuard, log_error  # noqa: E402

BATCH_JOBS_PATH = DATA_DIR / "batch_jobs.jsonl"
SUMMARIES_PATH = DATA_DIR / "summaries.jsonl"
MANIFEST_PATH = DATA_DIR / "manifest.jsonl"

# Strictest common denominator across providers' custom_id/key constraints
# (observed: Anthropic rejects the ENTIRE batch if any one custom_id fails
# its pattern check). doi_to_slug() only escapes '.', '/', ':' — a DOI with
# any other character (e.g. the literal parentheses in a bogus journal-ISSN
# placeholder DOI) produces a custom_id that fails this, so it's checked
# before a request is ever built — see run_batch_summarisation.
_VALID_CUSTOM_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# Mirrors evaluator.JUDGE_PROMPT_SHAPE exactly (not imported from there, to
# keep this module's lightweight parsers from pulling in evaluator.py's
# heavier import graph merely to read one constant string). Both must be
# bumped together if the judge prompt's block layout ever changes again —
# see evaluator.py's definition for the full rationale.
JUDGE_PROMPT_SHAPE = "segmented_v1"

TEMPERATURE = float(os.getenv("TEMPERATURE", "0.0"))
SEED = int(os.getenv("SEED", "42"))
# Keep batch and real-time runs on one output cap. A lower batch-only default
# caused structured summaries to truncate before they could be parsed.
MAX_OUTPUT_TOKENS = int(os.getenv("MAX_OUTPUT_TOKENS", "1200"))


def _timestamp_slug() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _assert_budget_authorised(stage: str, request_count: int) -> None:
    """Refuse to submit a batch when no budget has been authorised.

    Batch is the one paid path whose cost cannot be charged at call time: the
    provider bills for work that completes hours later, so the real spend is
    only knowable at merge (see check_batch_status._charge_batch_result). That
    makes submission the last point where a runaway can still be prevented for
    free — once the job is accepted, the money is committed no matter what the
    guard does afterwards.

    Deliberately a floor check, not an estimate: BUDGET_HARD_STOP defaults to
    $0.00 precisely so an unconfigured environment cannot spend anything, and
    batch is the full-corpus mode where an accidental submission is most
    expensive. Estimating the cost here to pre-charge it would violate the rule
    that token counts come from provider responses, never from guesses.
    """
    guard = BudgetGuard()
    if guard.hard_stop <= 0:
        raise SystemExit(
            f"[phase3:batch] REFUSING to submit {request_count} {stage} request(s): "
            f"BUDGET_HARD_STOP is ${guard.hard_stop:.2f}. Batch results are billed "
            f"even though their cost is only recorded when you later merge them, "
            f"so set BUDGET_HARD_STOP in .env to the amount you intend to spend "
            f"before submitting."
        )


# ---------------------------------------------------------------------------
# Build request payloads
# ---------------------------------------------------------------------------

def build_openai_request(custom_id: str, user_message: str | tuple[str, str, str],
                          model_id: str, *, for_judge: bool = False,
                          cache_article_id: str | None = None) -> dict:
    """One row of OpenAI's batch JSONL format.

    ``for_judge=True`` builds a judge request instead of a summarisation
    request: plain ``json_object`` mode, matching
    ``evaluator._call_judge_openai`` exactly, rather than the strict
    ``VeterinarySummary`` schema a judge cannot answer in. In that case
    ``user_message`` is the 3-tuple returned by
    ``evaluator.build_judge_prompt_segments`` (rubric_prefix, reference_block,
    candidate_block), segmented into a system + user message exactly like the
    real-time path (Phase A1) — never a flat string. Summarisation requests
    (``for_judge=False``) are unaffected: nothing to cache there (Finding D),
    so ``user_message`` stays a plain string.
    """
    if for_judge:
        rubric_prefix, reference_block, candidate_block = user_message  # type: ignore[misc]
        response_format = {"type": "json_object"}
        body: dict[str, Any] = {
            "model": model_id,
            "messages": [
                {"role": "system", "content": rubric_prefix},
                {"role": "user", "content": reference_block + candidate_block},
            ],
            "temperature": TEMPERATURE,
            "max_completion_tokens": MAX_OUTPUT_TOKENS,
            "seed": SEED,
            "response_format": response_format,
        }
        # PROMPT_CACHE_ENABLED toggles ONLY this key — a routing hint, not a
        # scoring or block-structure change (Phase A3).
        if PROMPT_CACHE_ENABLED:
            body["prompt_cache_key"] = openai_prompt_cache_key(cache_article_id)
        return {
            "custom_id": custom_id,
            "method": "POST",
            "url": "/v1/chat/completions",
            "body": body,
        }
    else:
        from summarizer import VeterinarySummary  # noqa: E402
        # OpenAI's Structured Outputs "strict" mode requires every key in
        # `properties` to also appear in `required` (with optional-semantics
        # fields expressed as nullable `anyOf` unions instead of just being
        # omitted) — plain `VeterinarySummary.model_json_schema()` follows
        # normal JSON Schema semantics instead, so a real batch job rejected
        # it: "'required' is required to be supplied and to be an array
        # including every key in properties. Missing 'sample_size'."
        # `openai.lib._pydantic` is a private module (leading underscore),
        # but it's the exact function OpenAI's own `.parse()` helper uses
        # internally for the real-time path (see summarizer._call_openai,
        # which passes the Pydantic class straight to
        # client.beta.chat.completions.parse and never hits this bug).
        # Reusing it here keeps the batch schema byte-for-byte consistent
        # with what real-time OpenAI calls already send successfully,
        # instead of hand-rolling the strict-mode required/nullable
        # transform ourselves.
        from openai.lib._pydantic import to_strict_json_schema  # noqa: E402
        response_format = {
            "type": "json_schema",
            "json_schema": {
                "name": "VeterinarySummary",
                "schema": to_strict_json_schema(VeterinarySummary),
                "strict": True,
            },
        }

    return {
        "custom_id": custom_id,
        "method": "POST",
        "url": "/v1/chat/completions",
        "body": {
            "model": model_id,
            "messages": [{"role": "user", "content": user_message}],
            "temperature": TEMPERATURE,
            "max_completion_tokens": MAX_OUTPUT_TOKENS,
            "seed": SEED,
            "response_format": response_format,
        },
    }


def build_gemini_batch_request(custom_id: str, user_message: str | tuple[str, str, str],
                               model_id: str, *, for_judge: bool = False) -> dict:
    """
    One row of Gemini's Batch Mode JSONL format (ai.google.dev/gemini-api/docs/batch-mode).

    Google's file-based batch input is ``{"key": ..., "request": {...}}``, where
    ``request`` is a raw REST ``GenerateContentRequest``: ``contents`` plus a
    ``generation_config`` field (snake_case) holding ``temperature``,
    ``max_output_tokens``, ``response_mime_type``, and (for summaries)
    ``response_schema``. This is confirmed against Google's live batch-mode
    docs, whose file-based JSONL example is exactly
    ``{"key": ..., "request": {"contents": [...], "generation_config": {...}}}``
    — note this is *not* the same key the real-time
    ``client.models.generate_content(config=...)`` call in
    ``summarizer._call_gemini`` uses (that ``config`` name is an SDK wrapper
    for inline calls only; the batch endpoint rejects it outright with
    ``no such field: 'config'``). Our ``custom_id`` convention becomes Gemini's
    ``key`` field, which the provider echoes back on every result line so
    ``parse_gemini_result_line`` can rejoin it the same way the OpenAI/
    Anthropic parsers rejoin ``custom_id``.

    Unlike OpenAI/Anthropic, Gemini's batch job takes its model as a
    ``client.batches.create(model=...)`` argument rather than a per-row field
    — ``model_id`` is accepted here only to keep this function's signature
    matching ``build_openai_request``/``build_anthropic_request`` for the
    shared call sites in ``run_batch_summarisation``/``run_batch_evaluation``.

    ``for_judge=True`` drops ``response_schema`` — matching
    ``evaluator._call_judge_gemini``, which asks for JSON mime type but never
    forces the ``VeterinarySummary`` schema a judge cannot answer in. In that
    case ``user_message`` is the 3-tuple from
    ``evaluator.build_judge_prompt_segments``, rendered as three
    ``contents[0].parts[]`` entries in segment order — matching
    ``evaluator._call_judge_gemini``'s real-time shape exactly (verification
    item 3), so the same corpus scored partly by batch and partly real-time
    never sees two different prompt shapes. Summarisation requests
    (``for_judge=False``) are unaffected: a single part, unchanged.
    """
    from summarizer import gemini_response_schema, _gemini_output_token_limit  # noqa: E402

    config: dict[str, Any] = {
        "temperature": TEMPERATURE,
        "max_output_tokens": _gemini_output_token_limit(),
        "response_mime_type": "application/json",
        # Disable "thinking": invisible reasoning tokens were eating the output
        # budget on real batch jobs, causing MAX_TOKENS truncation on otherwise-
        # fine papers (large token cap configured, tiny visible output, cut off
        # anyway). Neither summarization nor judging needs open-ended reasoning
        # here — both are fixed-shape extraction/scoring tasks — so this is
        # applied unconditionally for judge and non-judge requests alike.
        "thinking_config": {"thinking_budget": 0},
    }
    if not for_judge:
        config["response_schema"] = gemini_response_schema()

    if for_judge:
        rubric_prefix, reference_block, candidate_block = user_message  # type: ignore[misc]
        parts = [
            {"text": rubric_prefix},
            {"text": reference_block},
            {"text": candidate_block},
        ]
    else:
        parts = [{"text": user_message}]

    return {
        "key": custom_id,
        "request": {
            "contents": [{"parts": parts}],
            "generation_config": config,
        },
    }


def build_anthropic_request(custom_id: str, user_message: str | tuple[str, str, str],
                            model_id: str, *, for_judge: bool = False) -> dict:
    """
    One row of Anthropic's Message Batches format. The shape differs from
    OpenAI's: the request body is nested under "params" and the URL is
    implicit.

    ``for_judge=True`` omits ``tools``/``tool_choice`` entirely — plain text
    completion, matching ``evaluator._call_judge_anthropic``. This isn't just
    a schema-accuracy detail: a forced ``tool_choice`` makes Anthropic return
    a ``tool_use`` content block instead of a ``text`` block, and
    ``parse_anthropic_result_line(..., expect_summary_schema=False)`` (what
    the judge merge path uses) only reads ``text``-type blocks — so a forced
    tool call would come back as an empty string, not just a wrong shape.

    In the judge case ``user_message`` is the 3-tuple from
    ``evaluator.build_judge_prompt_segments``: the rubric goes into its own
    ``system`` block, and the reference/candidate texts become two content
    blocks in the one user message — matching
    ``evaluator._call_judge_anthropic``'s real-time shape exactly. Cache
    markers (Phase A3, flag-gated) land on the system block and the reference
    block ONLY, never the candidate block — see that function's docstring.
    """
    if for_judge:
        rubric_prefix, reference_block, candidate_block = user_message  # type: ignore[misc]
        system_block: dict[str, Any] = {"type": "text", "text": rubric_prefix}
        reference_content_block: dict[str, Any] = {"type": "text", "text": reference_block}
        candidate_content_block: dict[str, Any] = {"type": "text", "text": candidate_block}
        if PROMPT_CACHE_ENABLED:
            marker = anthropic_cache_control()
            system_block["cache_control"] = marker
            reference_content_block["cache_control"] = marker
        params = {
            "model": model_id,
            "max_tokens": MAX_OUTPUT_TOKENS,
            "temperature": TEMPERATURE,
            "system": [system_block],
            "messages": [{"role": "user", "content": [reference_content_block, candidate_content_block]}],
        }
        return {"custom_id": custom_id, "params": params}

    params: dict[str, Any] = {
        "model": model_id,
        "max_tokens": MAX_OUTPUT_TOKENS,
        "temperature": TEMPERATURE,
        "messages": [{"role": "user", "content": user_message}],
    }
    if not for_judge:
        from summarizer import VeterinarySummary  # noqa: E402
        params["tools"] = [{
            "name": "VeterinarySummary",
            "description": (
                "Return the veterinary article summary using exactly the "
                "VeterinarySummary schema."
            ),
            "input_schema": VeterinarySummary.model_json_schema(),
        }]
        params["tool_choice"] = {"type": "tool", "name": "VeterinarySummary"}

    return {
        "custom_id": custom_id,
        "params": params,
    }


# ---------------------------------------------------------------------------
# Write JSONL to disk
# ---------------------------------------------------------------------------

def write_batch_jsonl(rows: list[dict], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return path


# ---------------------------------------------------------------------------
# Batch submission (lazy SDK imports so test paths don't need them installed)
# ---------------------------------------------------------------------------

def submit_openai_batch(jsonl_path: Path) -> dict:
    """
    Upload the JSONL, create the batch job, return job metadata.
    """
    import openai  # type: ignore[import-not-found]

    client = openai.OpenAI()
    with open(jsonl_path, "rb") as f:
        upload = client.files.create(file=f, purpose="batch")
    job = client.batches.create(
        input_file_id=upload.id,
        endpoint="/v1/chat/completions",
        completion_window="24h",
    )
    return {
        "job_id": job.id,
        "provider": "openai",
        "input_file_id": upload.id,
        "input_file_path": str(jsonl_path),
        "submitted_at": datetime.now(timezone.utc).isoformat(),
        "status": job.status,
    }


def submit_anthropic_batch(jsonl_path: Path) -> dict:
    """Submit a Message Batches job from a JSONL file."""
    import anthropic  # type: ignore[import-not-found]

    client = anthropic.Anthropic()
    requests = []
    with open(jsonl_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            requests.append(json.loads(line))

    response = client.messages.batches.create(requests=requests)
    return {
        "job_id": response.id,
        "provider": "anthropic",
        "input_file_id": None,  # Anthropic doesn't pre-upload.
        "input_file_path": str(jsonl_path),
        "submitted_at": datetime.now(timezone.utc).isoformat(),
        "status": response.processing_status,
    }


def submit_gemini_batch(jsonl_path: Path, *, model_id: str) -> dict:
    """
    Upload the JSONL request file and create a Gemini Batch Mode job.

    250 papers' worth of structured-summary requests comfortably exceed the
    20MB inline-request limit, so this always uses the file-upload path
    (never the alternate inline-list ``src=[...]`` form Google's SDK also
    supports).

    The per-request JSONL shape (built by ``build_gemini_batch_request``) and
    this upload config are confirmed against Google's live batch-mode docs
    (ai.google.dev/gemini-api/docs/batch-mode) and a real submission: the
    file-upload example there uses
    ``types.UploadFileConfig(display_name=..., mime_type='jsonl')``, and the
    per-line request nests generation settings under ``generation_config``
    (see ``build_gemini_batch_request``'s docstring). If a future SDK/API
    change breaks this shape again, this call is where it would surface first
    (an upload or batch-create error, or a job that never produces usable
    results) — see the re-raise below and, before trusting the full corpus to
    this path, run the ``--limit 2`` smoke test described in
    ``.env.template`` section 12.
    """
    from google import genai  # type: ignore[import-not-found]
    from google.genai import types  # type: ignore[import-not-found]

    client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))
    try:
        uploaded = client.files.upload(
            file=str(jsonl_path),
            config=types.UploadFileConfig(display_name="vet-llm-sum-batch", mime_type="jsonl"),
        )
        job = client.batches.create(
            model=model_id,
            src=uploaded.name,
            config={"display_name": f"vet-llm-sum-{_timestamp_slug()}"},
        )
    except Exception as exc:
        raise RuntimeError(
            "Gemini batch submission failed even though the request/upload "
            "shape matches Google's documented file-based batch-mode format "
            "(see this function's docstring) — before retrying, check "
            "ai.google.dev/gemini-api/docs/batch-mode for API changes, and "
            "consider smoke-testing with `summarize --mode batch --limit 2` "
            "first. "
            f"Original error: {exc}"
        ) from exc

    state = getattr(job, "state", None)
    status = state.name if state is not None and hasattr(state, "name") else str(state)
    return {
        "job_id": job.name,
        "provider": "gemini",
        "input_file_id": uploaded.name,
        "input_file_path": str(jsonl_path),
        "submitted_at": datetime.now(timezone.utc).isoformat(),
        "status": status,
    }


# ---------------------------------------------------------------------------
# Batch job ledger (crash recovery)
# ---------------------------------------------------------------------------

def record_batch_job(entry: dict) -> None:
    """
    Append one line to data/batch_jobs.jsonl. Crash-safe so a mid-run kill
    cannot orphan a submitted (paid) batch job.
    """
    BATCH_JOBS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(BATCH_JOBS_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def safe_job_id_for_path(job_id: str) -> str:
    """Sanitise a job_id for use in a local result-file name.

    Some providers' job ids are resource paths rather than bare ids (e.g.
    Gemini's ``batches/abc123``) — replacing ``/`` here keeps the result-file
    name from turning into an unwanted subdirectory under ``BATCH_DIR``. The
    real ``job_id`` (with the slash) is still what's sent back to the
    provider's API for status/download calls; this is a path-safety
    transform only. Shared by ``check_batch_status.poll_all`` and the
    duplicate-submission guard below so both agree on the same filename.
    """
    return job_id.replace("/", "_")


def _unresolved_batch_jobs(stage: str, providers: list[str]) -> list[dict]:
    """Return ``batch_jobs.jsonl`` entries for this stage/providers with no
    local ``.merged`` marker yet — i.e. jobs that may still be in flight.
    """
    unresolved = []
    for job in load_batch_jobs():
        if job.get("stage") != stage or job.get("provider") not in providers:
            continue
        safe_id = safe_job_id_for_path(str(job.get("job_id", "")))
        result_path = BATCH_DIR / f"{job['provider']}_{safe_id}_results.jsonl"
        if not result_path.with_suffix(".merged").exists():
            unresolved.append(job)
    return unresolved


def _refuse_duplicate_submission(stage: str, providers: list[str], *, force: bool) -> None:
    """Refuse to submit a new batch job when an earlier one for the same
    stage/provider(s) hasn't been resolved yet.

    A ``resume``-based skip only prevents resubmitting individual papers
    already marked ``success``; it does nothing for a job that's merely
    ``pending`` (or, for evaluation, has no local state at all — see
    ``run_batch_evaluation``'s docstring) from an earlier submission that
    hasn't been polled/merged yet. Re-running the submission step in that
    window would create a second, duplicate paid batch job for the same
    papers. ``--force`` bypasses this for the rare legitimate case (a truly
    abandoned/expired job that genuinely needs resubmitting).
    """
    if force:
        return
    unresolved = _unresolved_batch_jobs(stage, providers)
    if not unresolved:
        return
    lines = "\n".join(
        f"  - {job.get('provider')} job_id={job.get('job_id')}" for job in unresolved
    )
    raise SystemExit(
        f"[phase3:batch] REFUSING to submit a new {stage} batch job: "
        f"{len(unresolved)} unresolved job(s) already exist for this stage/provider(s):\n"
        f"{lines}\n"
        "Run `python llm-sum/check_batch_status.py` first to check/merge them, "
        "or pass --force if you're certain a fresh submission is correct."
    )


def load_batch_jobs() -> list[dict]:
    if not BATCH_JOBS_PATH.exists():
        return []
    out = []
    with open(BATCH_JOBS_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


# ---------------------------------------------------------------------------
# High-level orchestration for the summarisation stage
# ---------------------------------------------------------------------------

def _iter_manifest(path: Path):
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _read_cached_text(record_or_doi) -> str | None:
    """
    Locate this paper's cleaned-text cache and return its body. Delegates
    to ``prepare_texts.read_cached_text`` so descriptive and legacy filenames
    both work.
    """
    from prepare_texts import read_cached_text as _shared_read
    input_source = "processed"
    if not isinstance(record_or_doi, str):
        input_source = str(record_or_doi.get("input_source") or "processed")
    return _shared_read(record_or_doi, input_source=input_source)


def _failed_batch_result(custom_id: str, error: str) -> dict:
    """Return the standard failed model-slot shape for one batch response row."""
    return {
        "custom_id": custom_id,
        "status": "failed",
        "error": error,
        "summary": None,
        "structured_summary": None,
        "input_tokens": None,
        "output_tokens": None,
        "model_version": None,
        "system_fingerprint": None,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def _successful_batch_summary(
    *,
    custom_id: str,
    payload: Any,
    input_tokens: int,
    output_tokens: int,
    model_version: str,
    system_fingerprint: str | None = None,
    cache_read_input_tokens: int | None = None,
    cache_creation_input_tokens: int | None = None,
) -> dict:
    """
    Validate a batch summary through the same Pydantic repair path as real-time.

    Batch APIs return raw JSONL later, outside the provider SDK's parsed object
    helpers. Validating here prevents unstructured prose from silently entering
    ``summaries.jsonl`` and breaking the blind judge pipeline.

    ``cache_read_input_tokens``/``cache_creation_input_tokens`` default to
    None (not 0): summarisation has nothing shared to cache (Finding D), so
    these are normally absent here, but the same three result-line parsers
    build both summary and judge rows, and forwarding whatever the parser
    found keeps this one schema shared across data/summaries.jsonl model
    slots and data/evaluations.jsonl rows.
    """
    from summarizer import coerce_veterinary_summary, veterinary_summary_to_result  # noqa: E402

    parsed = coerce_veterinary_summary(payload)
    result = veterinary_summary_to_result(parsed)
    result.update({
        "custom_id": custom_id,
        "status": "success",
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "model_version": model_version,
        "system_fingerprint": system_fingerprint,
        "cache_read_input_tokens": cache_read_input_tokens,
        "cache_creation_input_tokens": cache_creation_input_tokens,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    return result


def run_batch_summarisation(
    *,
    manifest_path: Path = MANIFEST_PATH,
    resume: bool = False,
    providers: list[str] | None = None,
    guide_summary_path: Path | None = None,
    force: bool = False,
    paper_limit: int | None = None,
) -> dict:
    """
    Build & submit summarisation batch jobs for whichever providers are
    passed in — openai, anthropic, and gemini are all handled here. Callers
    that want Gemini to skip batch and go real-time instead (the default,
    when GEMINI_BATCH_ENABLED=false) should simply omit it from ``providers``;
    see summarizer.main()'s provider split in the ``profile.use_batch`` branch.

    ``force`` bypasses ``_refuse_duplicate_submission``'s check for an
    unresolved prior batch job covering the same providers — see that
    function's docstring.

    ``paper_limit``, when set, stops after N papers that actually get queued
    for at least one requested provider — NOT the first N manifest rows
    scanned. Those differ once ``resume`` is skipping most of the corpus for
    a provider that's mostly done already (e.g. resuming just OpenAI after
    Anthropic/Gemini finished): counting scanned rows would let ``--limit``
    silently stop after N already-resolved papers and submit nothing, which
    defeats the reason ``--limit`` exists here — chunking a large remaining
    backlog into submissions that fit a provider's enqueued-token cap (see
    the ``token_limit_exceeded`` batch error) without resubmitting anything
    already merged.
    """
    # Imported here (not at module top) so unit tests that mock the batch
    # path don't require the summarizer's full DEVELOPMENT_MODE guard to
    # have run first.
    from summarizer import (  # noqa: E402
        load_existing_summaries,
        load_provider_prompt_templates_with_optional_guide,
        build_user_message,
        GUIDE_SUMMARY_FILE,
        _new_summary_entry,
        _write_all_summaries,
        _custom_id_for_source,
        _summary_key,
    )

    providers = providers or ["openai", "anthropic"]
    _refuse_duplicate_submission("summarize", providers, force=force)
    prompt_templates, guide_summary, resolved_guide_path, _prompt_paths = (
        load_provider_prompt_templates_with_optional_guide(
            providers, guide_summary_path or GUIDE_SUMMARY_FILE
        )
    )
    if guide_summary:
        print(f"[phase3:batch] using format guide: {resolved_guide_path}")
    # Always keep rows from other papers and other input sources. ``resume``
    # only decides whether an already-successful model slot is skipped or
    # refreshed (the check inside the provider loop below).
    #
    # Loading conditionally here would be silent data loss: _write_all_summaries
    # further down rewrites the WHOLE file from this dict, so starting empty
    # drops every raw_text/pdf row and every gemini result — gemini is never
    # batched, so its summaries only ever exist in the file we would clobber.
    # summarizer.run_realtime does the same thing for the same reason.
    existing = load_existing_summaries()
    summaries_by_doi: dict[str, dict] = dict(existing)

    rows_per_provider: dict[str, list[dict]] = {p: [] for p in providers}
    # Parallel to rows_per_provider: the actual slot dict each row came from,
    # so a failed submission (see the submit loop below) can revert exactly
    # those slots back to not-yet-attempted instead of leaving them stranded
    # at "pending" with no job behind them.
    slots_per_provider: dict[str, list[dict]] = {p: [] for p in providers}
    slug_to_doi: dict[str, str] = {}
    processed_papers = 0

    for record in _iter_manifest(manifest_path):
        if paper_limit is not None and processed_papers >= paper_limit:
            break
        doi = str(record.get("doi", "")).strip()
        if not doi:
            continue
        input_source = "processed"
        custom_id = _custom_id_for_source(doi, input_source)
        if not _VALID_CUSTOM_ID.match(custom_id):
            # doi_to_slug only escapes '.', '/', ':' — a DOI with any other
            # disallowed character (seen in this corpus: bogus journal-ISSN
            # placeholder "DOIs" like "10.1111/(issn)1740-8261" on mistagged,
            # non-article PDFs) produces a custom_id every batch API rejects.
            # Skip just this one paper rather than letting one bad row abort
            # the whole submission — a provider validates the ENTIRE batch
            # atomically, so one invalid custom_id previously blocked every
            # other paper in the same request, for every provider still left
            # in the loop below.
            log_error(doi, "summarize",
                      f"custom_id {custom_id!r} does not match the batch API's allowed "
                      "pattern ([a-zA-Z0-9_-], 1-64 chars) — skipping this paper.")
            continue
        summary_key = _summary_key(doi, input_source)
        article_text = _read_cached_text(record)
        if article_text is None:
            log_error(doi, "summarize",
                      f"No cached text for {custom_id} (descriptive + legacy lookups failed)")
            continue

        slug_to_doi[custom_id] = doi
        entry = summaries_by_doi.get(summary_key) or _new_summary_entry(record, input_source)
        summaries_by_doi[summary_key] = entry

        queued_this_paper = False
        for provider in providers:
            slot = entry["models"].setdefault(provider, {})
            # "pending" means an earlier submission already covers this slot
            # and is awaiting merge — resubmitting it would double-pay for
            # the same paper. See _refuse_duplicate_submission for the
            # job-level counterpart of this same protection.
            #
            # This only works because _empty_model_slot() defaults "status"
            # to None, not "pending" — a brand-new paper's slot must NOT look
            # already-submitted before a request has ever been built for it,
            # or --resume would skip it forever on its very first appearance.
            # "pending" is set explicitly below, only once a request is
            # actually queued into rows_per_provider for submission this run.
            if resume and slot.get("status") in ("success", "pending"):
                continue
            spec = get_model_spec(provider, role="summarize")
            # Build inside the provider loop so PROMPT_MODE=provider_specific
            # changes only the intended provider's request body.
            user_message = build_user_message(article_text, prompt_templates[provider])
            if provider == "openai":
                rows_per_provider["openai"].append(
                    build_openai_request(custom_id=custom_id, user_message=user_message,
                                         model_id=spec.model_id)
                )
            elif provider == "anthropic":
                rows_per_provider["anthropic"].append(
                    build_anthropic_request(custom_id=custom_id, user_message=user_message,
                                            model_id=spec.model_id)
                )
            elif provider == "gemini":
                rows_per_provider["gemini"].append(
                    build_gemini_batch_request(custom_id=custom_id, user_message=user_message,
                                               model_id=spec.model_id)
                )
            else:
                raise ValueError(
                    f"Unknown provider '{provider}'. Valid: openai, anthropic, gemini."
                )
            slot["status"] = "pending"
            slots_per_provider[provider].append(slot)
            queued_this_paper = True

        if queued_this_paper:
            processed_papers += 1

    # Persist updated entries (with any new `pending` slots) before submission
    # so a crash between build and submit still leaves a valid summaries file.
    _write_all_summaries(SUMMARIES_PATH, summaries_by_doi)

    submitted: list[dict] = []
    failed_providers: list[str] = []
    timestamp = _timestamp_slug()
    for provider, rows in rows_per_provider.items():
        if not rows:
            print(f"[phase3:batch] no requests to submit for {provider}")
            continue
        _assert_budget_authorised("summarize", len(rows))
        jsonl_path = BATCH_DIR / f"{provider}_sum_{timestamp}.jsonl"
        write_batch_jsonl(rows, jsonl_path)

        # Isolated per provider: one provider's submission failing (network,
        # a provider-side validation error, etc.) must not prevent the other
        # requested providers from still getting submitted this run.
        try:
            if provider == "openai":
                job_meta = submit_openai_batch(jsonl_path)
            elif provider == "anthropic":
                job_meta = submit_anthropic_batch(jsonl_path)
            else:
                job_meta = submit_gemini_batch(jsonl_path, model_id=get_model_spec(provider, role="summarize").model_id)
        except Exception as exc:  # noqa: BLE001
            # The submission never succeeded, so these slots must NOT be left
            # at "pending" — that would strand them exactly like the bug this
            # revert exists to avoid (a slot marked pending with no job behind
            # it, permanently skipped by a future --resume).
            for slot in slots_per_provider[provider]:
                slot["status"] = None
            _write_all_summaries(SUMMARIES_PATH, summaries_by_doi)
            log_error("N/A", "summarize", f"{provider} batch submission failed: {exc}")
            print(f"[phase3:batch] {provider} submission FAILED ({exc}); "
                  f"{len(rows)} slot(s) reverted to not-yet-attempted. "
                  "Continuing with remaining providers.")
            failed_providers.append(provider)
            continue

        job_meta.update({"stage": "summarize", "request_count": len(rows)})
        record_batch_job(job_meta)
        submitted.append(job_meta)
        print(f"[phase3:batch] submitted {provider} job_id={job_meta['job_id']} "
              f"({len(rows)} requests)")

    return {"submitted": submitted, "slug_to_doi": slug_to_doi, "failed_providers": failed_providers}


# ---------------------------------------------------------------------------
# High-level orchestration for the evaluation stage
# ---------------------------------------------------------------------------

def _iter_summaries(path: Path | None = None):
    """Yield parsed entries from summaries.jsonl. Resolved at call time."""
    resolved = path if path is not None else SUMMARIES_PATH
    if not resolved.exists():
        return
    with open(resolved, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def run_batch_evaluation(
    *,
    judges: list[str] | None = None,
    resume: bool = False,
    force: bool = False,
    doi_filter: set[str] | None = None,
    max_new_requests: int | None = None,
) -> dict:
    """
    Build & submit evaluation batch jobs for whichever judges are passed in —
    openai, anthropic, and gemini are all handled here.

    The judge prompt is constructed blind — the summariser identity is placed
    only in the custom_id (format: "{slug}__{summariser}") and never enters
    the message body. This preserves the blind protocol for batch jobs the
    same way run_evaluation() does for real-time jobs.

    Callers that want a judge to skip batch and go real-time instead should
    omit it from ``judges`` and call evaluator.run_evaluation() with that
    judge separately — same split as the summarisation stage.

    ``doi_filter``, when provided, restricts the run to exactly those DOIs —
    mirrors ``run_evaluation(doi_filter=...)``'s existing behavior, so the
    journal-stratified sampling `cmd_evaluate` already computes applies to
    batch-submitted judges too, not just real-time ones.

    Unlike ``run_batch_summarisation``, ``resume`` here has no local
    "pending" state to skip — it only checks ``already_evaluated()`` against
    already-*merged* rows in ``evaluations.jsonl``, which an in-flight batch
    job hasn't produced yet. ``force`` (bypassing
    ``_refuse_duplicate_submission`` below) is this function's only
    protection against submitting a second batch job while an earlier one
    for the same judge(s) is still unresolved.

    ``max_new_requests``, when set, caps how many NEW requests get queued
    per judge in THIS call — not a cap on papers scanned. This exists
    because OpenAI enforces a hard per-organization "enqueued tokens" ceiling
    on batch jobs (observed directly: 900,000 tokens for gpt-5.4, see
    data/error_log.jsonl entries for job batch_6a604fc4eb5c8190bc63c0e66f1e0323
    and batch_6a60540619d481908fefd10801f1c67d, both of which submitted 720
    judge requests and were rejected OUTRIGHT — zero processed — with
    "token_limit_exceeded"). A large remaining backlog (e.g. 672 requests for
    one judge) cannot safely be submitted in a single call.

    Unlike the CLI's ``--limit`` (which drives per-journal SAMPLING with a
    fixed seed — see run_phase3._sample_by_journal), this cap is evaluated
    against ``already_evaluated()`` fresh on every call and simply stops
    appending to ``rows_per_judge[judge]`` once the cap is hit — it does not
    change WHICH combinations are considered, only how many make it into
    this submission. That makes repeated calls with the same
    ``max_new_requests`` genuinely progressive: run 1 queues the first N
    not-yet-evaluated combinations it encounters (in the loop's natural,
    deterministic paper-then-summariser order — see the load-bearing
    ordering comment below), those get merged, and run 2 finds them now
    "already evaluated" and naturally advances to the next N. A fixed-seed
    ``--limit`` sample does NOT have this property — an identical resample
    finds nothing new to submit, and a larger ``--limit`` is not guaranteed
    to be a superset of a smaller one (Python's ``random.sample`` is not
    prefix-stable across different sample sizes), so it cannot be used to
    sweep a large backlog into safe-sized chunks the way this can.

    The cap is per-judge (a fresh count per entry in ``judges``), matching
    OpenAI's error message ("Enqueued token limit reached for gpt-5.4" —
    the limit is scoped to one model/provider, not shared across judges).
    """
    from evaluator import build_judge_prompt_segments, load_judge_prompt, already_evaluated  # noqa: E402

    judges = judges or ["openai"]
    _refuse_duplicate_submission("evaluate", judges, force=force)
    prompt_template = load_judge_prompt()

    rows_per_judge: dict[str, list[dict]] = {j: [] for j in judges}

    # LOAD-BEARING ORDERING: this loop is paper-outer, summariser-inner (and
    # judge innermost), so a judge's batch JSONL — and therefore its
    # downloaded result file — reads paperA__openai, paperA__anthropic,
    # paperA__gemini, paperB__openai, ... one article's three requests
    # adjacent, before moving to the next article. Prompt caching (Phase A3)
    # depends on this: Anthropic/OpenAI/Gemini's cache hit chance for the
    # shared rubric+reference blocks is highest when requests that share a
    # prefix are near each other in submission order. Reordering this loop
    # (e.g. to judge-outer) would silently degrade cache locality with no
    # error — see Finding B in the caching design plan.
    for entry in _iter_summaries():
        doi = str(entry.get("doi", "")).strip()
        if not doi:
            continue
        if doi_filter is not None and doi not in doi_filter:
            continue
        input_source = str(entry.get("input_source") or "processed")
        slug = entry.get("custom_id") or doi_to_slug(doi)
        # The summary entry carries journal+title so the descriptive lookup works.
        reference_text = _read_cached_text(entry)
        if reference_text is None:
            log_error(doi, "batch_evaluate",
                      f"No cached text for {slug} — skipping")
            continue

        for summariser, slot in (entry.get("models") or {}).items():
            if slot.get("status") != "success" or not slot.get("summary"):
                continue
            candidate_summary = slot["summary"]
            # Build the segments here — summariser name is NOT in any of them
            # (Phase A1: rubric / reference / candidate blocks, same split
            # every judge path uses — see build_judge_prompt_segments).
            segments = build_judge_prompt_segments(reference_text, candidate_summary,
                                                   prompt_template)
            # Summariser encoded only in the custom_id so we can join results later.
            custom_id = f"{slug}__{summariser}"

            for judge in judges:
                if resume and already_evaluated(doi, summariser, judge, input_source):
                    continue
                # max_new_requests caps THIS judge's queue only — a different
                # judge with room left still gets its own combos considered.
                if max_new_requests is not None and len(rows_per_judge[judge]) >= max_new_requests:
                    continue
                spec = get_model_spec(judge, role="judge")
                if judge == "openai":
                    rows_per_judge["openai"].append(
                        build_openai_request(custom_id=custom_id,
                                             user_message=segments,
                                             model_id=spec.model_id,
                                             for_judge=True,
                                             cache_article_id=slug)
                    )
                elif judge == "anthropic":
                    rows_per_judge["anthropic"].append(
                        build_anthropic_request(custom_id=custom_id,
                                                user_message=segments,
                                                model_id=spec.model_id,
                                                for_judge=True)
                    )
                elif judge == "gemini":
                    rows_per_judge["gemini"].append(
                        build_gemini_batch_request(custom_id=custom_id,
                                                   user_message=segments,
                                                   model_id=spec.model_id,
                                                   for_judge=True)
                    )
                else:
                    raise ValueError(
                        f"Unknown judge '{judge}'. Valid: openai, anthropic, gemini."
                    )

    if max_new_requests is not None:
        for judge, rows in rows_per_judge.items():
            if len(rows) >= max_new_requests:
                print(f"[phase3:batch] {judge}: hit max_new_requests={max_new_requests} — "
                      f"more may remain unfilled. Re-run the same command with --resume "
                      f"(the default) once this job is merged to submit the next chunk.")

    submitted: list[dict] = []
    timestamp = _timestamp_slug()
    for judge, rows in rows_per_judge.items():
        if not rows:
            print(f"[phase3:batch] no evaluation requests to submit for judge {judge}")
            continue
        _assert_budget_authorised("evaluate", len(rows))
        jsonl_path = BATCH_DIR / f"{judge}_eval_{timestamp}.jsonl"
        write_batch_jsonl(rows, jsonl_path)

        if judge == "openai":
            job_meta = submit_openai_batch(jsonl_path)
        elif judge == "anthropic":
            job_meta = submit_anthropic_batch(jsonl_path)
        else:
            job_meta = submit_gemini_batch(jsonl_path, model_id=get_model_spec(judge, role="judge").model_id)

        job_meta.update({"stage": "evaluate", "judge": judge, "request_count": len(rows)})
        record_batch_job(job_meta)
        submitted.append(job_meta)
        print(f"[phase3:batch] submitted {judge} evaluation job_id={job_meta['job_id']} "
              f"({len(rows)} requests)")

    return {"submitted": submitted}


# ---------------------------------------------------------------------------
# Result parsers (used by check_batch_status.py)
# ---------------------------------------------------------------------------

def _openai_batch_cached_tokens(usage: dict) -> int | None:
    """Return usage.prompt_tokens_details.cached_tokens from a raw batch
    result dict, or None if the field is absent (older/pre-caching rows).
    ``prompt_tokens`` already includes cached tokens — never re-add."""
    cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
    return int(cached) if cached is not None else None


def parse_openai_result_line(line: str, *, expect_summary_schema: bool = True) -> dict:
    """
    Parse one line of an OpenAI batch result file into our standard
    summarizer result shape.
    """
    raw = json.loads(line)
    custom_id = raw.get("custom_id", "")
    response = (raw.get("response") or {})
    body = response.get("body") or {}
    error = raw.get("error")

    if error:
        return _failed_batch_result(custom_id, str(error))

    choices = body.get("choices") or [{}]
    usage = body.get("usage") or {}
    content = choices[0].get("message", {}).get("content", "")
    if not content:
        finish_reason = choices[0].get("finish_reason", "unknown")
        completion_tokens = usage.get("completion_tokens", "?")
        reasoning_tokens = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens", 0)
        return _failed_batch_result(
            custom_id,
            f"OpenAI batch row returned empty content (finish_reason={finish_reason}, "
            f"completion_tokens={completion_tokens}, reasoning_tokens={reasoning_tokens}, "
            f"cap=MAX_OUTPUT_TOKENS={MAX_OUTPUT_TOKENS}). "
            + ("Reasoning consumed the whole output budget — raise MAX_OUTPUT_TOKENS."
               if reasoning_tokens and finish_reason == "length"
               else "Raise MAX_OUTPUT_TOKENS if finish_reason is 'length'."),
        )
    cache_read_input_tokens = _openai_batch_cached_tokens(usage)
    if not expect_summary_schema:
        return {
            "custom_id": custom_id,
            "status": "success",
            "summary": content,
            "input_tokens": int(usage.get("prompt_tokens", 0)),
            "output_tokens": int(usage.get("completion_tokens", 0)),
            "model_version": str(body.get("model", "")),
            "system_fingerprint": body.get("system_fingerprint"),
            "cache_read_input_tokens": cache_read_input_tokens,
            # OpenAI does not report a separate cache-write token count.
            "cache_creation_input_tokens": None,
            "judge_prompt_shape": JUDGE_PROMPT_SHAPE,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    try:
        payload = json.loads(content) if isinstance(content, str) else content
        return _successful_batch_summary(
            custom_id=custom_id,
            payload=payload,
            input_tokens=int(usage.get("prompt_tokens", 0)),
            output_tokens=int(usage.get("completion_tokens", 0)),
            model_version=str(body.get("model", "")),
            system_fingerprint=body.get("system_fingerprint"),
            cache_read_input_tokens=cache_read_input_tokens,
        )
    except Exception as exc:
        finish_reason = choices[0].get("finish_reason", "unknown")
        hint = (
            f" (finish_reason={finish_reason}, completion_tokens={usage.get('completion_tokens', '?')}, "
            f"cap=MAX_OUTPUT_TOKENS={MAX_OUTPUT_TOKENS} — raise MAX_OUTPUT_TOKENS if truncated)"
            if finish_reason == "length" else ""
        )
        return _failed_batch_result(custom_id, f"Invalid structured summary: {exc}{hint}")


def _extract_anthropic_summary_payload(content_blocks: list[dict]) -> Any:
    """Return the forced VeterinarySummary tool payload from an Anthropic batch row."""
    for block in content_blocks:
        if block.get("type") == "tool_use" and block.get("name") == "VeterinarySummary":
            return block.get("input")
    text = "".join(b.get("text", "") for b in content_blocks if b.get("type") == "text")
    if text:
        return json.loads(text)
    raise ValueError("Anthropic batch row did not include a VeterinarySummary tool payload.")


def parse_anthropic_result_line(line: str, *, expect_summary_schema: bool = True) -> dict:
    """Parse one line of an Anthropic Message Batches result."""
    raw = json.loads(line)
    custom_id = raw.get("custom_id", "")
    result = raw.get("result") or {}
    rtype = result.get("type")

    if rtype != "succeeded":
        return _failed_batch_result(custom_id, str(result.get("error") or rtype))

    message = result.get("message") or {}
    content_blocks = message.get("content") or []
    usage = message.get("usage") or {}
    # Anthropic's usage.input_tokens is the UNCACHED remainder only — total
    # input is the sum of all three (Finding C in the caching design plan).
    # Both cache fields are always present in a real response (0 when no
    # caching happened), so this is never None for a genuine Anthropic row.
    cache_read = int(usage.get("cache_read_input_tokens", 0) or 0)
    cache_creation = int(usage.get("cache_creation_input_tokens", 0) or 0)
    total_input_tokens = int(usage.get("input_tokens", 0)) + cache_read + cache_creation
    if not expect_summary_schema:
        summary_text = "".join(
            b.get("text", "") for b in content_blocks if b.get("type") == "text"
        )
        return {
            "custom_id": custom_id,
            "status": "success",
            "summary": summary_text,
            "input_tokens": total_input_tokens,
            "output_tokens": int(usage.get("output_tokens", 0)),
            "model_version": str(message.get("model", "")),
            "system_fingerprint": None,
            "cache_read_input_tokens": cache_read,
            "cache_creation_input_tokens": cache_creation,
            "judge_prompt_shape": JUDGE_PROMPT_SHAPE,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    try:
        payload = _extract_anthropic_summary_payload(content_blocks)
        return _successful_batch_summary(
            custom_id=custom_id,
            payload=payload,
            input_tokens=total_input_tokens,
            output_tokens=int(usage.get("output_tokens", 0)),
            model_version=str(message.get("model", "")),
            cache_read_input_tokens=cache_read,
            cache_creation_input_tokens=cache_creation,
        )
    except Exception as exc:
        return _failed_batch_result(custom_id, f"Invalid structured summary: {exc}")


def _extract_gemini_text(response: dict) -> str:
    """Return the concatenated text of a downloaded Gemini batch response.

    Downloaded batch result JSON uses the REST/proto field casing
    (``camelCase``), unlike the Python SDK's snake_case attribute access used
    in ``summarizer._call_gemini`` — so this reads ``candidates`` /
    ``content`` / ``parts`` directly from the raw dict rather than reusing
    that real-time helper.
    """
    candidates = response.get("candidates") or []
    if not candidates:
        return ""
    content = candidates[0].get("content") or {}
    parts = content.get("parts") or []
    return "".join(str(p.get("text", "")) for p in parts)


def _gemini_batch_finish_reason(response: dict) -> str:
    """Extract Gemini's finish reason from a downloaded batch response dict.

    Mirrors ``summarizer._gemini_finish_reason``, but reads the raw
    downloaded-JSON dict (camelCase ``finishReason``) rather than an SDK
    response object, consistent with how ``_extract_gemini_text`` above
    already works for this module.
    """
    candidates = response.get("candidates") or []
    if not candidates:
        return "unknown"
    return str(candidates[0].get("finishReason") or "unknown")


def parse_gemini_result_line(line: str, *, expect_summary_schema: bool = True) -> dict:
    """Parse one line of a downloaded Gemini Batch Mode result file.

    Each line echoes the ``key`` we submitted as ``custom_id`` in
    ``build_gemini_batch_request``, plus either a ``response``
    (GenerateContentResponse-shaped dict) or a ``status``/``error`` object
    for a failed row.
    """
    raw = json.loads(line)
    custom_id = raw.get("key", "")
    error = raw.get("error") or (raw.get("status") or {}).get("message")
    response = raw.get("response") or {}

    if error or not response:
        return _failed_batch_result(custom_id, str(error or "no response in batch result row"))

    usage = response.get("usageMetadata") or response.get("usage_metadata") or {}
    model_version = str(response.get("modelVersion") or response.get("model_version") or "")
    text = _extract_gemini_text(response)
    # prompt_token_count already includes cached tokens — never re-add.
    cached = usage.get("cachedContentTokenCount")
    if cached is None:
        cached = usage.get("cached_content_token_count")
    cache_read_input_tokens = int(cached) if cached is not None else None

    if not expect_summary_schema:
        return {
            "custom_id": custom_id,
            "status": "success",
            "summary": text,
            "input_tokens": int(usage.get("promptTokenCount") or usage.get("prompt_token_count") or 0),
            "output_tokens": int(usage.get("candidatesTokenCount") or usage.get("candidates_token_count") or 0),
            "model_version": model_version,
            "system_fingerprint": None,
            "cache_read_input_tokens": cache_read_input_tokens,
            # Gemini's implicit caching has no separate "write" step to report.
            "cache_creation_input_tokens": None,
            "judge_prompt_shape": JUDGE_PROMPT_SHAPE,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    try:
        payload = json.loads(text)
        return _successful_batch_summary(
            custom_id=custom_id,
            payload=payload,
            input_tokens=int(usage.get("promptTokenCount") or usage.get("prompt_token_count") or 0),
            output_tokens=int(usage.get("candidatesTokenCount") or usage.get("candidates_token_count") or 0),
            model_version=model_version,
            cache_read_input_tokens=cache_read_input_tokens,
        )
    except Exception as exc:
        reason = _gemini_batch_finish_reason(response)
        return _failed_batch_result(
            custom_id, f"Invalid structured summary (finish_reason={reason}): {exc}"
        )


# Eval custom_id like "10_1111_jvim_16872__openai". Match suffix to find summariser.
EVAL_CUSTOM_ID_RE = re.compile(r"^(?P<slug>.+?)__(?P<summariser>[a-z]+)$")


def parse_evaluation_custom_id(custom_id: str) -> tuple[str, str] | None:
    """Return (slug, summariser) for evaluation custom_ids, or None on mismatch."""
    m = EVAL_CUSTOM_ID_RE.match(custom_id)
    if not m:
        return None
    return m.group("slug"), m.group("summariser")
