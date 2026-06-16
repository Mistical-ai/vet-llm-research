# Veterinary LLM Research Pipeline

**Goal:** Collect 250 veterinary journal papers (50 from each of 5 journals, 2023–2026), download open-access PDFs, clean the text, and prepare for LLM summarization and evaluation.

**Project context:** OVC Pet Trust Summer Studentship, 2026.

---

## Table of Contents

1. [How the pipeline works (plain English)](#how-the-pipeline-works-plain-english)
2. [Project guide](#project-guide)
3. [First-time setup](#first-time-setup)
4. [The 4 commands you need to know](#the-4-commands-you-need-to-know)
5. [Step-by-step guide](#step-by-step-guide)
   - [Step 1 — Build the paper list](#step-1--build-the-paper-list)
   - [Step 2 — Auto-download open-access PDFs](#step-2--auto-download-open-access-pdfs)
   - [Step 3 — Check your progress](#step-3--check-your-progress)
   - [Step 4 — Find out what's still missing](#step-4--find-out-whats-still-missing)
   - [Step 5 — Add papers manually](#step-5--add-papers-manually)
   - [Step 6 — Repeat until done](#step-6--repeat-until-done)
6. [Understanding the folders](#understanding-the-folders)
7. [Troubleshooting failed PDFs](#troubleshooting-failed-pdfs)
8. [Optional / advanced commands](#optional--advanced-commands)
9. [Corpus design](#corpus-design)
10. [All environment variables (.env)](#all-environment-variables-env)
11. [Design decisions](#design-decisions)
12. [Glossary](#glossary)

---

## How the pipeline works (plain English)

Think of the pipeline as a **post-office assembly line** with three main stations:

```
[CrossRef API]              [Open-access sources]          [Your computer]
      ↓                             ↓                              ↓
  collect.py          →        download.py           →         data/raw/
  (builds a list)           (grabs free PDFs)             (your PDF collection)
      ↓                             ↓
 manifest.jsonl               pipeline.py
 (catalogue of                (scoreboard:
  known papers)               how close to 250?)
```

When automatic download isn't enough (many papers are paywalled), you download them yourself from the library and run:

```
data/incoming_manuals/    →    auto_ingest_workflow.py    →    data/raw/
(you drop PDFs here)           (matches, renames, moves)       (accepted PDFs)
```

The workflow script is smart: before trying to match each PDF, it looks up the paper's DOI in CrossRef and adds it to the catalogue automatically. This means PDFs that used to get stuck in a `failed/` folder will now be matched and accepted.

### Phase 3 (LLM summarisation & evaluation)

Once `data/raw/` is full, Phase 3 turns the PDFs into LLM summaries and judge scores. Extraction is column-aware for two-column journals such as JVIM and VRU, writes raw extracted text to `data/raw_text/`, then writes cleaned body text to `data/processed/` with publisher noise and references removed.

**Primary command right now — six summaries from one matched article (PDF + processed JSONL):**

```powershell
python llm-sum/run_phase3.py summarize-all --mode single
```

Use the hyphenated subcommand `summarize-all` (not `summarize all`). The script finds one article stem that exists in both `data/raw/*.pdf` and `data/processed/*.jsonl`, then runs OpenAI, Anthropic, and Gemini on each source:

| Source | Input folder | Summaries |
|--------|--------------|-----------|
| Raw PDF | `data/raw/` | 3 |
| Processed JSONL | `data/processed/` | 3 |
| **Total** | | **6** |

Outputs land in readable text files (not `summaries.jsonl`):

```text
data/summaries_pdf/<matched-article-stem>.txt
data/summaries_txt/<matched-article-stem>.txt
```

Same six-summary default in dev mode: `python llm-sum/run_phase3.py summarize-all --mode dev`. Free dry run: add `--mode test`.

All Phase 3 code lives in [`llm-sum/`](llm-sum/) and is controlled by a single `PHASE3_MODE={test,single,dev,batch}` knob in `.env`. Full command reference: **[docs/phase3/run_phase3.md](docs/phase3/run_phase3.md)**. Beginner walkthrough: **[docs/phase3/README.md](docs/phase3/README.md)**.

---

## Project guide

If you need a simple explanation of the project structure and methods, start here:

**[docs/GUIDE.md](docs/GUIDE.md)**

That guide explains:

- how the folders connect,
- how data moves from PDF to text to summary to evaluation,
- why JSONL files are used,
- how OpenAI, Anthropic, and Gemini are called,
- how the six-summary PDF-vs-JSONL comparison works,
- how the format guide summary is used without copying facts,
- how safety modes prevent accidental API spending,
- and how to describe the project in a meeting.

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

# 5. Verify everything loaded correctly (optional)
python src/main.py
# Expected output: "Pipeline Initialized." — if you see this, you're ready.
```

---

## The 4 commands you need to know

| # | Command | What it does |
|---|---------|-------------|
| 1 | `python src/collect.py` | Build the paper list |
| 2 | `python src/download.py` | Auto-download open-access PDFs |
| 3 | `python src/supplement.py` | See what's still missing |
| 4 | `python src/auto_ingest_workflow.py` | Add manual PDFs to the corpus |

Run `python pipeline.py` at any time to see the scoreboard.

---

## Step-by-step guide

### Step 1 — Build the paper list

```powershell
python src/collect.py
```

**What it does:** Asks CrossRef (a public academic database) for papers from each of the 5 journals published between 2023 and 2026. Writes one line per paper to `data/manifest.jsonl` — this is your catalogue of known papers.

**What you'll see:**
```
[collect] JVIM: querying years 2023, 2024, 2025, 2026 ...
[collect] JVIM 2023: 487 candidates
[collect] JAVMA: querying years ...
...
[collect] Collection complete. Manifest contains 8,432 new entries.
```

**What you'll have after:** A `data/manifest.jsonl` file with thousands of candidate papers. No PDFs yet.

**Important note:** Running `collect.py` again will **add** more entries — it never deletes. If you want to start fresh, delete `data/manifest.jsonl` first.

**Settings you can change in `.env`:**
| Variable | Default | What it does |
|----------|---------|-------------|
| `DRY_RUN` | `true` | Set to `false` for real network calls |
| `COLLECT_CANDIDATES_PER_JOURNAL` | `2000` | How many candidates per journal to collect |
| `COLLECT_YEARS` | `2023,2024,2025,2026` | Which years to include |
| `UNPAYWALL_EMAIL` | *(required)* | Your email — needed for CrossRef polite-pool access |

---

### Step 2 — Auto-download open-access PDFs

```powershell
python src/download.py
```

**What it does:** Goes through every paper in `data/manifest.jsonl`, tries up to 6 legal open-access sources for each one, validates each PDF (right format? enough text? proper structure?), and saves passing PDFs to `data/raw/`. Stops at 50 PDFs per journal.

**The 6 sources it tries (in order):**
1. Unpaywall API (largest OA index)
2. PubMed Central (NIH's free archive)
3. Semantic Scholar
4. fulltext-article-downloader tool
5. Publisher-direct URLs (Wiley, AVMA, SAGE)
6. HTML scraping (follows the DOI link, looks for a PDF button)

**The 5 validation checks every PDF must pass:**
1. Starts with the `%PDF` magic bytes (confirms it's a real PDF, not a login page)
2. Has at least 3 pages
3. Has extractable text (not a scanned image)
4. Has at least 3 of: Introduction, Methods, Results, Discussion, Conclusion
5. Has at least 3,000 words after removing the reference list

**What you'll see:**
```
[download] JVIM: 10.1111/jvim.16872 → Unpaywall OK (direct PDF)
[download] JVIM: 10.1111/jvim.16858 → Cloudflare blocked — try library
[download] JVIM: 10.1111/jvim.16918 → PMC OK
...
[download] JVIM: 50/50 ✓  JAVMA: 32/50  Vet Surgery: 45/50  VRU: 41/50  JFMS: 28/50
[download] Complete. data/missing_papers.csv updated.
```

**What you'll have after:**
- `data/raw/` — PDFs that passed all checks
- `data/error_log.jsonl` — details on every failed attempt
- `data/missing_papers.csv` — list of papers still needed (if any journal is under 50)

**If many papers fail with "Cloudflare blocked":** That's normal. Publisher websites block automated tools. Those papers need to be downloaded manually through the library (see Step 5).

---

### Step 3 — Check your progress

```powershell
python pipeline.py
```

**What it does:** Counts every PDF in `data/raw/`, cross-references with `data/manifest.jsonl` and `data/manual_manifest.jsonl`, and prints a scoreboard.

**What you'll see:**
```
====================================================================
  CORPUS STATUS
====================================================================
  PDFs confirmed — primary:          147  (count toward quota)
  PDFs confirmed — secondary (2_):    12  (reviews; not in quota)

  Journal                  Target  Primary  Secondary  Status
  ——————————————————————————————————————————————————————————————
  JVIM                        50      50         4     ✓ OK
  JAVMA                       50      32         2     NEED 18 MORE PRIMARY
  Veterinary Surgery          50      28         3     NEED 22 MORE PRIMARY
  VRU                         50      21         2     NEED 29 MORE PRIMARY
  JFMS                        50      16         1     NEED 34 MORE PRIMARY
====================================================================
```

**Exit codes:**
- Exit 0 (success) = 200 or more primary PDFs
- Exit 1 (warning) = fewer than 200 primary PDFs — not a crash, just means you're not done yet

---

### Step 4 — Find out what's still missing

```powershell
python src/supplement.py
```

**What it does:** Compares the manifest to `data/raw/` and generates a fresh list of papers still needed for each journal. Rewrites `data/missing_papers.csv` with different suggestions each time (randomly shuffled so you get variety).

**What you'll see:**
```
[supplement] Per-journal acquisition status:
  Journal                    Have  Target  Missing
  --------------------------------------------------
  JVIM                         50      50        0
  JAVMA                        32      50       18
  Veterinary Surgery           28      50       22
  ...

  DOI     : 10.1177/1098612X231170159
  Title   : Feline hypertrophic cardiomyopathy in 847 cats...
  Journal : JFMS
  -> Check University of Guelph library access (https://lib.uoguelph.ca)
  -> Email corresponding author for preprint / accepted manuscript
  ...
[supplement] Missing papers report written: data/missing_papers.csv
```

**What you'll have after:** A fresh `data/missing_papers.csv` with actionable suggestions. Open it in Excel or any spreadsheet app.

**Options:**
```powershell
python src/supplement.py           # different suggestions every run (recommended)
python src/supplement.py --seed 42 # same suggestions every run (for reproducibility)
```

---

### Step 5 — Add papers manually

When automatic download can't get a paper (usually because it's paywalled), you can download it yourself through the University of Guelph library and add it to the corpus.

#### The recommended way (one command does everything)

**1. Download the PDF** from the UoG library or by emailing the author.

**2. Drop the PDF into `data/incoming_manuals/`** — any filename is fine.

**3. Run:**
```powershell
python src/auto_ingest_workflow.py
```

**What it does automatically (in order):**

| Step | What happens |
|------|-------------|
| 1. Retry failures | Any PDFs that failed on a previous run are automatically tried again |
| 2. Stage new files | Moves PDFs from `data/incoming_manuals/` → `data/manual_inbox/` |
| 3. Enrich manifest | Reads each PDF's DOI, looks it up in CrossRef, adds missing entries to `data/manifest.jsonl` |
| 4. Ingest | Matches PDFs to manifest entries, renames them properly, moves to `data/raw/` |
| 5. Update scoreboard | Runs `pipeline.py` to show updated counts |
| 6. Refresh missing list | Runs `supplement.py` to update `data/missing_papers.csv` |
| 7. Clean up | Archives any remaining failures to `data/manual_inbox/archive_failed/YYYY-MM-DD/` |

**What you'll see:**
```
[2026-05-19T14:32:01Z] === auto_ingest_workflow start ===
[2026-05-19T14:32:01Z] retry_failed: returned 3 PDF(s) to inbox for retry.
[2026-05-19T14:32:01Z] stage_incoming: moved paper1.pdf → manual_inbox/paper1.pdf
[2026-05-19T14:32:02Z] enrich_manifest: starting pre-ingest CrossRef enrichment…
[enrich] (1/4) paper1.pdf
[enrich]   DOI found via filename: 10.1177/1098612x231170159
[enrich]   Querying CrossRef for 10.1177/1098612x231170159 …
[enrich]   Appended: 10.1177/1098612x231170159 | JFMS | 2023
...
[manual_ingest] IMPORTED paper1.pdf → JFMS__Feline HCM__10_1177_1098612X231170159.pdf
[2026-05-19T14:32:15Z] === auto_ingest_workflow complete (retried=3, staged=1) pipeline_rc=0 ===
```

**What you'll have after:**
- New PDFs in `data/raw/` with proper descriptive names
- Log file: `data/logs/auto_ingest_workflow.log`
- Anything that still couldn't be matched: `data/manual_inbox/failed/` (with a reason in `data/error_log.jsonl`)

**Options:**
```powershell
python src/auto_ingest_workflow.py                    # standard run
python src/auto_ingest_workflow.py --no-supplement    # skip refreshing missing_papers.csv
python src/auto_ingest_workflow.py --no-clean         # keep failed/ folder intact for inspection
python src/auto_ingest_workflow.py --incoming PATH    # use a different drop folder
```

#### How the PDF matching works

When you drop a PDF in `incoming_manuals/`, the pipeline identifies it using three methods (in order):

1. **PDF metadata** — Some PDFs store the DOI in their document properties
2. **Filename** — If the filename looks like a DOI (e.g. `10.1177_1098612X231170159.pdf`), it reads the DOI from there
3. **First page text** — Reads only page 1 of the PDF looking for the DOI in the header or footer

Once a DOI is found, the pipeline checks if it's in `manifest.jsonl`. If not, it automatically looks it up in CrossRef and adds it before trying to ingest. This means PDFs no longer fail just because their DOI wasn't in the manifest.

#### What if a PDF still ends up in `failed/`?

Check `data/error_log.jsonl` for the reason. Common causes:

| Reason | What it means | Fix |
|--------|--------------|-----|
| `failed_unknown` | No DOI found anywhere in the PDF or filename | Re-download a text-based PDF (not a scan) |
| `failed_match` | DOI found but CrossRef doesn't know this paper | Try a different DOI, or check if the DOI is correct |
| `failed_ambiguous_title` | Title matches multiple papers in the manifest | Rare — check the PDF's actual DOI manually |

**Good news:** PDFs in `failed/` are automatically retried on the next run of `auto_ingest_workflow.py`. You don't need to move them manually.

---

### Step 6 — Repeat until done

```
python src/download.py         → try more OA sources
python src/supplement.py       → see updated missing list
[download PDFs from library]
python src/auto_ingest_workflow.py  → add manual PDFs
python pipeline.py             → check the scoreboard
```

Stop when `pipeline.py` shows 250/250 primary PDFs (exit code 0 means you've hit the 200-minimum threshold).

---

## Understanding the folders

```
vet-llm-research/
├── data/
│   ├── manifest.jsonl            ← Paper catalogue (built by collect.py)
│   ├── manual_manifest.jsonl     ← Manually ingested papers (built by auto_ingest_workflow.py)
│   ├── error_log.jsonl           ← All errors with details (never deleted)
│   ├── missing_papers.csv        ← What's still needed (rewritten each supplement run)
│   │
│   ├── raw/                      ← ✅ ACCEPTED PDFs (your corpus lives here)
│   ├── incoming_manuals/         ← 📥 DROP NEW PDFs HERE
│   │
│   ├── manual_inbox/             ← Staging area (auto_ingest_workflow.py manages this)
│   │   ├── *.pdf                 ← Being processed right now
│   │   ├── failed/               ← Couldn't match (automatically retried on next run)
│   │   ├── skipped_existing_in_raw/  ← Already in corpus (safe to ignore)
│   │   └── archive_failed/
│   │       └── YYYY-MM-DD/       ← Failed PDFs archived by date
│   │
│   ├── quarantine/               ← PDFs moved out by audit_article_types.py
│   ├── raw_text/                 ← Phase 3 raw column-aware extracted text
│   ├── processed/                ← Phase 3 cleaned text for LLM summaries
│   └── logs/
│       └── auto_ingest_workflow.log  ← Full run log
│
├── src/
│   ├── collect.py                ← Build manifest.jsonl
│   ├── download.py               ← Auto-download OA PDFs
│   ├── supplement.py             ← Missing-paper report + shopping list
│   ├── auto_ingest_workflow.py   ← One-command manual ingest
│   ├── enrich_manifest_from_pdfs.py  ← CrossRef lookup for unknown PDFs
│   ├── ingest_manual_pdfs.py     ← Match PDFs to manifest, move to raw/
│   ├── audit_article_types.py    ← Reclassify/remove non-primary articles
│   └── extract.py                ← Text extraction (smoke test only)
│
├── pipeline.py                   ← Scoreboard
├── .env                          ← Your settings (never commit this)
└── .env.template                 ← Settings reference
```

---

## Troubleshooting failed PDFs

### "A PDF I dropped in incoming_manuals/ ended up in failed/"

**Step 1 — Check the error log:**
```powershell
# Find entries for your PDF
Select-String -Path data\error_log.jsonl -Pattern "your_filename_or_doi"
```

**Step 2 — Re-run the workflow** (failed PDFs are automatically retried):
```powershell
python src/auto_ingest_workflow.py
```

**Step 3 — If it still fails, check the reason:**

| Error in log | Meaning | Fix |
|-------------|---------|-----|
| `pdfplumber metadata read failed` | PDF is a scanned image or corrupt | Download a text-based PDF from the publisher's website (not a scan) |
| `CrossRef 404` | DOI not registered in CrossRef | Verify the DOI is correct; try searching the title on CrossRef.org |
| `No DOI found (tried metadata, filename, page-1)` | No DOI anywhere in the file | Rename the file to match its DOI: `10.1177_1098612X231170159.pdf` |
| `No OA or title linkage` | DOI found but not in manifest and CrossRef failed | Run `collect.py` to refresh the manifest, then retry |

**Pro tip:** If you know the DOI, rename the file before dropping it:
```
10.1177_1098612X231170159.pdf    ← use _ instead of / in the DOI
```
The pipeline will extract the DOI from the filename automatically.

### "supplement.py keeps showing me papers I've already skipped"

Run it again — it shuffles randomly each time:
```powershell
python src/supplement.py
```
Each run shows a different subset of missing papers. If you don't want certain papers (e.g., they're all paywalled with no library access), just skip them and run again for new suggestions.

### "pipeline.py exits with code 1"

That just means you haven't reached 200 primary PDFs yet — it's not a crash. Keep going through the loop (download → supplement → manual add).

### "I accidentally ran collect.py twice and have duplicates in manifest.jsonl"

Not a problem — the rest of the pipeline deduplicates by DOI. Duplicate lines in `manifest.jsonl` are safely ignored.

### "verify_extraction.py shows WARN or FAIL"

Run this after Phase 3 extraction and before any paid summaries:

```powershell
python llm-sum/run_phase3.py extract
python scripts/verify_extraction.py --limit 20
```

`WARN` can be legitimate for short communications, case reports, and other article types that may still exist in `data/raw/` but are excluded from the final corpus. `FAIL` needs investigation before spending money. Compare `data/raw_text/<name>.jsonl` against `data/processed/<name>.jsonl`: if Methods, Results, or Discussion disappeared, reference stripping or watermark cleanup needs adjustment; if the raw text itself is tiny, the PDF is probably scanned or not a full research article.

---

## Optional / advanced commands

### Clean up article types already in `data/raw/`

If you realize some PDFs in `data/raw/` are case reports, editorials, or other non-primary articles:

```powershell
# Preview only — no changes
python src/audit_article_types.py

# Move excluded types to quarantine + rename reviews with 2_ prefix
python src/audit_article_types.py --remove --tag-secondary
```

After running `--remove`, run `supplement.py` again to get updated missing-paper suggestions.

### Preview manifest enrichment without writing anything

```powershell
python src/enrich_manifest_from_pdfs.py --dry-run
```

Shows which PDFs would have new manifest entries added, without touching `manifest.jsonl`.

### Run supplement with a fixed seed (for reproducible results)

```powershell
python src/supplement.py --seed 42
```

Pass any integer as `--seed`. The same number always gives the same suggestions — useful if you need to share a specific list with a collaborator.

### Run a specific part of the workflow manually

```powershell
# If PDFs are already in manual_inbox/ (not incoming_manuals/)
python src/ingest_manual_pdfs.py
python pipeline.py

# Just add CrossRef entries for PDFs already in the inbox
python src/enrich_manifest_from_pdfs.py
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
- **Primary** (count toward the 50-per-journal quota): original research articles
- **Secondary** (tagged with `2_` prefix, do not count toward quota): systematic reviews, meta-analyses, scoping reviews

**Article types excluded entirely:** case reports, short communications, brief reports, imaging diagnoses, rapid communications

**Minimum threshold:** `pipeline.py` exits 0 when you have ≥ 200 primary PDFs. The full 250 is the goal.

---

## All environment variables (.env)

Copy `.env.template` to `.env` and fill in your values. The most important ones:

| Variable | Default | Description |
|----------|---------|-------------|
| `DRY_RUN` | `true` | Set to `false` for real network calls. Keep `true` to practice without the internet. |
| `UNPAYWALL_EMAIL` | *(required)* | Your email address — required for CrossRef and Unpaywall access |
| `COLLECT_CANDIDATES_PER_JOURNAL` | `2000` | How many candidate papers per journal to collect |
| `COLLECT_YEARS` | `2023,2024,2025,2026` | Which years to query |
| `DOWNLOAD_VERBOSE` | `false` | Set to `true` to see every URL attempted |
| `MAX_FAILED_PER_JOURNAL` | `300` | Stop-loss: give up on a journal after this many download failures |
| `MIN_PAGES` | `3` | Reject PDFs shorter than this many pages |
| `MIN_EXTRACTED_WORDS` | `3000` | Reject PDFs with fewer than this many words (after removing references) |
| `PDFPLUMBER_USE_TEXT_FLOW` | `true` | Preserve reading order in two-column PDFs during Phase 3 extraction |
| `EXCLUDE_ARTICLE_TYPE_PATTERNS` | *(see template)* | Comma-separated title phrases that mark non-primary articles |

Full list in `.env.template`.

---

## Design decisions

**Why CrossRef instead of PubMed?**
CrossRef covers all 5 target journals with structured metadata (title, DOI, year, authors, abstract). PubMed's E-utils are used as a *download fallback* (PMC hosts many free PDFs), not as the primary catalogue source.

**Why random shuffling in supplement.py?**
The paper list is always in the same manifest order (newest-first from CrossRef). Without shuffling, you'd see the same suggestions every run, even after skipping papers you can't get. Shuffling surfaces different candidates each time.

**Why retry failed PDFs automatically?**
A PDF can fail on one run (because its DOI wasn't in the manifest yet) and succeed on the next (after `enrich_manifest_from_pdfs.py` adds the entry). Automatically moving PDFs out of `failed/` and retrying them eliminates a manual step.

**Why look up DOIs from three places (metadata, filename, page 1)?**
Most publisher PDFs don't store the DOI in their embedded metadata dictionary — they show it as text on page 1, and download tools often name files after their DOI (e.g. `10.1177_xxx.pdf`). Checking all three places means the pipeline can identify almost any publisher PDF without human intervention.

**Why metadata-only DOIs are NOT used for page-text scanning?**
The first 5 pages of many papers include a reference list. Scanning page text for DOIs would risk adding a *cited paper* to the manifest instead of the paper itself. The enrichment step therefore checks metadata, then filename, then page 1 only — never pages 2–5.

**Why append-only manifests?**
`manifest.jsonl` and `manual_manifest.jsonl` are only ever appended. This preserves the full collection history and makes reruns safe: deduplication happens at read time, never by deleting existing rows.

**Why 1-second delay between CrossRef calls in enrich_manifest_from_pdfs.py?**
CrossRef's polite pool provides higher rate limits to clients that identify themselves with an email address and keep requests to ~1/second. The `UNPAYWALL_EMAIL` in `.env` is sent in the `User-Agent` header for this reason.

---

## Glossary

| Term | Meaning |
|------|---------|
| **API** | Application Programming Interface — a way for two programs to talk to each other over the internet. CrossRef, Unpaywall, and PubMed are all accessed via their APIs. |
| **Candidate** | A paper in `manifest.jsonl` that has not yet been downloaded. All manifest entries start as candidates. |
| **CrossRef** | The non-profit registry that assigns and tracks DOIs for academic journals. Used by `collect.py` to find paper metadata. |
| **DOI** | Digital Object Identifier — a permanent link to a paper, e.g. `10.1111/jvim.12345`. Every paper in the corpus has one. |
| **DRY_RUN** | When `DRY_RUN=true`, scripts skip real network calls and use mock/fixture data. Safe for testing and practice. |
| **Enrich (manifest enrichment)** | The step where `enrich_manifest_from_pdfs.py` looks up unknown DOIs in CrossRef and adds them to `manifest.jsonl` so ingest can match the PDFs. |
| **Exit code** | A number a program returns when it finishes. 0 = success; 1 = something needs attention (not necessarily a crash). |
| **failed/** | The subfolder inside `data/manual_inbox/` where PDFs land if the pipeline can't identify them. They are automatically retried on the next `auto_ingest_workflow.py` run. |
| **Fixture** | A small, pre-built data file used for testing. Lives in `tests/fixtures/`. |
| **ISSN** | International Standard Serial Number — a journal's unique identifier, like a DOI for the journal itself. |
| **LLM** | Large Language Model — an AI like GPT-4 or Claude. The goal of this pipeline is to build a dataset for evaluating how well LLMs summarize veterinary papers. |
| **magic bytes** | The first few bytes of a file that identify its type. A real PDF always starts with `%PDF`. If a "PDF" starts with `<html>`, it's a login wall, not a paper. |
| **manifest.jsonl** | The catalogue file. Each line is a JSON object representing one paper (DOI, title, journal, year, authors, abstract). |
| **manual_manifest.jsonl** | Same format as `manifest.jsonl`, but contains only papers you added manually. Both are merged by `pipeline.py`. |
| **OA / open access** | A paper that can be legally downloaded for free. The pipeline only ever downloads OA papers. |
| **Paywall** | A barrier that requires a journal subscription to access a paper. The pipeline cannot bypass paywalls — those papers must be obtained through the library. |
| **PMC / PubMed Central** | The U.S. National Institutes of Health free archive of biomedical papers. Many veterinary papers are deposited here. |
| **Primary PDF** | An original research article that counts toward the 50-per-journal quota. Contrast with secondary (reviews). |
| **Quarantine** | `data/quarantine/` — where `audit_article_types.py` moves PDFs that shouldn't be in `data/raw/`. |
| **Raw** | `data/raw/` — the final home of all accepted PDFs, renamed to a consistent format. |
| **Secondary PDF** | A review article (systematic review, meta-analysis, etc.) — tagged with a `2_` filename prefix and not counted toward the quota. |
| **Stop-loss** | A safety limit that prevents infinite downloading. `MAX_FAILED_PER_JOURNAL=300` means: give up on a journal after 300 consecutive failures. |
| **Supplement** | The process of manually filling gaps that `download.py` couldn't fill automatically. `supplement.py` generates the shopping list. |
| **Unpaywall** | A database that tracks legal open-access locations for papers. `download.py` uses it as its first download source. |
| **Virtual environment** | An isolated Python installation. `.venv\Scripts\activate` activates it so your packages don't conflict with other Python projects. |
| **Word-count gate** | The validation rule in `download.py` that rejects PDFs with fewer than 3,000 extracted words. Prevents short articles and corrupted text from entering the corpus. |
