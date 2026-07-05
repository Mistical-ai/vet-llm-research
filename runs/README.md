# Run Artifacts

Manuscript-grade runs should write immutable artifacts under:

```text
runs/<run_id>/
```

Expected contents include:

- `run_manifest.json`
- `summaries.jsonl`
- `evaluations.jsonl`
- `reports/summary.json`
- `reports/summary.csv`
- `reports/paired_model_comparison.csv`
- `reports/strata_summary.csv`

Real run directories are intentionally ignored by git because they can contain
large generated outputs and paid API results. Commit only this README and
`.gitkeep`.
