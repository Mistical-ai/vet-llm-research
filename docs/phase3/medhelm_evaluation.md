# MedHELM-Style Evaluation For Veterinary Summaries

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

## Beginner Mental Model

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
- `llm-sum/eval_metrics.py` calculates local secondary metrics.
- `llm-sum/evaluator.py` calls the judge, parses JSON, calculates scores, and
  appends rows to `data/evaluations.jsonl`.
- `llm-sum/eval_report.py` summarizes results by model and clinical strata.

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

The judge does not calculate the final score. It only returns five criterion
scores. Python calculates the weighted average so the math is reproducible.

Weights:

- Faithfulness: 1.5
- Completeness: 1.0
- Clinical usefulness: 1.2
- Clarity: 0.8
- Safety: 1.3

Formula:

```text
jury_score =
  weighted_sum(criteria_score * criterion_weight)
  / sum(all criterion weights)
```

The primary score is `jury_score`, on a 1 to 5 scale. The row also includes
`quality_score`, a derived 1 to 10 compatibility field for older notebooks. New
analysis should use `jury_score`.

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

Default evaluation uses one judge to control cost. Reliability mode can use more
than one judge, for example `JUDGE_MODELS=openai,anthropic`, but that increases
paid judge calls.

## Output Fields

Each evaluation row is appended to `data/evaluations.jsonl`.

Important fields:

- `benchmark_name`: the benchmark recipe name.
- `rubric_version`: the rubric version that produced the score.
- `criteria_scores`: each rubric criterion score and short reasoning.
- `jury_score`: the primary MedHELM-style score.
- `quality_score`: derived legacy 1 to 10 score.
- `hallucination_claims`: quoted unsupported claims.
- `automatic_metrics`: local secondary metrics.
- `strata`: species, study design, clinical topic, journal, and input source.
- `judge_model_version` and `system_fingerprint`: provider version metadata
  when available.

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
