# check_batch_status.py

**For the full batch-mode picture — every command, `--resume`/`--force`/
`--limit`, and the troubleshooting playbook this page's error table
summarizes — see [batch_mode.md](batch_mode.md).**

## What it does

Polls every batch job recorded in `data/batch_jobs.jsonl`, fetches results when a job is complete, and merges them into `data/summaries.jsonl` (for summarisation batches) or `data/evaluations.jsonl` (for judge batches). Read-only with respect to the providers — it does not submit new jobs.

This script is **mode-agnostic**. It always works against `data/batch_jobs.jsonl` regardless of the current `PHASE3_MODE`. The whole point of batch mode is that you can launch a 24-hour job, switch to `single` to iterate on prompts while you wait, then come back and run this script to collect — the script does not care what mode you're currently in.

This script makes live calls to poll job status and download results (OpenAI/Anthropic), so it records real spend: `_batch_result_cost()` computes the USD cost of each merged result from the provider-returned `input_tokens`/`output_tokens` (never an estimate), and `guard.add_cost(run_cost)` — called once per provider merge, during both summarisation and evaluation merging — can trigger `BudgetGuard`'s hard-stop mid-poll if the configured budget cap is exceeded.

## When to run it

* A few hours to 24 hours after submitting a batch (`PHASE3_MODE=batch` → `summarize`).
* Whenever you suspect a batch job might be done.
* Safe to re-run; already-merged results are skipped on the next poll.

This script polls OpenAI/Anthropic and downloads real batch results, so it is a live-API script. It should be run manually by the researcher in their own PowerShell window; Claude does not execute it (see `CLAUDE.md`).

## Inputs

| Path                       | Role                                              |
|----------------------------|---------------------------------------------------|
| `data/batch_jobs.jsonl`    | Ledger of submitted batch jobs (provider, job ID, stage, status). |
| `.env`                     | `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`.            |

## Outputs

| Path                          | What it writes                                                       |
|-------------------------------|----------------------------------------------------------------------|
| `data/summaries.jsonl`        | Merged summary results from completed summarisation batches.         |
| `data/evaluations.jsonl`      | Merged judge results from completed evaluation batches.              |
| `data/batch_jobs.jsonl`       | Updated `status` field per job.                                      |
| `data/batch_summaries_jsonl/` | Readable bullet `.txt` + `prose/` pair for every DOI just merged, written by `_write_batch_readable_outputs` after each summarisation-provider merge — the batch-mode twin of `data/dev_summaries_jsonl/` and `data/single_summaries_jsonl/`. Rewritten per provider merge (Gemini is not batched, so it never appears here). |

## CLI

```powershell
python llm-sum/check_batch_status.py                # poll, merge, exit
python llm-sum/check_batch_status.py --no-download   # only print statuses; do not download or merge
```

There is no `--mode` flag because the script ignores the active mode by design.

## Behaviour by job state

| Provider state | Action                                                                                |
|----------------|---------------------------------------------------------------------------------------|
| `validating` / `in_progress` | Logged; nothing merged this run.                                          |
| `completed`, every row succeeded | Result JSONL downloaded and merged into `summaries.jsonl` or `evaluations.jsonl`. `.merged` marker written. |
| `completed`, every row failed (e.g. a schema error) | Per-row error text downloaded and logged to `data/error_log.jsonl`. `.merged` marker written. For `summarize`-stage jobs, every paper this job covered has its `pending` slot reset back to not-yet-attempted so a future `--resume` can retry it. |
| `failed` / `expired` / `cancelled` (rejected before processing any row — e.g. `token_limit_exceeded`) | Batch-level error text (from the provider's own `errors` field) printed and logged. `.merged` marker written. Same `pending`-slot reset as above. |

The last two rows both mean "this job will never produce results" — the
difference is only *when* it died (mid-processing vs. before the first
row). Either way, `check_batch_status.py` now marks it resolved and un-sticks
its papers automatically; you don't need to do anything extra beyond running
it. See [batch_mode.md](batch_mode.md#problem-4-a-batch-fails-and-its-papers-stay-stuck-at-pending-forever)
for the failure this fixed.

## Common errors and fixes

| Symptom                                            | Cause                                                                | Fix                                                                                       |
|----------------------------------------------------|----------------------------------------------------------------------|-------------------------------------------------------------------------------------------|
| `No batch jobs found at data/batch_jobs.jsonl`     | No batches have been submitted yet.                                  | Run `python llm-sum/run_phase3.py summarize` with `PHASE3_MODE=batch` first.              |
| Status stays `in_progress` for >24h                | Provider batch backlog (rare).                                       | Check the provider's status page; rerun the polling script periodically.                  |
| `failed` status with `error: rate_limited`         | Submission was too large for the batch endpoint at that moment.      | Resubmit by re-running `summarize`; resume picks up only the unsuccessful slots.          |
| `failed` status, `request_counts` all `0`, error text `token_limit_exceeded: Enqueued token limit reached ...` | The whole batch file's prompt tokens exceeded the provider's per-model enqueued-token cap (900,000 for `gpt-5.4` at time of writing) — a real incident on this project's ~207-paper corpus. Not a per-row error and not related to `MAX_OUTPUT_TOKENS`. | Resubmit in smaller chunks with `--resume --limit N` (e.g. `--limit 60`), waiting for each chunk to reach `completed` before submitting the next. Full writeup: [batch_mode.md](batch_mode.md#problem-2-batch-rejected-outright-with-token_limit_exceeded). |
| Every row's error mentions `'required' is required to be supplied ... Missing 'sample_size'` | OpenAI Structured Outputs strict-mode schema rejection — already fixed in `build_openai_request` (`llm-sum/batch_utils.py`) via `to_strict_json_schema()`. | Should not recur; if it does, the schema-building code has regressed. See [batch_mode.md](batch_mode.md#problem-1-request-rejected-with-a-missing-sample_size-schema-error). |
| A resubmission was refused: `REFUSING to submit ... unresolved job(s) already exist` | An earlier batch job for the same provider/stage has no `.merged` marker yet — by design, to prevent a duplicate paid submission. | Run this script first to resolve or merge it. Only pass `--force` once you've confirmed the earlier job is genuinely dead — see [batch_mode.md](batch_mode.md#problem-3-force-created-a-duplicate-batch-job). |
| Duplicate rows in `summaries.jsonl`                | Merge keys use the batch `custom_id`, which encodes DOI plus input source, then replace the matching provider slot in place. | Report a bug if the same `(doi, input_source, provider)` appears twice. |

## Worked example

Run manually in PowerShell — not something Claude runs automatically:

```powershell
# 6 hours after submission:
python llm-sum/check_batch_status.py
# → [phase3:batch] openai_sum_2026-05-25T14-22-01Z status=completed; downloading result file
# → [phase3:batch] merged 248 summaries into data/summaries.jsonl
# → [phase3:batch] anthropic_sum_2026-05-25T14-22-04Z status=in_progress; skipping

# Next day:
python llm-sum/check_batch_status.py
# → [phase3:batch] anthropic_sum_2026-05-25T14-22-04Z status=completed; downloading result file
# → [phase3:batch] merged 247 summaries into data/summaries.jsonl
```
