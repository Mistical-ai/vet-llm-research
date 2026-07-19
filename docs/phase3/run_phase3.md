# run_phase3.py

## Primary command — six summaries (PDF + processed JSONL)

**Use `summarize-all` (hyphenated), not `summarize all`.**

```powershell
python llm-sum/run_phase3.py summarize-all --mode single
```

Prerequisite: run `extract` first so `data/processed/` exists.

| What happens | Detail |
|--------------|--------|
| Matching | Finds article stems present in both `data/raw/*.pdf` and `data/processed/*.jsonl` |
| Default in `single` / `dev` | 1 matched pair → **6 summaries** (3 providers × 2 source types) |
| PDF summaries (`single`/`test`) | `data/summaries_pdf/<stem>.txt` — OpenAI, Anthropic, Gemini on the raw PDF |
| JSONL summaries (`single`/`test`) | `data/summaries_txt/<stem>.txt` — same 3 providers on processed text |
| Dev mode | Same 1-matched-pair default, but outputs go to `data/dev_tests/summaries_pdf/<stem>.txt` and `data/dev_tests/summaries_txt/<stem>.txt` instead: `python llm-sum/run_phase3.py summarize-all --mode dev` |
| Free mock | `python llm-sum/run_phase3.py summarize-all --mode test` |
| More articles | Add `--limit N` for N matched pairs (N × 6 summaries) |

Type `yes` at the confirmation prompt for paid modes (`single`, `dev`).

---

## What it does

The Phase 3 orchestrator CLI. One entry point with thirteen subcommands; every subcommand prints the active `PHASE3_MODE` banner before doing anything so you cannot run a paid call without seeing the mode first.

