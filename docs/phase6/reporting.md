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
  interval**, plus mean cost, **cost-per-quality-point**, a **subscription
  cost-per-quality-point** (§5), and mean judge disagreement.
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
- **Information density** (§6) — word count + TF-IDF/cosine similarity of
  each summary against the paper's own abstract, re-running Appleby et al.
  (2023)'s benchmark against this study's providers.
- **Covariate analysis** (§7) — one table per (provider × species /
  study_design / journal) cell, with hallucination rate, quality, **and**
  Cohen's Kappa (LLM judge vs. human) together — the "research meat" a
  stratified veterinary study needs, not just overall averages.

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

Sections 5-7 below (subscription economics, information density, covariate
analysis) are also available as their own standalone command, independent of
the rest of the publication report:

```powershell
python llm-sum/run_phase3.py stats-engine --markdown
```

which accepts its own `--subscription-price-usd` / `--papers-per-month`
overrides (see §5) alongside the same `--evaluations` / `--summaries` /
`--human-reviews` / `--json` / `--markdown` / `--no-save` flags as the other
Phase 6 commands.

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
- **Use the BH-adjusted p, not the raw one, to call a pair significant.** Each
  score mode runs several pairwise Wilcoxon tests, so the pairwise table adds a
  **`p (BH-adj)`** column — the Benjamini-Hochberg false-discovery-rate-adjusted
  p-value (see §8). The raw Wilcoxon p is kept beside it for transparency, but a
  "provider A beat provider B" claim should rest on the adjusted value.

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

## 5. Subscription-based cost-per-quality

Section 4's cost is a **research-budget** question: what did this study
actually spend? This section answers a different, **consumer-economics**
question: *is a $20/month subscription worth it to a practicing vet?*

The assumption: a vet summarizing 500 papers/month (20/day × 25 days) on a
flat $20/month subscription pays **$0.04/summary**, regardless of token usage
— unlike the real API cost, which scales with how long the paper and summary
are. `efficiency = mean quality / $0.04` is reported per provider so a higher
number means better value for the subscription price, not a cheaper API bill.

This is a deliberately separate column from `cost_per_quality_point` in the
provider comparison table — neither replaces the other, since they answer
different questions. Defaults are overridable via `SUBSCRIPTION_COST_PER_MONTH_USD`
and `SUBSCRIPTION_PAPERS_PER_MONTH` in `.env` (see `.env.template`), or
`--subscription-price-usd` / `--papers-per-month` on the standalone
`stats-engine` command (§2).

## 6. Information density (vs. Appleby et al. 2023)

A 2023 study (Appleby et al.) found that AI-written summaries carried *less*
information than the source abstract 79.7% of the time. This section re-runs
that check against this study's own three providers, using two measuring
sticks computed by [`llm-sum/stats_engine.py`](../../llm-sum/stats_engine.py):

- **Word count** — is the summary shorter, about the same length, or longer
  than the abstract?
- **TF-IDF + cosine similarity** — does the summary's *meaningful* vocabulary
  (rare, specific veterinary terms, not filler words) overlap with the
  abstract's? Fit once over the **whole corpus** (every abstract + every
  candidate summary in the run), not per-pair — a 2-document IDF is
  degenerate and washes out exactly the signal this metric needs. Computed
  via `sklearn.feature_extraction.text.TfidfVectorizer` and
  `sklearn.metrics.pairwise.cosine_similarity`.

Cosine similarity **>= 0.85** is banded "high fidelity" (kept almost all
technical meaning); **< 0.50** is "lost key details"; in between is
"moderate." The headline number — percent of summaries with similar-or-more
information (cosine >= 0.50) — is compared against Appleby et al.'s
benchmarks: **>= 60%** reads as meaningful progress since 2023; **<= 20%**
(i.e. still failing >= 80% of the time) reads as the problem persisting. The
0.50/0.85 cutoffs are named, tunable constants
(`stats_engine.INFORMATION_DENSITY_RETENTION_THRESHOLD` /
`_HIGH_THRESHOLD`) — an explicit modeling choice, not a re-derivation of
Appleby et al.'s own exact formula, since that isn't reproducible from the
numbers available. See
[`docs/statistics_explained.md`](../statistics_explained.md#part-46--tf-idf-and-cosine-similarity-information-density)
for the plain-English explanation of TF-IDF and cosine similarity.

## 7. Covariate analysis (the "research meat")

Per-stratum quality (§1's per-stratum breakdown) and per-stratum hallucination
rate live in different tables today; this section joins them **and** adds
Cohen's Kappa, one table per (provider × species / study_design / journal)
cell:

- **Hallucination rate** — share of that provider's summaries in that stratum
  value with at least one unsupported claim.
- **Mean quality** — same unweighted jury score as the rest of this report,
  scoped to that cell.
- **Cohen's Kappa** (+ n) — agreement between the LLM judge and a human
  reviewer on that cell's items, from `data/human_reviews.jsonl` (Phase 5).
  See [`docs/statistics_explained.md`](../statistics_explained.md#part-45--cohens-kappa-and-percent-agreement)
  for what Kappa measures and why it's different from Krippendorff's alpha.

This lets a reader ask "does GPT-5 perform worse on equine papers (rare in
training data) than canine papers?" or "do all providers struggle with case
series but do fine on RCTs?" directly, e.g. *"GPT-5 has a 0.82 Kappa for
canine papers but only 0.45 for equine papers."*

**Read the n before the Kappa.** The human-review sample is small — the
minimum is 5 articles → 15 items (`HUMAN_REVIEW_SAMPLE_SIZE` is an article
count that must be a multiple of 5; unset offers a 5/10/25 menu), split across
every provider and every stratum value, so most cells will have very few
human-reviewed items.
Any cell below `stats_engine.MIN_ITEMS_FOR_KAPPA` (default 5) is flagged
`kappa_underpowered: true` in the JSON / "(underpowered)" in the Markdown —
shown for transparency, not as a firm conclusion. Collect more human reviews
before leaning on a specific cell's Kappa.

## 8. Statistics

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
- **Multiple-comparison correction** — `scipy.stats.false_discovery_control`
  (`method="bh"`, Benjamini-Hochberg). Each score mode's pairwise Wilcoxon
  p-values form one family and are FDR-adjusted together; unweighted and
  weighted are separate families, each independently gated by its own Friedman
  omnibus. Benjamini-Hochberg (false discovery rate) is chosen over
  Holm/Bonferroni (family-wise error rate) because a stratified study runs many
  comparisons and FDR preserves statistical power while still controlling false
  positives. The raw and adjusted p-values are both reported; the adjusted one
  (`p_adjusted` / `wilcoxon_p_bh_adjusted`) is the one to cite. A family of one
  comparison is returned unchanged (nothing to correct).

## 9. Guardrails

This command is **read-only and offline** — it never calls a provider API and
never writes to `evaluations.jsonl` (only to `data/results/`). It is safe for
the AI assistant to run under `PHASE3_MODE=test`. It consumes whatever is in
`data/evaluations.jsonl`, so to populate real tables you first run a real
evaluation manually in PowerShell (see
[`docs/phase3/run_phase3.md`](../phase3/run_phase3.md)); a jury run
(`--jury`) additionally fills the reliability line.

## 10. Figures + the VetHELM-style leaderboard

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
  providers, which is the default). If dialed down to a single judge, every
  reliability cell reads "not available," not zero.
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
