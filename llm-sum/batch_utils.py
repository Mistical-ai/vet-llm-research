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

Gemini batch is intentionally NOT here — its API doesn't follow the same
shape, and for 250 papers the real-time call delta is negligible.
"""

from __future__ import annotations

from _bootstrap import *  # noqa: F401,F403

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from models_config import get_model_spec  # noqa: E402
from file_paths import doi_to_slug  # noqa: E402
from utils import log_error  # noqa: E402

BATCH_JOBS_PATH = DATA_DIR / "batch_jobs.jsonl"
SUMMARIES_PATH = DATA_DIR / "summaries.jsonl"
MANIFEST_PATH = DATA_DIR / "manifest.jsonl"

TEMPERATURE = float(os.getenv("TEMPERATURE", "0.0"))
SEED = int(os.getenv("SEED", "42"))
MAX_OUTPUT_TOKENS = int(os.getenv("MAX_OUTPUT_TOKENS", "500"))


def _timestamp_slug() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


# ---------------------------------------------------------------------------
# Build request payloads
# ---------------------------------------------------------------------------

def build_openai_request(custom_id: str, user_message: str, model_id: str) -> dict:
    """One row of OpenAI's batch JSONL format."""
    return {
        "custom_id": custom_id,
        "method": "POST",
        "url": "/v1/chat/completions",
        "body": {
            "model": model_id,
            "messages": [{"role": "user", "content": user_message}],
            "temperature": TEMPERATURE,
            "max_tokens": MAX_OUTPUT_TOKENS,
            "seed": SEED,
        },
    }


def build_anthropic_request(custom_id: str, user_message: str, model_id: str) -> dict:
    """
    One row of Anthropic's Message Batches format. The shape differs from
    OpenAI's: the request body is nested under "params" and the URL is
    implicit.
    """
    return {
        "custom_id": custom_id,
        "params": {
            "model": model_id,
            "max_tokens": MAX_OUTPUT_TOKENS,
            "temperature": TEMPERATURE,
            "messages": [{"role": "user", "content": user_message}],
        },
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
    return _shared_read(record_or_doi)


def run_batch_summarisation(
    *,
    manifest_path: Path = MANIFEST_PATH,
    resume: bool = False,
    providers: list[str] | None = None,
    guide_summary_path: Path | None = None,
) -> dict:
    """
    Build & submit summarisation batch jobs for OpenAI and Anthropic. Gemini
    is not batched; the summarizer's real-time loop handles it separately
    (run summarizer.run_realtime() with providers=["gemini"]).
    """
    # Imported here (not at module top) so unit tests that mock the batch
    # path don't require the summarizer's full DEVELOPMENT_MODE guard to
    # have run first.
    from summarizer import (  # noqa: E402
        load_existing_summaries,
        load_prompt,
        load_optional_guide_summary,
        apply_guide_summary_to_prompt,
        build_user_message,
        GUIDE_SUMMARY_FILE,
        _new_summary_entry,
        _write_all_summaries,
    )

    providers = providers or ["openai", "anthropic"]
    guide_summary_path = guide_summary_path or GUIDE_SUMMARY_FILE
    guide_summary = load_optional_guide_summary(guide_summary_path)
    prompt_template = apply_guide_summary_to_prompt(load_prompt(), guide_summary)
    if guide_summary:
        print(f"[phase3:batch] using format guide: {guide_summary_path}")
    existing = load_existing_summaries() if resume else {}
    summaries_by_doi: dict[str, dict] = dict(existing)

    rows_per_provider: dict[str, list[dict]] = {p: [] for p in providers}
    slug_to_doi: dict[str, str] = {}

    for record in _iter_manifest(manifest_path):
        doi = str(record.get("doi", "")).strip()
        if not doi:
            continue
        slug = doi_to_slug(doi)
        article_text = _read_cached_text(record)
        if article_text is None:
            log_error(doi, "summarize",
                      f"No cached text for {slug} (descriptive + legacy lookups failed)")
            continue

        slug_to_doi[slug] = doi
        entry = summaries_by_doi.get(doi) or _new_summary_entry(record)
        summaries_by_doi[doi] = entry

        user_message = build_user_message(article_text, prompt_template)

        for provider in providers:
            slot = entry["models"].get(provider, {})
            if resume and slot.get("status") == "success":
                continue
            spec = get_model_spec(provider)
            if provider == "openai":
                rows_per_provider["openai"].append(
                    build_openai_request(custom_id=slug, user_message=user_message,
                                         model_id=spec.model_id)
                )
            elif provider == "anthropic":
                rows_per_provider["anthropic"].append(
                    build_anthropic_request(custom_id=slug, user_message=user_message,
                                            model_id=spec.model_id)
                )
            else:
                raise ValueError(
                    f"Batch path does not handle provider '{provider}'. "
                    "Use real-time mode for it (e.g. gemini)."
                )

    # Persist updated entries (with any new `pending` slots) before submission
    # so a crash between build and submit still leaves a valid summaries file.
    _write_all_summaries(SUMMARIES_PATH, summaries_by_doi)

    submitted: list[dict] = []
    timestamp = _timestamp_slug()
    for provider, rows in rows_per_provider.items():
        if not rows:
            print(f"[phase3:batch] no requests to submit for {provider}")
            continue
        jsonl_path = BATCH_DIR / f"{provider}_sum_{timestamp}.jsonl"
        write_batch_jsonl(rows, jsonl_path)

        if provider == "openai":
            job_meta = submit_openai_batch(jsonl_path)
        else:
            job_meta = submit_anthropic_batch(jsonl_path)

        job_meta.update({"stage": "summarize", "request_count": len(rows)})
        record_batch_job(job_meta)
        submitted.append(job_meta)
        print(f"[phase3:batch] submitted {provider} job_id={job_meta['job_id']} "
              f"({len(rows)} requests)")

    return {"submitted": submitted, "slug_to_doi": slug_to_doi}


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
) -> dict:
    """
    Build & submit evaluation batch jobs for OpenAI and Anthropic judges.

    The judge prompt is constructed blind — the summariser identity is placed
    only in the custom_id (format: "{slug}__{summariser}") and never enters
    the message body. This preserves the blind protocol for batch jobs the
    same way run_evaluation() does for real-time jobs.

    Gemini is not supported here (no batch API); call evaluator.run_evaluation()
    with judges=["gemini"] for the real-time path.
    """
    from evaluator import build_judge_prompt, load_judge_prompt, already_evaluated  # noqa: E402

    judges = judges or ["openai"]
    prompt_template = load_judge_prompt()

    rows_per_judge: dict[str, list[dict]] = {j: [] for j in judges}

    for entry in _iter_summaries():
        doi = str(entry.get("doi", "")).strip()
        if not doi:
            continue
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
            # Build the prompt here — summariser name is NOT in it.
            user_message = build_judge_prompt(reference_text, candidate_summary,
                                              prompt_template)
            # Summariser encoded only in the custom_id so we can join results later.
            custom_id = f"{slug}__{summariser}"

            for judge in judges:
                if resume and already_evaluated(doi, summariser, judge):
                    continue
                spec = get_model_spec(judge)
                if judge == "openai":
                    rows_per_judge["openai"].append(
                        build_openai_request(custom_id=custom_id,
                                             user_message=user_message,
                                             model_id=spec.model_id)
                    )
                elif judge == "anthropic":
                    rows_per_judge["anthropic"].append(
                        build_anthropic_request(custom_id=custom_id,
                                                user_message=user_message,
                                                model_id=spec.model_id)
                    )
                else:
                    raise ValueError(
                        f"Batch evaluation does not handle judge '{judge}'. "
                        "Use evaluator.run_evaluation() for real-time judges."
                    )

    submitted: list[dict] = []
    timestamp = _timestamp_slug()
    for judge, rows in rows_per_judge.items():
        if not rows:
            print(f"[phase3:batch] no evaluation requests to submit for judge {judge}")
            continue
        jsonl_path = BATCH_DIR / f"{judge}_eval_{timestamp}.jsonl"
        write_batch_jsonl(rows, jsonl_path)

        if judge == "openai":
            job_meta = submit_openai_batch(jsonl_path)
        else:
            job_meta = submit_anthropic_batch(jsonl_path)

        job_meta.update({"stage": "evaluate", "judge": judge, "request_count": len(rows)})
        record_batch_job(job_meta)
        submitted.append(job_meta)
        print(f"[phase3:batch] submitted {judge} evaluation job_id={job_meta['job_id']} "
              f"({len(rows)} requests)")

    return {"submitted": submitted}


