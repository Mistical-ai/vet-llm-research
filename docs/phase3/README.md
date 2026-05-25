# Phase 3 — LLM summarisation & evaluation

This folder is the user manual for everything under [`llm-sum/`](../../llm-sum/) and the Phase 3 helper [`scripts/`](../../scripts/). One file per script, all in the same shape, so you can pick any one and skim it without learning a new layout.

If you only read one section: [the mode cheat-sheet below](#mode-cheat-sheet).

---

## What Phase 3 does

You already have ~250 cleaned PDFs in `data/raw/` from Phase 2. Phase 3 turns those into summaries from three LLMs (OpenAI, Anthropic, Gemini) and then has a "blind judge" score the summaries. The output is two append-only JSON Lines files you can analyse in Phase 4–5:

```
data/summaries.jsonl     # one row per paper, with one slot per model
data/evaluations.jsonl   # one row per (paper, summariser, judge) triple
```

The pipeline runs in four steps:

```
extract        PDFs in data/raw/         →  data/processed/<stem>.jsonl
summarize      processed/*.jsonl         →  data/summaries.jsonl
evaluate       summaries.jsonl           →  data/evaluations.jsonl
status         (read-only)               →  prints counts to stdout
```

There is one orchestrator script that wraps all four ([`run_phase3.py`](run_phase3.md)) and there are individual scripts you can call directly when you want fine-grained control. They do exactly the same thing — pick whichever you prefer.

---

## Mode cheat-sheet

Every Phase 3 script reads `PHASE3_MODE` from `.env` (or accepts `--mode` on the CLI). One knob, four values:

| Mode     | API calls?           | Papers processed                  | Confirm prompt | Use when                                                  |
|----------|----------------------|------------------------------------|----------------|-----------------------------------------------------------|
| `test`   | None (mocks)         | All                                | No             | Running `pytest` or smoke-testing the pipeline end-to-end |
| `single` | Real-time, ~$0.16    | 1                                  | Yes (`yes`)    | Validating a new prompt before bulk runs                  |
| `dev`    | Real-time, budget-cap| `PHASE3_DEV_LIMIT` (default `5`)   | Yes            | Sanity-checking on a small batch with real models         |
| `batch`  | Batch API, 50% off   | All                                | Yes            | The real 250-paper run                                    |

* CLI `--mode` overrides `.env`.
* CLI `--limit N` overrides the mode's paper count for that one run (e.g. `--mode dev --limit 1` = a 1-paper real-time test).
* `test` mode forces `dry_run=True` even if `DRY_RUN=false` is set somewhere — last guardrail.
* Unknown mode names fall back to `test` with a warning printed.

The full mode table and its rules live in [`llm-sum/phase3_mode.py`](../../llm-sum/phase3_mode.py).

---

## Three named recipes

### "I want to test my prompt on one paper"

```powershell
# In your .env: PHASE3_MODE=single
python llm-sum/run_phase3.py extract        # only if data/processed/ is empty
python llm-sum/run_phase3.py summarize      # asks 'yes', then runs 1 paper × 3 models
python llm-sum/run_phase3.py evaluate
```

Cost: ~$0.16 per paper. Use this before any bulk run — if the prompt is wrong, you've spent 16 cents instead of $40.

### "I want a small dev run on 5 papers"

```powershell
# In your .env: PHASE3_MODE=dev, PHASE3_DEV_LIMIT=5
python llm-sum/run_phase3.py extract
python llm-sum/run_phase3.py summarize --estimate    # prints the projected cost first
python llm-sum/run_phase3.py summarize               # asks 'yes', then runs 5 papers
python llm-sum/run_phase3.py evaluate
python llm-sum/run_phase3.py status
```

### "I want to launch the full 250-paper batch"

```powershell
# In your .env: PHASE3_MODE=batch
python scripts/verify_extraction.py                  # confirm no FAILs
python llm-sum/run_phase3.py summarize --estimate    # confirm the projected cost
python llm-sum/run_phase3.py summarize               # submits batch jobs; asks 'yes'

# walk away for up to 24h. then:
python llm-sum/check_batch_status.py                 # collects results
python llm-sum/run_phase3.py evaluate                # blind judge on the collected summaries
python llm-sum/run_phase3.py status                  # final counts
```

Cost: ~$40 of API spend per the budget in the workplan.

---

## Per-script guides

| Script                                                           | One-line purpose                                  |
|------------------------------------------------------------------|---------------------------------------------------|
| [`prepare_texts.md`](prepare_texts.md)                           | PDFs → `data/processed/<stem>.jsonl` (cleaned text cache) |
| [`verify_extraction.md`](verify_extraction.md)                   | Audit raw vs cleaned text quality per paper       |
| [`summarizer.md`](summarizer.md)                                 | Run 3 LLM summarisers; supports batch + real-time |
| [`evaluator.md`](evaluator.md)                                   | Blind judge over `data/summaries.jsonl`           |
| [`check_batch_status.md`](check_batch_status.md)                 | Poll batch jobs and merge results into the JSONL  |
| [`cost_estimator.md`](cost_estimator.md)                         | Offline cost forecast (no API calls)              |
| [`run_phase3.md`](run_phase3.md)                                 | Orchestrator wrapping every step above            |

The migration helper [`scripts/migrate_processed_filenames.py`](../../scripts/migrate_processed_filenames.py) is documented inline; it's a one-shot tool.

---

## File outputs at a glance

```
data/
├── processed/<descriptive_stem>.jsonl   # written by prepare_texts
├── summaries.jsonl                      # written by summarizer (real-time) or check_batch_status (batch)
├── evaluations.jsonl                    # written by evaluator
├── batch_jobs.jsonl                     # batch job ledger (provider job IDs + status)
├── batch/                               # scratch JSONL for batch submissions
├── verify_extraction_report.txt         # written by scripts/verify_extraction.py
└── error_log.jsonl                      # any failed call across the project
```

`data/summaries.jsonl` and `data/evaluations.jsonl` are append-only — never edit them by hand. The summarizer rewrites `summaries.jsonl` atomically once per run, which is the only exception, and even that preserves every existing line.
