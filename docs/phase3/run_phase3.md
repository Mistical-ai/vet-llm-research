# run_phase3.py

## What it does

The Phase 3 orchestrator CLI. One entry point with six subcommands; every subcommand prints the active `PHASE3_MODE` banner before doing anything so you cannot run a paid call without seeing the mode first.

```
run_phase3.py extract       → prepare_texts.main()
run_phase3.py summarize     → summarizer.main()  (or --estimate → cost_estimator)
run_phase3.py summarize-all → paired PDF + processed JSONL comparison (readable .txt files)
run_phase3.py evaluate      → evaluator.main()
run_phase3.py status        → reads jsonl files, prints counts
run_phase3.py clean         → deletes data/batch/*.jsonl scratch files
```

Calling `run_phase3.py <cmd>` and calling the underlying script directly do exactly the same thing. The orchestrator is just for the muscle-memory of "I want to do Phase 3 stuff" without remembering which file each step lives in.

## When to run it

Every time you want to make Phase 3 progress. Read `docs/phase3/README.md`'s "Three named recipes" — they're all written against this script.

## Inputs

Whatever the delegated subcommand needs. See the per-script docs.

## Outputs

Whatever the delegated subcommand writes. See the per-script docs.

---

## Modes (`PHASE3_MODE` or `--mode`)

Set in `.env` or override per command with `--mode {test,single,dev,batch}`.

| Mode     | API calls | Default paper limit | Batch API | Confirm prompt |
|----------|-----------|---------------------|-----------|----------------|
| `test`   | Mock only | all                 | No        | No             |
| `single` | Real      | 1                   | No        | Yes (`yes`)    |
| `dev`    | Real      | `PHASE3_DEV_LIMIT` (default 5) | No | Yes (`yes`)    |
| `batch`  | Real      | all                 | Yes (50% off) | Yes (`yes`) |

**Important:** `summarize-all` uses different defaults than `summarize` in `single` and `dev`. For `summarize-all`, both `single` and `dev` default to **one matched article pair** (not `PHASE3_DEV_LIMIT`), which produces **six summaries** — see below.

---

## Full command reference

### Help

```powershell
python llm-sum/run_phase3.py --help
python llm-sum/run_phase3.py extract --help
python llm-sum/run_phase3.py summarize --help
python llm-sum/run_phase3.py summarize-all --help
python llm-sum/run_phase3.py evaluate --help
python llm-sum/run_phase3.py status --help
```

### `extract` (always free — no API calls)

Build or refresh `data/raw_text/*.jsonl` and `data/processed/*.jsonl` from PDFs in `data/raw/`.

```powershell
python llm-sum/run_phase3.py extract
python llm-sum/run_phase3.py extract --limit 10
python llm-sum/run_phase3.py extract --manifest data/manifest.jsonl
```

| Flag | Purpose |
|------|---------|
| `--manifest PATH` | Manifest JSONL (default: `data/manifest.jsonl`) |
| `--limit N` | Process only N PDFs |
| `--mode` | Prints banner only; extract never calls APIs |

### `summarize` (paid unless `--mode test` or `--estimate`)

Run OpenAI, Anthropic, and Gemini summarisers. Writes to `data/summaries.jsonl`.

```powershell
# Free: offline cost forecast (processed text only)
python llm-sum/run_phase3.py summarize --estimate
python llm-sum/run_phase3.py summarize --estimate --mode batch

# Free: full mock run
python llm-sum/run_phase3.py summarize --mode test

# Paid: one paper × 3 providers → 3 summaries in summaries.jsonl
python llm-sum/run_phase3.py summarize --mode single

# Paid: small run (default 5 papers × 3 providers)
python llm-sum/run_phase3.py summarize --mode dev
python llm-sum/run_phase3.py summarize --mode dev --limit 3

# Paid: full corpus via batch API
python llm-sum/run_phase3.py summarize --mode batch

# Resume / force
python llm-sum/run_phase3.py summarize --mode dev --resume
python llm-sum/run_phase3.py summarize --mode single --force

# Input source (default: processed)
python llm-sum/run_phase3.py summarize --mode single --input-source processed
python llm-sum/run_phase3.py summarize --mode single --input-source raw_text
python llm-sum/run_phase3.py summarize --mode single --input-source pdf

# Subset of providers
python llm-sum/run_phase3.py summarize --mode single --providers openai,anthropic

# Optional format guide (section style only — facts must come from the paper)
python llm-sum/run_phase3.py summarize --mode single --guide-summary llm-sum/prompts/guide_summary_template.txt
```