```
run_phase3.py extract                     → prepare_texts.main()
run_phase3.py summarize                   → summarizer.main()  (or --estimate → cost_estimator)
run_phase3.py summarize-all               → paired PDF + processed JSONL comparison (readable .txt files)
run_phase3.py evaluate                    → evaluator.main()
run_phase3.py eval-report                 → eval_report.main()  (read-only; saves to data/results/)
run_phase3.py report-figures              → report_figures.main()  (Phase 6 charts and leaderboard)
run_phase3.py stats-engine                → stats_engine.main()  (Phase 6 significance tests)
run_phase3.py export-human-review         → human_review.main()  (Phase 5 blind review packets)
run_phase3.py ingest-human-review         → human_review.ingest_main()  (Phase 5 load vet scoresheets)
run_phase3.py export-pilot-human-review   → pilot_human_review.main()  (Phase 5 dev-pool trial run, see pilot_human_review.md)
run_phase3.py ingest-pilot-human-review   → pilot_human_review.ingest_main()  (Phase 5 load pilot scoresheets into data/pilot_human_reviews.jsonl, see pilot_human_review.md)
run_phase3.py status                      → reads jsonl files, prints counts
run_phase3.py clean                       → deletes data/batch/*.jsonl scratch files
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

**Also important:** for `evaluate` in its default `jsonl` input mode, the "Default paper limit" above (and any `--limit` override) is a **per-journal quota**, not a flat total — `_sample_by_journal` draws that many papers from *each* journal represented in `data/summaries.jsonl`. So `evaluate --mode single` with no `--limit` judges 1 paper × (number of journals present), not 1 paper total, and `evaluate --mode dev` judges `PHASE3_DEV_LIMIT` papers × (number of journals). This quota behavior is specific to `evaluate`'s default `jsonl` mode; `evaluate --mode test` and `summarize`'s paper limit remain flat caps.

---

## Full command reference

### Help

```powershell
python llm-sum/run_phase3.py --help
python llm-sum/run_phase3.py extract --help
python llm-sum/run_phase3.py summarize --help
python llm-sum/run_phase3.py summarize-all --help
python llm-sum/run_phase3.py evaluate --help
python llm-sum/run_phase3.py eval-report --help
python llm-sum/run_phase3.py status --help
```

### `extract` (always free — no API calls)

Build or refresh `data/raw_text/*.jsonl` and `data/processed/*.jsonl` (folder name configurable via `PROCESSED_DIR_NAME`; ships as `processedv2` by default) from PDFs in `data/raw/`.

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

# Paid: one paper × 3 providers → 3 summaries in summaries.jsonl,
# plus readable data/single_summaries_jsonl/*.txt and its prose/ sibling.
# (A fully-resumed run touches no paper and writes nothing there; it says so.)
python llm-sum/run_phase3.py summarize --mode single

# Paid: small run (default 5 papers × 3 providers)
# → summaries.jsonl + data/dev_summaries_jsonl/*.txt + prose/
python llm-sum/run_phase3.py summarize --mode dev
python llm-sum/run_phase3.py summarize --mode dev --limit 3

# Paid: full corpus via batch API. Readable files appear later, when
# check_batch_status.py merges each provider's results back, in
# data/batch_summaries_jsonl/ (+ prose/).
python llm-sum/run_phase3.py summarize --mode batch

# Resume / force
python llm-sum/run_phase3.py summarize --mode dev --resume
python llm-sum/run_phase3.py summarize --mode single --force

# Dev mode skips papers already in data/dev_summaries_jsonl/ (incremental sampling);
# --no-skip-existing reconsiders them
python llm-sum/run_phase3.py summarize --mode dev --no-skip-existing

# Write this run's readable output into its own pool instead of the top level.
# The subfolder tracks its OWN "already done" set, so a paper already summarized
# at the top level can be re-picked here -- and re-paid for.
python llm-sum/run_phase3.py summarize --mode dev --output-subdir promptv2
# NOTE: `evaluate --mode dev` and the pilot review sampler read only the TOP
# LEVEL of data/dev_summaries_jsonl/, so a subfolder pool is invisible to them
# until you point them at it (both warn when they spot one):
python llm-sum/run_phase3.py evaluate --mode dev --source-dir data/dev_summaries_jsonl/promptv2

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
| `--no-skip-existing` | Dev mode only: reconsider papers already in the active output folder — the top level, or the `--output-subdir` folder if one is given (default: skip them so dev sampling is incremental) |
| `--output-subdir NAME` | Dev mode only: write readable output to `data/dev_summaries_jsonl/NAME/` and track skip-existing against that subfolder alone. Judge it with `evaluate --source-dir data/dev_summaries_jsonl/NAME` |
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

Outputs are readable text files (not `summaries.jsonl`). `single`/`test` write
to the top-level folders; `dev` defaults to `data/dev_tests/` instead so paid
dev experiments stay separate from the main comparison output:

```text
data/summaries_pdf/<matched-article-stem>.txt              ← single / test, 3 provider sections
data/summaries_txt/<matched-article-stem>.txt               ← single / test, 3 provider sections

data/dev_tests/summaries_pdf/<matched-article-stem>.txt     ← dev, 3 provider sections
data/dev_tests/summaries_txt/<matched-article-stem>.txt     ← dev, 3 provider sections
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
| `--output-set {auto,regular,dev-tests}` | Where readable `.txt` outputs are written. `auto` (default, or `SUMMARIZE_ALL_OUTPUT_SET` from `.env`) sends `dev` mode to `data/dev_tests/` and other modes to the original `data/summaries_pdf`/`data/summaries_txt` folders; `regular` always uses the original top-level folders; `dev-tests` always uses `data/dev_tests/`, regardless of mode. |

**Not supported:** `--mode batch` (direct PDF summarisation is real-time only).

**Prerequisite:** Run `extract` first so `data/processed/` exists, and ensure at least one PDF stem appears in both `data/raw/` and `data/processed/`.

### `evaluate` (paid unless `--mode test`)

Blind judge over `data/summaries.jsonl`. Writes to `data/evaluations.jsonl`.

`--mode dev` is special: it runs the **folder-driven dev loop** — it reads the DOIs already
in the **top level** of `data/dev_summaries_jsonl/` (written by `summarize --mode dev`), judges
their matching articles, and mirrors the scores into `data/dev_evals_jsonl/`, skipping papers
already evaluated there. See [dev_evaluation_guide.md](dev_evaluation_guide.md) for the full loop.

The folder scan is deliberately **non-recursive**: `prose/` holds the same papers in their
flowing-prose form and must never be counted twice. The side effect is that an
`--output-subdir` pool, `data/single_summaries_jsonl/`, and `data/batch_summaries_jsonl/`
are invisible to the default scan — use `--source-dir` to judge those. The command prints a
warning when it notices summaries filed in a subfolder it isn't reading.

Whichever folder seeds the run, the judge always scores the **prose `summary_text`** pulled
from `data/summaries.jsonl` — the readable folder only decides *which* papers to judge.

```powershell
python llm-sum/run_phase3.py evaluate --mode test
python llm-sum/run_phase3.py evaluate --mode single
python llm-sum/run_phase3.py evaluate --mode dev          # folder-driven dev loop
python llm-sum/run_phase3.py evaluate --mode dev --limit 3
python llm-sum/run_phase3.py evaluate --mode batch
python llm-sum/run_phase3.py evaluate --mode dev --judges openai,anthropic
python llm-sum/run_phase3.py evaluate --mode dev --jury
python llm-sum/run_phase3.py evaluate --mode dev --no-resume   # re-judge papers already done
python llm-sum/run_phase3.py evaluate --mode dev --force

# Judge a different readable folder (results still go to data/dev_evals_jsonl/):
python llm-sum/run_phase3.py evaluate --mode dev --source-dir data/dev_summaries_jsonl/promptv2
python llm-sum/run_phase3.py evaluate --mode dev --source-dir data/single_summaries_jsonl
python llm-sum/run_phase3.py evaluate --mode dev --source-dir data/batch_summaries_jsonl

# Override the dev-mode default for a single run:
python llm-sum/run_phase3.py evaluate --mode dev --input-mode jsonl   # journal-stratified summaries.jsonl
python llm-sum/run_phase3.py evaluate --mode dev --input-mode dev     # summarize-all dev_tests comparison
python llm-sum/run_phase3.py evaluate --mode batch --input-mode regular
```

| Flag | Purpose |
|------|---------|
| `--limit N` | Override mode's paper limit. In the default `jsonl` input mode this is a **per-journal quota** — N papers are judged from *each* journal in `data/summaries.jsonl`, so total judged = N × number of journals present (see the "Also important" note under **Modes** above). |
| `--judges` | Comma-separated judge provider keys. Overrides `--jury` and `JURY_PRESET`. Default judges: the full 3-judge panel `openai,anthropic,gemini`. |
| `--jury` | Full 3-judge panel (`openai,anthropic,gemini`); same as `JURY_PRESET=panel`. Presets: `panel` (3, default) / `duo` (2: `openai,anthropic`) / `solo` (1: `openai`). |
| `--input-mode` | `jsonl` (default), `dev-jsonl`, `dev`, `regular`, or `auto` — where to read summaries from. `--mode dev` defaults to `dev-jsonl` regardless of `EVAL_INPUT_MODE`. See [evaluator.md](evaluator.md#choosing-the-input-source-run_phase3py-evaluate). |
| `--source-dir PATH` | dev-jsonl mode only: seed the run from this readable-summary folder instead of `data/dev_summaries_jsonl/`. Use for an `--output-subdir` pool, `data/single_summaries_jsonl/`, or `data/batch_summaries_jsonl/`. Results still go to `data/dev_evals_jsonl/`, and the folder actually used is recorded in the run manifest. Judge-side twin of `pilot_human_review --dev-summaries-dir`. |
| `--no-resume` | Re-evaluate pairs already in `evaluations.jsonl`; in dev-jsonl mode also re-includes papers already in `data/dev_evals_jsonl/` |
| `--force` | Bypass confirmation |

### `eval-report` (always free, read-only)

Summarizes `data/evaluations.jsonl` by summariser and clinical strata, plus
inter-judge reliability when a jury scored the data. See
[eval_report.md](eval_report.md) for the full flag reference.

```powershell
python llm-sum/run_phase3.py eval-report
python llm-sum/run_phase3.py eval-report --json
python llm-sum/run_phase3.py eval-report --no-save
```

| Flag | Purpose |
|------|---------|
| `--evaluations PATH` | Evaluation rows to summarize (default: `data/evaluations.jsonl`) |
| `--json` | Print the full report as JSON |
| `--results-dir PATH` | Where to save the report snapshot (default: `data/results/`) |
| `--no-save` | Print only; skip writing to `data/results/` |

### `report-figures` (always free, read-only)

Renders Phase 6 publication figures (PNG/SVG) and exports a VetHELM-style
leaderboard (JSON + Markdown) from `data/evaluations.jsonl`. See
[`docs/phase6/reporting.md`](../phase6/reporting.md) for the full flag
reference and figure catalog.

### `stats-engine` (always free, read-only)

Phase 6 significance and secondary-analysis suite: information density,
Cohen's Kappa inter-rater reliability, subscription economics, and
provider × covariate (species/study_design/journal) tables. See
[`docs/phase6/reporting.md`](../phase6/reporting.md) for the full flag
reference.

### `export-human-review` (always free, read-only)

Exports one more blind human-validation reviewer folder (`humanN/`) from
`data/evaluations.jsonl` for the real study ledger. See
[`docs/phase5/human_validation.md`](../phase5/human_validation.md) for the
full workflow and flag reference.

### `ingest-human-review` (always free, read-only)

Ingests filled reviewer scoresheets into `data/human_reviews.jsonl`, the
real study ledger. Refuses to run against a pilot export folder. See
[`docs/phase5/human_validation.md`](../phase5/human_validation.md) for the
full workflow and flag reference.

### `export-pilot-human-review` (always free, read-only)

Exports one more pilot reviewer folder (`humanN/`) from the dev-summary
pool, to trial the human-validation workflow before the real export. See
[`docs/phase5/pilot_human_review.md`](../phase5/pilot_human_review.md) for
the full workflow and flag reference.

### `ingest-pilot-human-review` (always free, read-only)

Ingests filled pilot reviewer scoresheets into `data/pilot_human_reviews.jsonl`
— never the real study ledger. See
[`docs/phase5/pilot_human_review.md`](../phase5/pilot_human_review.md) for
the full workflow and flag reference.

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
python llm-sum/run_phase3.py eval-report
python llm-sum/run_phase3.py status
```

### One paper, three summaries (`summaries.jsonl`)

```powershell
python llm-sum/run_phase3.py summarize --mode single
python llm-sum/run_phase3.py evaluate --mode single
python llm-sum/run_phase3.py eval-report
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
python llm-sum/run_phase3.py eval-report
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
python llm-sum/run_phase3.py eval-report
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
python llm-sum/run_phase3.py eval-report          # snapshot saved to data/results/
python llm-sum/run_phase3.py status               # final counts
python llm-sum/run_phase3.py clean                # tidy up
```
