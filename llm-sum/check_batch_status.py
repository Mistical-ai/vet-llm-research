"""
llm-sum/check_batch_status.py — Poll batch jobs and merge results
===================================================================

Reads the batch job ledger at data/batch_jobs.jsonl, asks each provider
about the current status, and — when a job has completed — downloads the
output file and merges the results into data/summaries.jsonl (or
data/evaluations.jsonl for evaluation-stage jobs) via the standard
parsers in batch_utils.

Run manually whenever you want to check progress:
    python llm-sum/check_batch_status.py
    python llm-sum/check_batch_status.py --no-download
"""

from __future__ import annotations

from _bootstrap import *  # noqa: F401,F403

import argparse
import json
import os
import sys
from pathlib import Path

from batch_utils import (  # noqa: E402
    BATCH_JOBS_PATH,
    SUMMARIES_PATH,
    _read_cached_text,
    load_batch_jobs,
    parse_anthropic_result_line,
    parse_gemini_result_line,
    parse_openai_result_line,
    parse_evaluation_custom_id,
    safe_job_id_for_path,
)
from models_config import compute_cost, PROMPT_CACHE_TTL  # noqa: E402
from utils import BudgetGuard, log_error  # noqa: E402

EVALUATIONS_PATH = DATA_DIR / "evaluations.jsonl"
# Readable sibling of data/summaries.jsonl for batch runs — the batch-mode twin
# of data/dev_summaries_jsonl/ and data/single_summaries_jsonl/. Kept here (not
# imported from run_phase3) so this script stays standalone; run_phase3 defines
# the same constant for its own CLI help. Tests monkeypatch this name.
BATCH_SUMMARIES_JSONL_DIR = DATA_DIR / "batch_summaries_jsonl"


# ---------------------------------------------------------------------------
# Provider status polling
# ---------------------------------------------------------------------------

def _openai_batch_level_errors(job: object) -> list[dict]:
    """Extract the Batch object's own ``errors`` field (distinct from
    ``error_file_id``): the reason a batch never processed a single request
    (e.g. rejected at file validation) lives here, not in a per-row file.
    """
    errors_obj = getattr(job, "errors", None)
    if errors_obj is None:
        return []
    data = getattr(errors_obj, "data", None)
    if data is None and isinstance(errors_obj, dict):
        data = errors_obj.get("data")
    out = []
    for item in data or []:
        if isinstance(item, dict):
            out.append({"code": item.get("code"), "message": item.get("message")})
        else:
            out.append({"code": getattr(item, "code", None),
                        "message": getattr(item, "message", None)})
    return out


def fetch_openai_status(job_id: str) -> dict:
    import openai  # type: ignore[import-not-found]
    client = openai.OpenAI()
    job = client.batches.retrieve(job_id)
    return {
        "status": job.status,
        "output_file_id": job.output_file_id,
        "error_file_id": job.error_file_id,
        "request_counts": dict(job.request_counts) if job.request_counts else {},
        "errors": _openai_batch_level_errors(job),
    }


def fetch_anthropic_status(job_id: str) -> dict:
    import anthropic  # type: ignore[import-not-found]
    client = anthropic.Anthropic()
    batch = client.messages.batches.retrieve(job_id)
    return {
        "status": batch.processing_status,
        "request_counts": dict(batch.request_counts) if batch.request_counts else {},
    }


# Gemini's Batch Mode reports state as JOB_STATE_* (ai.google.dev/gemini-api/docs/batch-mode)
# rather than OpenAI's "completed" / Anthropic's "ended". Normalising here means
# SUCCESS_STATUSES / TERMINAL_STATUSES below stay untouched and provider-agnostic.
GEMINI_STATE_TO_STATUS = {
    "JOB_STATE_SUCCEEDED": "completed",
    "JOB_STATE_FAILED": "failed",
    "JOB_STATE_EXPIRED": "expired",
    "JOB_STATE_CANCELLED": "cancelled",
    "JOB_STATE_PENDING": "pending",
    "JOB_STATE_RUNNING": "running",
}


