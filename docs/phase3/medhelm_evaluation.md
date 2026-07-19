# MedHELM-Style Evaluation For Veterinary Summaries

> **Doc role: authoritative methods reference.** This page defines the rubric,
> the hallucination taxonomy, and the jury-score math — it is the source of
> truth for "what is scored and how." For CLI usage and file paths, see the
> operator how-to: [evaluator.md](evaluator.md). For the superseded rubric
> this one replaced, see the [legacy reference](judge_and_rubric.md). To read
> one paper's actual judge output field-by-field, see
> [reading_detail_eval_reports.md](reading_detail_eval_reports.md).

## Purpose

This document explains the evaluation method used after the LLMs generate
summaries of veterinary journal articles. The goal is not just to ask "which
model sounds best?" The goal is to produce a reproducible, blind, clinically
meaningful benchmark that can support a research methods section.

The pipeline asks a separate judge model to compare each candidate summary with
the cleaned full article text. The judge does not see the summarizer name. It
scores several criteria, identifies unsupported claims with source quotes, and
writes structured JSONL rows that can be analyzed later.

## What MedHELM Means Here

MedHELM is a medical evaluation framework built around complete clinical tasks,
task metadata, annotators, metrics, and reproducible reporting. This project
borrows the useful methodology, not the full software stack.

Borrowed ideas:

- Treat evaluation as a benchmark with a declared recipe.
- Represent each paper-summary pair as an evaluation instance.
- Use a blind LLM-as-jury annotator with structured criterion scores.
- Store automatic metrics as secondary evidence.
- Report results by clinically meaningful strata such as species and topic.
- Track reliability signals such as parse failures, low confidence, major
  hallucinations, and judge disagreement.

Not borrowed:

- The full HELM runner.
- The leaderboard web server.
- GPU-heavy faithfulness stacks.
- Human clinical datasets that do not apply to veterinary literature.

This keeps the pipeline lightweight and auditable while still following the
stronger evaluation logic.

## Simplified Explanation

- An instance is one paper-summary pair.
- A judge is a separate LLM that evaluates the summary.
- A criterion is one part of the rubric, such as faithfulness or safety.
- A jury score is the weighted average of the criterion scores.
- A stratum is a subgroup label, such as feline papers, oncology papers, or
  retrospective studies.

## Files Involved

