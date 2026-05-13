# Veterinary LLM Research Pipeline

This project is a research pipeline for collecting veterinary journal papers,
downloading open-access PDFs, cleaning the text, and preparing the data for
future LLM summarization and evaluation.

Research goal: evaluate large language models on veterinary literature
summarization using papers from 2023-2025 in major veterinary journals.

Project context: OVC Pet Trust Summer Studentship, 2026.

## How this fits together (beginner overview)

Think of the pipeline as an assembly line with **four main stations** you usually run in order:

1. **List papers** — `python src/collect.py` asks CrossRef for candidate DOIs and writes one line per paper to `data/manifest.jsonl` (a simple text file where each line is JSON).
2. **Download PDFs** — `python src/download.py` tries several **legal open-access** sources for each DOI and saves passing PDFs under `data/raw/`.
3. **Extract text** — `python src/extract.py` (or your future summarization code) reads PDFs and prepares clean text. The repo’s `extract.py` entry point is a **small smoke test** using fixtures.
4. **Check overall progress** — `python pipeline.py` merges the automatic list with any hand-added papers and prints how close you are to **250 PDFs**.

Optional: **`python src/supplement.py`** helps when a journal cannot reach 50 OA PDFs automatically — it explains what to do next and updates `data/missing_papers.csv`.