# ---------------------------------------------------------------------------
# Result parsers (used by check_batch_status.py)
# ---------------------------------------------------------------------------

def parse_openai_result_line(line: str) -> dict:
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
        return {
            "custom_id": custom_id,
            "status": "failed",
            "error": str(error),
            "summary": None,
            "input_tokens": None,
            "output_tokens": None,
            "model_version": None,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    choices = body.get("choices") or [{}]
    usage = body.get("usage") or {}
    return {
        "custom_id": custom_id,
        "status": "success",
        "summary": choices[0].get("message", {}).get("content", ""),
        "input_tokens": int(usage.get("prompt_tokens", 0)),
        "output_tokens": int(usage.get("completion_tokens", 0)),
        "model_version": str(body.get("model", "")),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def parse_anthropic_result_line(line: str) -> dict:
    """Parse one line of an Anthropic Message Batches result."""
    raw = json.loads(line)
    custom_id = raw.get("custom_id", "")
    result = raw.get("result") or {}
    rtype = result.get("type")

    if rtype != "succeeded":
        return {
            "custom_id": custom_id,
            "status": "failed",
            "error": str(result.get("error") or rtype),
            "summary": None,
            "input_tokens": None,
            "output_tokens": None,
            "model_version": None,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    message = result.get("message") or {}
    content_blocks = message.get("content") or []
    summary_text = "".join(b.get("text", "") for b in content_blocks if b.get("type") == "text")
    usage = message.get("usage") or {}
    return {
        "custom_id": custom_id,
        "status": "success",
        "summary": summary_text,
        "input_tokens": int(usage.get("input_tokens", 0)),
        "output_tokens": int(usage.get("output_tokens", 0)),
        "model_version": str(message.get("model", "")),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# Eval custom_id like "10_1111_jvim_16872__openai". Match suffix to find summariser.
EVAL_CUSTOM_ID_RE = re.compile(r"^(?P<slug>.+?)__(?P<summariser>[a-z]+)$")


def parse_evaluation_custom_id(custom_id: str) -> tuple[str, str] | None:
    """Return (slug, summariser) for evaluation custom_ids, or None on mismatch."""
    m = EVAL_CUSTOM_ID_RE.match(custom_id)
    if not m:
        return None
    return m.group("slug"), m.group("summariser")
