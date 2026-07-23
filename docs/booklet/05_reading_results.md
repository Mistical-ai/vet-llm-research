# Chapter 5 — Reading Your Results

**Who this is for:** a technical beginner who has just run (or is about to
look at the output of) an evaluation. You don't need to have read the
earlier chapters, though Chapter 4 (how the jury score is computed) is the
closest relative to this one.

**What you'll be able to do after reading this:** know which command to run
to see your results, understand what each table and artifact is actually
telling you, and be able to explain the two or three most important tables
out loud without getting lost in the statistics. This chapter deliberately
does **not** teach how the underlying math works (that's Chapter 7) or how
to trace a number back to its exact source data (that's Chapter 6). Here,
the goal is navigation: what am I looking at, and what does it mean?

---

## Where we are

By this point, `data/evaluations.jsonl` has rows in it — one row per
(paper, provider) pair that a **judge** (an AI model whose job is to score a
summary; see Chapter 4) has scored. Each row carries a **jury score** (the
1-to-5 number summarizing how good one summary is — Chapter 4 covers exactly
how it's computed) plus hallucination evidence, confidence, and metadata.

That file is an append-only log, not a report. This chapter is about the
tools that turn it into something a human can actually read: `eval-report`
for a quick operator snapshot, and `eval-report --publication` for the
deeper tables a paper or a stratified veterinary analysis needs.

---

## 1. Two commands, two audiences

This project has two different reporting commands, aimed at two different
moments:

| Command | Answers | Audience |
|---|---|---|
| `python llm-sum/run_phase3.py eval-report` | "How did each provider do, broken down by species/journal/etc.?" | You, right after a run, checking things look sane. |
| `python llm-sum/run_phase3.py eval-report --publication` | "Which provider is best, is the difference statistically real, and what does quality cost?" | A methods section, a stratified analysis, a paper. |

Both are **read-only and offline** — they only read `data/evaluations.jsonl`
(and a couple of companion files) and never call a provider API. They're
safe to run as often as you like, and safe under `PHASE3_MODE=test`.

---

## 2. `eval-report`: the operator snapshot

### What an evaluation row looks like, practically

Each line in `data/evaluations.jsonl` is one JSON object (JSONL — one
self-contained record per line — is explained fully in Chapter 2). For
this chapter, the fields that matter are the ones `eval-report` groups by:

- `summarizer` — which provider wrote the summary (openai / anthropic /
  gemini).
- `jury_score`, `jury_score_weighted`, `jury_score_unweighted` — the score,
  in both aggregation modes (Chapter 4).
- `hallucination_claims`, `confidence_score`, `parse_method` — reliability
  signals: did the judge find made-up facts, how confident was it, could
  its response even be read.
- `strata` (and some top-level fields) — `species`, `study_design`,
  `clinical_topic`, `journal`, `input_source` — the subgroup labels a
  stratified veterinary study needs.

### How `eval-report` groups it

Run it with:

```powershell
python llm-sum/run_phase3.py eval-report
```

It reads every row and prints one table per grouping: **overall**, **by
provider**, **by species**, **by study design**, **by clinical topic**, **by
journal**, and **by input source**. Each row of each table shows how many
items were scored, both jury-score means, and three reliability rates:
hallucination rate, major-hallucination rate, and parse-failure rate (how
often the judge's reply couldn't be read cleanly at all — see Chapter 4).

Here's a real shape of what prints (numbers illustrative, but this is the
project's own worked example from `docs/phase3/medhelm_evaluation.md`):

```text
[by_summarizer]
anthropic: n=15 mean=4.0 (weighted=4.02 unweighted=4.0) halluc=0.067 major=0.0 parse_fail=0.0
gemini: n=15 mean=3.71 (weighted=3.74 unweighted=3.71) halluc=0.133 major=0.067 parse_fail=0.0
openai: n=15 mean=3.89 (weighted=3.92 unweighted=3.89) halluc=0.067 major=0.0 parse_fail=0.0

[by_species]
Canine: n=30 mean=3.95 (weighted=3.97 unweighted=3.95) halluc=0.1 major=0.033 parse_fail=0.0
Feline: n=15 mean=3.82 (weighted=3.85 unweighted=3.82) halluc=0.067 major=0.0 parse_fail=0.0
```

Reading the first line: anthropic's 15 scored summaries average a jury
score of 4.0 (both aggregation modes agree closely here), 6.7% of its
summaries had at least one hallucination, none were `major` severity, and
every judge response parsed cleanly.

A section that would only have one group — and that group is literally
"unknown" — is skipped rather than printed as noise; the report tells you
which sections were skipped and why (usually: that metadata isn't attached
to rows judged from the older `summarize-all` `.txt` workflow, since it
normally comes from `manifest.jsonl`).

### The narrative version: `--markdown`

```powershell
python llm-sum/run_phase3.py eval-report --markdown
```

Same underlying data, rendered as plain English instead of dense tables:
a short **aggregate summary** file (headline numbers, a glossary of terms),
plus a **per-article detail** file — one section per paper, showing every
provider's score breakdown, the judge's one-sentence reasoning for each
criterion, and any hallucination claims with their quoted evidence. The
detail file exists so you can manually open the real article and check
whether the judge's verdict actually holds up — the same kind of check
Chapter 8's human reviewer does, just self-directed.

Add `--no-detail` to skip the (longer) per-article file if you only want
the aggregate summary.

### Where results go

Every run writes its own timestamped file(s) under `data/results/` — split
into a `json/`, `reports/`, and `detail/` subfolder so each kind never mixes
with the others — and nothing is ever overwritten, so a report from last week
survives a fresh `evaluate` run changing `evaluations.jsonl`. Pass `--no-save`
to print only.

---

## 3. `eval-report --publication`: the paper-ready layer

```powershell
python llm-sum/run_phase3.py eval-report --publication
```

This produces three linked artifacts in `data/results/`, all sharing one
timestamp:

| Artifact | What it is |
|---|---|
| `publication_report_<ts>.json` | The full report, every number, machine-readable. |
| `publication_report_<ts>.md` | The same tables as human-readable Markdown — pastable into a manuscript or slide. |
| `publication_report_<ts>_tables/` | A folder of CSVs, one per table, for a stats package or spreadsheet. |

### The unit of analysis, in plain terms

Before the tables make sense, one framing detail matters: **one "item" is a
(paper, input channel) pair.** A paper judged from both processed text and
a direct PDF counts as *two* items. If more than one judge scored the same
item (a **jury** of judges — see Chapter 4), their scores are averaged
first, so every provider contributes exactly **one** score per item.

That matters because it makes the comparisons **paired**: the significance
tests below ask "did provider A beat provider B *on the same papers*?" —
not "does provider A's average look bigger than provider B's average across
two separate piles of papers?" Those are different, and much weaker,
questions. Pairing is what makes "A beat B" a meaningful claim.

### What each table answers

The rest of this section walks through every table `--publication` produces,
in plain terms — **not** how the statistics behind them work. That's
Chapter 7; each subsection below points forward to it explicitly.

#### Provider comparison

One row per provider: mean jury score in **both** modes (unweighted and
weighted), each with a **95% bootstrap confidence interval** (a range that
likely contains the true average — Chapter 7 explains exactly what
"bootstrap" means and how the interval is built), mean cost, **cost-per-
quality-point**, a **subscription cost-per-quality-point** (§4 below), and
mean judge disagreement.

> **How you'd describe this table in a meeting:** *"Anthropic has the
> highest average quality at 4.0, with a confidence interval of roughly
> [3.7, 4.3] — meaning we're fairly confident the true average sits
> somewhere in that range, not that every single summary scored exactly
> 4.0. OpenAI's interval overlaps with Anthropic's, so on quality alone
> the gap between them isn't resolved yet — we'd want the significance
> table to say more."*

#### Significance

A **Friedman** test (an omnibus test — "is there a difference *somewhere*
among all providers?" — asked once per score mode) plus **pairwise
Wilcoxon signed-rank** tests for every pair of providers, each with its own
bootstrap confidence interval on the size of the difference. Run separately
for the unweighted and the weighted score.

Two things to know before you read a p-value here (a **p-value** is a
number that roughly answers "if there were really no difference, how
surprising would data like this be?" — smaller means more surprising, i.e.
more evidence of a real difference; Chapter 7 teaches this from scratch):

- **Use the `p (BH-adj)` column, not the raw p-value**, to call a pair
  "significant." Running several pairwise comparisons at once inflates the
  chance that *one* of them looks significant purely by luck; Benjamini-
  Hochberg correction (Chapter 7) adjusts for that. The raw p is kept beside
  it for transparency only.
- **`underpowered (n<10)`** means fewer than ten shared items stood behind
  that specific test. The p-value is still shown, but it's too unstable to
  lean on — collect more evaluated papers first.

> **How you'd describe this table in a meeting:** *"The Friedman test says
> there's a real difference somewhere among the three providers on the
> unweighted score. Looking at the pairwise breakdown, Anthropic beats
> Gemini with an adjusted p-value under 0.05 — that one holds up. The
> Anthropic-vs-OpenAI comparison shows a smaller gap and isn't significant
> after adjustment, so we can't yet say one beats the other — we'd need
> more evaluated papers."*

#### Per-stratum breakdowns

Provider × species / study design / clinical topic / journal cross-tabs of
the unweighted mean — the same idea as `eval-report`'s per-stratum tables
above, but computed on the item-level, paired dataset the publication
report uses throughout, so every table in this artifact is internally
consistent.

#### Processed text vs. direct PDF

A dedicated provider × input-channel table, kept separate from the clinical
strata above, answering one focused question: does feeding a provider the
cleaned **processed text** (Chapter 2) versus the raw **PDF** systematically
help or hurt its scores?

#### Inter-judge reliability

A one-line summary of **Krippendorff's alpha** (a chance-corrected agreement
score between judges — Chapter 4 introduces it as "the rigorous version of
judge disagreement"; Chapter 7 teaches the actual math) — populated only
when a **jury** of two or more judges scored the same articles (the default).
If `JUDGE_MODELS` is dialed down to a single judge, this reads "not available,"
not zero.

#### Information density

Word count and **TF-IDF + cosine similarity** (a way of checking whether a
summary's meaningful, specific vocabulary overlaps with the source
abstract's — Chapter 7 has the full explanation) for every summary against
its paper's own abstract. This re-runs a published 2023 benchmark (Appleby
et al.) against this study's own three providers, banding each summary
"high fidelity" (cosine ≥ 0.85), "moderate," or "lost key details"
(cosine < 0.50).

#### Covariate analysis (the "research meat")

One table per (provider × species / study design / journal) cell, joining
three numbers that normally live in separate tables: **hallucination
rate**, **mean quality**, and **Cohen's Kappa** (LLM judge vs. human
reviewer agreement on that specific cell — see Chapter 7 for how Kappa
differs from Krippendorff's alpha). This is the table that lets you ask a
genuinely clinical question directly.

> **How you'd describe this table in a meeting:** *"Breaking it down by
> species, OpenAI's Kappa against our human reviewers is 0.82 for canine
> papers but only 0.45 for equine papers — so the AI judge tracks a human's
> judgment much more closely on dog papers than horse papers, which makes
> sense given how much more canine literature these models likely saw in
> training. But read the n first: with only 15 human-reviewed items split
> across three providers and several species, most of these cells are
> thin — the report flags any cell under 5 items as `(underpowered)`, so
> treat a single cell's Kappa as a signal to investigate, not a settled
> conclusion."*

---

## 4. Cost: two different questions

The publication report answers **two separate cost questions**, and it's
worth being precise about which one a number is answering:

- **`cost_per_quality_point`** (in the provider comparison table) is a
  **research-budget** question: what did this study actually spend to
  generate each provider's summaries, priced from the summarizer's own
  logged token counts? The judge's own cost is a separate, shared overhead
  and is deliberately *not* charged to any one provider here.
- **`subscription_cost_per_quality_point`** is a **consumer-economics**
  question: *is a flat-rate AI subscription worth it to a practicing vet?*
  The assumption is a vet summarizing 500 papers a month on a $20/month
  subscription, which works out to $0.04 per summary regardless of how long
  the paper or summary actually is — unlike the real API cost, which scales
  with token count. `efficiency = mean quality ÷ $0.04` per provider, so a
  higher number means better value for the subscription price.

Neither number replaces the other — they're deliberately kept side by side
because they answer different questions ("what did the *study* spend?" vs.
"is this *worth paying for* day to day?").

---

## 5. The leaderboard and figures

```powershell
python llm-sum/run_phase3.py report-figures
```

This reuses the exact same publication-report numbers (same bootstrap seed,
same cost basis) and produces a slide- or results-page-ready summary rather
than a methods-section table:

| Artifact | What it is |
|---|---|
| `leaderboard_<ts>.json` / `.md` | One row per provider, ranked by unweighted quality, best first. |
| `figures_<ts>/*.png` + `*.svg` | Four presentation charts. |

The leaderboard's columns are all numbers you've already met above — quality
(both modes, with CIs), cost and cost-per-quality-point, per-provider
Krippendorff's alpha (scoped to *that provider's* summaries specifically,
answering "do judges agree with each other about THIS provider?"), and
per-provider Spearman correlation against human reviewers (from Chapter
7's validation data, when it exists).

The four figures:

1. **`provider_comparison`** — grouped bars, unweighted and weighted jury
   score with 95% confidence-interval error bars, one color per provider.
2. **`cost_quality`** — a scatter plot of cost vs. quality, one labeled
   point per provider (skipped if no provider has a priced summary).
3. **`criterion_heatmap`** — provider × criterion (faithfulness /
   completeness / clinical usefulness / clarity / safety) mean score, one
   color intensity per cell, with the exact value also printed on the cell
   since color alone can't carry two-decimal precision.
4. **`reliability`** — Krippendorff's alpha per criterion plus overall,
   emitted only when a jury (2+ judges) actually ran; skipped for the
   default single-judge run.

Add `--no-figures` for the leaderboard only, or `--formats png` to skip the
SVG copies.

---

## Recap

- **`eval-report`** is your quick, repeatable operator snapshot — grouped
  by provider, species, study design, clinical topic, journal, and input
  source, with a narrative `--markdown` mode for a plain-English read plus
  a per-article detail file for manual spot-checks.
- **`eval-report --publication`** builds the deeper, paper-ready layer:
  provider comparison with confidence intervals, paired significance
  tests, per-stratum and processed-vs-PDF breakdowns, inter-judge
  reliability, information density, and the covariate "research meat"
  table joining quality, hallucination rate, and Cohen's Kappa.
- **The unit of analysis is one item = one (paper, input channel) pair**,
  and providers are compared **paired by item** — the same papers, not two
  separate piles — which is what makes a "provider A beat provider B" claim
  meaningful.
- **Cost is answered twice, on purpose**: a research-spend number
  (`cost_per_quality_point`) and a separate consumer-subscription number
  (`subscription_cost_per_quality_point`).
- **The leaderboard and four figures** repackage the same underlying
  numbers for a slide or results page rather than a methods section.
- This chapter deliberately stayed at the "what does this table mean"
  level. **Chapter 6** shows you how to trace any of these rows back to the
  exact paper, code version, prompt, and model that produced it. **Chapter 7**
  teaches every formula behind these numbers — p-values, Wilcoxon, Friedman,
  Benjamini-Hochberg correction, bootstrap confidence intervals,
  Krippendorff's alpha, Cohen's Kappa, and TF-IDF/cosine similarity — from
  scratch, with worked examples.