- `llm-sum/eval_config/medhelm_vet_summary.yaml` declares the benchmark recipe.
- `llm-sum/prompts/judge_medhelm_v1.txt` contains the blind judge rubric.
- `llm-sum/eval_instances.py` joins summaries, cleaned text, and covariates.
- `llm-sum/summarize_all_ingest.py` builds the same evaluation instances from
  summarize-all's `.txt` comparison files, for `EVAL_INPUT_MODE=dev|regular`
  (see [evaluator.md](evaluator.md#choosing-the-input-source-run_phase3py-evaluate)).
- `llm-sum/eval_metrics.py` calculates local secondary metrics.
- `llm-sum/evaluator.py` calls the judge, parses JSON, calculates scores, and
  appends rows to `data/evaluations.jsonl`.
- `llm-sum/reliability.py` computes offline inter-judge agreement
  (Krippendorff's alpha, pairwise agreement) for multi-judge runs.
- `llm-sum/eval_report.py` summarizes results by model and clinical strata, and
  prints the reliability section when a jury was used.

## The Rubric

The judge scores five criteria from 1 to 5. A score of 1 means poor, 3 means
acceptable but imperfect, and 5 means excellent.

### Faithfulness

Faithfulness asks whether the summary is supported by the article text.

Why it matters: a summary that invents a treatment effect, species, sample size,
or statistical result can mislead a clinician even if it is well written.

Scoring guide:

- 5: Every clinically meaningful claim is supported by the article.
- 3: Mostly supported, but with minor unsupported wording or overgeneralization.
- 1: Important claims contradict the article or are not supported.

### Completeness

Completeness asks whether the summary covers the standard pieces a veterinary
research summary should contain.

Checklist:

- Objective or research question.
- Study design and methods.
- Species and sample size.
- Key results.
- Clinical significance.
- Limitations.

Scoring guide:

- 5: All six elements are present.
- 3: Four or five elements are present.
- 1: Three or fewer elements are present.

### Clinical Usefulness

Clinical usefulness asks whether a veterinary clinician can understand the
take-home message and apply it appropriately.

Why it matters: a summary can be factually accurate but still too vague to help
practice. Species context is especially important in veterinary medicine because
findings from cats, dogs, horses, cattle, and other species should not be
silently generalized.

Scoring guide:

- 5: Species and clinical context are clear early, with an actionable take-away.
- 3: Partly useful, but vague, delayed, or missing some species context.
- 1: A clinician would not get a reliable interpretation.

### Clarity

Clarity asks whether the summary is readable and organized.

Why it matters: confusing summaries slow interpretation and can hide important
caveats. This criterion is weighted lower than faithfulness and safety because a
clear but wrong summary is more dangerous than a clunky but accurate one.

Scoring guide:

- 5: Concise, organized, readable, and non-repetitive.
- 3: Understandable, with minor flow, length, or repetition issues.
- 1: Disorganized, confusing, or substantially repetitive.

### Safety

Safety asks whether the summary could mislead clinical interpretation.

Why it matters: in a clinical research context, wrong species, wrong
intervention, wrong outcome, exaggerated certainty, or missing caveats can
change how a veterinarian interprets the paper.

Scoring guide:

- 5: No misleading clinical interpretation and no species-generalization risk.
- 3: Minor wording could be misunderstood but is unlikely to change practice.
- 1: The summary could mislead a clinician.

## Hallucination Rules

A hallucination is any factual claim in the summary that is not explicitly
stated or directly implied by the reference text.

For each hallucination, the judge must return:

- `claim`: the exact unsupported text from the summary.
- `source_quote`: the article text that contradicts it or shows lack of support.
- `category`: `fabricated_statistics`, `omitted_caveat`, `contradiction`, or
  `unsupported_inference`.
- `severity`: `minor` or `major`.

Major hallucinations are flagged for human review because they could change
clinical interpretation.

## How The Score Is Calculated

### Who does the math?

The **judge LLM does not calculate the final score.** It only returns five
separate criterion scores (each from 1 to 5), plus hallucination evidence and a
confidence rating.

**Python calculates the jury score** after the judge responds. That way:

- The LLM never has to do arithmetic (LLMs sometimes make math mistakes).
- The same formula runs identically on every paper, every model, every rerun.
- Anyone reading the study can open `llm-sum/evaluator.py` and verify the math.

This is the same design principle as the older Vet-Score rubric — only the
number of criteria and the scale changed.

### Two aggregation modes, always both computed

Python actually computes **two** scores from the same five criterion scores
every time, and stores both on every row:

- `jury_score_unweighted` — a flat mean across all five criteria. This is
  stock MedHELM's method (`LLMJuryMetric` in the Stanford HELM codebase
  averages every judge×criterion score with no weighting).
- `jury_score_weighted` — this project's clinical-risk-weighted mean (below).

`jury_score` is just an alias for whichever one is primary, controlled by
`JURY_AGGREGATION_MODE` in `.env` (default: `unweighted`). Because both are
always stored, switching `JURY_AGGREGATION_MODE` — or just reading the other
field directly — never requires re-running judges. See
["Weighted vs. Unweighted"](#weighted-vs-unweighted) below for when to use each.

### What is a weighted average?

A **weighted average** means some criteria count more than others. Faithfulness
and safety matter more than clarity, so their scores pull the final number up or
down more strongly.

Think of it like a course grade where the final exam is worth 40% and homework
is worth 10% — not every assignment counts equally.

### The weights (importance multipliers)

| Criterion | Weight | Plain meaning |
|-----------|--------|----------------|
| Faithfulness | 1.5 | Most important — wrong facts are the worst failure |
| Safety | 1.3 | High — misleading clinical wording is dangerous |
| Clinical usefulness | 1.2 | Important — the summary must help a vet clinician |
| Completeness | 1.0 | Standard — cover the main paper sections |
| Clarity | 0.8 | Lowest — readability matters, but not more than correctness |

**Total weight** = 1.5 + 1.3 + 1.2 + 1.0 + 0.8 = **5.8**

### The formula (step by step)

For each criterion, multiply the judge's score by its weight. Add those products
up. Divide by the total weight.

```text
jury_score = (score₁ × weight₁ + score₂ × weight₂ + … + score₅ × weight₅)
             / (weight₁ + weight₂ + … + weight₅)
```

In code-friendly terms:

```text
jury_score =
  weighted_sum(criteria_score × criterion_weight)
  / sum(all criterion weights)
```

The result is a single number on a **1 to 5 scale**. That is the primary score
for MedHELM-style evaluation.

### Worked example

Suppose the judge returns these scores for one summary:

| Criterion | Judge score (1–5) | Weight | Score × weight |
|-----------|-------------------|--------|----------------|
| Faithfulness | 4 | 1.5 | 4 × 1.5 = **6.0** |
| Safety | 5 | 1.3 | 5 × 1.3 = **6.5** |
| Clinical usefulness | 3 | 1.2 | 3 × 1.2 = **3.6** |
| Completeness | 4 | 1.0 | 4 × 1.0 = **4.0** |
| Clarity | 4 | 0.8 | 4 × 0.8 = **3.2** |

**Step 1 — add the weighted products:**

```text
6.0 + 6.5 + 3.6 + 4.0 + 3.2 = 23.3
```

**Step 2 — divide by total weight (5.8):**

```text
jury_score = 23.3 / 5.8 = 4.02  (rounded to 4.02)
```

So this summary would get **`jury_score = 4.02`**. Clinical usefulness (3)
pulled the average down more than clarity did, because clinical usefulness has a
higher weight.

### What happens when a criterion is missing

The worked example above assumes the judge returned all five criterion scores.
When a judge's response is missing one (a partial parse, a criterion the model
skipped), that criterion drops out of **both** the numerator and the
denominator — `calculate_jury_score` in `llm-sum/evaluator.py` renormalizes
over whichever criteria actually have a usable score, rather than padding the
missing one to a default value. For example, if faithfulness (weight 1.5) is
absent and the other four criteria are all scored 4, the result is `4.00` —
the same as if faithfulness had never been part of the rubric for that row —
not a value dragged down by an imputed score. This matches how
`human_review.py` scores a partially-filled human reviewer sheet (see
[human_validation.md](../phase5/human_validation.md#why-both-jury-modes-weighted-and-unweighted-are-validated)),
which is what lets the human and LLM composites be correlated against each
other at all.

Every row also carries a `missing_criteria` field — the list of criterion
names (if any) that were absent for that row, so a partially-scored judgement
is visible in the data rather than only in the arithmetic. It is computed by
the sibling function `missing_criteria()` in `llm-sum/evaluator.py`, kept
separate so `calculate_jury_score` keeps returning a bare number.

**Best possible score:** if every criterion is 5:

```text
(5×1.5 + 5×1.3 + 5×1.2 + 5×1.0 + 5×0.8) / 5.8 = 29.0 / 5.8 = 5.0
```

**Worst possible score:** if every criterion is 1:

```text
(1×1.5 + 1×1.3 + 1×1.2 + 1×1.0 + 1×0.8) / 5.8 = 5.8 / 5.8 = 1.0
```

### Legacy `quality_score` (1–10)

The row also includes `quality_score`, a **derived** 1–10 field for older
notebooks and scripts. Python maps `jury_score` onto 1–10 with a simple linear
scale — it is not a separate judge opinion. **New analysis should use
`jury_score`.**

## Why These Weights

The weights reflect clinical risk:

- Faithfulness is highest because unsupported claims directly threaten validity.
- Safety is also high because clinically misleading summaries need special
  penalty even when they are readable.
- Clinical usefulness matters because veterinary summaries need species-specific
  interpretation.
- Completeness matters, but missing a minor section is less dangerous than a
  false claim.
- Clarity matters, but it is weighted lower because style should not outweigh
  correctness.

## Weighted vs. Unweighted

Both scores are computed from the same `criteria_scores` and stored on every
row. Use whichever fits the question you are asking.

### The two formulas, worked side by side

Using the same example scores as above (faithfulness 4, safety 5, clinical
usefulness 3, completeness 4, clarity 4):

| Aggregation | Formula | Result |
|-------------|---------|--------|
| Unweighted (`jury_score_unweighted`) | `(4 + 5 + 3 + 4 + 4) / 5` | **4.00** |
| Weighted (`jury_score_weighted`) | `(4×1.5 + 5×1.3 + 3×1.2 + 4×1.0 + 4×0.8) / 5.8` | **4.02** |

The two scores are close here because the example scores are all similar. They
diverge more when a low score lands on a high-weight criterion — for example,
a summary that is perfectly clear (clarity 5) but clinically unsafe (safety 1)
scores much lower under the weighted formula than the unweighted one, because
safety counts for more than clarity.

### Pros and cons

| | Unweighted (default) | Weighted |
|---|---|---|
| **Fidelity to stock MedHELM** | Exact match — the same flat mean `LLMJuryMetric` computes. | A deliberate deviation; not directly comparable to published MedHELM numbers. |
| **Simplicity** | Every criterion counts equally; nothing to justify or tune. | Requires justifying *why* faithfulness and safety outweigh clarity (see "Why These Weights" above). |
| **Clinical risk sensitivity** | Treats a wrong fact and a clunky sentence as equally damaging. | Penalizes clinically dangerous failures (wrong facts, misleading safety claims) more than stylistic ones — arguably more meaningful for a veterinary audience. |
| **Use when...** | You want a number you can compare against MedHELM literature, or you want the simplest possible defensible default. | You want the score to reflect this project's clinical-risk judgment, e.g., for the paper's primary results. |

### How to switch

Set in `.env`:

```
JURY_AGGREGATION_MODE=unweighted   # default; stock MedHELM's flat mean
JURY_AGGREGATION_MODE=weighted     # this project's clinical-risk-weighted mean
```

This only changes which field `jury_score` aliases — it does not require
re-evaluating anything, because `jury_score_weighted` and
`jury_score_unweighted` are both already in `data/evaluations.jsonl`. Running
`python llm-sum/run_phase3.py eval-report` always prints `mean_score`,
`mean_score_weighted`, and `mean_score_unweighted` together per stratum, so you
can compare the two methods directly without touching `.env` at all.

### How to customize the weights

The weighted formula's criterion weights can be overridden without touching
code, via `JURY_CRITERION_WEIGHTS` in `.env` (a JSON object naming all five
criteria):

```
JURY_CRITERION_WEIGHTS={"faithfulness":1.5,"completeness":1.0,"clinical_usefulness":1.2,"clarity":0.8,"safety":1.3}
```

An invalid value or a value missing one of the five criteria is ignored and
the documented defaults are used instead — a typo in `.env` cannot silently
corrupt every score.

## Why Blind Judging Matters

The judge sees only:

- The cleaned article text.
- The candidate summary.

The judge does not see:

- The summarizer provider.
- The model name.
- Words such as OpenAI, Anthropic, Gemini, GPT, or Claude.

This prevents model-name bias and keeps comparisons fair. The summarizer name is
attached only after scoring, as metadata in `data/evaluations.jsonl`.

## Why Automatic Metrics Are Secondary

The pipeline also stores local automatic metrics:

- `compression_ratio`: how short the summary is compared with the source.
- `extractive_coverage`: how much summary wording appears in the source text.
- `section_coverage`: whether the summary appears to mention key paper sections.
- `rouge_1`, `rouge_2`, `rouge_l`: word-overlap recall metrics.

These are useful for auditing and debugging. They are not the main score because
word overlap cannot tell whether a veterinary summary is clinically safe or
faithful.

## Reliability Checks

Each row includes signals that help judge whether the evaluation itself is
trustworthy:

- `confidence_score`: the judge's confidence from 1 to 5.
- `parse_method`: whether the output parsed as JSON, regex fallback, or
  sentinel.
- `requires_human_review`: true for low confidence, malformed output, or major
  hallucination.
- `judge_disagreement`: max-minus-min jury score when multi-judge rows are
  aggregated.
- `valid_judge_count`: how many judges produced usable scores.

Default evaluation uses the full 3-judge panel (`JUDGE_MODELS=openai,anthropic,gemini`),
matching stock MedHELM's default jury panel size (GPT-4o, Llama-3.3-70B,
Claude-3.7-Sonnet in the Stanford implementation; this project uses its three
existing provider integrations instead). `JUDGE_MODELS` can be dialed down to
fewer judges to control cost, each step a one-line `.env` change with no code
changes needed:

- `JUDGE_MODELS=openai,anthropic,gemini` — default, three judges.
- `JUDGE_MODELS=openai,anthropic` — two judges, still enables `judge_disagreement`.
- `JUDGE_MODELS=openai` — cheapest, one judge, no `judge_disagreement`.

Each step down roughly divides judge-call cost by the number of judges removed.

### One-step panel switch

Typing the three providers every time is easy to get wrong, so the panel has a
shortcut. All three of these select the same `openai,anthropic,gemini` jury:

- `JURY_PRESET=panel` in `.env` (and `JURY_PRESET=solo` for a single judge).
- `--jury` on `python llm-sum/run_phase3.py evaluate` or `llm-sum/evaluator.py`,
  scoped to one command.
- The explicit `JUDGE_MODELS=openai,anthropic,gemini`.

Precedence is: an explicit `--judges` list beats `--jury`, which beats
`JURY_PRESET`, which beats `JUDGE_MODELS`. The 3-judge panel default is unchanged
when none of these is set.

### Cross-judge aggregation

Each judge writes its own append-only row (the file is never rewritten). When
several judges score the same summary, `aggregate_jury_scores()` in
`evaluator.py` computes the cross-judge view **for both aggregation methods at
once** — `jury_score_weighted_mean` and `jury_score_unweighted_mean`, each with
its own `judge_disagreement_*` spread. Because both are kept, changing
`JURY_AGGREGATION_MODE` after a multi-judge run never requires re-running the
judges.

### Reliability statistics (2+ judges)

When at least two judges score the same summaries, `python llm-sum/run_phase3.py
eval-report` adds a **Reliability** section, computed offline by
`llm-sum/reliability.py` (no API calls, no `scipy`/`numpy` dependency):

- **Krippendorff's alpha (interval)** — a single agreement number corrected for
  chance, for the overall `jury_score` and for each of the five criteria. `1.0`
  is perfect agreement, `0.0` is chance-level, and negative means the judges
  disagree more than chance. It tolerates a judge missing on some items, which a
  simple max-minus-min spread cannot. The report labels the value using
  Krippendorff's own cutoffs: `>= 0.80` strong, `>= 0.667` acceptable for
  tentative conclusions, below that interpret with caution.
- **Per-criterion alpha, mean, and variance** — shows *which* criteria the
  judges argue about most (safety and clinical usefulness are often the least
  reproducible).
- **Pairwise absolute agreement** — for each judge pair, the mean and max score
  difference on the summaries both judged: a plain-language companion to alpha.

With a single judge the section prints one line explaining that reliability
needs a jury, and points at `JURY_PRESET=panel`.

## Output Fields

Each evaluation row is appended to `data/evaluations.jsonl`. JSONL means **one
JSON object per line** — you can open the file in a text editor and read row by
row, or load it into Python/pandas for analysis.

Important fields:

- `benchmark_name`: the benchmark recipe name.
- `rubric_version`: the rubric version that produced the score.
- `criteria_scores`: each rubric criterion score and short reasoning.
- `jury_score`: the primary score (aliases weighted or unweighted per `JURY_AGGREGATION_MODE`).
- `jury_score_weighted` / `jury_score_unweighted`: both aggregation methods, always present.
- `jury_aggregation_mode`: which one was primary for this row (`weighted` or `unweighted`).
- `missing_criteria`: list of criterion names the judge did not score for this
  row (empty when all five were scored). See ["What happens when a criterion
  is missing"](#what-happens-when-a-criterion-is-missing).
- `quality_score`: derived legacy 1 to 10 score.
- `hallucination_claims`: quoted unsupported claims.
- `automatic_metrics`: local secondary metrics.
- `strata`: species, study design, clinical topic, journal, and input source.
- `judge_model_version` and `system_fingerprint`: provider version metadata
  when available.

### Example evaluation row

Below is one **pretty-printed** row showing what you would see after evaluating
one paper-summary pair. In the real file it is stored as a **single line** of
JSON (no line breaks inside the object).

This example uses the same criterion scores as the worked example above. With
the default `JURY_AGGREGATION_MODE=unweighted`, `jury_score = 4.0` (equal to
`jury_score_unweighted`) and `quality_score = 8`. The weighted alternative
(`jury_score_weighted = 4.02`) is stored on the row too, but is not primary
unless `JURY_AGGREGATION_MODE=weighted`.

```json
{
  "benchmark_name": "vet_lit_summary_medhelm",
  "doi": "10.1111/jvim.16872",
  "input_source": "processed",
  "summarizer": "anthropic",
  "judge": "openai",
  "judge_model_version": "gpt-5.4-0325-preview",
  "system_fingerprint": "fp_abc123example",
  "evaluator_version": "evaluator-medhelm-v1.0",
  "rubric_version": "vet_medhelm_score_v1.0",
  "criteria_scores": {
    "faithfulness": {
      "score": 4,
      "reasoning": "Main findings match the article; one minor overgeneralization."
    },
    "completeness": {
      "score": 4,
      "reasoning": "Covers objective, methods, species, results, and limitations; clinical significance is brief."
    },
    "clinical_usefulness": {
      "score": 3,
      "reasoning": "Species is named, but the take-home message is vague."
    },
    "clarity": {
      "score": 4,
      "reasoning": "Readable and organized with minor repetition."
    },
    "safety": {
      "score": 5,
      "reasoning": "No misleading clinical interpretation detected."
    }
  },
  "jury_score": 4.0,
  "jury_score_weighted": 4.02,
  "jury_score_unweighted": 4.0,
  "jury_aggregation_mode": "unweighted",
  "missing_criteria": [],
  "judge_count": 1,
  "valid_judge_count": 1,
  "judge_disagreement": 0.0,
  "automatic_metrics": {
    "compression_ratio": 0.0842,
    "extractive_coverage": 0.7314,
    "section_coverage": {
      "objective": true,
      "methods": true,
      "species_sample": true,
      "results": true,
      "clinical_significance": true,
      "limitations": false,
      "covered_count": 5,
      "coverage_ratio": 0.8333
    },
    "rouge_1": 0.4125,
    "rouge_2": 0.1893,
    "rouge_l": 0.3561
  },
  "strata": {
    "species": ["Canine"],
    "study_design": "Retrospective Case Series",
    "clinical_topic": "Cardiology",
    "journal": "Journal of Veterinary Internal Medicine",
    "input_source": "processed"
  },
  "factual_accuracy": 4,
  "completeness": 4,
  "clinical_relevance": 3,
  "organization": 4,
  "composite_score": 4.0,
  "hallucination_present": true,
  "hallucination_claims": [
    {
      "claim": "Mortality was significantly reduced in all animals.",
      "source_quote": "Mortality reduction was observed in the canine cohort only (n=42).",
      "category": "unsupported_inference",
      "severity": "minor"
    }
  ],
  "quality_score": 8,
  "hallucination_count": 1,
  "hallucination_categories": ["unsupported_inference"],
  "confidence_score": 4,
  "requires_human_review": false,
  "parse_method": "json",
  "reasoning": "Mostly faithful summary with one minor overgeneralization and vague clinical take-away.",
  "input_tokens": 8420,
  "output_tokens": 312,
  "raw_response_excerpt": "{\"criteria_scores\":{\"faithfulness\":{\"score\":4,\"reasoning\":\"Main findings match...",
  "timestamp": "2026-07-03T18:45:12.123456+00:00"
}
```

#### How to read this row

| Field | What it tells you |
|-------|-------------------|
| `doi` + `summarizer` | Which paper and which model's summary was judged |
| `judge` | Which provider acted as the blind judge (separate from the summarizer) |
| `criteria_scores` | The five rubric scores the judge returned — Python did not change these |
| `jury_score` | **Primary score** — aliases `jury_score_unweighted` or `jury_score_weighted` depending on `JURY_AGGREGATION_MODE` (4.0 here, unweighted by default) |
| `jury_score_weighted` / `jury_score_unweighted` | Both aggregation methods, always stored (4.02 / 4.0 here) — see ["Weighted vs. Unweighted"](#weighted-vs-unweighted) |
| `quality_score` | Legacy 1–10 alias derived from the primary `jury_score` (8 here) |
| `hallucination_claims` | Quoted evidence when the summary says something the paper does not support |
| `requires_human_review` | `false` here because confidence is high and the hallucination is minor |
| `automatic_metrics` | Secondary local checks — useful, but not the main clinical score |
| `strata` | Subgroup labels for reporting (species, topic, study design, journal) |
| `parse_method` | `json` means the judge response parsed cleanly on the first try |

#### What the file looks like on disk

`data/evaluations.jsonl` is append-only. After scoring three summaries for one
paper (OpenAI, Anthropic, Gemini), you might have three lines like:

```text
{"doi":"10.1111/jvim.16872","summarizer":"openai","jury_score":3.85,...}
{"doi":"10.1111/jvim.16872","summarizer":"anthropic","jury_score":4.0,...}
{"doi":"10.1111/jvim.16872","summarizer":"gemini","jury_score":3.67,...}
```

Each line is independent. Re-running evaluation skips rows that already exist for
the same `(doi, summarizer, judge, input_source, rubric_version)` unless you use
`--no-resume`.

#### Example report output

After evaluation, you can summarize rows without any API calls:

```powershell
python llm-sum/run_phase3.py eval-report
```

Example console output:

```text
[by_summarizer]
anthropic: n=15 mean=4.0 (weighted=4.02 unweighted=4.0) halluc=0.067 major=0.0 parse_fail=0.0
gemini: n=15 mean=3.71 (weighted=3.74 unweighted=3.71) halluc=0.133 major=0.067 parse_fail=0.0
openai: n=15 mean=3.89 (weighted=3.92 unweighted=3.89) halluc=0.067 major=0.0 parse_fail=0.0

[by_species]
Canine: n=30 mean=3.95 (weighted=3.97 unweighted=3.95) halluc=0.1 major=0.033 parse_fail=0.0
Feline: n=15 mean=3.82 (weighted=3.85 unweighted=3.82) halluc=0.067 major=0.0 parse_fail=0.0
```

This shows the primary mean `jury_score`, both aggregation methods side by
side, and reliability rates broken down by model and species — the
MedHELM-style stratified view of your results.

For a narrative, easier-to-read version of the same data — plus a
companion per-article file for manually cross-checking the judge's verdict
against the real article — run `eval-report --markdown` instead. See
[eval_report.md](eval_report.md) for details.

## Safe Mock Run

The agent and unit tests may use only mock mode:

```powershell
python llm-sum/run_phase3.py evaluate --mode test
```

Mock mode uses deterministic fake judge responses. It does not call paid APIs.

## Read-Only Report

After evaluations exist, summarize them without calling any APIs:

```powershell
python llm-sum/run_phase3.py eval-report
```

For machine-readable output:

```powershell
python llm-sum/run_phase3.py eval-report --json
```

Every run also saves the full report JSON to
`data/results/eval_report_<UTC timestamp>.json` (one file per run, never
overwritten) — pass `--no-save` to print only, or `--results-dir PATH` to
save elsewhere for one run. See [eval_report.md](eval_report.md) for the full
flag reference.

## Live Evaluation

Live modes can call paid APIs. They should be run manually by the researcher in
PowerShell after checking `.env`, `BUDGET_HARD_STOP`, and the confirmation
prompt.

Example small paid smoke test:

```powershell
python llm-sum/run_phase3.py evaluate --mode single
```

Do not compare `vet_score_v2.0` rows directly with
`vet_medhelm_score_v1.0` rows. They use different rubrics.
