# Report Schema

The reporting layer writes machine-readable artifacts under
`runs/<run_id>/reports/` when `eval-report --output-dir` is used.

## `summary.json`

Top-level fields:

- `schema_version`: report schema version, currently `report_v1`.
- `overall`: aggregate primary metrics across all rows.
- `strata`: list of grouped summaries.
- `paired_model_comparisons`: model-vs-model paired win-rate summaries.

Common metric fields:

- `n`: number of evaluation rows in the group.
- `valid_scores`: rows with a usable `jury_score` or legacy `quality_score`.
- `mean_score`: arithmetic mean of valid primary scores.
- `score_ci95`: deterministic bootstrap confidence interval for the mean.
- `hallucination_rate`: fraction of rows with `hallucination_count > 0`.
- `flagged_for_review_rate`: fraction of rows requiring manual review.

## `strata_summary.csv`

Each row describes one group:

- `stratum`: grouping field, such as `journal`, `year`, `article_type`, or
  `input_source`.
- `value`: observed group value.
- The common metric fields listed above.

## `paired_model_comparison.csv`

Each row compares two summarizer models only on instances where both produced a
valid score:

- `model_a`, `model_b`: model/provider labels being compared.
- `n`: paired instance count.
- `mean_delta`: mean of `model_a - model_b`.
- `model_a_win_rate`: fraction of paired instances where model A scored higher.
- `model_b_win_rate`: fraction where model B scored higher.
- `tie_rate`: fraction where the scores were equal.

## Reproducibility Notes

Bootstrap intervals use a fixed seed by default (`42`). Paired comparisons drop
unpaired instances because comparing different papers would mix model effects
with dataset differences.