def fetch_gemini_status(job_id: str) -> dict:
    from google import genai  # type: ignore[import-not-found]
    client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))
    job = client.batches.get(name=job_id)
    state = getattr(job, "state", None)
    state_name = state.name if state is not None and hasattr(state, "name") else str(state)
    dest = getattr(job, "dest", None)
    return {
        "status": GEMINI_STATE_TO_STATUS.get(state_name, str(state_name).lower()),
        "output_file_name": getattr(dest, "file_name", None) if dest is not None else None,
        "request_counts": {},
    }


def download_openai_results(output_file_id: str, dest: Path) -> Path:
    import openai  # type: ignore[import-not-found]
    client = openai.OpenAI()
    content = client.files.content(output_file_id).read()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(content)
    return dest


def download_openai_error_file(error_file_id: str) -> str:
    """
    Fetch a completed-but-failed OpenAI batch job's per-row error detail.

    A job can be terminal-success (status "completed") while every request
    in it failed — in that case ``output_file_id`` is None and the failure
    detail lives in ``error_file_id`` instead (see fetch_openai_status). This
    is the read-only counterpart to download_openai_results: same client
    shape, but returns text (each line already valid JSON) for the caller to
    parse and log, rather than writing a results file to merge.
    """
    import openai  # type: ignore[import-not-found]
    client = openai.OpenAI()
    content = client.files.content(error_file_id).read()
    if isinstance(content, bytes):
        content = content.decode("utf-8")
    return content


def download_anthropic_results(job_id: str, dest: Path) -> Path:
    """
    Anthropic returns results via a streamable endpoint. Write each row
    as one JSON line to match the OpenAI shape.
    """
    import anthropic  # type: ignore[import-not-found]
    client = anthropic.Anthropic()
    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(dest, "w", encoding="utf-8") as f:
        for line in client.messages.batches.results(job_id):
            # SDK yields dict-like objects; serialise to JSON.
            f.write(json.dumps(line.model_dump() if hasattr(line, "model_dump") else line) + "\n")
    return dest


def download_gemini_results(output_file_name: str, dest: Path) -> Path:
    """Download a completed Gemini batch job's result JSONL via the Files API."""
    from google import genai  # type: ignore[import-not-found]
    client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))
    content = client.files.download(file=output_file_name)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(content if isinstance(content, (bytes, bytearray)) else bytes(content))
    return dest


# ---------------------------------------------------------------------------
# Merge downloaded results into data/summaries.jsonl
# ---------------------------------------------------------------------------