| Flag | Purpose |
|------|---------|
| `--estimate` | Print projected cost via tiktoken; no API calls. Not available for `--input-source pdf`. |
| `--limit N` | Override mode's paper limit |
| `--resume` | Skip (doi, model) pairs already at `status=success` |
| `--force` | Bypass interactive `yes` confirmation |
| `--providers` | Comma-separated subset (default: all three) |
| `--manifest PATH` | Manifest JSONL |
| `--input-source` | `processed` (default), `raw_text`, or `pdf` |
| `--guide-summary PATH` | Human-written format guide file |

Direct PDF input (`--input-source pdf`) is only allowed in `test` and `single` — not `dev` or `batch`.

### `summarize-all` — six summaries in `single` and `dev`

The manual PDF-vs-processed comparison workflow. Matches articles by shared filename stem (title + DOI slug) between `data/raw/*.pdf` and `data/processed/*.jsonl`, then summarises each matched pair with all three providers.

**In `--mode single` and `--mode dev`, the default is one matched pair → six summaries total:**

| Source | Providers | Summaries |
|--------|-----------|-----------|
| Raw PDF (`data/raw/`) | OpenAI, Anthropic, Gemini | 3 |
| Processed JSONL (`data/processed/`) | OpenAI, Anthropic, Gemini | 3 |
| **Total** | | **6** |

Outputs are readable text files (not `summaries.jsonl`):

```text
data/summaries_pdf/<matched-article-stem>.txt   ← 3 provider sections
data/summaries_txt/<matched-article-stem>.txt   ← 3 provider sections
```

```powershell
# Paid: 1 matched article → 6 summaries (type 'yes' at prompt)
python llm-sum/run_phase3.py summarize-all --mode single
python llm-sum/run_phase3.py summarize-all --mode dev

# Same six-summary default in test mode (mock, $0)
python llm-sum/run_phase3.py summarize-all --mode test

# More matched pairs: N articles × 2 sources × 3 providers = N×6 summaries
python llm-sum/run_phase3.py summarize-all --mode single --limit 3
python llm-sum/run_phase3.py summarize-all --mode dev --limit 2

# Resume / force / provider subset
python llm-sum/run_phase3.py summarize-all --mode single --resume
python llm-sum/run_phase3.py summarize-all --mode single --force
python llm-sum/run_phase3.py summarize-all --mode single --providers openai,gemini
```

| Flag | Purpose |
|------|---------|
| `--limit N` | Number of matched article pairs. Default: **1** in `test`/`single`/`dev`; full corpus in `batch` (but batch is not supported — use `single` or `dev`) |
| `--resume` | Skip provider slots already at `status=success` |
| `--force` | Bypass interactive confirmation |
| `--providers` | Comma-separated subset |

**Not supported:** `--mode batch` (direct PDF summarisation is real-time only).

**Prerequisite:** Run `extract` first so `data/processed/` exists, and ensure at least one PDF stem appears in both `data/raw/` and `data/processed/`.

### `evaluate` (paid unless `--mode test`)

Blind judge over `data/summaries.jsonl`. Writes to `data/evaluations.jsonl`.

```powershell
python llm-sum/run_phase3.py evaluate --mode test
python llm-sum/run_phase3.py evaluate --mode single
python llm-sum/run_phase3.py evaluate --mode dev
python llm-sum/run_phase3.py evaluate --mode dev --limit 3
python llm-sum/run_phase3.py evaluate --mode batch
python llm-sum/run_phase3.py evaluate --mode dev --judges openai,anthropic
python llm-sum/run_phase3.py evaluate --mode dev --no-resume
python llm-sum/run_phase3.py evaluate --mode dev --force
```

