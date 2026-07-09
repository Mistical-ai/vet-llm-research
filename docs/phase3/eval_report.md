# eval_report.py

## What it does

Reads `data/evaluations.jsonl` and prints a MedHELM-style stratified summary:
overall, and broken down by summariser, species, study design, clinical
topic, journal, and input source. Also computes inter-judge reliability
(Krippendorff's alpha + pairwise agreement) when two or more judges scored
the same articles. Read-only with respect to the providers — makes no API
calls.

`--markdown` renders the same data as two plain-English Markdown files
instead of the dense text/JSON views: a short aggregate summary, and a
companion per-article detail file (title, journal, every model's score
breakdown and hallucination claims) for manually cross-checking the judge's
verdict against the real article.

## When to run it

* After `run_phase3.py evaluate` has appended at least one row to
  `data/evaluations.jsonl`.
* Any time you want a snapshot of current scores without re-running judges —
  safe to run repeatedly.

## Inputs

| Path                       | Role                                                                    |
|----------------------------|---------------------------------------------------------------------------|
| `data/evaluations.jsonl`   | Source rows (override with `--evaluations PATH`).                      |
| `src/scenarios/taxonomy.py`| Taxonomy header (`vet_taxonomy_v1`) attached to every report.           |

## Outputs

| Path                                                       | When                                                        |
|--------------------------------------------------------------|--------------------------------------------------------------|
| Stdout — text table, `--json`, or `--markdown`                | Always.                                                       |
| `data/results/eval_report_<UTC timestamp>.json`               | Every run, unless `--no-save` or there are zero evaluation rows to report. |
| `data/results/eval_report_<UTC timestamp>.md`                 | With `--markdown` (same rule as the `.json` file above).      |
| `data/results/eval_report_<UTC timestamp>_detail.md`          | With `--markdown`, unless `--no-detail` is also passed.       |

Each run writes its own timestamped file(s) — nothing in `data/results/` is
ever overwritten, so past snapshots survive a later `evaluate` run changing
`data/evaluations.jsonl`. The `.json`, `.md`, and `_detail.md` files from one
run always share the same timestamp.

## CLI

```powershell
python llm-sum/run_phase3.py eval-report                        # text report; saves to data/results/
python llm-sum/run_phase3.py eval-report --json                  # full report JSON on stdout; also saves
python llm-sum/run_phase3.py eval-report --markdown               # plain-English summary + per-article detail .md files
python llm-sum/run_phase3.py eval-report --markdown --no-detail   # summary .md only, skip the per-article detail file
python llm-sum/run_phase3.py eval-report --no-save                # print only, skip the data/results/ file
python llm-sum/run_phase3.py eval-report --results-dir data/tmp   # save elsewhere for one run
python llm-sum/run_phase3.py eval-report --evaluations data/evaluations_backup.jsonl
```

You can also call the script directly: `python llm-sum/eval_report.py --json`.

| Flag | Purpose |
|------|---------|
| `--evaluations PATH` | Evaluation rows to summarize (default: `data/evaluations.jsonl`). |
| `--json` | Print the full report as JSON instead of the compact text tables. |
| `--markdown` | Print a plain-English Markdown report; also saves it and a companion per-article detail file. Mutually exclusive with `--json`. |
| `--no-detail` | With `--markdown`, skip the per-article detail file (aggregate summary only). |
| `--results-dir PATH` | Where to save the report snapshot (default: `data/results/`). |
| `--no-save` | Print only; don't write a report file. |

## Common errors and fixes

| Symptom                                              | Cause                                                    | Fix                                                     |
|-------------------------------------------------------|-----------------------------------------------------------|------------------------------------------------------------|
| `No evaluation rows found yet.`                       | `data/evaluations.jsonl` is empty or missing.             | Run `python llm-sum/run_phase3.py evaluate` first.          |
| `(No breakdown for: ...)` at the end of the report    | Those rows have no `species`/`study_design`/etc. metadata (common for `summarize-all` `.txt`-sourced rows). | Expected — `manifest.jsonl`-backed rows carry that metadata; `.txt`-sourced rows don't. |
| No file appears in `data/results/`                    | `--no-save` was passed, or there were zero rows to report. | Drop `--no-save`; confirm `data/evaluations.jsonl` has rows. |
| Detail file says "Summary text not found..." for a model | The candidate summary wasn't found in `data/summaries.jsonl` or `data/*summaries_txt/` for that `(doi, summarizer)` — the source file may have moved, or the row predates both. | Expected in some cases; the score, judge reasoning, and hallucination claims are still shown — only the raw candidate text is missing. |

See [MedHELM-Style Evaluation](medhelm_evaluation.md#example-report-output) for
a worked example of the printed table and what each column means.
