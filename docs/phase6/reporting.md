# Phase 6 — Publication reporting

**Role of this doc:** operator how-to *and* methods reference for the
publication tables. For the day-to-day operator summary (per-stratum scores in
the terminal) see [`docs/phase3/eval_report.md`](../phase3/eval_report.md); for
the authoritative rubric and jury math see
[`docs/phase3/medhelm_evaluation.md`](../phase3/medhelm_evaluation.md). This
page covers only the extra, paper-ready layer.

---

## 1. What it produces

One command turns `data/evaluations.jsonl` into the quantitative results a paper
needs:

```powershell
python llm-sum/run_phase3.py eval-report --publication
```

It writes three artifacts to `data/results/` (timestamped so nothing is
overwritten):

| Artifact | What it is |
|---|---|
| `publication_report_<ts>.json` | The full report, every number, machine-readable. |
| `publication_report_<ts>.md` | The same tables, human-readable Markdown for a manuscript or slide. |
| `publication_report_<ts>_tables/` | A folder of CSVs (one per table) for a stats package or spreadsheet. |

The tables:

- **Provider comparison** — each provider's mean jury score in **both modes**
  (unweighted = plain MedHELM average; weighted = this project's
  clinical-risk-weighted average), each with a **95% bootstrap confidence
  interval**, plus mean cost, **cost-per-quality-point**, and mean judge
  disagreement.
- **Significance** — a **Friedman** omnibus test across all providers and
  **pairwise Wilcoxon signed-rank** tests, computed **separately for the
  unweighted and weighted score**, with a bootstrap CI on each pair's mean
  difference.
- **Per-stratum breakdowns** — provider × species / study design / clinical
  topic / journal cross-tabs of the unweighted mean.
- **Processed text vs. direct PDF** — the input-channel comparison, per
  provider, kept separate from the clinical strata.
- **Inter-judge reliability** — a one-line summary (Krippendorff's alpha),
  populated only for a multi-judge jury run (reuses `reliability.py`).

If there are no evaluation rows yet, nothing is written and the command says so.

## 2. Common options

```powershell
# Print the Markdown to the terminal (still saves the files):
python llm-sum/run_phase3.py eval-report --publication --markdown

# Print the full JSON instead:
python llm-sum/run_phase3.py eval-report --publication --json

# Print only, write nothing:
python llm-sum/run_phase3.py eval-report --publication --no-save
```

Direct invocation exposes a few more knobs (bootstrap seed/resamples, batch
pricing, an alternate summaries source for cost):

```powershell
python llm-sum/report_tables.py --markdown --bootstrap-resamples 5000 --cost-batched
```

Environment defaults live in `.env` (`PUBLICATION_BOOTSTRAP_SEED`,
`PUBLICATION_BOOTSTRAP_RESAMPLES`, `PUBLICATION_COST_BATCHED`); a CLI flag always
overrides the env value.

## 3. How to read the numbers

- **The unit of analysis is one item = (paper × input channel).** A paper judged
  from both cleaned text and direct PDF is two items. Within an item, if several
  judges scored the same summary (a jury), their scores are **averaged first**,
  so every provider contributes exactly one score per item.
- **Providers are paired by item.** The significance tests are paired/blocked
  (a paper is its own block), not two-independent-sample tests — the correct
  design for "did provider A beat provider B on the *same* papers?".
- **Unweighted is the headline**, matching stock MedHELM; weighted is reported
  alongside so the safety-oriented score the study actually uses is validated
  too. Both get their own significance tests.
- **A confidence interval that straddles another provider's mean** means the gap
  is not resolved at this sample size — read it together with the Wilcoxon
  p-value, not instead of it.
- **`underpowered (n<10)`** on a pairwise row means fewer than ten shared items
  stood behind that test; the p-value is shown for transparency but is too
  unstable to lean on. Collect more evaluated papers before concluding.

## 4. Cost

Cost is the price to **generate** each summary — the summariser's own token
counts from `data/summaries.jsonl` priced with
[`models_config.py`](../../llm-sum/models_config.py), the same single source of
truth the live pipeline bills against. The **judge's** cost is a separate,
shared overhead and is deliberately **not** attributed to a provider here.

Two caveats surface as honest gaps rather than wrong numbers:

- A summary judged from the `summarize-all` `.txt` folders has no token record
  in `summaries.jsonl`, so its cost is left blank (`-`) instead of guessed.
- A summary slot does not record whether it was produced via the real-time or
  batch API, so cost defaults to **real-time** rates. Pass `--cost-batched`
  (or set `PUBLICATION_COST_BATCHED=true`) if the run used the batch API.

## 5. Statistics

> **New to Wilcoxon, Friedman, or what scipy/numpy even are?** Read the plain-
> language primer first: [`docs/statistics_explained.md`](../statistics_explained.md).
> This section assumes you know what those tests do.

The significance tests and confidence intervals come from **`scipy.stats`** — a
peer-reviewed, citable library — so a paper's numbers are computed by a
validated implementation rather than hand-rolled math. `scipy` and `numpy` are
**pinned in `requirements.txt`**; a pinned, validated library is *more*
scientifically reproducible than bespoke code, because every machine runs the
identical algorithm. (The one statistic that stays hand-rolled is Krippendorff's
alpha in [`reliability.py`](../../llm-sum/reliability.py) — scipy does not
implement it.)

Specifics for a methods section:

- **Wilcoxon signed-rank** — `scipy.stats.wilcoxon`, two-sided, `method="auto"`
  (exact for small samples without ties/zeros, normal approximation otherwise).
  Zero differences are dropped. Exact small-sample p-values are the reason to use
  the library here: a stratified study runs many comparisons at small per-cell
  n, exactly where a normal approximation is weakest.
- **Friedman** — `scipy.stats.friedmanchisquare`, a chi-square with `k-1`
  degrees of freedom, over items every provider scored (complete blocks).
- **Confidence intervals** — `scipy.stats.bootstrap`, percentile method, 2000
  resamples by default, seeded (`numpy` `default_rng`) for exact reproducibility.

## 6. Guardrails

This command is **read-only and offline** — it never calls a provider API and
never writes to `evaluations.jsonl` (only to `data/results/`). It is safe for
the AI assistant to run under `PHASE3_MODE=test`. It consumes whatever is in
`data/evaluations.jsonl`, so to populate real tables you first run a real
evaluation manually in PowerShell (see
[`docs/phase3/run_phase3.md`](../phase3/run_phase3.md)); a jury run
(`--jury`) additionally fills the reliability line.

## 7. Figures + the VetHELM-style leaderboard

The tables above are for a methods section; this command is for a slide or a
results-page hero:

```powershell
python llm-sum/run_phase3.py report-figures
```

It reuses the same publication report as `eval-report --publication` (same
bootstrap seed/resamples, same cost basis) and writes two things to
`data/results/`, timestamped like every other Phase 6 artifact:

| Artifact | What it is |
|---|---|
| `leaderboard_<ts>.json` | The named, taxonomy-keyed leaderboard, machine-readable. |
| `leaderboard_<ts>.md` | The same leaderboard as a Markdown table. |
| `figures_<ts>/*.png` + `*.svg` | Four presentation figures (below). |

**The leaderboard.** One row per provider, ranked by unweighted quality (best
first), each column pulled from the modules that already compute it —
nothing here is a new statistic:

- **Quality** — unweighted and weighted jury score, each with its 95%
  bootstrap CI (same numbers as `eval-report --publication`'s provider
  comparison table).
- **Cost** and **cost-per-quality-point** — same cost basis as the
  publication tables (`--cost-batched` / `PUBLICATION_COST_BATCHED` applies
  here too).
- **Reliability** — Krippendorff's alpha, but scoped to *that provider's*
  summaries only (the evaluation rows are filtered to one summarizer before
  calling `reliability.compute_reliability`), answering "do the judges agree
  with each other about THIS provider specifically?" A separate,
  all-providers-pooled alpha is reported once under "Overall inter-judge
  reliability." Both need a jury run (`--jury` / `JUDGE_MODELS` with 2+
  providers) — with the default single judge, every reliability cell reads
  "not available," not zero.
