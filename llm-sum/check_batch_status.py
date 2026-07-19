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
import sys
from pathlib import Path

from batch_utils import (  # noqa: E402
    BATCH_JOBS_PATH,
    SUMMARIES_PATH,
    load_batch_jobs,
    parse_anthropic_result_line,
    parse_openai_result_line,
    parse_evaluation_custom_id,
)
from models_config import compute_cost  # noqa: E402
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

def fetch_openai_status(job_id: str) -> dict:
    import openai  # type: ignore[import-not-found]
    client = openai.OpenAI()
    job = client.batches.retrieve(job_id)
    return {
        "status": job.status,
        "output_file_id": job.output_file_id,
        "error_file_id": job.error_file_id,
        "request_counts": dict(job.request_counts) if job.request_counts else {},
    }


def fetch_anthropic_status(job_id: str) -> dict:
    import anthropic  # type: ignore[import-not-found]
    client = anthropic.Anthropic()
    batch = client.messages.batches.retrieve(job_id)
    return {
        "status": batch.processing_status,
        "request_counts": dict(batch.request_counts) if batch.request_counts else {},
    }


def download_openai_results(output_file_id: str, dest: Path) -> Path:
    import openai  # type: ignore[import-not-found]
    client = openai.OpenAI()
    content = client.files.content(output_file_id).read()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(content)
    return dest


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
    )


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
    parser = (
        parse_openai_result_line if provider == "openai" else parse_anthropic_result_line
    )

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
    time (gemini is not batched at all). The renderer re-reads summaries.jsonl
    each call, so the first merge writes a file with one provider populated and
    the next overwrites it with both — expected, not a bug.

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


def merge_evaluation_results(result_path: Path, provider: str, judge: str,
                             guard: BudgetGuard | None = None) -> int:
    """
    Append evaluation results to data/evaluations.jsonl. Each result corresponds
    to one (paper, summariser) pair encoded in the custom_id.

    The raw batch response content is treated as the judge's raw text and
    routed through parse_judge_response() → build_evaluation_row() so the
    output has the same schema as real-time evaluations: quality_score,
    requires_human_review, parse_method, confidence_score, etc. are all set.

    ``guard`` records the real cost of each merged judge result — see
    _charge_batch_result for why merge time is where batch spend is booked.
    """
    # Imported inside the function to avoid a circular import at module load.
    from evaluator import build_evaluation_row, append_evaluation  # noqa: E402

    guard = guard if guard is not None else BudgetGuard()

    parser = (
        parse_openai_result_line if provider == "openai" else parse_anthropic_result_line
    )
    summaries = _load_summaries()
    custom_id_to_key = _build_custom_id_to_key(summaries)

    merged = 0
    run_cost = 0.0
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

            # The result line parsers reuse the "summary" field name for the
            # model's text output — for judge results that is the raw JSON
            # response containing quality_score, hallucination_count, etc.
            judge_response = {
                "raw_text": parsed.get("summary", ""),
                "input_tokens": parsed.get("input_tokens", 0),
                "output_tokens": parsed.get("output_tokens", 0),
                "model_version": parsed.get("model_version", ""),
            }

            entry = summaries[summary_key]
            doi = str(entry.get("doi", "")).strip()
            input_source = str(entry.get("input_source") or "processed")
            candidate_summary = (entry.get("models") or {}).get(summariser, {}).get("summary", "")

            row = build_evaluation_row(
                doi=doi,
                summariser=summariser,
                judge=judge,
                input_source=input_source,
                reference_text="",   # not stored in the output row — safe to omit
                candidate_summary=candidate_summary,
                judge_response=judge_response,
            )
            # Pass path explicitly so tests can redirect by monkeypatching
            # check_batch_status.EVALUATIONS_PATH without touching evaluator's module state.
            append_evaluation(row, EVALUATIONS_PATH)
            merged += 1

    print(f"[phase3:batch] appended {merged} evaluations to {EVALUATIONS_PATH.name}"
          f"  (batch cost ${run_cost:.4f})")
    # Charged only once every row is safely appended — see the note in
    # merge_summarisation_results for why the hard stop must not fire mid-loop.
    guard.add_cost(run_cost)
    return merged


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

TERMINAL_STATUSES = {"completed", "ended", "failed", "expired", "cancelled"}
# The subset of TERMINAL_STATUSES that actually produced results worth
# downloading. OpenAI reports "completed"; Anthropic reports "ended". The rest
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
            status = (
                fetch_openai_status(job_id) if provider == "openai"
                else fetch_anthropic_status(job_id)
            )
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
                print(f"[phase3:batch] {provider} {job_id} finished as '{s}' — "
                      f"no results to download.")
            continue

        # Download and merge are tracked separately on purpose. Previously a
        # single `if result_path.exists(): continue` skipped both, so a run that
        # downloaded successfully and then crashed mid-merge could never merge
        # those results — the download it had already done was the very thing
        # that stopped it retrying. Paid results stayed stranded on disk.
        result_path = BATCH_DIR / f"{provider}_{job_id}_results.jsonl"
        if not result_path.exists():
            try:
                if provider == "openai" and status.get("output_file_id"):
                    download_openai_results(status["output_file_id"], result_path)
                elif provider == "anthropic":
                    download_anthropic_results(job_id, result_path)
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
