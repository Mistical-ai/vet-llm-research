# How to run a dev-mode evaluation (step by step)

**What this does:** lets you try the full summarize → judge pipeline on a *small* random
sample (one paper per journal) so you can eyeball quality before spending on the whole
corpus. Everything is incremental: each run adds new papers and never re-does finished ones.

The two folders this guide revolves around:

| Folder | Written by | Holds |
| --- | --- | --- |
| `data/dev_summaries_jsonl/` | `summarize --mode dev` | readable `.txt` of the summaries you made (one per paper) |
| `data/dev_evals_jsonl/` | `evaluate --mode dev` | readable `.txt` of the judge scores (one per paper) |

> Don't confuse these with `data/dev_tests/` — that's the *separate* `summarize-all`
> PDF-vs-processed-text comparison workflow, unrelated to this loop.

---

## Before you start (one-time checks)

1. Your `.env` has valid API keys and a budget (`BUDGET_USD` / budget guard) large enough for
   a few papers.
2. Processed text exists: `data/processedv2/*.jsonl` is populated. If not, run
   `python llm-sum/run_phase3.py extract` first.
3. `data/manifest.jsonl` has `journal` fields (needed for the one-per-journal pick).

## Step 1 — Summarize a dev sample

In your PowerShell window:

```powershell
python llm-sum/run_phase3.py summarize --mode dev
```

Type `yes` at the safety prompt. This picks one random paper per journal, summarizes each
with all three providers, updates `data/summaries.jsonl`, and writes one readable `.txt` per
paper into `data/dev_summaries_jsonl/`.

## Step 2 — Eyeball the summaries

Open the `.txt` files in `data/dev_summaries_jsonl/`. Each has a section per provider. If they
look reasonable, continue.

## Step 3 — Evaluate that sample

```powershell
python llm-sum/run_phase3.py evaluate --mode dev
```

Type `yes` at the safety prompt. This automatically reads the DOIs sitting in
`data/dev_summaries_jsonl/`, finds their matching articles (reference text from `processedv2`,
summaries from `summaries.jsonl`), runs the blind judge, appends rows to
`data/evaluations.jsonl`, and writes one readable `.txt` per paper into a new
`data/dev_evals_jsonl/` folder.

## Step 4 — Eyeball the scores

Open the `.txt` files in `data/dev_evals_jsonl/`: each shows per-provider scores,
hallucination counts, human-review flags, and the judge's reasoning. For an aggregate view,
run the offline report:

```powershell
python llm-sum/run_phase3.py eval-report --markdown
```

## Step 5 — Grow the sample (optional)

Just run Steps 1 and 3 again. `summarize --mode dev` skips papers already in
`dev_summaries_jsonl/` and picks *new* ones; `evaluate --mode dev` skips papers already in
`dev_evals_jsonl/` and judges only the new ones. Repeat until you've seen enough.

## If you need to redo a paper

- Re-summarize papers already done: `summarize --mode dev --no-skip-existing`.
- Re-judge papers already done: `evaluate --mode dev --no-resume` (this re-spends on the
  judge and rewrites the readable files).
- Or simply delete the specific `.txt` file from the folder and re-run.

## Common gotchas

- **"No journal-mapped papers / nothing to summarize"**: run `extract` first, or check
  `PROCESSED_DIR_NAME` in `.env` points at `processedv2`.
- **"All dev articles already evaluated; nothing to do"**: expected once every summarized
  paper has been judged — run Step 1 again to add more, or use `--no-resume` to re-judge.
- **Want the old behavior back for a dev run?** `--mode dev` now defaults to this
  folder-driven loop and ignores `EVAL_INPUT_MODE`. Pass `--input-mode jsonl` to restore the
  journal-stratified sampling over all of `data/summaries.jsonl`, or `--input-mode dev` for
  the `summarize-all` `data/dev_tests` comparison files.
