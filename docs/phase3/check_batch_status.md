# check_batch_status.py

## What it does

Polls every batch job recorded in `data/batch_jobs.jsonl`, fetches results when a job is complete, and merges them into `data/summaries.jsonl` (for summarisation batches) or `data/evaluations.jsonl` (for judge batches). Read-only with respect to the providers — it does not submit new jobs.

This script is **mode-agnostic**. It always works against `data/batch_jobs.jsonl` regardless of the current `PHASE3_MODE`. The whole point of batch mode is that you can launch a 24-hour job, switch to `single` to iterate on prompts while you wait, then come back and run this script to collect — the script does not care what mode you're currently in.

## When to run it

* A few hours to 24 hours after submitting a batch (`PHASE3_MODE=batch` → `summarize`).
* Whenever you suspect a batch job might be done.
* Safe to re-run; already-merged results are skipped on the next poll.

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

## CLI

```powershell
python llm-sum/check_batch_status.py    # poll, merge, exit
```

There is no `--mode` flag because the script ignores the active mode by design.

## Behaviour by job state

| Provider state | Action                                                                                |
|----------------|---------------------------------------------------------------------------------------|
| `validating` / `in_progress` | Logged; nothing merged this run.                                          |
| `completed`    | Result JSONL downloaded and merged into `summaries.jsonl` or `evaluations.jsonl`. Job marked `merged` in the ledger. |
| `failed` / `expired` / `cancelled` | Logged to `error_log.jsonl`; ledger updated; nothing merged.            |

## Common errors and fixes

| Symptom                                            | Cause                                                                | Fix                                                                                       |
|----------------------------------------------------|----------------------------------------------------------------------|-------------------------------------------------------------------------------------------|
| `No batch jobs found at data/batch_jobs.jsonl`     | No batches have been submitted yet.                                  | Run `python llm-sum/run_phase3.py summarize` with `PHASE3_MODE=batch` first.              |
| Status stays `in_progress` for >24h                | Provider batch backlog (rare).                                       | Check the provider's status page; rerun the polling script periodically.                  |
| `failed` status with `error: rate_limited`         | Submission was too large for the batch endpoint at that moment.      | Resubmit by re-running `summarize`; resume picks up only the unsuccessful slots.          |
| Duplicate rows in `summaries.jsonl`                | Cannot happen — merge is keyed on `(doi, provider)` and replaces slots in place. | Report a bug. |

## Worked example

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
