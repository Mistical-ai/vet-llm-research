# How to run a dev-mode evaluation (step by step)

**What this does:** lets you try the full summarize → judge pipeline on a *small* random
sample (one paper per journal) so you can eyeball quality before spending on the whole
corpus. Everything is incremental: each run adds new papers and never re-does finished ones.

The folders this guide revolves around:

| Folder | Written by | Holds |
| --- | --- | --- |
| `data/dev_summaries_jsonl/` | `summarize --mode dev` | readable `.txt` of the summaries you made (one per paper) — the **structured bullets** view |
| `data/dev_summaries_jsonl/prose/` | the same `summarize --mode dev` run | the same papers as **flowing prose** (`summary_text`) with a word count and an `[OVER 400]` flag — this is the text the judge actually scores |
| `data/dev_evals_jsonl/` | `evaluate --mode dev` | readable `.txt` of the judge scores (one per paper) -- quick glance: aggregate score, hallucination count, judge reasoning |
| `data/dev_detailEval_reports/` | `evaluate --mode dev` | readable `.md` deep-dive per paper -- title, every judge's own per-criterion score + reasoning, hallucination claim detail, automatic metrics, cross-judge agreement stats |

> Don't confuse these with `data/dev_tests/` — that's the *separate* `summarize-all`
> PDF-vs-processed-text comparison workflow, unrelated to this loop.

---

## Before you start (one-time checks)

1. Your `.env` has valid API keys and a budget (`BUDGET_HARD_STOP` / budget guard) large enough
   for a few papers.
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

Each paper is written twice, and which file you open depends on what you're checking:

- **`data/dev_summaries_jsonl/prose/*.txt`** — open these first. They hold the flowing
  `summary_text`, which is exactly what the blind judge scores in Step 3 and what human
  reviewers grade in Phase 5. Each provider section shows a `Summary (prose): N words`
  line, flagged `[OVER 400]` if it breaks the ceiling. The 300-340-word budget applies
  here and nowhere else.
- **`data/dev_summaries_jsonl/*.txt`** (top level) — the structured bullet view
  (`key_methods` / `key_findings` / `limitations`). Useful for spotting missing fields,
  but it has *no* word budget, only item-count caps, so don't judge length from it.
  It also shows at most 4/5/4 items; `data/summaries.jsonl` keeps the full lists.

Each file has a section per provider. If they look reasonable, continue.

## Step 3 — Evaluate that sample

```powershell
python llm-sum/run_phase3.py evaluate --mode dev
```

Type `yes` at the safety prompt. This automatically reads the DOIs sitting in
`data/dev_summaries_jsonl/`, finds their matching articles (reference text from `processedv2`,
summaries from `summaries.jsonl`), runs the blind judge, appends rows to
`data/evaluations.jsonl`, and writes one readable file per paper into **both**
`data/dev_evals_jsonl/` (quick-glance `.txt`) and `data/dev_detailEval_reports/`
(deep-dive `.md`).

**Capped and journal-balanced per run:** if more papers are pending (summarized but not yet
judged) than `PHASE3_DEV_LIMIT` (default 5, or `--limit N`), this run judges only that many —
picked evenly across journals, one per journal per round — and prints a "pending pool …
sampled … balanced across N journal(s)" table showing exactly which DOIs were picked and how
many were deferred. Deferred papers aren't lost; they're simply still pending, so the next
`evaluate --mode dev` run reconsiders them (see Step 5). `--no-resume` ignores this cap and
always re-judges every paper in `data/dev_summaries_jsonl/`.

## Step 4 — Eyeball the scores

- `data/dev_evals_jsonl/*.txt` for a quick glance: per-provider aggregate scores,
  hallucination counts, human-review flags, and the judge's overall reasoning.
- `data/dev_detailEval_reports/*.md` when you want the full picture for one paper: the
  article title (so you can match it back to the paper without decoding a DOI), every
  judge's own score *and* reasoning for each of the five criteria (factual accuracy,
  completeness, practical usefulness, clarity, safety), the individual hallucination
  claims (not just a count), automatic text metrics, and how much the judges agreed with
  each other on this paper. Open it in a Markdown preview -- headings and tables, not a
  flat text dump. **New to these files?**
  [reading_detail_eval_reports.md](reading_detail_eval_reports.md) walks through one
  real example top to bottom, explains what every number means, and shows how to set up
  the Markdown preview in VS Code.

For an aggregate view across every judged paper, run the offline report:

```powershell
python llm-sum/run_phase3.py eval-report --markdown
```

## Step 5 — Grow the sample (optional)

Just run Steps 1 and 3 again. `summarize --mode dev` skips papers already in the top level of
`dev_summaries_jsonl/` and picks *new* ones; `evaluate --mode dev` skips papers already in
`dev_evals_jsonl/` and judges up to `PHASE3_DEV_LIMIT` of the rest, journal-balanced (and this
always keeps `dev_detailEval_reports/` in sync with `dev_evals_jsonl/` -- same DOIs, written
together). If Step 3 deferred some papers because the pending pool was bigger than the cap,
running it again picks up wherever it left off — no need to track which ones by hand. Repeat
until you've seen enough.

## Trying a different prompt side-by-side

`summarize --mode dev --output-subdir NAME` writes into
`data/dev_summaries_jsonl/NAME/` and keeps its own independent "already done" pool, so a
paper already summarized at the top level can be re-picked (and re-paid for) there.

**This loop's Step 3 will not see those papers.** The folder readers glob `*.txt`
non-recursively — that's what keeps `prose/` from being counted twice — so a subfolder
pool is invisible until you point the judge at it:

```powershell
python llm-sum/run_phase3.py evaluate --mode dev --source-dir data/dev_summaries_jsonl/NAME
```

The same applies to `data/single_summaries_jsonl/` and `data/batch_summaries_jsonl/`.
Both `evaluate` and the pilot review sampler warn when they notice a subfolder holding
summaries they aren't reading.

## If you need to redo a paper

- Re-summarize papers already done: `summarize --mode dev --no-skip-existing`.
- Re-judge papers already done: `evaluate --mode dev --no-resume` (this re-spends on the
  judge and rewrites both readable folders).
- Or simply delete the specific file(s) from `dev_evals_jsonl/` and/or
  `dev_detailEval_reports/` and re-run.

## Common gotchas

- **"No journal-mapped papers / nothing to summarize"**: run `extract` first, or check
  `PROCESSED_DIR_NAME` in `.env` points at `processedv2`.
- **"All dev articles already evaluated; nothing to do"**: expected once every summarized
  paper has been judged — run Step 1 again to add more, or use `--no-resume` to re-judge.
- **Want the old behavior back for a dev run?** `--mode dev` now defaults to this
  folder-driven loop and ignores `EVAL_INPUT_MODE`. Pass `--input-mode jsonl` to restore the
  journal-stratified sampling over all of `data/summaries.jsonl`, or `--input-mode dev` for
  the `summarize-all` `data/dev_tests` comparison files.
