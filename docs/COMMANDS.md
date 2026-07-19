# Full Pipeline Commands Reference

A single, ordered, copy-paste command list spanning the **entire** pipeline —
from gathering open-access articles off the web, through turning them into
JSONL, through AI summaries and blind-judge evaluation, to producing a
`humanN/` folder for human validation. Each command gets a one-line note, not
a methods explanation — for the "why," see the linked doc.

**Where to go for the "why":**

| Topic | Doc |
|---|---|
| Everything, taught from scratch, for a complete beginner | [docs/booklet/BOOKLET.md](booklet/BOOKLET.md) |
| Shorter command tables + first-time setup | [README.md](../README.md) — see "The commands you need to know" |
| Phase 3 (extract/summarize/evaluate) beginner walkthrough | [docs/phase3/README.md](phase3/README.md) |
| Phase 3 full CLI/flag reference | [docs/phase3/run_phase3.md](phase3/run_phase3.md) |
| Phase 5 human validation, full rationale | [docs/phase5/human_validation.md](phase5/human_validation.md) |

**Live-API rule (see `CLAUDE.md`):** every command below that can spend real
money is marked **PAID**. An AI assistant working in this repo must not
execute those commands automatically — they're for you to run yourself, in
your own terminal, after reading what they'll do. `PHASE3_MODE=test` (the
default) makes every Phase 3+ command free; you switch modes deliberately.

---

## 1. Gather the corpus (Phase 2 — open-access articles off the web)

```powershell
python src/collect.py               # 1. Build the candidate list from CrossRef -> data/manifest.jsonl
python src/download.py               # 2. Auto-download open-access PDFs -> data/raw/*.pdf
python pipeline.py                   # 3. Scoreboard: how close to 250 primary PDFs?
python src/supplement.py             # 4. What's still missing, and why (data/missing_papers.csv)

# For each paper supplement.py lists: get the PDF via UoG library access or
# an author email, then drop it into data/incoming_manuals/ and run:
python src/auto_ingest_workflow.py   # 5. Match, rename, move manual PDFs into data/raw/

python pipeline.py                   # 6. Re-check the scoreboard
```