def _load_summaries() -> dict[str, dict]:
    if not SUMMARIES_PATH.exists():
        return {}
    from summarizer import _entry_key  # noqa: E402

    out: dict[str, dict] = {}
    with open(SUMMARIES_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            doi = str(entry.get("doi", "")).strip()
            if doi:
                out[_entry_key(entry)] = entry
    return out


def _build_custom_id_to_key(summaries: dict[str, dict]) -> dict[str, str]:
    return {
        str(entry.get("custom_id") or ""): key
        for key, entry in summaries.items()
        if entry.get("custom_id")
    }


def _write_summaries(summaries: dict[str, dict]) -> None:
    SUMMARIES_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = SUMMARIES_PATH.with_suffix(SUMMARIES_PATH.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        for entry in summaries.values():
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    tmp.replace(SUMMARIES_PATH)


def _reset_pending_slots_for_job(provider: str, input_file_path: str | None) -> int:
    """Reset this job's papers' provider slot from "pending" back to the
    not-yet-attempted state (mirrors summarizer._empty_model_slot / the
    same-purpose reset in scripts/reset_phantom_pending_slots.py).

    Called only once a job is confirmed dead (no usable results possible —
    a batch-level failure, or a "completed" job where every row errored).
    A --resume run skips any slot at status=="pending" unconditionally, so
    without this reset those papers could never be retried even after the
    dead job is marked resolved. Only touches slots still exactly "pending"
    — a paper that already succeeded via a different job/provider run is
    left untouched.
    """
    if not input_file_path or not Path(input_file_path).exists():
        return 0
    custom_ids: set[str] = set()
    with open(input_file_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            cid = row.get("custom_id") or row.get("key")  # Gemini rows key on "key"
            if cid:
                custom_ids.add(cid)
    if not custom_ids:
        return 0

    summaries = _load_summaries()
    custom_id_to_key = _build_custom_id_to_key(summaries)
    reset_count = 0
    for cid in custom_ids:
        key = custom_id_to_key.get(cid)
        if not key:
            continue
        models = summaries[key].setdefault("models", {})
        if (models.get(provider) or {}).get("status") == "pending":
            models[provider] = {
                "status": None,
                "summary": None,
                "input_tokens": None,
                "output_tokens": None,
                "model_version": None,
                "timestamp": None,
            }
            reset_count += 1
    if reset_count:
        _write_summaries(summaries)
    return reset_count


def _batch_result_cost(provider: str, parsed: dict) -> float:
    """Return the real USD cost of one merged batch result.

    Batch results are the only paid calls in the pipeline whose cost is not
    known until long after the request was sent, so merge time is the first —
    and only — moment real spend can be recorded for them. Not doing so is what
    let the batch path run with no budget tracking at all, in the very mode that
    processes the whole corpus.

    Tokens come from the provider's own response via the result-line parsers,
    never from an estimate, which is what keeps ``total_spent`` aligned with the
    actual bill. ``batched=True`` selects the 50%-discounted batch prices.
    """
    if parsed.get("status") != "success":
        return 0.0
    return compute_cost(
        provider,
        int(parsed.get("input_tokens") or 0),
        int(parsed.get("output_tokens") or 0),
        batched=True,
        cache_read_tokens=int(parsed.get("cache_read_input_tokens") or 0),
        cache_write_tokens=int(parsed.get("cache_creation_input_tokens") or 0),
        cache_ttl=PROMPT_CACHE_TTL,
    )


_RESULT_PARSERS = {
    "openai": parse_openai_result_line,
    "anthropic": parse_anthropic_result_line,
    "gemini": parse_gemini_result_line,
}


def merge_summarisation_results(result_path: Path, provider: str,
                                guard: BudgetGuard | None = None) -> int:
    """
    Read the downloaded JSONL and merge each result into data/summaries.jsonl.
    Returns the count of successfully merged rows.

    ``guard`` accumulates the real cost of the merged results. Callers that
    merge several jobs in one run (poll_all) pass a shared guard so the hard
    stop sees the whole run's spend rather than each job's in isolation.
    """
    guard = guard if guard is not None else BudgetGuard()
    parser = _RESULT_PARSERS[provider]

    summaries = _load_summaries()
    custom_id_to_key = _build_custom_id_to_key(summaries)

    merged = 0
    run_cost = 0.0
    touched_dois: set[str] = set()
    with open(result_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            parsed = parser(line)
            custom_id = parsed.pop("custom_id", "")
            summary_key = custom_id_to_key.get(custom_id)
            if not summary_key:
                log_error(custom_id, "batch_merge",
                          f"custom_id has no matching DOI in summaries.jsonl")
                continue
            entry = summaries[summary_key]
            entry.setdefault("models", {})[provider] = parsed
            doi = str(entry.get("doi", "")).strip()
            if doi:
                touched_dois.add(doi)
            run_cost += _batch_result_cost(provider, parsed)
            merged += 1

    # Persist BEFORE charging. add_cost() hard-stops via sys.exit when the limit
    # is crossed, and this money was already spent when the batch ran — exiting
    # first would throw away results we have paid for and cannot re-derive. The
    # guard's job here is to record the spend and stop the NEXT thing, not to
    # protect a purchase that already completed.
    _write_summaries(summaries)
    print(f"[phase3:batch] merged {merged} {provider} summaries into "
          f"{SUMMARIES_PATH.name}  (batch cost ${run_cost:.4f})")
    _write_batch_readable_outputs(touched_dois)
    guard.add_cost(run_cost)
    return merged


def _write_batch_readable_outputs(dois: set[str]) -> int:
    """Render the just-merged DOIs into data/batch_summaries_jsonl/.

    Gives batch runs the same readable pair every other run kind produces — the
    bullet ``.txt`` at the top level plus its flowing-prose sibling in
    ``prose/`` — so the finished corpus is ready for
    ``evaluate --mode dev --source-dir data/batch_summaries_jsonl`` and for
    human validation without any extra step.

    Called once per provider merge, and batch merges arrive one provider at a
    time (and Gemini may not appear here at all if GEMINI_BATCH_ENABLED=false,
    since it was already written by the real-time fallback during `summarize`).
    The renderer re-reads summaries.jsonl each call, so the first merge writes
    a file with one provider populated and the next overwrites it with more —
    expected, not a bug.

    Never fatal: a rendering problem must not lose an expensive completed batch
    merge that is already safely on disk.
    """
    if not dois:
        return 0
    try:
        from summarizer import write_dev_summary_jsonl_outputs
        written = write_dev_summary_jsonl_outputs(
            dois,
            output_dir=BATCH_SUMMARIES_JSONL_DIR,
            input_source="processed",
            summaries_path=SUMMARIES_PATH,
            run_kind="batch",
        )
        print(f"[phase3:batch] wrote {written} readable batch summary "
              f"file(s) to {BATCH_SUMMARIES_JSONL_DIR}")
        return written
    except Exception as exc:  # noqa: BLE001 - never lose a merged batch over a render
        print(f"[phase3:batch] WARNING: could not write readable batch summaries "
              f"to {BATCH_SUMMARIES_JSONL_DIR}: {exc}")
        return 0


def _existing_evaluation_keys(path: Path) -> set[tuple[str, str, str]]:
    """(doi, summariser, judge) triples already on disk in evaluations.jsonl.

    IN PLAIN ENGLISH: reads the ledger and remembers, for every row already
    saved, which (paper, AI summariser, judge) combination it represents.

    Lets ``merge_evaluation_results`` skip a result that already made it into
    the append-only ledger on an earlier, interrupted run of this same merge
    (e.g. the process died partway through a batch of appends) instead of
    writing a duplicate row. Returns an empty set if the file doesn't exist
    yet — nothing merged so far, so nothing to skip.
    """
    if not path.exists():
        return set()
    keys: set[tuple[str, str, str]] = set()
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            keys.add((row.get("doi", ""), row.get("summarizer", ""), row.get("judge", "")))
    return keys


def merge_evaluation_results(result_path: Path, provider: str, judge: str,
                             guard: BudgetGuard | None = None) -> int:
    """
    Append evaluation results to data/evaluations.jsonl. Each result corresponds
    to one (paper, summariser) pair encoded in the custom_id.

    The raw batch response content is treated as the judge's raw text and
    routed through parse_judge_response() → build_evaluation_row() so the
    output has the same schema as real-time evaluations: quality_score,
    requires_human_review, parse_method, confidence_score, etc. are all set.

    Also builds ``strata`` (species/study_design/clinical_topic/journal) the
    same way the real-time path (eval_instances.iter_evaluation_instances)
    does, via the same build_strata()/load_manifest_index() helpers — this
    function used to omit strata entirely, silently leaving every batch-
    merged row's stratified breakdowns unpopulated (eval_report's "By
    Journal"/"By Species"/etc. tables would show it as "(no metadata)")
    even though the journal/species/etc. were sitting right there in
    summaries.jsonl and manifest.jsonl the whole time.

    ``guard`` records the real cost of each merged judge result — see
    _charge_batch_result for why merge time is where batch spend is booked.
    """
    # Imported inside the function to avoid a circular import at module load.
    from evaluator import build_evaluation_row, append_evaluation  # noqa: E402
    from eval_instances import build_strata, load_manifest_index  # noqa: E402

    guard = guard if guard is not None else BudgetGuard()

    parser = _RESULT_PARSERS[provider]
    summaries = _load_summaries()
    custom_id_to_key = _build_custom_id_to_key(summaries)
    manifest_index = load_manifest_index()
    already_merged = _existing_evaluation_keys(EVALUATIONS_PATH)

    merged = 0
    skipped_duplicates = 0
    run_cost = 0.0
    # Cache-token totals across this merge, so the pilot's own hit rate is
    # visible right here with no extra tooling (Phase A3) — summed from
    # whatever the result-line parser reported, which is None (not counted)
    # on a pre-caching row and an explicit int (possibly 0) on any row
    # produced by the current segmented-prompt code (see the "absent vs
    # zero" note in build_evaluation_row).
    run_cache_read = 0
    run_cache_creation = 0
    run_input_tokens = 0
    with open(result_path, encoding="utf-8") as in_f:
        for line in in_f:
            line = line.strip()
            if not line:
                continue
            parsed = parser(line, expect_summary_schema=False)
            cid = parsed.pop("custom_id", "")
            pair = parse_evaluation_custom_id(cid)
            if pair is None:
                log_error(cid, "batch_merge",
                          "evaluation custom_id did not match slug__summariser pattern")
                continue
            slug, summariser = pair
            summary_key = custom_id_to_key.get(slug)
            if not summary_key:
                log_error(cid, "batch_merge",
                          "evaluation slug has no matching DOI in summaries.jsonl")
                continue

            entry = summaries[summary_key]
            doi = str(entry.get("doi", "")).strip()

            # Guards against re-processing the same downloaded result file
            # twice — e.g. a prior run of this merge crashed/lost its
            # connection partway through (append-only ledger, no in-place
            # dedup on write) and is now being retried. Without this check a
            # retry would re-append every already-merged row a second time.
            if (doi, summariser, judge) in already_merged:
                skipped_duplicates += 1
                continue

            if parsed.get("status") != "success":
                # `cid`, not `doi`: the DOI is not resolved until further down,
                # so naming it here raised UnboundLocalError on the first failed
                # result and aborted the whole merge. `cid` is what the two
                # log_error calls above use, and it carries the same identity.
                log_error(cid, "batch_merge",
                          f"evaluation result failed for {cid}: {parsed.get('error')}")
                continue

            # Judge calls are paid too. Accumulated, not charged inline: a
            # hard stop mid-loop would leave some rows appended and the rest
            # not, and re-running would then duplicate the appended ones in an
            # append-only ledger.
            run_cost += _batch_result_cost(provider, parsed)
            run_input_tokens += int(parsed.get("input_tokens") or 0)
            run_cache_read += int(parsed.get("cache_read_input_tokens") or 0)
            run_cache_creation += int(parsed.get("cache_creation_input_tokens") or 0)

            # The result line parsers reuse the "summary" field name for the
            # model's text output — for judge results that is the raw JSON
            # response containing quality_score, hallucination_count, etc.
            judge_response = {
                "raw_text": parsed.get("summary", ""),
                "input_tokens": parsed.get("input_tokens", 0),
                "output_tokens": parsed.get("output_tokens", 0),
                "model_version": parsed.get("model_version", ""),
                "cache_read_input_tokens": parsed.get("cache_read_input_tokens"),
                "cache_creation_input_tokens": parsed.get("cache_creation_input_tokens"),
                "judge_prompt_shape": parsed.get("judge_prompt_shape"),
            }

            input_source = str(entry.get("input_source") or "processed")
            candidate_summary = (entry.get("models") or {}).get(summariser, {}).get("summary", "")
            manifest_record = manifest_index.get(doi, {})
            strata = build_strata(entry, manifest_record)
            # NOT stored raw in the output row (build_evaluation_row only keeps
            # the computed automatic_metrics dict) — but it IS needed as input
            # to that computation. Passing "" here used to silently zero out
            # every ROUGE/compression/coverage stat for every batch-merged row
            # (a blank reference makes ROUGE recall mathematically always 0),
            # since calculate_automatic_metrics() has no other source for the
            # article text. Same cached-text lookup run_batch_evaluation()
            # already uses when it first builds the judge request.
            reference_text = _read_cached_text(entry) or ""

            row = build_evaluation_row(
                doi=doi,
                summariser=summariser,
                judge=judge,
                input_source=input_source,
                reference_text=reference_text,
                candidate_summary=candidate_summary,
                judge_response=judge_response,
                strata=strata,
            )
            # Pass path explicitly so tests can redirect by monkeypatching
            # check_batch_status.EVALUATIONS_PATH without touching evaluator's module state.
            append_evaluation(row, EVALUATIONS_PATH)
            merged += 1

    hit_rate = (run_cache_read / run_input_tokens) if run_input_tokens else 0.0
    skipped_note = f", skipped {skipped_duplicates} already-merged duplicate(s)" if skipped_duplicates else ""
    print(f"[phase3:batch] appended {merged} evaluations to {EVALUATIONS_PATH.name}"
          f"  (batch cost ${run_cost:.4f}, cache read={run_cache_read} "
          f"write={run_cache_creation} tokens, hit_rate={hit_rate:.1%} of "
          f"{run_input_tokens} input tokens{skipped_note})")
    # Charged only once every row is safely appended — see the note in
    # merge_summarisation_results for why the hard stop must not fire mid-loop.
    guard.add_cost(run_cost)
    return merged


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

TERMINAL_STATUSES = {"completed", "ended", "failed", "expired", "cancelled"}
# The subset of TERMINAL_STATUSES that actually produced results worth
# downloading. OpenAI reports "completed"; Anthropic reports "ended"; Gemini's
# JOB_STATE_* names are normalised to these same strings by
# GEMINI_STATE_TO_STATUS above, so this set stays provider-agnostic. The rest
# of TERMINAL_STATUSES are terminal *failures* — asking a provider for the
# output of a failed or expired job just wastes a request and logs a confusing
# error, so they are reported and skipped instead.
SUCCESS_STATUSES = {"completed", "ended"}


def poll_all(*, download_when_complete: bool = True) -> None:
    """
    Iterate every job in the ledger, print status, and download results for
    completed jobs we haven't merged yet.
    """
    jobs = load_batch_jobs()
    if not jobs:
        print(f"[phase3:batch] no jobs in {BATCH_JOBS_PATH.name}")
        return

    # One guard for the whole poll so the hard stop reflects everything this
    # run merges, not each job in isolation.
    guard = BudgetGuard()

    for job in jobs:
        provider = job["provider"]
        job_id = job["job_id"]
        try:
            if provider == "openai":
                status = fetch_openai_status(job_id)
            elif provider == "anthropic":
                status = fetch_anthropic_status(job_id)
            else:
                status = fetch_gemini_status(job_id)
        except Exception as exc:
            print(f"[phase3:batch] {provider} {job_id} status fetch failed: {exc}")
            continue

        print(f"[phase3:batch] {provider} {job_id} status={status['status']}  "
              f"counts={status.get('request_counts')}")

        if not download_when_complete:
            continue
        s = status["status"]
        if s not in SUCCESS_STATUSES:
            if s in TERMINAL_STATUSES:
                # A batch that fails/expires/is cancelled BEFORE processing any
                # row (e.g. OpenAI status="failed", request_counts all 0 — a
                # file-validation rejection, not a per-row error) used to just
                # print and `continue` here with no `.merged` marker ever
                # written. batch_utils._unresolved_batch_jobs treats "no
                # marker" as still-in-flight, so a dead job like this blocked
                # every future submission for this stage/provider forever
                # (short of --force) and its papers' "pending" slots could
                # never be retried either. Resolve it the same way the
                # completed-but-every-row-failed branch below does.
                safe_job_id = safe_job_id_for_path(job_id)
                result_path = BATCH_DIR / f"{provider}_{safe_job_id}_results.jsonl"
                merged_marker = result_path.with_suffix(".merged")
                if not merged_marker.exists():
                    errors = status.get("errors") or []
                    detail = ("; ".join(
                        f"{e.get('code', '?')}: {e.get('message', '?')}" for e in errors
                    ) or "no per-row/batch error detail returned by the provider")
                    print(f"[phase3:batch] {provider} {job_id} finished as '{s}' — "
                          f"no results to download. {detail}")
                    reset_count = 0
                    if job.get("stage", "summarize") == "summarize":
                        reset_count = _reset_pending_slots_for_job(
                            provider, job.get("input_file_path"))
                    log_error("N/A", "batch_merge",
                              f"{provider} {job.get('stage', 'summarize')} job {job_id} "
                              f"ended as '{s}' with no usable results ({detail}); "
                              f"reset {reset_count} paper(s) to retryable state.")
                    merged_marker.write_text(
                        f"resolved stage={job.get('stage', 'summarize')} provider={provider} "
                        f"job_id={job_id} status=dead reason={s} reset_slots={reset_count}\n",
                        encoding="utf-8",
                    )
                    print(f"[phase3:batch] {provider} {job_id}: marked resolved, "
                          f"reset {reset_count} paper(s) for a future --resume.")
            continue

        # Download and merge are tracked separately on purpose. Previously a
        # single `if result_path.exists(): continue` skipped both, so a run that
        # downloaded successfully and then crashed mid-merge could never merge
        # those results — the download it had already done was the very thing
        # that stopped it retrying. Paid results stayed stranded on disk.
        #
        # Gemini job names look like "batches/abc123" (a resource path, not a
        # bare id) — sanitised here so the "/" never turns into an unwanted
        # subdirectory under BATCH_DIR. The real job_id (with the slash) is
        # still what's sent back to the Gemini API in fetch/download above.
        # Shared with batch_utils._unresolved_batch_jobs so both agree on the
        # same result-file name.
        safe_job_id = safe_job_id_for_path(job_id)
        result_path = BATCH_DIR / f"{provider}_{safe_job_id}_results.jsonl"
        # OpenAI's terminal-success statuses can still mean "every request in
        # the job failed" — in that case output_file_id is None and the
        # failure detail lives in error_file_id instead (see
        # fetch_openai_status above). The generic download branch below only
        # checks output_file_id for OpenAI, so an all-failed job used to fall
        # through to `else: continue` silently: no print, no .merged marker.
        # Since batch_utils._unresolved_batch_jobs treats "no .merged marker"
        # as "still unresolved", that stranded job blocked every future
        # OpenAI batch submission until the user passed --force — even
        # though the job was genuinely done, just failed. Handle it here,
        # before the normal download/merge path, so a real all-failed job
        # still gets logged and marked resolved instead of merged.
        if (provider == "openai" and not status.get("output_file_id")
                and status.get("error_file_id")):
            merged_marker = result_path.with_suffix(".merged")
            if merged_marker.exists():
                continue
            try:
                error_content = download_openai_error_file(status["error_file_id"])
            except Exception as exc:
                print(f"[phase3:batch] error-file download failed for {job_id}: {exc}")
                continue
            print(error_content)
            count = 0
            for line in error_content.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                custom_id = row.get("custom_id", "")
                message = (
                    row.get("response", {}).get("body", {}).get("error", {}).get("message")
                    or str(row.get("error"))
                )
                log_error(custom_id, "batch_merge", message)
                count += 1
            stage = job.get("stage", "summarize")
            reset_count = 0
            if stage == "summarize":
                reset_count = _reset_pending_slots_for_job(provider, job.get("input_file_path"))
            merged_marker.write_text(
                f"resolved stage={stage} provider={provider} job_id={job_id} "
                f"status=all_failed reset_slots={reset_count}\n",
                encoding="utf-8",
            )
            print(f"[phase3:batch] {provider} {job_id}: all {count} requests failed — "
                  f"see data/error_log.jsonl for details "
                  f"(reset {reset_count} paper(s) for a future --resume)")
            continue

        if not result_path.exists():
            try:
                if provider == "openai" and status.get("output_file_id"):
                    download_openai_results(status["output_file_id"], result_path)
                elif provider == "anthropic":
                    download_anthropic_results(job_id, result_path)
                elif provider == "gemini" and status.get("output_file_name"):
                    download_gemini_results(status["output_file_name"], result_path)
                else:
                    continue
            except Exception as exc:
                print(f"[phase3:batch] download failed for {job_id}: {exc}")
                continue

        # The merge marker, not the results file, is what says "already done".
        # It matters most for the evaluate stage: merge_evaluation_results
        # APPENDS to the append-only evaluations.jsonl, so merging twice would
        # duplicate every row and quietly inflate the judge panel. The marker is
        # written only after a merge returns cleanly, so a crashed merge leaves
        # it absent and the next poll retries.
        merged_marker = result_path.with_suffix(".merged")
        if merged_marker.exists():
            continue

        stage = job.get("stage", "summarize")
        if stage == "summarize":
            merge_summarisation_results(result_path, provider=provider, guard=guard)
        elif stage == "evaluate":
            judge = job.get("judge", provider)
            merge_evaluation_results(result_path, provider=provider, judge=judge,
                                     guard=guard)
        merged_marker.write_text(
            f"merged stage={stage} provider={provider} job_id={job_id}\n",
            encoding="utf-8",
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Poll Phase 3 batch jobs and merge results.")
    parser.add_argument("--no-download", action="store_true",
                        help="Only print statuses; do not download or merge.")
    args = parser.parse_args(argv)
    poll_all(download_when_complete=not args.no_download)
    return 0


if __name__ == "__main__":
    sys.exit(main())