All detailed terms (DOI, CrossRef, OA, etc.) are defined in the [Glossary](#glossary) at the end of this file.

## Balanced Corpus Design

The pipeline targets **exactly 250 papers — 50 from each of 5 journals**:

| Journal | ISSN | Target |
|---------|------|--------|
| Journal of Veterinary Internal Medicine (JVIM) | 1939-1676 | 50 |
| Journal of the American Veterinary Medical Association (JAVMA) | 0003-1488 | 50 |
| Veterinary Surgery | 0161-3499 | 50 |
| Veterinary Radiology & Ultrasound (VRU) | 1058-8183 | 50 |
| Journal of Feline Medicine and Surgery (JFMS) | 1098-612X | 50 |

Equal-quota sampling is used instead of proportional sampling for two reasons:

1. **Bias prevention**: proportional sampling would over-represent high-OA journals
   like JVIM and under-represent surgical journals, biasing the LLM's learned
   writing-style patterns.
2. **Subgroup analysis power**: 50 papers per journal gives adequate statistical
   power for per-journal comparisons in Phase 4.

**Date range**: 2023-01-01 to 2025-12-31.  
**Open-access only**: no paywalled content is ever attempted.  
**Shortfall handling**: if a journal has fewer than 50 OA papers available,
the pipeline logs the deficit and generates `data/missing_papers.csv` for
manual supplementation via `src/supplement.py`.

## Current Status — Phase 2.1 Validation

This branch is the Phase 2.1 validation iteration.  The baseline Phase 2
pipeline has been significantly hardened with a production-safe OA downloader.

### What is new in Phase 2.1

**Expanded OA fallback chain (6 sources, in order):**

1. Unpaywall API — now tries every `oa_locations` entry, prioritizing direct PDFs
2. PubMed Central (NCBI E-utils) — DOI → PMCID → PDF or OA tar.gz package
3. Semantic Scholar API — returns `openAccessPdf` URL when known
4. `fulltext-article-downloader` CLI — queries Unpaywall, BASE, and CORE
5. Publisher-direct PDF URL — Wiley (`10.1111/`), AVMA (`10.2460/`), SAGE (`10.1177/`)
6. Article-page HTML scraping — follows DOI redirect, reads `citation_pdf_url` meta tag

**Publisher-safe throttling:**

- API requests use a short fixed delay (`DOWNLOAD_DELAY_SECONDS`)
- Publisher website requests use a separate configurable random jitter
  (`PUBLISHER_DELAY_MIN_SECONDS` / `PUBLISHER_DELAY_MAX_SECONDS`)
- HTTP 429 rate-limit responses are respected via `Retry-After` header

**Structured failure diagnostics:**

- Every attempted URL now records HTTP status code, final redirect URL,
  `Content-Type`, and the first 200 bytes of the response body
- `MIME_TYPE_MISMATCH`, `HTTP_403`, `HTTP_429`, `PDF_MAGIC_MISMATCH`,
  `NO_PDF_URL`, and `REQUEST_TIMEOUT` are distinct failure codes
- `data/error_log.jsonl` now includes the best failure detail per DOI,
  not just a generic "all fallbacks failed" message

**NCBI OA package support:**

- When PMC's browser-facing PDF URL returns a JavaScript challenge page,
  the pipeline falls back to NCBI's official `oa.fcgi` OA package service,
  downloads the `tar.gz`, and extracts the PDF from it

**All downloads validated by `%PDF` magic bytes:**

- HTML pages, login walls, bot-challenge pages, and empty files are all
  rejected before saving, regardless of HTTP status or Content-Type header

**All accepted PDFs must contain enough extractable text:**

- A file can be a valid PDF and still be useless for LLM summarization. For
  example, it might be a one-page abstract, a correction notice, an image-only
  scan, or a publisher placeholder page.
- After the basic `%PDF` file-format check, `src/download.py` opens the PDF
  with `pdfplumber`, counts pages (`MIN_PAGES`, default `3`; reject with
  `TOO_FEW_PAGES` if shorter), extracts text from every page, removes anything after a
  References/Bibliography heading, then checks IMRAD-style headings
  (`MIN_SECTIONS`, default `3` matches among introduction / methods /
  materials-and-methods / results / discussion; reject with `MISSING_SECTIONS`).
- The final acceptance rule remains `MIN_EXTRACTED_WORDS=3000` by default. A PDF with
  fewer useful words after that pipeline is rejected and does not count toward the 50-paper journal quota.
- `TARGET_EXTRACTED_WORDS=4500` documents the desired paper length used for
  budgeting, while `MIN_EXTRACTED_TOKENS=4000` is kept as a diagnostic estimate.
  The pipeline does not reject by token count because exact token counts depend
  on the model tokenizer.
- Existing PDFs in `data/raw/` are rechecked at the start of each live download
  run. Bad existing PDFs are moved to `data/quarantine/` instead of deleted, so
  the pipeline can retry that DOI while preserving an audit trail.

**The pipeline can now:**

- collect up to 2000 DOI candidates per journal from CrossRef (stratified across 2023-2025)
- skip obvious non-paper records such as corrections, errata, issue information, and abstract programs
- optionally annotate candidates with Unpaywall OA metadata before downloading
- infer covariates: species, study design, clinical topic
- download only legally open-access PDFs through a 6-source fallback chain
- detect and log OA shortfalls; generate `data/missing_papers.csv`
- extract and clean PDF text, remove references before truncating
- keep a structured error ledger with per-attempt diagnostic detail
- merge OA and manually supplemented PDFs into a unified corpus view
- run in dry-run mode with no network calls

Future phases will add LLM summarization, LLM-as-a-judge evaluation, human
review, and statistical analysis.

## Project Structure

```text
.
|-- README.md
|-- requirements.txt
|-- .env.template
|-- pipeline.py               # Merges OA + manual PDFs; reports corpus status
|-- data/                     # Created locally; usually gitignored
|   |-- manifest.jsonl         # OA paper list created by collect.py
|   |-- manual_manifest.jsonl  # Manually added papers (same schema as manifest.jsonl)
|   |-- raw/                   # Accepted PDFs that passed format + text validation
|   |-- quarantine/            # Rejected old PDFs plus JSON sidecar metadata
|   |-- error_log.jsonl        # Pipeline errors + OA shortfall entries
|   `-- missing_papers.csv     # Papers needing manual supplementation
|-- src/
|   |-- main.py                # Simple startup check
|   |-- collect.py             # Stratified CrossRef collection (200 candidates/journal)
|   |-- download.py            # Balanced OA download (stop-loss at 50 PDFs/journal)
|   |-- supplement.py          # Manual supplement helper — prints instructions, writes CSV
|   |-- extract.py             # Extracts and cleans PDF text (library + smoke-test `__main__`)
|   |-- file_paths.py          # Shared PDF naming + path resolution (descriptive vs legacy)
|   |-- utils.py               # Budget guard, rate limits, error logging
|   `-- utils/
|       `-- generate_mock.py   # Development helper: writes a synthetic JSON paper fixture
`-- tests/
    |-- manual_test_phase2.py  # Phase 2 checklist tests
    |-- test_pdf_validation.py # Unit tests for the PDF text validation gate
    |-- test_file_paths.py     # Unit tests for descriptive filenames and path resolution
    |-- test_collect_filters.py # Unit tests for non-article filters and year windows
    `-- fixtures/
        `-- sample_paper.json
```

## Every script and what it does

| What you run | Role |
|--------------|------|
| `python src/collect.py` | Builds or appends `data/manifest.jsonl` from CrossRef (or mock rows if `DRY_RUN=true`). |
| `python src/download.py` | Walks the manifest, downloads **OA-only** PDFs with validation, writes `data/error_log.jsonl` and possibly `data/missing_papers.csv`. |
| `python src/extract.py` | **Smoke test only**: forces `DRY_RUN=true`, loads a fixture DOI, prints success and a short preview. Import `extract_clean_text` from code for real extraction. |
| `python src/supplement.py` | Human-facing report for journals below 50 PDFs; refreshes `data/missing_papers.csv`. |
| `python src/ingest_manual_pdfs.py` | Moves legal manual PDFs from `data/manual_inbox/` into `data/raw/` with descriptive filenames; appends `data/manual_manifest.jsonl`. Read-only on `manifest.jsonl`. |
| `python pipeline.py` | Loads OA + `data/manual_manifest.jsonl`, validates PDFs on disk, prints per-journal status. **Exits with code 1** if fewer than **200** confirmed PDFs (for scripts/CI). |
| `python src/main.py` | Minimal sanity check: loads `.env` and prints that the environment is initialized. |
| `python src/utils/generate_mock.py` | Developer utility to regenerate rich mock JSON (not required for the normal user workflow). |

**Library modules** (imported by other code, not “steps”): `src/file_paths.py` (filenames), `src/utils.py` (logging, budget guard, delays).

## Before You Start

You need these installed on your computer:

- Git
- Python 3.10 or newer
- A terminal, such as PowerShell on Windows
- Internet access for live collection and download runs

You do not need paid API keys for the current Phase 2 dry run.

## Quick Start: Run The Pipeline After Cloning

These steps are written for Windows PowerShell. Run each command from the
terminal, one at a time.

### Step 1: Clone The Project

Cloning means "download a copy of this project onto your computer."

```powershell
git clone <REPOSITORY_URL>
```

Replace `<REPOSITORY_URL>` with the real GitHub repository URL.

### Step 2: Go Into The Project Folder

After cloning, move your terminal into the new folder.

```powershell
cd vet-llm-research
```

If your cloned folder has a different name, use that folder name instead.

### Step 3: Create A Virtual Environment

A virtual environment is a small, private Python workspace for this project.
It keeps this project's packages separate from other Python projects.

```powershell
python -m venv .venv
```

### Step 4: Turn On The Virtual Environment

```powershell
.\.venv\Scripts\Activate.ps1
```

If PowerShell says scripts are blocked, run this once in the same terminal:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

Then try activating again:

```powershell
.\.venv\Scripts\Activate.ps1
```

You will know it worked if you see `(.venv)` at the start of your terminal line.

### Step 5: Install The Required Packages

Packages are pieces of Python code that this project depends on.

```powershell
pip install -r requirements.txt
```

This installs all dependencies, including `beautifulsoup4` (for HTML parsing
in the downloader) and `fulltext-article-downloader` (one option **midway**
through the six-step OA acquisition chain — after Unpaywall, PubMed Central,
and Semantic Scholar). Wait until the install finishes before moving to the
next step.

### Step 6: Create Your Local Settings File

The `.env` file stores settings for your own computer. It is not committed to
Git because it may later contain private API keys.

```powershell
Copy-Item .env.template .env
```

Open `.env` and check these values:

```text
DRY_RUN=true
BUDGET_HARD_STOP=0.00
UNPAYWALL_EMAIL=your.name@uoguelph.ca

# -- Downloader settings (safe production defaults) --
DOWNLOAD_VERBOSE=false
DOWNLOAD_DELAY_SECONDS=2
PUBLISHER_DELAY_MIN_SECONDS=10
PUBLISHER_DELAY_MAX_SECONDS=25
RATE_LIMIT_BACKOFF_SECONDS=60

# -- PDF text validation settings --
MIN_EXTRACTED_WORDS=3000
TARGET_EXTRACTED_WORDS=4500
MIN_EXTRACTED_TOKENS=4000
SKIP_VALIDATION_FOR_TESTING=false
```

For your first run, keep `DRY_RUN=true`. This is the safest mode. It uses mock
data and avoids real downloads.

Change `UNPAYWALL_EMAIL` to your university email address (no leading space).
This is not a secret; it is used by CrossRef and Unpaywall to identify the
requesting institution.

The downloader variables control throttling for publisher-facing requests:

| Variable | Default | Description |
|----------|---------|-------------|
| `DOWNLOAD_VERBOSE` | `false` | Print per-URL debug info (status, Content-Type, body start) |
| `DOWNLOAD_DELAY_SECONDS` | `2` in `.env.template` | Pause between API calls (Unpaywall, Semantic Scholar, PMC) |
| `PUBLISHER_DELAY_MIN_SECONDS` | `10` | Minimum random jitter before a publisher URL request |
| `PUBLISHER_DELAY_MAX_SECONDS` | `25` | Maximum random jitter before a publisher URL request |
| `RATE_LIMIT_BACKOFF_SECONDS` | `60` | Fallback sleep when a server returns HTTP 429 with no `Retry-After` |

Set all delay values to `0` only during single-DOI debugging. Leave them at
their defaults for real collection runs to respect publisher rate limits.

The validation variables control which PDFs are allowed into `data/raw/`:

- `MIN_EXTRACTED_WORDS=3000` is the hard gate. If `pdfplumber` extracts fewer
  than 3,000 useful words after references are removed, the PDF is rejected.
- `TARGET_EXTRACTED_WORDS=4500` is the desired planning target. It explains the
  budget assumption but does not reject papers by itself.
- `MIN_EXTRACTED_TOKENS=4000` is recorded in logs for cost awareness. It is not
  the hard gate because token counts vary by LLM provider.
- `SKIP_VALIDATION_FOR_TESTING=false` should stay false for real work. Only set
  it to true in narrow unit tests that are not testing PDF extraction.

The `.env.template` file also documents **collection** options (`COLLECT_CANDIDATES_PER_JOURNAL`, year balancing, optional Unpaywall preflight). You can leave them at their defaults until you read the **Running With Real Data** section.

### Step 7: Run The Phase 2 Checklist

This checks the safety pieces before you run the pipeline.

```powershell
python tests/manual_test_phase2.py
```

You want to see:

```text
ALL TESTS PASSED - Phase 2 is ready.
```

If a test fails, read the message in the terminal before continuing.

### Step 8: Collect The Paper List

This creates `data/manifest.jsonl` with up to `COLLECT_CANDIDATES_PER_JOURNAL` OA candidates per journal (default **2000**).
Live collection queries 2023, 2024, and 2025 separately by default so the
manifest does not fill only from the newest CrossRef results. It also skips
obvious non-paper records such as corrections, errata, issue information, and
conference abstract/report programs.

In dry-run mode, it writes 2 fake papers per journal (10 total) so you can
test the pipeline without using the internet.

```powershell
python src/collect.py
```

Expected result:

```text
[collect] DRY_RUN per-journal candidate counts:
  JVIM                       2 mock candidates  (download target: 50)
  JAVMA                      2 mock candidates  (download target: 50)
  Veterinary Surgery         2 mock candidates  (download target: 50)
  VRU                        2 mock candidates  (download target: 50)
  JFMS                       2 mock candidates  (download target: 50)
Collection complete. Manifest contains 10 new entries.
```

Important: `collect.py` appends to `data/manifest.jsonl`. For a clean fresh
run, delete `data/manifest.jsonl` before running collection again.

### Step 9: Test The Download Step

In dry-run mode, this does not download real PDFs. It simulates the balanced
per-journal scheduling and shows what would happen per journal.

```powershell
python src/download.py
```

Expected result (dry-run with 2 mock papers per journal):

```text
[download] DRY_RUN=true — no network calls.
[download] Simulating balanced download (existing PDFs already counted).

  Journal                   Exist  Would add  Shortfall
  -------------------------------------------------------
  JVIM                          0          2          0
  JAVMA                         0          2          0
  Veterinary Surgery            0          2          0
  VRU                           0          2          0
  JFMS                          0          2          0
  -------------------------------------------------------
  TOTAL                         0         10          0

[download] DRY_RUN summary: 10/250 PDFs would be acquired, 0 shortfall.
```

In dry-run mode, "would be acquired" means the logic would succeed for those
DOIs. No real PDFs are saved. The shortfall of 240 (250 - 10) is expected
because the dry-run manifest only has 2 mock papers per journal.

### Step 10: Test The Text Extraction Step

This runs a small smoke test using the fixture data in `tests/fixtures`. The
script sets `DRY_RUN=true` internally so it does not need live PDFs.

```powershell
python src/extract.py
```

Expected result (your line lengths may differ slightly):

```text
Extraction successful. Output length: ... chars.
First 500 chars:
 ...
```

At this point, the Phase 2 dry-run pipeline is working.

## Running With Real Data

Only do this after the dry run works.

### Step 1: Edit `.env`

Change:

```text
DRY_RUN=true
```

to:

```text
DRY_RUN=false
```

Keep:

```text
BUDGET_HARD_STOP=0.00
```

Phase 2 does not call paid LLM APIs, so the budget should stay at zero.

### Step 2: Collect Real Metadata

```powershell
python src/collect.py
```

This queries CrossRef for papers from the target journals and writes them to
`data/manifest.jsonl`.

### Step 3: Download Open-Access PDFs

```powershell
python src/download.py
```

The downloader tries **six sources** for each DOI, in order:

1. Unpaywall API — all `oa_locations`, prioritizing direct PDF/repository URLs
2. PubMed Central — DOI → PMCID → direct PDF, then NCBI OA tar.gz package
3. Semantic Scholar API — `openAccessPdf` URL
4. `fulltext-article-downloader` CLI (Unpaywall + BASE + CORE)
5. Publisher-direct PDF URL — Wiley, AVMA, SAGE heuristics
6. Article-page HTML scraping — `citation_pdf_url` meta tag or `<a>` links

Every downloaded file goes through chained validation layers before it is counted:

1. **Format validation** checks that the response really starts with `%PDF`.
   This rejects HTML bot-challenge pages, login walls, and empty responses.
2. **Page-count validation** opens the temporary PDF with `pdfplumber` and
   rejects PDFs shorter than `MIN_PAGES` (`TOO_FEW_PAGES`); extraction is skipped
   for failures at this step.
3. **Text extraction** reads every page, then strips the References/Bibliography
   tail using the same rules as downstream extraction.
4. **Section-heading validation** requires at least `MIN_SECTIONS` distinct hits
   among canonical headings (introduction, methods/materials-and-methods,
   results, discussion). Failures log `MISSING_SECTIONS`.
5. **Word-count validation** applies the final gate: at least `MIN_EXTRACTED_WORDS`
   useful words (`TEXT_TOO_SHORT` / related types if extraction failed earlier).

Short PDFs or front-matter stubs can satisfy `%PDF` but still fail IMRAD-ish
signals or length. Only PDFs that pass the chain are moved into `data/raw/` and
counted toward the 50-per-journal quota.

If validation fails during a new download, the temporary file is deleted and the
pipeline tries the next legal OA source for that DOI. The failure is recorded in
`data/error_log.jsonl` with `"stage": "validation"` and an `error_type` such as
`TOO_FEW_PAGES`, `MISSING_SECTIONS`, `TEXT_TOO_SHORT`, `NO_EXTRACTABLE_TEXT`, or
`PDF_CORRUPT`.

At the start of a live download run, the pipeline also revalidates PDFs that
already exist in `data/raw/`. If an older PDF now fails any validation gate, it is
moved to `data/quarantine/` with a timestamped filename and a matching `.json`
metadata file. It is not permanently deleted. After quarantine, that DOI is no
longer counted as already downloaded, so the normal fallback chain can try to
find a better source.

PDFs are saved in `data/raw/` with descriptive filenames that include the
journal, paper title, and DOI suffix. Older DOI-only filenames are still
recognized, so existing downloads continue to count. The download stops at
50 PDFs per journal (stop-loss). If a journal falls short, a warning is written to
`data/error_log.jsonl` and `data/missing_papers.csv` is created.

Optional settings in `.env` that affect live runs:

```text
DOWNLOAD_VERBOSE=false           # set to true to print per-URL debug info
COLLECT_CANDIDATES_PER_JOURNAL=2000  # DOI candidates to collect per journal
COLLECT_YEAR_BALANCED=true       # collect across 2023, 2024, and 2025
COLLECT_YEARS=2023,2024,2025     # publication years to query separately
COLLECT_PREFLIGHT_UNPAYWALL=false # add OA metadata during collection
COLLECT_REQUIRE_UNPAYWALL_OA=false # skip non-OA candidates during collection
COLLECT_MIN_OA_LOCATIONS=1       # strict-mode minimum OA locations
COLLECT_PREFER_DIRECT_PDF=true   # strict-mode preference for PDF URLs
DOWNLOAD_DELAY_SECONDS=2         # pause between API calls (see .env.template)
PUBLISHER_DELAY_MIN_SECONDS=10   # random jitter floor before publisher URLs
PUBLISHER_DELAY_MAX_SECONDS=25   # random jitter ceiling before publisher URLs
RATE_LIMIT_BACKOFF_SECONDS=60    # fallback sleep on HTTP 429 with no Retry-After
MAX_FAILED_PER_JOURNAL=250       # stop a journal after this many OA failures
MIN_PAGES=3                      # reject PDFs shorter than this before extraction
MIN_SECTIONS=3                   # min distinct heading matches among IMRAD-ish set
MIN_EXTRACTED_WORDS=3000         # hard minimum useful words after references
TARGET_EXTRACTED_WORDS=4500      # planning target for paper length/budget
MIN_EXTRACTED_TOKENS=4000        # diagnostic token estimate, not hard gate
SKIP_VALIDATION_FOR_TESTING=false
```

To debug a single DOI with full diagnostic output:

```powershell
$env:DOWNLOAD_VERBOSE="true"; $env:PUBLISHER_DELAY_MIN_SECONDS="0"; $env:PUBLISHER_DELAY_MAX_SECONDS="0"
python -c "import sys; sys.path.insert(0,'src'); from download import download_paper; download_paper('10.1111/jvim.70254')"
```

### Step 4: Check Corpus Status

```powershell
python pipeline.py
```

This merges `data/manifest.jsonl` (OA papers) with `data/manual_manifest.jsonl`
(manually added papers, if any) and prints a per-journal status table.

If you have **fewer than 200** PDFs confirmed in `data/raw/`, the process
**exits with status code 1**. That is intentional so automated checks can fail
fast; it does not mean Python is broken.

### Step 5: Handle OA Shortfalls (if needed)

If any journal has fewer than 50 PDFs, run:

```powershell
python src/supplement.py
```

This prints per-missing-paper instructions ("Check UoG library" / "Email author")
and writes `data/missing_papers.csv`. Once you have obtained the PDFs:

1. Place the PDF files in `data/raw/`. The pipeline accepts either the new
   descriptive filename format or the legacy DOI-only format.
2. Add entries for each paper to `data/manual_manifest.jsonl` using the same
   schema as `data/manifest.jsonl`. (Alternatively, for many files at once,
   use **Manually ingesting PDFs** to rename and append automatically.)
3. Re-run `python pipeline.py` to confirm the updated corpus count.

The pipeline accepts a final corpus of `250 - total_missing` OA papers if at
least 200 OA PDFs were downloaded. The 200-paper threshold ensures enough data
for a meaningful Phase 4 analysis even without complete manual supplementation.

### Manually ingesting PDFs

Use this when you already have **legal** OA PDFs on disk under random browser
download names. The helper **never** touches `data/manifest.jsonl` (CrossRef
truth stays read-only); it only **reads** it to match DOIs/titles and copies
the canonical JSON line into `data/manual_manifest.jsonl` when needed.

1. Run `python src/collect.py` at least once so `data/manifest.jsonl` lists the
   DOIs you care about.
2. Create `data/manual_inbox/` (the script creates it on first run if missing)
   and drop the PDFs directly in that folder—do not nest them in subfolders for
   routine use.
3. From the project root:

   ```powershell
   python src/ingest_manual_pdfs.py
   ```

   Override the folder with `--inbox path\to\folder` if needed.

4. The script uses `pdfplumber` to collect DOIs from document metadata and from
   the first five pages (pattern `10.xxxx/...`). If no DOI resolves to a manifest
   row, it tries a **case-insensitive** match of the PDF `/Title` metadata against
   manifest `title`; ambiguous titles are rejected.
5. Success moves each file to `data/raw/` using `descriptive_pdf_filename()` from
   `src/file_paths.py`, **without overwriting** existing corpus files. Duplicate
   uploads are moved aside to `data/manual_inbox/skipped_existing_in_raw/`.
6. Failures (unmatched DOI/title, malformed PDF, manifest miss) move to
   `data/manual_inbox/failed/` and `log_error(..., stage="manual_ingest", ...)`
   captures details in `data/error_log.jsonl`.
7. Finish with `python pipeline.py` to merge OA + manual manifests and confirm
   counts.

## Useful Commands

Run the simple startup check:

```powershell
python src/main.py
```

Run the Phase 2 tests with pytest:

```powershell
python -m pytest tests/manual_test_phase2.py -v
```

Run the PDF validation gate tests:

```powershell
python -m pytest tests/test_pdf_validation.py -v
```

Run tests for filename rules and collection filters:

```powershell
python -m pytest tests/test_file_paths.py tests/test_collect_filters.py -v
```

## Important Notes

- Keep `.env` private. Do not commit API keys or your email address.
- The `data/` folder is gitignored because it can contain downloaded papers,
  generated manifests, and error logs.
- The downloader only uses open-access sources. It does not bypass paywalls,
  use institutional proxies, or attempt login. This is intentional and required
  by research ethics and University of Guelph policy.
- Some publishers (Wiley, SAGE) use Cloudflare bot-detection and will return
  `403 Forbidden` even for genuinely OA papers. The script logs the specific
  failure reason rather than silently skipping the paper.
- `UNPAYWALL_EMAIL` must have no leading or trailing space. A space causes
  Unpaywall to treat your address as invalid and return no OA locations.
- Dry-run mode is the safest first step. Use it before every major change.
- If something fails, check `data/error_log.jsonl`. Each failed DOI now
  includes the most informative failure code (`HTTP_403`, `MIME_TYPE_MISMATCH`,
  `NO_PDF_URL`, `TEXT_TOO_SHORT`, `NO_EXTRACTABLE_TEXT`, etc.) rather than a
  generic message.
- If a PDF is moved to `data/quarantine/`, it means the file existed in
  `data/raw/` but did not pass the current text-validation gate. Look at the
  matching `.json` sidecar file to see the DOI, word count, threshold, and
  reason. This is intentional: quarantine preserves evidence while allowing the
  downloader to retry that DOI.
- If a journal has < 50 OA PDFs, `data/error_log.jsonl` will contain entries
  with `"stage": "insufficient_oa"`. Run `python src/supplement.py` to act on them.
- `data/manual_manifest.jsonl` must only contain entries whose PDFs are already
  in `data/raw/`. `pipeline.py` validates this and will warn about missing PDFs.
- To avoid duplicate papers: delete `data/manifest.jsonl` before re-running
  `collect.py` for a fresh corpus build.

## Troubleshooting

If `python` is not recognized, try:

```powershell
py --version
```

If that works, use `py` instead of `python` in the commands above.

If activation fails in PowerShell, run:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

If imports fail, make sure your virtual environment is active and run:

```powershell
pip install -r requirements.txt
```

If `download.py` says the manifest is missing, run:

```powershell
python src/collect.py
```

If you get duplicate papers in `data/manifest.jsonl`, delete that file and run
collection again.

If `download.py` reports a shortfall for a journal (fewer than 50 OA PDFs):

```powershell
python src/supplement.py
```

This generates `data/missing_papers.csv` and prints acquisition instructions.

If `download.py` rejects a PDF with `TEXT_TOO_SHORT`:
the PDF was technically readable, but after removing references it had fewer
than `MIN_EXTRACTED_WORDS` useful words. This usually means it was not a full
research article, or it was a short notice/editorial/correction. The downloader
will treat that DOI as a failed attempt and continue looking for another legal
OA source if one is available.

If `download.py` rejects a PDF with `NO_EXTRACTABLE_TEXT`:
`pdfplumber` could open the PDF, but it found no real text. This commonly means
the PDF is image-only or scanned. The pipeline rejects it because the LLM would
receive no usable paper text.

If `download.py` rejects a PDF with `PDF_CORRUPT`:
the file was empty, unreadable, password protected, malformed, or could not be
opened by `pdfplumber`. The pipeline rejects it before it can break extraction
or summarization later.

If `pipeline.py` warns about manual manifest entries with missing PDFs:
make sure the PDF file is in `data/raw/` using either the descriptive filename
format or the legacy DOI-only filename (`/`, `:`, and `.` replaced by `_`, plus
`.pdf` extension).

If `download.py` says `ModuleNotFoundError: No module named 'bs4'`:
your virtual environment is not active, or you ran `pip install` in the wrong
Python. Re-activate `.venv` and run `pip install -r requirements.txt` again.
Then invoke the script through the virtual environment explicitly:

```powershell
& ".\.venv\Scripts\python.exe" src/download.py
```

If every DOI fails with `HTTP_403` or `MIME_TYPE_MISMATCH`:
the publisher is blocking automated requests with Cloudflare or a similar CDN.
This is expected for some journals. Check `data/error_log.jsonl` for the
`body_start` field — it will show the exact HTML the server returned (e.g.,
"Just a moment..."). No script change can legally bypass this; use the manual
supplement workflow for those papers.

If a PMC paper returns "Preparing to download..." HTML:
the NCBI viewer requires JavaScript to start the download. The script handles
this automatically by falling back to NCBI's official OA package service
(`oa.fcgi`). If the package is also unavailable (FTP 550), that paper must be
obtained manually.

If you get `UnicodeEncodeError` in the PowerShell terminal:
set `DOWNLOAD_VERBOSE=false` to suppress the diagnostic body-start output,
which may contain non-ASCII characters from publisher HTML pages.

## Glossary

Short definitions for terms used in this repository. Follow the links in **Useful Commands** and **Project Structure** for hands-on context.

| Term | Meaning |
|------|---------|
| **API** | An automated web interface programs use to request data (for example, CrossRef or Unpaywall). You call an API with HTTP requests instead of clicking in a browser. |
| **BASE / CORE** | Academic search indexes. The optional `fulltext-article-downloader` tool can query them to discover full-text links for some papers. |
| **candidate** | A paper (usually identified by DOI) that `collect.py` wrote into `manifest.jsonl`. Only some candidates become downloaded PDFs after `download.py` runs. |
| **CDN (Content Delivery Network)** | A network that serves publisher websites quickly; it sometimes includes bot protection (for example Cloudflare), which can return `403` or challenge pages to scripts. |
| **Content-Type** | An HTTP header describing what kind of data a server is returning (for example `application/pdf` vs `text/html`). The downloader checks this along with raw bytes. |
| **covariate** | Extra structured fields inferred for each paper (for example species or study design) so later analysis can group or compare results fairly. |
| **CrossRef** | A nonprofit registry of scholarly metadata. This project uses CrossRef to list recent works in each target journal (by ISSN and date). |
| **DOI (Digital Object Identifier)** | A permanent string ID for a scholarly work (for example `10.1111/jvim.70254`). It is the main key for manifests, downloads, and deduplication. |
| **dry run** | Mode with `DRY_RUN=true`: no real network collection or downloads; mock rows exercise the rest of the pipeline safely. |
| **E-utils** | NCBI’s programmatic interface for PubMed and related databases. Used here to map DOIs to PMC IDs when fetching OA copies. |
| **exit code** | A small integer a program returns when it finishes. `pipeline.py` uses **1** when fewer than 200 PDFs are present so scripts know the corpus is below the minimum. |
| **fixture** | A small test file bundled in `tests/fixtures/` so `extract.py` can run without your own PDFs. |
| **HTML scraping** | Parsing a journal’s public article webpage to find a linked PDF URL (only after APIs and repositories are tried). |
| **HTTP 403 / 429** | Standard web errors: **403** means “forbidden” (often bot blocking); **429** means “too many requests” (rate limiting). The downloader backs off when servers ask it to wait. |
| **idempotent** | Safe to run more than once without breaking things: if a PDF is already valid on disk, `download.py` skips re-downloading that DOI. |
| **ISSN** | A serial number for a journal (like an ISBN for periodicals). Each target journal has a known ISSN used in CrossRef queries. |
| **JSON** | A structured text format `{ "key": "value" }` used for manifests and error logs. |
| **JSONL** | “JSON Lines”: one JSON object per line in a text file (`data/manifest.jsonl`). Easy to append and stream. |
| **Large Language Model (LLM)** | A broad class of AI text models. Phase 2 only prepares PDFs and text; later phases will use an LLM for summarization and evaluation. |
| **magic bytes** | The first bytes of a file. Real PDFs start with `%PDF`; a downloaded “PDF” that is actually HTML will fail this check. |
| **manifest** | The project’s list of papers to consider, stored as `data/manifest.jsonl` (OA pipeline) plus optional `data/manual_manifest.jsonl` (hand-added). |
| **MAX_FAILED_PER_JOURNAL** | Stop-loss for wasted attempts: after this many failures in one journal, `download.py` stops trying more candidates there and relies on supplementation. |
| **MIME type** | Same idea as **Content-Type**: what kind of file the server claims to send. Mismatches versus real bytes are logged as `MIME_TYPE_MISMATCH`. |
| **OA (open access)** | Legally free-to-read (and here, free-to-download) versions of a paper, hosted by publishers or repositories. The pipeline never attempts paywalled downloads. |
| **`oa.fcgi` / OA package** | NCBI’s service that bundles some PMC articles as downloadable archives when a browser PDF URL is not script-friendly. |
| **Paywall** | A publisher restriction that requires payment or institutional login. This pipeline does not circumvent paywalls. |
| **pdfplumber** | A Python library that extracts text from PDF pages. It powers the “enough words for summarization” quality gate. |
| **Phase 2 / Phase 4** | **Phase 2** is ingestion (collect, download, validate, extract plumbing). **Phase 4** refers to planned statistical analysis and evaluation on the finished corpus. |
| **PMC / PubMed Central** | NIH’s full-text archive for many life-science articles. Some veterinary papers have PMC OA copies even when the publisher site is awkward. |
| **PMCID** | PubMed Central’s ID for an article deposit, discovered from the DOI when available. |
| **Preflight** | Optional extra step during collection: call Unpaywall early to annotate or filter candidates by known OA links before `download.py` runs. |
| **Quarantine** | Folder `data/quarantine/` where an older file in `data/raw/` is moved (with a `.json` sidecar) if it fails re-validation, instead of deleting it. |
| **Repository** | A trusted archive (institutional or subject-specific) that hosts an OA PDF or landing page; Unpaywall lists these as **OA locations**. |
| **Retry-After** | HTTP header telling clients how many seconds to wait after rate limiting (`429`). |
| **Semantic Scholar** | A literature search API that sometimes exposes `openAccessPdf` URLs. |
| **sidecar** | A small metadata file saved next to a quarantined PDF (JSON) describing why it was rejected. |
| **Stop-loss** | The per-journal cap of **50 accepted PDFs** in `download.py`: once a journal reaches 50 good files, no more are downloaded for that journal in that run configuration. |
| **Stratified / year-balanced collection** | Querying each calendar year (for example 2023, 2024, 2025) separately so the manifest is not dominated by the newest year only. |
| **Throttling / jitter** | Pauses between requests. **Jitter** means random delay in a range so many clients do not retry at the exact same instant. |
| **Token** | A chunk of text an LLM bills or measures; can differ by model. This repo tracks tokens only as a **rough budget estimate**, not as the PDF acceptance rule. |
| **Unpaywall** | A service that maps DOIs to legal OA locations and license types. An **email** in `.env` is required for polite use. |
| **Virtual environment** | An isolated Python install (`python -m venv .venv`) so this project’s packages do not clash with other Python work. |
| **Word-count gate** | Rule enforced by `MIN_EXTRACTED_WORDS`: after stripping the reference list, the PDF must still contain enough words to resemble a full article. |
