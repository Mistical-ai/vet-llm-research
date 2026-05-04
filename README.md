# Veterinary LLM Research Pipeline

This project is a research pipeline for collecting veterinary journal papers,
downloading open-access PDFs, cleaning the text, and preparing the data for
future LLM summarization and evaluation.

Research goal: evaluate large language models on veterinary literature
summarization using papers from 2023-2025 in major veterinary journals.

Project context: OVC Pet Trust Summer Studentship, 2026.

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

1. `fulltext-article-downloader` CLI — queries Unpaywall, BASE, and CORE
2. Unpaywall API — now tries every `oa_locations` entry, not just `best_oa_location`
3. Semantic Scholar API — returns `openAccessPdf` URL when known
4. PubMed Central (NCBI E-utils) — DOI → PMCID → PDF or OA tar.gz package
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

**The pipeline can now:**

- collect up to 80 DOI candidates per journal from CrossRef (stratified)
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
|-- data/                     # Created locally; gitignored
|   |-- manifest.jsonl         # OA paper list created by collect.py
|   |-- manual_manifest.jsonl  # Manually added papers (same schema as manifest.jsonl)
|   |-- raw/                   # All PDFs (OA-downloaded and manually placed)
|   |-- error_log.jsonl        # Pipeline errors + OA shortfall entries
|   `-- missing_papers.csv     # Papers needing manual supplementation
|-- src/
|   |-- main.py                # Simple startup check
|   |-- collect.py             # Stratified CrossRef collection (80 candidates/journal)
|   |-- download.py            # Balanced OA download (stop-loss at 50 PDFs/journal)
|   |-- supplement.py          # Manual supplement helper — prints instructions, writes CSV
|   |-- extract.py             # Extracts and cleans PDF text
|   |-- utils.py               # Budget guard, rate limits, error logging
|   `-- utils/
|       `-- generate_mock.py   # Development helper for mock data
`-- tests/
    |-- manual_test_phase2.py  # Phase 2 checklist tests
    `-- fixtures/
        `-- sample_paper.json
```

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
in the downloader) and `fulltext-article-downloader` (for the first fallback
in the OA acquisition chain). Wait until the install finishes before moving
to the next step.

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
DOWNLOAD_DELAY_SECONDS=1
PUBLISHER_DELAY_MIN_SECONDS=10
PUBLISHER_DELAY_MAX_SECONDS=25
RATE_LIMIT_BACKOFF_SECONDS=60
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
| `DOWNLOAD_DELAY_SECONDS` | `1` | Pause between API calls (Unpaywall, Semantic Scholar, PMC) |
| `PUBLISHER_DELAY_MIN_SECONDS` | `10` | Minimum random jitter before a publisher URL request |
| `PUBLISHER_DELAY_MAX_SECONDS` | `25` | Maximum random jitter before a publisher URL request |
| `RATE_LIMIT_BACKOFF_SECONDS` | `60` | Fallback sleep when a server returns HTTP 429 with no `Retry-After` |

Set all delay values to `0` only during single-DOI debugging. Leave them at
their defaults for real collection runs to respect publisher rate limits.

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

This creates `data/manifest.jsonl` with up to 80 OA candidates per journal.

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

This runs a small smoke test using the fixture data in `tests/fixtures`.

```powershell
python src/extract.py
```

Expected result:

```text
Extraction successful.
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

1. `fulltext-article-downloader` CLI (Unpaywall + BASE + CORE)
2. Unpaywall API — all `oa_locations`, not just the best one
3. Semantic Scholar API — `openAccessPdf` URL
4. PubMed Central — DOI → PMCID → direct PDF, then NCBI OA tar.gz package
5. Publisher-direct PDF URL — Wiley, AVMA, SAGE heuristics
6. Article-page HTML scraping — `citation_pdf_url` meta tag or `<a>` links

Every downloaded file is validated against PDF magic bytes (`%PDF`) before
being saved. HTML bot-challenge pages, login walls, and empty responses are all
rejected. Failure details (HTTP status, Content-Type, final redirect URL,
body preview) are recorded in `data/error_log.jsonl`.

PDFs are saved in `data/raw/`. The download stops at 50 PDFs per journal
(stop-loss). If a journal falls short, a warning is written to
`data/error_log.jsonl` and `data/missing_papers.csv` is created.

Optional settings in `.env` that affect live runs:

```text
DOWNLOAD_VERBOSE=false           # set to true to print per-URL debug info
DOWNLOAD_DELAY_SECONDS=1         # pause between API calls
PUBLISHER_DELAY_MIN_SECONDS=10   # random jitter floor before publisher URLs
PUBLISHER_DELAY_MAX_SECONDS=25   # random jitter ceiling before publisher URLs
RATE_LIMIT_BACKOFF_SECONDS=60    # fallback sleep on HTTP 429 with no Retry-After
MAX_FAILED_PER_JOURNAL=100       # stop a journal after this many OA failures
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

### Step 5: Handle OA Shortfalls (if needed)

If any journal has fewer than 50 PDFs, run:

```powershell
python src/supplement.py
```

This prints per-missing-paper instructions ("Check UoG library" / "Email author")
and writes `data/missing_papers.csv`. Once you have obtained the PDFs:

1. Place the PDF files in `data/raw/` (using the same filename format as
   download.py: slashes and dots in the DOI replaced with underscores).
2. Add entries for each paper to `data/manual_manifest.jsonl` using the same
   schema as `data/manifest.jsonl`.
3. Re-run `python pipeline.py` to confirm the updated corpus count.

The pipeline accepts a final corpus of `250 - total_missing` OA papers if at
least 200 OA PDFs were downloaded. The 200-paper threshold ensures enough data
for a meaningful Phase 4 analysis even without complete manual supplementation.

## Useful Commands

Run the simple startup check:

```powershell
python src/main.py
```

Run the Phase 2 tests with pytest:

```powershell
python -m pytest tests/manual_test_phase2.py -v
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
  `NO_PDF_URL`, etc.) rather than a generic message.
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

If `pipeline.py` warns about manual manifest entries with missing PDFs:
make sure the PDF file is in `data/raw/` with the correct filename (DOI with
`/`, `:`, and `.` replaced by `_`, plus `.pdf` extension).

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
