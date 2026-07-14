# Veterinary LLM Research Pipeline

**Goal:** Build a reproducible research pipeline that (1) collects 250 veterinary journal papers, (2) downloads and validates open-access PDFs, (3) summarizes them with three LLMs under controlled conditions, (4) scores every summary with a blind LLM jury, (5) validates those scores against human experts, and (6) produces publication-ready tables and figures.

**Project context:** OVC Pet Trust Summer Studentship, 2026.

---

## Table of Contents

1. [How the pipeline works (plain English)](#how-the-pipeline-works-plain-english)
2. [Project phases at a glance](#project-phases-at-a-glance)
3. [Project guide](#project-guide)
4. [First-time setup](#first-time-setup)
5. [The commands you need to know](#the-commands-you-need-to-know)
6. [Phase 2 — Build the corpus](#phase-2--build-the-corpus)
7. [Phase 3 — Summarize and evaluate](#phase-3--summarize-and-evaluate)
8. [Phase 4 — Scenarios and provenance](#phase-4--scenarios-and-provenance)
9. [Phase 5 — Human validation](#phase-5--human-validation)
10. [Phase 6 — Publication reporting](#phase-6--publication-reporting)
11. [Understanding the folders](#understanding-the-folders)
12. [Troubleshooting](#troubleshooting)
13. [Optional / advanced commands](#optional--advanced-commands)
14. [Corpus design](#corpus-design)
15. [Key environment variables (.env)](#key-environment-variables-env)
16. [Design decisions](#design-decisions)
17. [Glossary](#glossary)

---

## How the pipeline works (plain English)

Think of the project as an **assembly line** with six stations. Each station writes files to disk before the next one starts, so you never lose progress if something fails.

```
PHASE 2 — CORPUS
[CrossRef API]              [Open-access sources]          [Your computer]
      ↓                             ↓                              ↓
  collect.py          →        download.py           →         data/raw/
  (builds a list)           (grabs free PDFs)             (your PDF collection)
      ↓                             ↓
 manifest.jsonl               pipeline.py
 (catalogue of                (scoreboard:
  known papers)               how close to 250?)

When automatic download isn't enough (many papers are paywalled), you download them yourself from the library and run:

data/incoming_manuals/    →    auto_ingest_workflow.py    →    data/raw/
(you drop PDFs here)           (matches, renames, moves)       (accepted PDFs)


PHASE 3 — SUMMARIZE & EVALUATE
data/raw/*.pdf    →    extract    →    data/raw_text/ + data/processed/
                              ↓
                         summarize    →    data/summaries.jsonl
                              ↓
                         evaluate     →    data/evaluations.jsonl
                              ↓
                         eval-report  →    data/results/


PHASE 4 — SCENARIOS (optional helpers around Phase 3)
pipeline.py --scenario    →    reusable paper-selection rules
pipeline.py --use-rubric  →    free offline sanity check (not the real judge)
run_manifest              →    provenance record for every evaluation run


PHASE 5 — HUMAN VALIDATION
export-human-review   →   (vets fill blind CSVs)   →   ingest-human-review
                                                              ↓
                                                    data/human_reviews.jsonl


PHASE 6 — PUBLICATION
eval-report --publication   →   tables + stats (JSON, Markdown, CSV)
report-figures              →   charts + leaderboard (PNG/SVG)
```

**Golden rule for scores:** Phase 3's blind LLM jury (`llm-sum/evaluator.py` → `data/evaluations.jsonl`) is the **authoritative score** for this study. Everything in Phases 4–6 is a helper around that — none of it replaces or outranks the real judge.

---

## Project phases at a glance

| Phase | What it does | Main command | Key output |
|-------|-------------|--------------|------------|
| **2 — Corpus** | Collect metadata, download OA PDFs, fill gaps manually | `python pipeline.py` | `data/raw/*.pdf` (250 target) |
| **3 — Summarize & evaluate** | Extract text, run 3 LLMs, blind-judge every summary | `python llm-sum/run_phase3.py evaluate` | `data/evaluations.jsonl` |
| **4 — Scenarios** | Named paper-selection rules, offline rubric, run manifests | `python pipeline.py --list-scenarios` | `data/rubric_scores.jsonl`, run manifests |
| **5 — Human validation** | Export blind review packets, ingest vet scoresheets | `python llm-sum/run_phase3.py export-human-review` | `data/human_reviews.jsonl` |
| **6 — Publication** | Bootstrap CIs, significance tests, figures | `python llm-sum/run_phase3.py eval-report --publication` | `data/results/publication_report_*` |

All Phase 3+ commands live in [`llm-sum/`](llm-sum/) and are controlled by one safety knob: `PHASE3_MODE={test,single,dev,batch}` in `.env`. The default is `test` (free mocks, no API calls).

---

## Project guide

If you need a simple explanation of the project structure and methods, start here:

**[docs/GUIDE.md](docs/GUIDE.md)**

That guide explains how the folders connect, how data moves from PDF to summary to evaluation, why JSONL files are used, how OpenAI/Anthropic/Gemini are called, how the six-summary PDF-vs-JSONL comparison works, and how safety modes prevent accidental API spending.

**Phase-specific docs:**

| Phase | Doc |
|-------|-----|
| 3 | [docs/phase3/README.md](docs/phase3/README.md) — beginner walkthrough |
| 3 | [docs/phase3/run_phase3.md](docs/phase3/run_phase3.md) — full CLI reference |
| 3 | [docs/phase3/medhelm_evaluation.md](docs/phase3/medhelm_evaluation.md) — authoritative rubric and jury math |
| 4 | [docs/phase4/README.md](docs/phase4/README.md) — scenarios, offline rubric, run manifests |
| 5 | [docs/phase5/human_validation.md](docs/phase5/human_validation.md) — blind human review workflow |
| 6 | [docs/phase6/reporting.md](docs/phase6/reporting.md) — publication tables and figures |
| Stats | [docs/statistics_explained.md](docs/statistics_explained.md) — Friedman, Wilcoxon, bootstrap CIs in plain English |
| Booklet | [docs/booklet/BOOKLET.md](docs/booklet/BOOKLET.md) — full teaching booklet, start to finish, for a complete beginner (plus a standalone chapter for veterinarian reviewers) |

---

## First-time setup

```powershell
# 1. Clone the repo and enter it
git clone https://github.com/Mistical-ai/vet-llm-research.git
cd vet-llm-research

# 2. Create a virtual environment and activate it
python -m venv .venv
.venv\Scripts\activate          # Windows PowerShell

# 3. Install dependencies
pip install -r requirements.txt

# 4. Copy the environment template and fill it in
copy .env.template .env
# Open .env in any text editor:
#   - Set DRY_RUN=false  (use real network; keep =true just to practice)
#   - Set UNPAYWALL_EMAIL=your.name@uoguelph.ca  (required for CrossRef)
#   - Leave PHASE3_MODE=test until you are ready for paid API calls

# 5. Verify everything loaded correctly (optional)
python src/main.py
# Expected output: "Pipeline Initialized." — if you see this, you're ready.

# 6. Run the test suite (optional — all API calls are mocked)
pytest tests/ -q
```

---

## The commands you need to know

### Phase 2 — Corpus

| # | Command | What it does |
|---|---------|-------------|
| 1 | `python src/collect.py` | Build the paper list |
| 2 | `python src/download.py` | Auto-download open-access PDFs |
| 3 | `python src/supplement.py` | See what's still missing |
| 4 | `python src/auto_ingest_workflow.py` | Add manual PDFs to the corpus |
| 5 | `python pipeline.py` | Scoreboard — how close to 250? |

### Phase 3–6 — Summarize, evaluate, report

| # | Command | What it does |
|---|---------|-------------|
| 1 | `python llm-sum/run_phase3.py extract` | PDF → cleaned text in `data/processed/` |
| 2 | `python llm-sum/run_phase3.py summarize` | Run OpenAI, Anthropic, Gemini on each paper |
| 3 | `python llm-sum/run_phase3.py evaluate` | Blind LLM jury scores every summary |
| 4 | `python llm-sum/run_phase3.py eval-report` | Stratified scoreboard + save to `data/results/` |
| 5 | `python llm-sum/run_phase3.py eval-report --publication` | Paper-ready tables with stats (Phase 6) |
| 6 | `python llm-sum/run_phase3.py report-figures` | Charts and leaderboard (Phase 6) |
| 7 | `python llm-sum/run_phase3.py export-human-review` | Blind review packets for vets (Phase 5) |
| 8 | `python llm-sum/run_phase3.py ingest-human-review` | Load filled vet scoresheets (Phase 5) |

**Quick dev test (six summaries, one paper, PDF vs processed text):**

```powershell
python llm-sum/run_phase3.py summarize-all --mode single
```

Use the hyphenated subcommand `summarize-all` (not `summarize all`). Free dry run: add `--mode test`.

---

## Phase 2 — Build the corpus

### Step 1 — Build the paper list

```powershell
python src/collect.py
```

**What it does:** Asks CrossRef for papers from each of the 5 journals published between 2023 and 2026. Writes one line per paper to `data/manifest.jsonl`.

**What you'll have after:** A `data/manifest.jsonl` file with thousands of candidate papers. No PDFs yet.

**Important note:** Running `collect.py` again will **add** more entries — it never deletes. If you want to start fresh, delete `data/manifest.jsonl` first.

---

### Step 2 — Auto-download open-access PDFs

```powershell
python src/download.py
```

**What it does:** Goes through every paper in `data/manifest.jsonl`, tries up to 6 legal open-access sources for each one, validates each PDF, and saves passing PDFs to `data/raw/`. Stops at 50 PDFs per journal.

**The 6 sources it tries (in order):**
1. Unpaywall API
2. PubMed Central
3. Semantic Scholar
4. fulltext-article-downloader tool
5. Publisher-direct URLs (Wiley, AVMA, SAGE)
6. HTML scraping

**The 5 validation checks every PDF must pass:**
1. Starts with `%PDF` magic bytes
2. Has at least 3 pages
3. Has extractable text (not a scanned image)
4. Has at least 3 of: Introduction, Methods, Results, Discussion, Conclusion
5. Has at least 3,000 words after removing the reference list

**What you'll have after:**
- `data/raw/` — PDFs that passed all checks
- `data/error_log.jsonl` — details on every failed attempt
- `data/missing_papers.csv` — papers still needed (if any journal is under 50)

---

### Step 3 — Check your progress

```powershell
python pipeline.py
```

**What it does:** Counts every PDF in `data/raw/`, cross-references with the manifests, and prints a per-journal scoreboard.

**Exit codes:**
- Exit 0 = 200 or more primary PDFs (acceptable to proceed)
- Exit 1 = fewer than 200 primary PDFs — keep going, not a crash

---

### Step 4 — Find out what's still missing

```powershell
python src/supplement.py
```

**What it does:** Compares the manifest to `data/raw/` and generates a fresh `data/missing_papers.csv` with actionable suggestions. Shuffles randomly each run so you get variety.

---

### Step 5 — Add papers manually

When automatic download can't get a paper (usually paywalled), download it through the UoG library and:

1. Drop the PDF into `data/incoming_manuals/` (any filename is fine)
2. Run:

```powershell
python src/auto_ingest_workflow.py
```

**What it does automatically:**
- Retries any PDFs that failed on a previous run
- Looks up each PDF's DOI in CrossRef and adds missing entries to `manifest.jsonl`
- Matches, renames, and moves accepted PDFs to `data/raw/`
- Updates the scoreboard and missing-papers list

---

### Step 6 — Repeat until done

```
python src/download.py              → try more OA sources
python src/supplement.py            → see updated missing list
[download PDFs from library]
python src/auto_ingest_workflow.py  → add manual PDFs
python pipeline.py                  → check the scoreboard
```

Stop when `pipeline.py` shows 250/250 primary PDFs.

---

## Phase 3 — Summarize and evaluate

Once `data/raw/` has enough PDFs, Phase 3 turns them into LLM summaries and judge scores. Extraction is column-aware for two-column journals (JVIM, VRU), writes raw text to `data/raw_text/`, then writes cleaned body text to `data/processed/` with references and publisher noise removed.

### The four steps (always in this order)

```
extract  →  summarize  →  evaluate  →  eval-report
```

```powershell
python llm-sum/run_phase3.py extract
python llm-sum/run_phase3.py summarize
python llm-sum/run_phase3.py evaluate
python llm-sum/run_phase3.py eval-report
```

| Step | Input | Output |
|------|-------|--------|
| `extract` | `data/raw/*.pdf` | `data/raw_text/*.jsonl`, `data/processed/*.jsonl` |
| `summarize` | `data/processed/*.jsonl` | `data/summaries.jsonl` |
| `evaluate` | `data/summaries.jsonl` | `data/evaluations.jsonl` (append-only) |
| `eval-report` | `data/evaluations.jsonl` | Terminal scoreboard + `data/results/eval_report_*.json` |

### Safety mode (`PHASE3_MODE`)

| Mode | API calls | Use for |
|------|-----------|---------|
| `test` (default) | None — mocks only | Learning the pipeline, running `pytest` |
| `single` | Real, one paper | First live test |
| `dev` | Real, small batch | Development runs |
| `batch` | Real, full corpus via batch APIs | Production run |

Every paid mode asks for confirmation before calling APIs. Set `PHASE3_MODE=test` in `.env` until you are ready to spend money.

**Dev mode is an incremental loop.** `summarize --mode dev` picks one random paper per journal and writes readable `.txt` files to `data/dev_summaries_jsonl/`; `evaluate --mode dev` then judges exactly those papers and writes readable scores to `data/dev_evals_jsonl/`. Both stages skip papers already done, so re-running grows the sample paper-by-paper. Full step-by-step: [docs/phase3/dev_evaluation_guide.md](docs/phase3/dev_evaluation_guide.md).

### PDF vs processed text comparison (`summarize-all`)

The primary dev command runs **six summaries** from one matched article — 3 providers × 2 input sources:

| Source | Input folder | Summaries |
|--------|--------------|-----------|
| Raw PDF | `data/raw/` | 3 |
| Processed JSONL | `data/processed/` | 3 |
| **Total** | | **6** |

```powershell
python llm-sum/run_phase3.py summarize-all --mode single
```

Outputs land in readable text files:

```text
data/summaries_pdf/<matched-article-stem>.txt
data/summaries_txt/<matched-article-stem>.txt
```

### The blind jury

Every summary is scored by an LLM judge that **never sees which model wrote it**. The judge scores five criteria on a 1–5 scale:

1. **Faithfulness** — facts match the source paper
2. **Completeness** — key findings are present
3. **Clinical usefulness** — a vet could act on this
4. **Clarity** — well-organized and readable
5. **Safety** — no dangerous omissions or fabrications

The judge also flags hallucinations (claims not supported by the source). Full rubric definition: **[docs/phase3/medhelm_evaluation.md](docs/phase3/medhelm_evaluation.md)**.

### Verify extraction before spending money

```powershell
python llm-sum/run_phase3.py extract
python scripts/verify_extraction.py --limit 20
```

`WARN` can be legitimate for short communications and case reports. `FAIL` needs investigation before any paid summaries.

---

## Phase 4 — Scenarios and provenance

Phase 4 adds three optional helpers around Phase 3. None of them replace the blind judge.

| Helper | What it solves | Command |
|--------|---------------|---------|
| **Scenarios** | Reusable, named paper-selection rules | `python pipeline.py` (default: `primary_research_corpus`) |
| **Offline rubric** | Free sanity check before paying the judge | `python pipeline.py --use-rubric` |
| **Run manifests** | Record exactly what produced each evaluation run | Written automatically before every `evaluate` run |

List all scenarios:

```powershell
python pipeline.py --list-scenarios
```

Full details: **[docs/phase4/README.md](docs/phase4/README.md)**

---

## Phase 5 — Human validation

Before trusting LLM jury scores at scale, we check them against human experts. The workflow is three commands:

```text
export-human-review  →  (vets fill blind CSVs)  →  ingest-human-review  →  eval-report
```

```powershell
# 1. Export blind review packets (no model names in the files)
python llm-sum/run_phase3.py export-human-review

# 2. After vets return filled scoresheets:
python llm-sum/run_phase3.py ingest-human-review

# 3. See human-vs-jury agreement in the eval report
python llm-sum/run_phase3.py eval-report --markdown
```

Reviewers score the **same five criteria** the jury uses, so agreement is measured on the same construct. Inter-reviewer reliability uses **Krippendorff's α**.

Full workflow: **[docs/phase5/human_validation.md](docs/phase5/human_validation.md)**

---

## Phase 6 — Publication reporting

One command turns evaluation rows into paper-ready quantitative results:

```powershell
python llm-sum/run_phase3.py eval-report --publication
```

**What it writes to `data/results/` (timestamped — nothing is overwritten):**

| Artifact | What it is |
|----------|-----------|
| `publication_report_<ts>.json` | Full report, machine-readable |
| `publication_report_<ts>.md` | Same tables in Markdown for a manuscript |
| `publication_report_<ts>_tables/` | One CSV per table for a stats package |

**Tables include:**
- Provider comparison (mean jury score, 95% bootstrap CI, cost, cost-per-quality-point)
- Significance tests (Friedman omnibus + pairwise Wilcoxon, separate for weighted and unweighted scores)
- Per-stratum breakdowns (species, study design, clinical topic, journal)
- Input-channel comparison (processed text vs direct PDF)
- Inter-judge reliability (Krippendorff's α)

**Figures and leaderboard:**

```powershell
python llm-sum/run_phase3.py report-figures
```

Writes PNG/SVG charts and a VetHELM-style leaderboard to `data/results/`.

Full details: **[docs/phase6/reporting.md](docs/phase6/reporting.md)** and **[docs/statistics_explained.md](docs/statistics_explained.md)**

---

## Understanding the folders

```
vet-llm-research/
├── data/
│   ├── manifest.jsonl            ← Paper catalogue (built by collect.py)
│   ├── manual_manifest.jsonl     ← Manually ingested papers
│   ├── summaries.jsonl           ← Phase 3: one row per paper, all provider summaries
│   ├── evaluations.jsonl         ← Phase 3: blind judge scores (append-only)
│   ├── human_reviews.jsonl       ← Phase 5: normalized vet scoresheets
│   ├── rubric_scores.jsonl       ← Phase 4: offline heuristic scores (optional)
│   ├── error_log.jsonl           ← All errors with details (never deleted)
│   ├── missing_papers.csv        ← What's still needed (rewritten each supplement run)
│   │
│   ├── raw/                      ← ✅ ACCEPTED PDFs (your corpus lives here)
│   ├── incoming_manuals/         ← 📥 DROP NEW PDFs HERE
│   ├── manual_inbox/             ← Staging area (auto_ingest_workflow.py manages this)
│   ├── quarantine/               ← PDFs moved out by audit_article_types.py
│   │
│   ├── raw_text/                 ← Phase 3: column-aware extracted text
│   ├── processed/                ← Phase 3: cleaned text for LLM summaries
│   ├── summaries_pdf/            ← Phase 3: summarize-all PDF-source outputs
│   ├── summaries_txt/            ← Phase 3: summarize-all processed-text outputs
│   ├── human_review/             ← Phase 5: exported blind review packets
│   ├── run_manifests/            ← Phase 4: provenance records per evaluation run
│   ├── results/                  ← Phase 3/6: timestamped eval and publication reports
│   └── logs/
│       └── auto_ingest_workflow.log
│
├── src/                          ← Phase 2: collect, download, extract, scenarios
│   ├── collect.py
│   ├── download.py
│   ├── supplement.py
│   ├── auto_ingest_workflow.py
│   ├── extract.py
│   ├── scenarios/                ← Phase 4: named paper-selection rules
│   └── evaluation/               ← Phase 4: offline rubric scoring
│
├── llm-sum/                      ← Phase 3–6: summarize, judge, report, human review
│   ├── run_phase3.py             ← Main orchestrator (all subcommands)
│   ├── summarizer.py
│   ├── evaluator.py
│   ├── eval_report.py
│   ├── report_tables.py          ← Phase 6: publication tables
│   ├── report_figures.py         ← Phase 6: charts and leaderboard
│   ├── human_review.py           ← Phase 5: export + ingest
│   ├── reliability.py            ← Krippendorff's α and agreement stats
│   └── prompts/                  ← Summarizer and judge prompt templates
│
├── scripts/
│   ├── verify_extraction.py      ← Audit extraction quality before paid runs
│   └── migrate_processed_filenames.py
│
├── tests/                        ← pytest suite (all API calls mocked)
├── docs/                         ← Phase guides and rubric definitions
├── pipeline.py                   ← Corpus scoreboard + scenario CLI
├── .env                          ← Your settings (never commit this)
└── .env.template                 ← Settings reference
```

---

## Troubleshooting

### Phase 2 — Failed PDFs

**A PDF in `incoming_manuals/` ended up in `failed/`:**

```powershell
Select-String -Path data\error_log.jsonl -Pattern "your_filename_or_doi"
python src/auto_ingest_workflow.py   # failed PDFs are automatically retried
```

| Error in log | Meaning | Fix |
|-------------|---------|-----|
| `pdfplumber metadata read failed` | Scanned image or corrupt PDF | Download a text-based PDF |
| `CrossRef 404` | DOI not in CrossRef | Verify the DOI on CrossRef.org |
| `No DOI found` | No DOI in metadata, filename, or page 1 | Rename file: `10.1177_1098612X231170159.pdf` |

**`pipeline.py` exits with code 1:** You haven't reached 200 primary PDFs yet — keep going.

**`supplement.py` keeps showing the same papers:** Run it again — it shuffles randomly each time.

### Phase 3 — Extraction and evaluation

**`verify_extraction.py` shows FAIL:** Compare `data/raw_text/<name>.jsonl` against `data/processed/<name>.jsonl`. If Methods/Results/Discussion disappeared, reference stripping needs adjustment. If raw text is tiny, the PDF is probably scanned.

**`eval-report` says zero rows:** Run `evaluate` first so `data/evaluations.jsonl` has at least one row.

**Accidental API spending:** Set `PHASE3_MODE=test` in `.env`. The default is already `test` — you must deliberately switch to a paid mode.

---

## Optional / advanced commands

### Clean up article types in `data/raw/`

```powershell
python src/audit_article_types.py              # preview only
python src/audit_article_types.py --remove --tag-secondary   # move excluded types to quarantine
```

### Phase 4 offline rubric (free smoke test)

```powershell
python pipeline.py --use-rubric
```

### Phase 3 batch API (full corpus, 50% cheaper)

```powershell
python llm-sum/run_phase3.py summarize --mode batch
python llm-sum/check_batch_status.py
```

### Cost estimate before a live run

```powershell
python llm-sum/cost_estimator.py
```

### Run supplement with a fixed seed

```powershell
python src/supplement.py --seed 42
```

---

## Corpus design

**Target:** 250 papers — 50 from each of 5 journals

| Journal | Full name | ISSN | Target |
|---------|-----------|------|--------|
| JVIM | Journal of Veterinary Internal Medicine | 1939-1676 | 50 |
| JAVMA | Journal of the American Veterinary Medical Association | 0003-1488 | 50 |
| Veterinary Surgery | Veterinary Surgery | 0161-3499 | 50 |
| VRU | Veterinary Radiology & Ultrasound | 1058-8183 | 50 |
| JFMS | Journal of Feline Medicine and Surgery | 1098-612X | 50 |

**Date range:** 2023-01-01 to 2026-12-31

**Primary vs. secondary PDFs:**
- **Primary** (count toward quota): original research articles
- **Secondary** (`2_` prefix, not in quota): systematic reviews, meta-analyses

**Article types excluded:** case reports, short communications, brief reports, imaging diagnoses

**Minimum threshold:** `pipeline.py` exits 0 when you have ≥ 200 primary PDFs.

---

## Key environment variables (.env)

Copy `.env.template` to `.env` and fill in your values. The most important ones:

| Variable | Default | Description |
|----------|---------|-------------|
| `DRY_RUN` | `true` | Set to `false` for real network calls in Phase 2 |
| `UNPAYWALL_EMAIL` | *(required)* | Your email — required for CrossRef and Unpaywall |
| `PHASE3_MODE` | `test` | `test` / `single` / `dev` / `batch` — controls all Phase 3 API calls |
| `COLLECT_YEARS` | `2023,2024,2025,2026` | Which years to query |
| `MIN_EXTRACTED_WORDS` | `3000` | Reject PDFs with fewer words after removing references |
| `PDFPLUMBER_USE_TEXT_FLOW` | `true` | Preserve reading order in two-column PDFs |
| `PUBLICATION_BOOTSTRAP_RESAMPLES` | `2000` | Bootstrap resamples for Phase 6 confidence intervals |
| `HUMAN_REVIEW_SAMPLE_SIZE` | `15` | How many summaries to export for Phase 5 |

Full list in `.env.template`.

---

## Design decisions

**Why CrossRef instead of PubMed?**
CrossRef covers all 5 target journals with structured metadata. PubMed's E-utils are used as a download fallback (PMC), not as the primary catalogue.

**Why JSONL everywhere?**
Each line is an independent JSON object. If one line is corrupted, the rest of the file is still readable. Append-only logs (`evaluations.jsonl`, `error_log.jsonl`) preserve full audit history.

**Why blind judging?**
If the judge knew which model wrote a summary, it could play favourites. Blinding both the LLM jury and human reviewers keeps scores independent and defensible.

**Why three LLM providers?**
OpenAI, Anthropic, and Gemini use different architectures and training data. Running all three under identical prompts isolates provider quality from prompt quality.

**Why separate `raw_text/` and `processed/`?**
Raw text is the faithful PDF extraction. Processed text has references and publisher noise removed. Keeping both lets you compare "what the PDF actually says" vs "what we send to the LLM."

**Why append-only `evaluations.jsonl`?**
Re-running the judge never deletes old scores. You can always trace which run produced which row via the run manifest.

**Why retry failed PDFs automatically?**
A PDF can fail because its DOI wasn't in the manifest yet, then succeed after enrichment adds the entry. Automatic retry eliminates a manual step.

---

## Glossary

| Term | Meaning |
|------|---------|
| **API** | Application Programming Interface — a way for two programs to talk over the internet. CrossRef, Unpaywall, and the LLM providers are all accessed via APIs. |
| **Append-only** | A file that is only ever added to, never overwritten. `evaluations.jsonl` and `error_log.jsonl` work this way so audit history is never lost. |
| **Batch API** | A cheaper, slower OpenAI/Anthropic mode where you submit many requests at once and collect results later (~50% off). Used for full-corpus runs via `PHASE3_MODE=batch`. |
| **Blind protocol** | The judge (and human reviewers) never see which LLM wrote a summary. This prevents expectation bias. Enforced in `evaluator.py` and `human_review.py`. |
| **Bootstrap CI** | A statistical method that resamples your data thousands of times to estimate a 95% confidence interval without assuming a normal distribution. Used in Phase 6 publication tables. |
| **BudgetGuard** | A safety class in `src/utils.py` that tracks total API spend and calls `sys.exit()` if a hard budget limit is exceeded. |
| **Candidate** | A paper in `manifest.jsonl` that has not yet been downloaded. |
| **CrossRef** | The non-profit registry that assigns DOIs for academic journals. Used by `collect.py` to find paper metadata. |
| **DOI** | Digital Object Identifier — a permanent link to a paper, e.g. `10.1111/jvim.12345`. |
| **DRY_RUN** | When `DRY_RUN=true`, Phase 2 scripts skip real network calls and use mock data. Safe for testing. |
| **Enrich (manifest enrichment)** | `enrich_manifest_from_pdfs.py` looks up unknown DOIs in CrossRef and adds them to `manifest.jsonl` so ingest can match PDFs. |
| **eval-report** | Read-only command that summarizes `data/evaluations.jsonl` by model, species, study design, and other strata. Saves timestamped snapshots to `data/results/`. |
| **Evaluation instance** | One (paper × input channel × summary) row that the blind judge scores. A paper judged from both PDF and processed text counts as two instances. |
| **Exit code** | A number a program returns when it finishes. 0 = success; 1 = something needs attention (not necessarily a crash). |
| **Friedman test** | A non-parametric test for whether provider scores differ across the whole corpus. Used in Phase 6 significance tables. |
| **Hallucination** | A claim in a summary that is not supported by the source paper. The judge flags these explicitly. |
| **Human validation** | Phase 5 workflow where veterinarians score a sample of summaries on the same rubric as the LLM jury, to check whether jury scores track expert judgment. |
| **ISSN** | International Standard Serial Number — a journal's unique identifier. |
| **JSONL** | JSON Lines — a text file where each line is one JSON object. Corruption-resistant and append-safe. |
| **Jury score** | The mean of a summary's five criterion scores (faithfulness, completeness, clinical usefulness, clarity, safety). Computed in both weighted and unweighted modes. |
| **Krippendorff's α (alpha)** | A chance-corrected inter-rater reliability statistic. α = 1 means perfect agreement; α = 0 means no better than chance. Used for inter-judge and inter-reviewer agreement. |
| **LLM** | Large Language Model — an AI like GPT-4, Claude, or Gemini. This pipeline compares how well different LLMs summarize veterinary papers. |
| **magic bytes** | The first few bytes of a file that identify its type. A real PDF always starts with `%PDF`. |
| **manifest.jsonl** | The catalogue file. Each line is one paper (DOI, title, journal, year, authors, abstract, covariates). |
| **MedHELM** | A medical-LLM evaluation framework from Stanford. We borrowed its habits (named benchmarks, versioned rubrics, stratified reporting) without importing its code. |
| **OA / open access** | A paper that can be legally downloaded for free. The pipeline only auto-downloads OA papers. |
| **Offline rubric** | A free, non-AI heuristic scorer (`src/evaluation/rubric_scoring.py`) that checks word overlap and keyword presence. A smoke test only — not the real judge. |
| **Paywall** | A barrier requiring a journal subscription. Those papers must be obtained through the library. |
| **PHASE3_MODE** | Single safety knob controlling all Phase 3 API behavior: `test` (mocks), `single` (one paper), `dev` (small batch), `batch` (full corpus). |
| **PMC / PubMed Central** | NIH's free archive of biomedical papers. A common OA download source. |
| **Primary PDF** | An original research article counting toward the 50-per-journal quota. |
| **Processed text** | Cleaned body text in `data/processed/` with references and publisher noise removed. Default input for summarization. |
| **Provider** | One of the three LLM backends: OpenAI, Anthropic, or Gemini. |
| **Publication report** | Phase 6 output with provider comparison tables, bootstrap CIs, significance tests, and per-stratum breakdowns. Written by `eval-report --publication`. |
| **Quarantine** | `data/quarantine/` — where `audit_article_types.py` moves PDFs that shouldn't be in `data/raw/`. |
| **Raw text** | Faithful column-aware PDF extraction in `data/raw_text/`, before reference stripping. |
| **Run manifest** | A JSON file written before every `evaluate` run recording code version, prompt version, model IDs, and dataset snapshot. Enables diffing two runs. |
| **Scenario** | A named, reusable paper-selection rule in `src/scenarios/`. Example: `primary_research_corpus` applies the journal quota and primary/secondary classification. |
| **Secondary PDF** | A review article tagged with `2_` prefix — kept but not counted toward quota. |
| **Seed** | A fixed random number (`seed=42`) that makes LLM outputs reproducible where the provider supports it. |
| **Stratified reporting** | Breaking down scores by research covariates (species, study design, clinical topic, journal) so you can see whether one provider wins everywhere or only in certain areas. |
| **Stop-loss** | A safety limit preventing infinite downloading. `MAX_FAILED_PER_JOURNAL=300` gives up on a journal after 300 consecutive failures. |
| **Summarize-all** | Dev command that runs 6 summaries (3 providers × 2 input sources) on one matched article for PDF-vs-processed comparison. |
| **Supplement** | Manually filling gaps that `download.py` couldn't fill. `supplement.py` generates the shopping list. |
| **Taxonomy** | `vet_taxonomy_v1` in `src/scenarios/taxonomy.py` — a versioned category tree for grouping evaluation tasks (species, study design, clinical topic). |
| **Temperature** | An LLM setting controlling randomness. This pipeline always uses `temperature=0.0` for reproducibility. |
| **Unpaywall** | A database tracking legal open-access locations for papers. `download.py` uses it as its first download source. |
| **Virtual environment** | An isolated Python installation. `.venv\Scripts\activate` activates it. |
| **VetHELM** | This project's name for its MedHELM-inspired evaluation layer: blind jury, stratified reporting, publication tables, and human validation. |
| **Wilcoxon signed-rank test** | A paired non-parametric test comparing two providers on the same papers. Used in Phase 6 pairwise significance tables. |
| **Word-count gate** | Validation rule rejecting PDFs with fewer than 3,000 extracted words. Prevents short articles from entering the corpus. |