| Flag | Purpose |
|------|---------|
| `--limit N` | Override mode's paper limit |
| `--judges` | Comma-separated judge provider keys |
| `--no-resume` | Re-evaluate pairs already in `evaluations.jsonl` |
| `--force` | Bypass confirmation |

### `status` (always free, read-only)

```powershell
python llm-sum/run_phase3.py status
python llm-sum/run_phase3.py status --mode dev
```

Prints counts for `data/raw_text/`, `data/processed/`, per-provider success/failed/pending in `summaries.jsonl`, and evaluation rows.

### `clean` (always free)

```powershell
python llm-sum/run_phase3.py clean
```

Removes `data/batch/*.jsonl` scratch files. Does **not** touch `summaries.jsonl`, `evaluations.jsonl`, or `batch_jobs.jsonl`.

---

## Quick recipes

### Free first run (test entire pipeline)

```powershell
python llm-sum/run_phase3.py extract
python scripts/verify_extraction.py
python llm-sum/run_phase3.py summarize --mode test
python llm-sum/run_phase3.py evaluate --mode test
python llm-sum/run_phase3.py status
```

### One paper, three summaries (`summaries.jsonl`)

```powershell
python llm-sum/run_phase3.py summarize --mode single
python llm-sum/run_phase3.py evaluate --mode single
```

### Same article: PDF vs processed — six summaries (readable `.txt` files)

```powershell
python llm-sum/run_phase3.py summarize-all --mode single
# identical default in dev:
python llm-sum/run_phase3.py summarize-all --mode dev
```

### Small real run (5 papers default)

```powershell
python llm-sum/run_phase3.py summarize --estimate --mode dev
python llm-sum/run_phase3.py summarize --mode dev
python llm-sum/run_phase3.py evaluate --mode dev
python llm-sum/run_phase3.py status
```

### Full batch run (~250 papers)

```powershell
python llm-sum/run_phase3.py extract
python scripts/verify_extraction.py
python llm-sum/run_phase3.py summarize --estimate --mode batch
python llm-sum/run_phase3.py summarize --mode batch

# Hours later:
python llm-sum/check_batch_status.py
python llm-sum/run_phase3.py evaluate --mode batch
python llm-sum/run_phase3.py status
python llm-sum/run_phase3.py clean
```

---

## The mode banner

Printed by every subcommand on entry. Example:

```
[phase3] mode=dev | limit=5 | real-time | confirm-required
```

If you don't see this banner, the command didn't actually start. If you see the wrong mode, abort and fix `.env` or pass `--mode`.

## `status` output example

```powershell
python llm-sum/run_phase3.py status
```

```
[phase3] mode=test | DRY_RUN
[phase3:status] data/raw_text/*.jsonl  : 247 files
[phase3:status] data/processed/*.jsonl : 247 files
[phase3:status] data/summaries.jsonl  : 247 papers
        openai: success=247 failed=0 pending=0
     anthropic: success=247 failed=0 pending=0
        gemini: success=247 failed=0 pending=0
[phase3:status] data/evaluations.jsonl: 741 rows (3 flagged for human review)
```

## Worked example: end-to-end batch run

```powershell
$env:PHASE3_MODE = "batch"

python llm-sum/run_phase3.py extract              # ~1 minute
python scripts/verify_extraction.py               # confirm no FAILs
python llm-sum/run_phase3.py summarize --estimate   # confirm projected cost
python llm-sum/run_phase3.py summarize            # type 'yes'; submits batch jobs

# Hours later (or next day):
python llm-sum/check_batch_status.py              # collect results
python llm-sum/run_phase3.py evaluate             # judge the collected summaries
python llm-sum/run_phase3.py status               # final counts
python llm-sum/run_phase3.py clean                # tidy up
```