- **Human validation** (`r_s`, Spearman) — per-provider correlation between
  the LLM jury and human reviewers, read from `data/human_reviews.jsonl`
  (Phase 5) if it exists. Missing file or a provider with no reviewed items →
  "not available," not an error. Because a typical human-review sample (e.g.
  15 items) is split across three providers, per-provider `n` is usually
  small — the coefficient is shown for transparency but the same
  underpowered-sample caveat from `reliability.MIN_CORRELATION_N` applies
  (see [`docs/phase5/human_validation.md`](../phase5/human_validation.md)).

**The figures**, one per PNG/SVG pair:

1. **`provider_comparison`** — grouped bars, unweighted + weighted jury score
   with 95% CI error bars, one color per provider (openai/anthropic/gemini
   keep the same hue across every figure in this folder).
2. **`cost_quality`** — a cost-vs-quality scatter, one labeled point per
   provider, skipped when no provider has a priced summary (same cost-source
   caveats as the publication tables).
3. **`criterion_heatmap`** — provider x criterion (faithfulness / completeness
   / clinical_usefulness / clarity / safety) mean score, one hue (sequential
   blue), every cell also value-labeled since color alone can't carry
   2-decimal precision.
4. **`reliability`** — Krippendorff's alpha per criterion + overall, only
   emitted for a jury run (2+ judges); skipped entirely for the default
   single-judge run.

Common options:

```powershell
# Leaderboard only, skip the PNG/SVG figures:
python llm-sum/run_phase3.py report-figures --no-figures

# Only PNG (skip the SVG copy):
python llm-sum/run_phase3.py report-figures --formats png
```

Guardrails are identical to `eval-report --publication`: read-only, offline,
safe under `PHASE3_MODE=test`. Figures render via matplotlib's headless
"Agg" backend, so no display server is required, including inside the AI
assistant's sandboxed shell.
