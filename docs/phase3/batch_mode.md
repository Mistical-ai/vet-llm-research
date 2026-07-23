# Batch mode — full guide & troubleshooting

**New to batch mode, or trying to make sense of `evaluate --mode batch --max-requests 50`?**
See [reporting_and_batch_explained.md](reporting_and_batch_explained.md) for
a plain-language walkthrough first — this page is the detailed reference it
links out to.

One page for everything specific to `PHASE3_MODE=batch`: the commands, what
each flag actually does, how state is tracked on disk, and the playbook for
every batch failure this project has actually hit. For the other modes
(`test`/`single`/`dev`) and the rest of the CLI, see
[run_phase3.md](run_phase3.md). This page only covers batch.

## What batch mode is

`summarize --mode batch` (and `evaluate --mode batch`) submit every request
for a provider as one **Batch API** job instead of one HTTP call per paper:
50% cheaper, but the provider returns results asynchronously — anywhere from
minutes to 24 hours later. This is the mode used for the full ~250-paper
corpus. `GEMINI_BATCH_ENABLED=false` (the `.env.template` section 12 default)
means Gemini always runs real-time in the same command even in batch mode;
OpenAI and Anthropic default to true.

## How the three providers differ in batch mode

OpenAI and Anthropic are **structurally similar** in this pipeline: both are
on by default (`OPENAI_BATCH_ENABLED`/`ANTHROPIC_BATCH_ENABLED=true`), both
submit an async job you poll for a terminal status, and both hand back a
result file/stream you download once done. Gemini is the odd one out — a
different SDK, a different status model, and off by default
(`GEMINI_BATCH_ENABLED=false`), so it always runs real-time in the same
`summarize --mode batch` command unless you explicitly flip that flag.

| | OpenAI | Anthropic | Gemini |
|---|---|---|---|
| On by default? | Yes | Yes | **No** — real-time unless `GEMINI_BATCH_ENABLED=true` |
| Submission | Upload a JSONL file, then create a batch job against the uploaded file ID | Submit the request array directly — no file upload step | Upload a JSONL file, then create a batch job (separate SDK: `google.genai`) |
| Status field / values | `status`: `validating` → `in_progress` → `finalizing` → `completed` / `failed` / `expired` / `cancelled` | `processing_status`: `in_progress` → `ended` | `state`: `JOB_STATE_PENDING` → `JOB_STATE_RUNNING` → `JOB_STATE_SUCCEEDED` / `JOB_STATE_FAILED` / `JOB_STATE_EXPIRED` / `JOB_STATE_CANCELLED` (normalized to the same strings as the others internally) |
| Where a failure shows up | Per-row in a separate downloaded error file, **or** at the whole-batch level in the job object's own `errors` field (see Problem 2) | Per-row: each result's `type` is `succeeded`/`errored`/`canceled`/`expired` | Per-row: inline `error`/`status` field on the same result line — no separate error file |
| Structured-output mechanism | Native JSON-schema "strict" mode | Forced tool call (`tool_choice`) | `response_schema` + JSON mime type |
| Known batch-specific quirk | Enqueued **prompt**-token cap per model per org (900,000 for `gpt-5.4`) — see [Problem 2](#problem-2-batch-rejected-outright-with-token_limit_exceeded) | None hit on this project so far | Hidden "thinking"/reasoning tokens used to eat the visible output budget — already mitigated by forcing `thinking_config.thinking_budget=0` and giving Gemini its own higher `GEMINI_MAX_OUTPUT_TOKENS` (7400 vs the shared `MAX_OUTPUT_TOKENS`) |

**Don't confuse the two token issues** — they look similar but are opposite
ends of the request: OpenAI's `token_limit_exceeded` (Problem 2) is about
**input/prompt** tokens queued across every in-flight batch for that model,
org-wide, and has nothing to do with `MAX_OUTPUT_TOKENS`. Gemini's (already-
fixed) issue was hidden **output** reasoning tokens silently consuming the
visible completion budget for a single request. Raising `MAX_OUTPUT_TOKENS`
fixes neither of these — it only helps if a real, visible completion is
being truncated (`finish_reason=length` with actual partial content).

## Prompt caching (judge/evaluate batch jobs only)