Repeat steps 2, 4, and 5 until `pipeline.py` reports 250/250 primary PDFs (or
you decide 200+ is an acceptable minimum — see
[docs/booklet/BOOKLET.md Chapter 1](booklet/BOOKLET.md#chapter-1--gathering-the-data)).
All free — no API keys, no paid calls.

---

## 2. PDF → JSONL (Phase 3 extract)

```powershell
python llm-sum/run_phase3.py extract        # data/raw/*.pdf -> data/raw_text/*.jsonl + data/processed/*.jsonl
python scripts/verify_extraction.py         # PASS/WARN/FAIL audit — always run before paying for summaries
```

Both free, no API calls, safe to re-run any time (extraction skips PDFs that
haven't changed). Investigate every `FAIL` before continuing — a garbled
input produces a summary you paid for and can't trust. See
[docs/booklet/BOOKLET.md Chapter 2](booklet/BOOKLET.md#chapter-2--from-pdf-to-jsonl).

---

## 3. AI summaries (Phase 3 summarize) — **PAID** outside `test` mode

```powershell
python llm-sum/run_phase3.py summarize --estimate   # forecast cost first, $0, no API calls
python llm-sum/run_phase3.py summarize              # PAID — do it (type 'yes' to confirm)
```

Real-time modes (`single`/`dev`) run immediately. For the full ~250-paper
corpus, use `batch` mode (50% cheaper, processed by the provider over the
following hours):

```powershell
# .env: PHASE3_MODE=batch
python llm-sum/run_phase3.py summarize          # PAID — submits batch jobs, returns immediately

#   ... wait up to 24h while providers process the batch ...

python llm-sum/check_batch_status.py            # collect finished results into data/summaries.jsonl
```

Six-summary PDF-vs-processed-text comparison (one matched article, 3
providers x 2 input sources):

```powershell
python llm-sum/run_phase3.py summarize-all --mode single   # PAID, ~$0.16
python llm-sum/run_phase3.py summarize-all --mode test     # free mock, same code path
```

See [docs/phase3/README.md](phase3/README.md) and
[docs/booklet/BOOKLET.md Chapter 3](booklet/BOOKLET.md#chapter-3--getting-ai-summaries).

---

## 4. Blind judge evaluation (Phase 3 evaluate) — **PAID** outside `test` mode

```powershell
python llm-sum/run_phase3.py evaluate           # PAID — blind LLM jury scores every summary
python llm-sum/run_phase3.py eval-report        # free — scoreboard + snapshot in data/results/
python llm-sum/run_phase3.py status             # free — read-only counts at every stage
```

See [docs/phase3/medhelm_evaluation.md](phase3/medhelm_evaluation.md) (the
rubric) and
[docs/booklet/BOOKLET.md Chapter 4](booklet/BOOKLET.md#chapter-4--the-blind-ai-judge).

---

## 5. Human validation — produce a `humanN/` folder (Phase 5) — free, offline

```powershell
python llm-sum/run_phase3.py export-human-review --sample-size 10
#   -> data/human_review/human1/  (or human2/, human3/, ... on each next run)
#   -> data/human_review/unblinding_key_human1.json   PRIVATE — never share with a reviewer

# Send the reviewer ONLY the humanN/ folder (never the unblinding_key_*.json).
# They read REVIEWER_GUIDE.md, then score every item_NNN/ into the .xlsx.
# Once you get the filled scoresheet back (dropped into its humanN/ folder):

python llm-sum/run_phase3.py ingest-human-review
#   -> data/human_reviews.jsonl

python llm-sum/run_phase3.py eval-report --markdown
#   -> adds a "Human validation" section: inter-reviewer agreement + jury correlation
```

`--sample-size` must be a multiple of 5 (one of `5`, `10`, `25` — an exact
number of articles, split evenly across the study's 5 journals, each
expanded to every provider's summary of it). Full flag reference and
methods rationale (blind protocol, why articles repeat, overlap ratio,
Krippendorff's alpha, Spearman/Pearson/Bland-Altman):
[docs/phase5/human_validation.md](phase5/human_validation.md).

**Rehearse the whole loop first, on the small dev pool, before spending a
real reviewer's time:**

```powershell
python llm-sum/run_phase3.py export-pilot-human-review --sample-size 10
#   -> data/pilot_human_review/human1/  (marked with a .pilot_export file)
#   -> data/pilot_human_review/unblinding_key_human1.json   PRIVATE

python llm-sum/run_phase3.py ingest-pilot-human-review
#   -> data/pilot_human_reviews.jsonl   (never merged into the real human_reviews.jsonl)
```

Same flags as `export-human-review`/`ingest-human-review` (`--sample-size`,
`--overlap-ratio`, `--seed`, `--review-dir`, `--output`), but reading from
the dev-summary pool and writing to a separate pilot ledger. See
[docs/phase5/pilot_human_review.md](phase5/pilot_human_review.md).

---

## 6. Publication reporting (Phase 6) — free, offline

```powershell
python llm-sum/run_phase3.py eval-report --publication
#   -> data/results/publication_report_<ts>.json / .md / _tables/*.csv

python llm-sum/run_phase3.py report-figures
#   -> data/results/*.png / *.svg + VetHELM-style leaderboard JSON/Markdown

python llm-sum/run_phase3.py stats-engine
#   -> information density, Cohen's Kappa IRR, subscription economics,
#      provider x covariate tables (species/study_design/journal)
```

`report-figures` and `stats-engine` both take `--evaluations`, `--summaries`,
`--human-reviews`, and `--results-dir` overrides (defaults: `data/evaluations.jsonl`,
`data/summaries.jsonl`, `data/human_reviews.jsonl`, `data/results/`). Point
`--human-reviews` at `data/pilot_human_reviews.jsonl` to inspect a pilot
rehearsal instead of the real study. Full details:
[docs/phase6/reporting.md](phase6/reporting.md).

---

## Maintenance

```powershell
python llm-sum/run_phase3.py clean      # delete temporary batch JSONL files
```

---

## Quick reference — every command, in order

```powershell
# Phase 2 — gather
python src/collect.py
python src/download.py
python pipeline.py
python src/supplement.py
python src/auto_ingest_workflow.py     # after dropping manual PDFs into data/incoming_manuals/

# Phase 3 — extract
python llm-sum/run_phase3.py extract
python scripts/verify_extraction.py

# Phase 3 — summarize (PAID outside test mode)
python llm-sum/run_phase3.py summarize --estimate
python llm-sum/run_phase3.py summarize
python llm-sum/check_batch_status.py   # batch mode only, after the provider finishes

# Phase 3 — evaluate (PAID outside test mode)
python llm-sum/run_phase3.py evaluate
python llm-sum/run_phase3.py eval-report
python llm-sum/run_phase3.py status

# Phase 5 — human validation (free)
python llm-sum/run_phase3.py export-human-review --sample-size 10
python llm-sum/run_phase3.py ingest-human-review
python llm-sum/run_phase3.py eval-report --markdown

# Phase 5 — pilot/rehearsal human validation (free)
python llm-sum/run_phase3.py export-pilot-human-review --sample-size 10
python llm-sum/run_phase3.py ingest-pilot-human-review

# Phase 6 — publication reporting (free)
python llm-sum/run_phase3.py eval-report --publication
python llm-sum/run_phase3.py report-figures
python llm-sum/run_phase3.py stats-engine

# Maintenance
python llm-sum/run_phase3.py clean
```