Every judge request — batch and real-time, all three providers — is now
**segmented** into rubric / reference / candidate blocks: Anthropic gets the
rubric as its own `system` block plus two content blocks (reference,
candidate) in the one user message; OpenAI gets a `system` message (rubric)
plus a `user` message (reference + candidate); Gemini gets three ordered
`contents[0].parts[]` entries. This split is **unconditional** — it lands the
same way whether `PROMPT_CACHE_ENABLED` is true or false, because it's the
prompt SHAPE, not the caching mechanism itself. Summarisation batch requests
are untouched: there's nothing to cache there (one call per paper per
provider, no shared prefix worth a cache breakpoint).

`PROMPT_CACHE_ENABLED=false` (`.env.template` section 12, the default) means
the segmented shape is sent with no cache marker at all — same text, same
blocks, zero caching-related billing difference from before. Setting it
`true` adds Anthropic's `cache_control` marker (system + reference blocks
only, never candidate) and OpenAI's `prompt_cache_key`. Gemini gets no marker
either way — its caching is implicit, keyed on prefix ordering, which the
three-part segmentation already gives it.

`data/evaluations.jsonl` rows written after this segmentation lands carry
`judge_prompt_shape: "segmented_v1"`, plus `cache_read_input_tokens` /
`cache_creation_input_tokens` (present as real integers — 0 counts as "tried
and missed", not absent). A row with **no** cache fields at all predates this
change entirely — see `docs/phase3/eval_report.md`'s "Absent vs zero"
section before averaging old and new rows into one hit rate.

**Pilot before trusting the full judge run to it:**

```powershell
# .env: PROMPT_CACHE_ENABLED=true
python llm-sum/run_phase3.py evaluate --mode batch --limit 4 --judges anthropic
python llm-sum/check_batch_status.py
```

`merge_evaluation_results`'s print line reports the merge's own hit rate —
`cache read=... write=... hit_rate=...% of N input tokens` — so a 4-paper
pilot answers the "is this actually caching?" question with no extra
tooling. If `cache_read_input_tokens` stays ~0, the article-level breakpoint
isn't paying off for this corpus: leave the flag off, or set
`PROMPT_CACHE_TTL=1h` (batch jobs run over up to 24h with no adjacency
guarantee — a longer TTL survives a bigger gap between an article's three
judge requests) and re-pilot before committing the full run. `PROMPT_CACHE_KEY_SCOPE`
(default `run`, alternative `article`) only affects OpenAI's `prompt_cache_key`
routing hint — `run` uses one key for the whole invocation, `article` emits a
per-DOI key (`veteval-judge-{doi_slug}`); it has no effect on Anthropic or
Gemini. See `.env.template` section 12 for the full break-even arithmetic and
the per-model minimum cacheable prefix.

## The lifecycle: submit → wait → collect

```powershell
# 1. Submit — returns immediately, spends nothing extra beyond the batch job itself
python llm-sum/run_phase3.py summarize --mode batch

# 2. Wait — minutes to 24h. You can safely switch .env to single/dev and do
#    other work; check_batch_status.py ignores the active mode.

# 3. Collect — safe to run anytime, as often as you like, no live spend of its own
python llm-sum/check_batch_status.py
```

Every command that submits or spends money is **PAID** and must be run
manually by you, in your own PowerShell window — an AI assistant working in
this repo does not execute these (see `CLAUDE.md`). `check_batch_status.py`
also makes live calls (it polls providers and downloads results), so it is
manual-only too, even though it never submits anything new.

## Every batch-relevant command

### Submitting: `summarize --mode batch`

```powershell
python llm-sum/run_phase3.py summarize --estimate --mode batch   # free — projected cost, no API calls
python llm-sum/run_phase3.py summarize --mode batch               # PAID — submits jobs, type 'yes'
python llm-sum/run_phase3.py summarize --mode batch --resume
python llm-sum/run_phase3.py summarize --mode batch --providers openai --resume
python llm-sum/run_phase3.py summarize --mode batch --providers openai --resume --limit 60
python llm-sum/run_phase3.py summarize --mode batch --force
```

| Flag | What it does | Batch-specific gotcha |
|------|--------------|------------------------|
| `--resume` | Skip any (doi, provider) pair already at `status=success`, **and** any already at `status=pending` (an earlier submission covers it and is awaiting merge). | This is what stops you from double-paying for a paper whose batch job just hasn't been checked yet. It does **not** know whether that "pending" job is still alive or dead — see [Problem 2](#problem-2-batch-rejected-outright-with-token_limit_exceeded) and [Problem 4](#problem-4-a-batch-fails-and-its-papers-stay-stuck-at-pending-forever) below for what un-sticks a dead one. |
| `--providers openai,anthropic` | Restrict this run to a subset of providers. | Use this to resubmit for exactly the provider that failed, without touching providers that already succeeded. |
| `--limit N` | Cap this run to **N papers that actually get queued for submission** — not the first N manifest rows scanned. | This is the chunking tool: with `--resume`, a mostly-finished provider's remaining backlog can be scattered anywhere in the manifest, so the limit has to count real, new submissions or it would stop after N already-done rows and submit nothing. Use it to stay under a provider's enqueued-token cap — see [Problem 2](#problem-2-batch-rejected-outright-with-token_limit_exceeded). |
| `--force` | Bypass the interactive `yes` confirmation, **and** bypass the refusal to submit while an earlier unresolved batch job for the same provider(s)/stage still exists. | This is the flag that caused a real duplicate submission in this project (same 207 papers submitted twice). Only pass it after `check_batch_status.py` confirms the earlier job is genuinely dead — see [Problem 3](#problem-3-force-created-a-duplicate-batch-job). |
| `--estimate` | Offline tiktoken-based cost forecast. No API calls. | Estimates the whole corpus, not just what `--resume` would still submit — treat it as a ceiling, not a precise "remaining work" number. |

### Submitting the judge: `evaluate --mode batch`

`evaluate --mode batch` takes the same `--resume`/`--force`/`--limit`/`--judges` flags for the judge side — see [run_phase3.md](run_phase3.md#evaluate-paid-unless---mode-test) — plus one judge-only flag:

```powershell
python llm-sum/run_phase3.py evaluate --mode batch --max-requests 50
#  ... wait for check_batch_status.py to show status=completed ...
python llm-sum/run_phase3.py evaluate --mode batch --max-requests 50   # --resume is on by default; sweeps the next chunk
```

| Flag | What it does | Batch-specific gotcha |
|------|--------------|------------------------|
| `--max-requests N` | Caps how many NEW judge requests get queued **per judge** in this submission (not a cap on papers scanned). Evaluate-only — `summarize` has no equivalent flag; use `--limit` for that side. | This is the recommended fix for the same enqueued-token cap described in [Problem 2](#problem-2-batch-rejected-outright-with-token_limit_exceeded), but for judge batches: unlike `--limit`'s fixed-seed sampling (not guaranteed to be a stable superset across sizes), `--max-requests` is checked live against `already_evaluated()` every call, so repeating the same command with `--resume` (the default) genuinely sweeps the remaining backlog chunk by chunk instead of resampling. When a judge hits the cap, the command prints `hit max_new_requests=N` and tells you to re-run once the job is merged. |

### Collecting: `check_batch_status.py`

```powershell
python llm-sum/check_batch_status.py                # poll every job, download + merge finished ones
python llm-sum/check_batch_status.py --no-download   # only print statuses; never download, merge, or resolve anything
```

Always safe to run, at any time, as many times as you like:
- It never submits anything new and never touches an in-progress job beyond printing its status.
- Already-merged jobs are skipped (checked via a `.merged` marker file next to the downloaded results in `data/batch/`).
- Running it while a different job is still in flight does not affect that job.

Full behavior reference: [check_batch_status.md](check_batch_status.md).

### Recovering stuck state: `scripts/reset_phantom_pending_slots.py`

```powershell
python scripts/reset_phantom_pending_slots.py            # dry-run report — reads only, no writes
python scripts/reset_phantom_pending_slots.py --apply     # writes the reset to data/summaries.jsonl
```

Local-only, no live API calls. Finds `summaries.jsonl` rows stuck at
`status: "pending"` with **no real accepted job behind them** (a historical
bug, now fixed at the source — see the script's own docstring) and resets
them to not-yet-attempted so `--resume` will actually submit them. It
deliberately leaves alone any `pending` slot that **does** have a real job
behind it (that's [Problem 4](#problem-4-a-batch-fails-and-its-papers-stay-stuck-at-pending-forever)'s job, which `check_batch_status.py` now handles automatically).

## How batch state is tracked on disk

| File | Role |
|------|------|
| `data/batch_jobs.jsonl` | Ledger — one row per submitted job: `provider`, `job_id`, `stage`, `input_file_path`, `request_count`, `status`. Never hand-edit this. |
| `data/summaries.jsonl` → `models.<provider>.status` | `None` = never attempted. `"pending"` = a request for this paper was queued into an accepted batch job and is awaiting merge. `"success"` = merged result on disk. Never hand-edit this either — a merge in progress can overwrite or confuse a manually-edited slot. |
| `data/batch/{provider}_{job_id}_results.jsonl` | Downloaded raw result rows for a completed job. |
| `data/batch/{provider}_{job_id}_results.merged` | Marker file: this job has been fully resolved (merged, or confirmed dead with nothing to merge). Its **absence** is what `_refuse_duplicate_submission` reads as "still unresolved" and blocks a fresh submission over. |
| `data/error_log.jsonl` | Append-only log of every per-row and per-job batch failure, with the real provider error text. |

## Troubleshooting playbook

Everything below actually happened on this project's corpus, in this order.

### Problem 1: request rejected with a `Missing 'sample_size'` schema error

**Symptom:** a batch job comes back with every row failed, error text like
`Invalid schema for response_format 'VeterinarySummary': ... 'required' is
required to be supplied and to be an array including every key in
properties. Missing 'sample_size'.`

**Cause:** OpenAI's Structured Outputs "strict" mode requires every schema
property to appear in `required`. Already fixed in `build_openai_request`
(`llm-sum/batch_utils.py`) by using `openai.lib._pydantic.to_strict_json_schema()`
instead of the plain Pydantic `.model_json_schema()`. If you ever see this
error again, the schema-building code has regressed — it is not a token or
prompt problem.

**Fix:** none needed going forward; this is a regression test
(`tests/test_batch_utils.py::test_build_openai_request_shape`), not a live
issue to work around.

### Problem 2: batch rejected outright with `token_limit_exceeded`

**Symptom:** the whole job comes back `status=failed` with
`request_counts` all zero (`{'completed': 0, 'failed': 0, 'total': 0}`) —
not per-row failures, the entire file was rejected before processing a
single request. Error text:

```
token_limit_exceeded: Enqueued token limit reached for gpt-5.4 in organization
org-.... Limit: 900,000 enqueued tokens. Please try again once some
in_progress batches have been completed.
```

**Cause:** OpenAI caps total **enqueued prompt (input) tokens** per model,
per org, across every batch that hasn't finished yet — not output tokens,
not something `MAX_OUTPUT_TOKENS` controls. Each paper's prompt in this
corpus runs roughly 12,000–12,500 tokens, so submitting ~180-plus papers at
once (~2.2–2.6M tokens) is 2.5–3x over a 900,000-token cap.

**Fix:** chunk the submission and let each chunk finish before submitting
the next one — the cap is shared across everything currently in flight for
that model:

```powershell
python llm-sum/run_phase3.py summarize --mode batch --providers openai --resume --limit 60
#  ... wait for it to show status=completed via check_batch_status.py ...
python llm-sum/run_phase3.py summarize --mode batch --providers openai --resume --limit 60
#  ... repeat until nothing is left to submit ...
```

~60 papers (~750k tokens) leaves comfortable headroom under 900k. Do not
submit the next chunk while the previous one is still `validating` /
`in_progress` — it will hit the same limit again.

### Problem 3: `--force` created a duplicate batch job

**Symptom:** two batch jobs in `data/batch_jobs.jsonl` for the same provider
and stage, submitted hours apart, covering the exact same set of papers.

**Cause:** `_refuse_duplicate_submission` (`llm-sum/batch_utils.py`) blocks a
new submission whenever an earlier one for the same provider/stage has no
`.merged` marker yet — genuinely correct behavior, meant to stop exactly
this. It only happens if `--force` bypassed that check before confirming
via `check_batch_status.py` that the earlier job was actually dead.

**Is it a double-charge?** Only if **both** jobs actually processed
requests. Check each job's `request_counts` in `check_batch_status.py`
output — a job that failed with `total: 0` (see Problem 2) cost nothing;
providers only bill for requests actually processed.

**Fix:** don't pass `--force` unless `check_batch_status.py`'s output
already shows the earlier job as a dead end (`failed`/`expired`/`cancelled`).
If a duplicate has already happened, merging both is harmless — the second
merge just overwrites the same `models.<provider>` slot per paper (see
`merge_summarisation_results` in `check_batch_status.py`) — the only real
cost is whatever the redundant job actually billed.

### Problem 4: a batch fails and its papers stay stuck at `pending` forever

**Symptom:** `--resume` keeps skipping a known-dead job's papers, and
`_refuse_duplicate_submission` keeps blocking new submissions, even though
`check_batch_status.py` already reported the job as `failed`.

**Cause (fixed):** a job that dies at the whole-batch level (Problem 2's
`status=failed`, not "completed but every row errored") used to be printed
and skipped with no `.merged` marker ever written, and its papers' `pending`
slots were never reset. `check_batch_status.py` now handles this
automatically: for any job that reaches a genuinely terminal, unsuccessful
state, it writes the `.merged` marker (unblocking future submissions) and
resets that job's papers back to not-yet-attempted (unblocking `--resume`)
— you'll see a line like:

```
[phase3:batch] openai batch_... finished as 'failed' — no results to download. token_limit_exceeded: ...
[phase3:batch] openai batch_...: marked resolved, reset 180 paper(s) for a future --resume.
```

**Fix:** nothing extra needed — just run `check_batch_status.py` and the
next `--resume` submission will pick the reset papers back up.

### Problem 5: readable output shows "Status: unknown / No readable summary was produced"

**Symptom:** a paper's entry in `data/batch_summaries_jsonl/*.txt` shows a
provider section with `Status: unknown`, `Model Version: Not recorded`,
`Error: Not recorded`.

**Cause:** that provider's slot for this paper is still `status: "pending"`
(a batch job covering it hasn't been merged yet) — this is a rendering
placeholder for "no result yet," not a failure. Nothing was lost.

**Fix:** run `check_batch_status.py` to check whether the covering job is
done; if it's genuinely dead, see Problem 4 (now automatic).

### Problem 6: `check_batch_status.py` fails immediately with `ModuleNotFoundError`

**Symptom:** running the script fails before it ever reaches the network:

```
ModuleNotFoundError: No module named 'openai'
```

(or `anthropic`, or `google.genai`).

**Cause:** `check_batch_status.py` imports all three provider SDKs to poll
each batch. Those packages (`openai`, `anthropic`, `google-genai`) are listed
in `requirements.txt`, so this means they aren't installed in the venv that's
currently active — a fresh or partially-completed venv, or a different
`python`/`pip` running than the one `.venv\Scripts\activate` put on `PATH`.
It is **not** a bad batch ID or an API-key problem: Python can't find the
libraries before it even talks to an API.

**Fix:** in the same PowerShell session where you'll run the script:

```powershell
# Reinstall everything from requirements.txt...
python -m pip install -r requirements.txt

# ...or target just the three provider SDKs:
python -m pip install openai anthropic google-genai

# Confirm the interpreter you're using actually has them:
python -c "import sys; print(sys.executable)"
python -c "import openai, anthropic; from google import genai; print('ok')"
```

`pip install` only installs the Python **client libraries** that call each
provider's API — it never downloads a model onto your machine. Model IDs
live in `.env` / `llm-sum/models_config.py`; the actual GPT/Claude/Gemini
models stay on the providers' servers and are only reached over the network
at request time. Once the `import` check above prints `ok`, rerun:

```powershell
python llm-sum/check_batch_status.py
```

## Golden safety rules

- Never hand-edit `data/summaries.jsonl` or `data/batch_jobs.jsonl` — a merge in progress can overwrite or confuse a manually-changed slot/ledger row.
- Never pass `--force` to `summarize --mode batch` without first running `check_batch_status.py` and confirming the blocking job is actually dead.
- `check_batch_status.py` (and `scripts/reset_phantom_pending_slots.py` in dry-run) are always safe to run — read/merge-only, no new submissions, no cost of their own.
- All of the above are live-API commands (except the dry-run reset script) and must be run manually by you — not executed automatically by an AI assistant in this repo (`CLAUDE.md`).
