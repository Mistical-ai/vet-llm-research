"""
src/download.py — Automated Open-Access PDF Acquisition
=========================================================

WHY DOES THIS MODULE EXIST?
-----------------------------
A language model can summarise a paper far better from its full text than
from a 250-word abstract.  This module attempts to download the open-access
full-text PDF for every paper in the manifest so that extract.py has something
to work with.

LEGAL CONSTRAINT (NON-NEGOTIABLE)
-----------------------------------
We ONLY download content that is explicitly marked as open access by the
publisher or an OA metadata service.  We do NOT:
  - Attempt to log in to journal websites.
  - Use a VPN to circumvent geographic or institutional access controls.
  - Use browser automation (Selenium, Playwright) to scrape subscription content.
  - Access Sci-Hub or similar shadow libraries.

Violating publisher terms of service could jeopardise the University of Guelph's
institutional access agreements.  The study will acknowledge in its limitations
section that the full-text corpus may be smaller than 250 papers because not all
papers are open access.

FALLBACK CHAIN (in priority order)
------------------------------------
1. fulltext-article-downloader CLI (if installed via pip):
   A specialist tool that queries multiple OA repositories at once.
   We call it as a subprocess so a missing installation doesn't crash the import.

2. Unpaywall API (https://api.unpaywall.org):
   A well-maintained, free service that returns the best OA PDF URL for a DOI.
   We need an email address in the request header (set via UNPAYWALL_EMAIL in .env).

3. Semantic Scholar Open Access PDF URL:
   Semantic Scholar's API returns an `openAccessPdf` field for many papers.
   No API key required for basic queries.

4. PubMed Central (via NCBI E-utils):
   For papers indexed in PMC, we can fetch the full-text PDF directly.
   Requires a DOI-to-PMCID lookup step.

If all four sources fail to find an OA version, we log the failure and move on.
The pipeline is designed to work with whatever subset of PDFs is legally available.

BALANCED CORPUS DOWNLOAD LOGIC
---------------------------------
This step enforces per-journal PDF quotas (50 PDFs per journal, 250 total)
through three mechanisms:

  1. STOP-LOSS: Once a journal reaches its 50-PDF quota, remaining candidates
     for that journal are skipped.  This prevents over-downloading from high-OA
     journals while under-collecting from lower-OA ones.

  2. MAX_FAILED_PER_JOURNAL: If a journal exhausts its failure budget before
     reaching 50 PDFs, it is marked for manual supplementation rather than
     burning through all 80 candidates pointlessly.  Default = 100 (set via
     .env to override).

  3. SHORTFALL LOGGING: If any journal ends below 50 PDFs, a structured entry
     is written to data/error_log.jsonl (stage="insufficient_oa") and
     data/missing_papers.csv is generated for manual supplementation.

PROGRESS BAR
--------------
The bar shows "Successful PDFs / 250" rather than "DOIs attempted / total".
This gives a more actionable picture: the bar moves only when a PDF is actually
saved, so stalling reveals OA problems immediately rather than after the run.

VERBOSITY
----------
Set DOWNLOAD_VERBOSE=false in .env to suppress per-DOI attempt and failure
messages.  Summary lines (per-journal status) and final counts always print
regardless of the verbosity flag.  All failures are still written to
data/error_log.jsonl regardless of DOWNLOAD_VERBOSE.

IDEMPOTENCY
-----------
WHY CHECK data/raw/ BEFORE DOWNLOADING?
    If the pipeline crashes mid-run and is restarted, we don't want to re-download
    papers that were already saved.  Checking `if pdf_path.exists(): skip` makes
    the download step safe to re-run as many times as needed.
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv
from tqdm import tqdm

from utils import log_error, ERROR_LOG_PATH
from collect import JOURNAL_TARGETS
from supplement import write_missing_report

load_dotenv()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MANIFEST_PATH = Path("data") / "manifest.jsonl"
RAW_DIR       = Path("data") / "raw"

# Email for Unpaywall and CrossRef polite-pool requests.
# Not a secret — just our contact email so the API provider can reach us.
UNPAYWALL_EMAIL = os.getenv("UNPAYWALL_EMAIL", "researcher@example.com")

REQUEST_TIMEOUT = 30

# Maximum PDF file size we'll accept.  25 MB is generous for a journal article;
# files larger than this are likely HTML error pages, not real PDFs.
MAX_PDF_BYTES = 25 * 1024 * 1024  # 25 MB

# WHY A VERBOSE FLAG?
# Per-DOI "No OA version available" messages can flood the terminal for
# high-failure journals, hiding the per-journal summaries that actually matter.
# DOWNLOAD_VERBOSE=false keeps the terminal clean: only per-journal summaries
# and final counts print.  All failures are still written to data/error_log.jsonl
# regardless of this flag (see _log_download_failure for the separation).
VERBOSE: bool = os.getenv("DOWNLOAD_VERBOSE", "true").lower() == "true"

# WHY MAX_FAILED_PER_JOURNAL?
# Without a per-journal failure cap, a journal whose 80 candidates are mostly
# paywalled would exhaust all 80 attempts before surfacing a shortfall.  Capping
# failures early lets us detect a low-OA journal and pivot to manual
# supplementation instead of making 80+ fruitless network requests.
# Default 100 = 1.25 × the 80-candidate pool, effectively meaning "exhaust the
# pool" unless you set a tighter value (e.g. 40 to save time at the cost of
# potentially missing a few late-pool OA papers).
MAX_FAILED_PER_JOURNAL: int = int(os.getenv("MAX_FAILED_PER_JOURNAL", "100"))


# ---------------------------------------------------------------------------
# Verbosity and error-logging helpers
# ---------------------------------------------------------------------------

def _vprint(*args, **kwargs) -> None:
    """
    Print only when DOWNLOAD_VERBOSE=true (the default).

    Used for per-DOI attempt messages and intermediate status lines.
    Summary lines and warnings always use print() directly.
    """
    if VERBOSE:
        print(*args, **kwargs)


def _log_download_failure(doi: str, message: str) -> None:
    """
    Write a per-DOI download failure to the error ledger.

    WHY NOT ALWAYS CALL utils.log_error()?
        utils.log_error() both writes to the JSONL file AND prints to stdout.
        In non-verbose mode we want to suppress the stdout print (to reduce
        noise) while still writing to the file (for later analysis).
        Splitting these responsibilities into a conditional here avoids changing
        the utils.py API.

    WHY WRITE TO THE FILE EVEN IN NON-VERBOSE MODE?
        The error ledger is the authoritative record of what failed.  A
        researcher running supplement.py after the fact needs complete data.
    """
    if VERBOSE:
        # verbose: use the canonical log_error which writes + prints.
        log_error(doi, "download", message)
    else:
        # non-verbose: write to file only, no stdout.
        ERROR_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "doi":       doi,
            "stage":     "download",
            "message":   message,
        }
        with open(ERROR_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")


def _log_insufficient_oa(journal: str, obtained: int, missing: int) -> None:
    """
    Write a journal-level OA shortfall entry to the error ledger.

    WHY A SEPARATE FUNCTION (not a call to utils.log_error)?
        The shortfall entry needs richer structure than log_error's
        (doi, stage, message) schema.  We write directly to ERROR_LOG_PATH
        with a superset schema: journal, obtained, missing, supplement_needed.
        This makes shortfall entries programmatically distinguishable from
        per-DOI failures by stage="insufficient_oa".

    Parameters
    ----------
    journal  : str — Journal short name (e.g. "JVIM").
    obtained : int — Number of PDFs successfully downloaded.
    missing  : int — Number of PDFs still needed to reach the 50-PDF quota.
    """
    ERROR_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "timestamp":        datetime.now(timezone.utc).isoformat(),
        "doi":              "N/A",
        "stage":            "insufficient_oa",
        "journal":          journal,
        "obtained":         obtained,
        "missing":          missing,
        "supplement_needed": True,
    }
    with open(ERROR_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")

    target = obtained + missing
    print(
        f"[download] WARNING: {journal} shortfall — "
        f"{obtained}/{target} OA PDFs found, "
        f"{missing} paper(s) need manual supplementation."
    )


# ---------------------------------------------------------------------------
# Helper: sanitise a DOI into a valid filename
# ---------------------------------------------------------------------------

def _doi_to_filename(doi: str) -> str:
    """
    Convert a DOI like '10.1111/jvim.12345' to a safe filename.

    WHY?
        DOIs contain slashes, which are illegal in file paths on all operating
        systems.  We also replace other special characters to keep filenames
        shell-friendly.

    NOTE: supplement.py contains an identical copy of this function.  They are
    intentionally kept in sync to avoid a circular import: download → supplement.
    """
    safe = doi.replace("/", "_").replace(":", "_").replace(".", "_")
    return f"{safe}.pdf"


# ---------------------------------------------------------------------------
# Helper: write bytes to disk only if they look like a real PDF
# ---------------------------------------------------------------------------

def _save_pdf(content: bytes, path: Path) -> bool:
    """
    Save `content` to `path` if it starts with the PDF magic bytes (%PDF).

    Returns True on success, False if the content is not a valid PDF.

    WHY CHECK MAGIC BYTES?
        Some OA repositories return an HTML error page (with HTTP 200!) when a
        PDF is unavailable.  Saving an HTML file with a .pdf extension would
        cause pdfplumber to crash in extract.py.  The magic byte check is a
        simple, reliable guard.
    """
    if not content.startswith(b"%PDF"):
        return False

    if len(content) > MAX_PDF_BYTES:
        return False

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return True


# ---------------------------------------------------------------------------
# Fallback 1: fulltext-article-downloader CLI
# ---------------------------------------------------------------------------

def _try_fulltext_downloader(doi: str, dest_path: Path) -> bool:
    """
    Attempt download using the `fulltext-download` CLI tool.

    WHY SUBPROCESS INSTEAD OF IMPORT?
        `fulltext-article-downloader` may not be installed on every system.
        Calling it as a subprocess means a missing installation only fails
        this one fallback, not the entire import chain.

    Returns True if the PDF was saved successfully, False otherwise.
    """
    try:
        result = subprocess.run(
            ["fulltext-download", doi, "--output", str(dest_path)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode == 0 and dest_path.exists():
            print(f"  [fulltext-downloader] Saved {dest_path.name}")
            return True
    except FileNotFoundError:
        # The CLI is not installed — fine, try the next fallback.
        pass
    except subprocess.TimeoutExpired:
        _vprint(f"  [fulltext-downloader] Timed out for DOI {doi}")
    return False


# ---------------------------------------------------------------------------
# Fallback 2: Unpaywall API
# ---------------------------------------------------------------------------

def _try_unpaywall(doi: str, dest_path: Path) -> bool:
    """
    Query the Unpaywall API for a best OA PDF URL, then download it.

    Unpaywall covers ~50% of all recent scholarly articles.
    API docs: https://unpaywall.org/data-format

    Returns True on success, False if no OA version is found.
    """
    url    = f"https://api.unpaywall.org/v2/{doi}"
    params = {"email": UNPAYWALL_EMAIL}

    try:
        resp = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
        if resp.status_code == 404:
            return False  # DOI not in Unpaywall — not an error, just unlisted.
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException:
        return False

    # `best_oa_location` is Unpaywall's recommended OA source.
    best    = data.get("best_oa_location") or {}
    pdf_url = best.get("url_for_pdf") or best.get("url")

    if not pdf_url:
        return False

    # Confirm the paper is explicitly open access before downloading.
    # This is the legal compliance gate — we never download non-OA content.
    license_str = (best.get("license") or "").lower()
    is_oa       = data.get("is_oa", False)

    if not is_oa:
        return False

    try:
        pdf_resp = requests.get(pdf_url, timeout=REQUEST_TIMEOUT)
        pdf_resp.raise_for_status()
        if _save_pdf(pdf_resp.content, dest_path):
            print(f"  [Unpaywall] Saved {dest_path.name} (license: {license_str or 'OA'})")
            return True
    except requests.RequestException:
        pass

    return False


# ---------------------------------------------------------------------------
# Fallback 3: Semantic Scholar OA PDF
# ---------------------------------------------------------------------------

def _try_semantic_scholar(doi: str, dest_path: Path) -> bool:
    """
    Check Semantic Scholar's API for an open access PDF URL.

    No API key required for individual paper lookups.
    API docs: https://api.semanticscholar.org/graph/v1

    Returns True on success, False otherwise.
    """
    url    = f"https://api.semanticscholar.org/graph/v1/paper/DOI:{doi}"
    params = {"fields": "openAccessPdf,isOpenAccess"}

    try:
        resp = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
        if resp.status_code == 404:
            return False
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException:
        return False

    if not data.get("isOpenAccess"):
        return False

    oa_pdf  = data.get("openAccessPdf") or {}
    pdf_url = oa_pdf.get("url")

    if not pdf_url:
        return False

    try:
        pdf_resp = requests.get(pdf_url, timeout=REQUEST_TIMEOUT)
        pdf_resp.raise_for_status()
        if _save_pdf(pdf_resp.content, dest_path):
            print(f"  [Semantic Scholar] Saved {dest_path.name}")
            return True
    except requests.RequestException:
        pass

    return False


# ---------------------------------------------------------------------------
# Fallback 4: PubMed Central
# ---------------------------------------------------------------------------

def _try_pubmed_central(doi: str, dest_path: Path) -> bool:
    """
    Look up the DOI in PubMed Central via NCBI E-utils, then download the PDF.

    PMC is a free archive maintained by the US National Library of Medicine.
    Many veterinary journals deposit OA papers there automatically.

    Returns True on success, False if the paper is not in PMC.
    """
    # Step 1: Resolve DOI to PMCID using the NCBI ID converter.
    id_url = "https://www.ncbi.nlm.nih.gov/pmc/utils/idconv/v1.0/"
    try:
        resp = requests.get(
            id_url,
            params={"ids": doi, "format": "json"},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        records = resp.json().get("records", [])
    except requests.RequestException:
        return False

    if not records or "pmcid" not in records[0]:
        return False

    pmcid = records[0]["pmcid"].replace("PMC", "")

    # Step 2: Download the PDF from PMC's FTP-style web interface.
    pdf_url = f"https://www.ncbi.nlm.nih.gov/pmc/articles/PMC{pmcid}/pdf/"

    try:
        pdf_resp = requests.get(pdf_url, timeout=REQUEST_TIMEOUT)
        pdf_resp.raise_for_status()
        if _save_pdf(pdf_resp.content, dest_path):
            print(f"  [PubMed Central] Saved {dest_path.name} (PMC{pmcid})")
            return True
    except requests.RequestException:
        pass

    return False


# ---------------------------------------------------------------------------
# Core single-paper download function
# ---------------------------------------------------------------------------

def download_paper(doi: str) -> bool:
    """
    Attempt to download the OA PDF for a single paper through the fallback chain.

    Idempotency: if the PDF already exists in data/raw/, this function returns
    True immediately without making any network requests.

    Returns True if a PDF is available in data/raw/ at the end of the call,
    False otherwise.
    """
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    dest_path = RAW_DIR / _doi_to_filename(doi)

    # --- Idempotency check ---
    # The pipeline may restart after a crash.  We don't want to re-download
    # papers that were already saved.
    if dest_path.exists():
        _vprint(f"  [download] Already exists, skipping: {dest_path.name}")
        return True

    _vprint(f"  [download] Trying OA sources for DOI: {doi}")

    # Try each fallback in priority order.  Stop as soon as one succeeds.
    if _try_fulltext_downloader(doi, dest_path):
        return True
    if _try_unpaywall(doi, dest_path):
        return True
    if _try_semantic_scholar(doi, dest_path):
        return True
    if _try_pubmed_central(doi, dest_path):
        return True

    # All fallbacks exhausted — log and continue.
    _log_download_failure(doi, "No OA version available")
    return False


# ---------------------------------------------------------------------------
# Manifest loader: group records by journal
# ---------------------------------------------------------------------------

def _load_manifest_by_journal() -> dict[str, list[dict]]:
    """
    Parse manifest.jsonl and return records grouped by journal.

    WHY RETURN A DICT OF LISTS (not a flat list)?
        The download loop processes journals one at a time, enforcing per-journal
        quotas and failure caps.  A dict[journal → records] allows O(1) lookup
        and makes the loop logic explicit.

    WHY DEDUPLICATE BY DOI?
        collect.py deduplicates within a single run, but the manifest is
        append-only — running collect.py twice without deleting the manifest
        can produce duplicate DOIs.  Deduplicating here ensures download.py
        never attempts the same paper twice in one run.

    Returns
    -------
    dict[str, list[dict]]
        Keys are journal names matching JOURNAL_TARGETS.
        Values are lists of manifest records in manifest order (newest-first
        if collect.py used sort=published desc).
    """
    # Initialise one queue per target journal (unknown journals are ignored).
    journal_queues: dict[str, list[dict]] = {j: [] for j in JOURNAL_TARGETS}
    seen_dois: set[str] = set()

    with open(MANIFEST_PATH, encoding="utf-8") as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                record  = json.loads(line)
                doi     = record.get("doi", "").strip()
                journal = record.get("journal", "").strip()
            except json.JSONDecodeError:
                log_error("N/A", "download", f"Malformed JSONL line {line_num}: {line[:80]}")
                continue

            if not doi or not journal:
                log_error("N/A", "download", f"Line {line_num}: missing doi or journal field")
                continue

            if doi in seen_dois:
                _vprint(f"[download] Skipping duplicate DOI {doi} (line {line_num})")
                continue

            seen_dois.add(doi)

            if journal in journal_queues:
                journal_queues[journal].append(record)
            else:
                _vprint(f"[download] Unknown journal '{journal}' on line {line_num}, skipping.")

    print("[download] Manifest loaded.  Per-journal candidate counts:")
    for journal, queue in journal_queues.items():
        target = JOURNAL_TARGETS.get(journal, 50)
        print(f"  {journal:<25} {len(queue):>3} candidates  (target: {target} PDFs)")

    return journal_queues


# ---------------------------------------------------------------------------
# Core download orchestration
# ---------------------------------------------------------------------------

def run_downloads() -> tuple[int, int]:
    """
    Read the manifest and download PDFs using balanced per-journal quotas.

    BALANCED DOWNLOAD SCHEDULING
    -----------------------------
    For each journal, we iterate its manifest queue until one of three
    conditions is met:
      a. journal_success[journal] >= JOURNAL_TARGETS[journal]  → STOP-LOSS
      b. failure_count >= MAX_FAILED_PER_JOURNAL                → FAIL-CAP
      c. The manifest queue is exhausted                        → SHORTFALL

    WHY SEQUENTIAL (journal-by-journal) NOT ROUND-ROBIN?
        Sequential is simpler to reason about and produces the same balanced
        result.  Round-robin would interleave journals, which could make tqdm
        output confusing and doesn't change the final distribution.

    In DRY_RUN mode: simulates the balanced flow (counting existing PDFs and
    showing per-journal projections) without any network calls.

    Returns
    -------
    tuple[int, int]
        (total_success, total_shortfall) — total PDFs acquired and total
        papers that remain unavailable as OA.
    """
    dry_run = os.getenv("DRY_RUN", "true").lower() == "true"

    if not MANIFEST_PATH.exists():
        print(f"[download] Manifest not found at {MANIFEST_PATH}. Run collect.py first.")
        sys.exit(1)

    journal_queues = _load_manifest_by_journal()

    total_target = sum(JOURNAL_TARGETS.values())

    # Pre-count PDFs already in data/raw/ — these count toward the quota even
    # in dry-run mode, so a re-run after a partial live run shows correct counts.
    journal_success: dict[str, int] = {}
    for journal, queue in journal_queues.items():
        journal_success[journal] = sum(
            1 for r in queue
            if (RAW_DIR / _doi_to_filename(r["doi"])).exists()
        )

    initial_success = sum(journal_success.values())

    # --- DRY-RUN MODE ---
    if dry_run:
        print(f"\n[download] DRY_RUN=true — no network calls.")
        print(f"[download] Simulating balanced download (existing PDFs already counted).\n")
        print(f"  {'Journal':<25} {'Exist':>5}  {'Would add':>9}  {'Shortfall':>9}")
        print("  " + "-" * 55)

        total_would_add  = 0
        total_shortfall  = 0

        for journal, queue in journal_queues.items():
            target      = JOURNAL_TARGETS.get(journal, 50)
            existing    = journal_success[journal]
            candidates  = len(queue) - existing          # not yet downloaded
            can_add     = min(candidates, max(0, target - existing))
            shortfall   = max(0, target - existing - can_add)
            total_would_add  += can_add
            total_shortfall  += shortfall
            print(f"  {journal:<25} {existing:>5}  {can_add:>9}  {shortfall:>9}")

        print("  " + "-" * 55)
        simulated_total = initial_success + total_would_add
        print(f"  {'TOTAL':<25} {initial_success:>5}  {total_would_add:>9}  {total_shortfall:>9}")

        # Simulate the progress bar so the researcher can verify it works.
        print()
        with tqdm(
            total=total_target,
            initial=initial_success,
            desc="Successful PDFs [DRY-RUN]",
        ) as pbar:
            for journal, queue in journal_queues.items():
                target   = JOURNAL_TARGETS.get(journal, 50)
                existing = journal_success[journal]
                would_add = min(
                    len(queue) - existing,
                    max(0, target - existing),
                )
                pbar.update(would_add)

        print(
            f"\n[download] DRY_RUN summary: "
            f"{simulated_total}/{total_target} PDFs would be acquired, "
            f"{total_shortfall} shortfall."
        )
        if total_shortfall > 0:
            print("[download] Run python src/supplement.py to see manual supplement instructions.")
        return simulated_total, total_shortfall

    # --- LIVE MODE ---
    print(
        f"\n[download] Targeting {total_target} PDFs across {len(JOURNAL_TARGETS)} journals "
        f"(50 per journal)."
    )
    if initial_success > 0:
        print(f"[download] {initial_success} PDFs already in data/raw/ — counting toward quotas.")

    total_shortfall = 0
    missing_papers: list[dict] = []

    # Progress bar: tracks successful PDFs / 250, not DOIs attempted.
    # WHY SUCCESS-ONLY UPDATES?
    #   A bar that advances on every attempted DOI would race ahead and then
    #   stall.  Updating only on success shows the researcher whether the run
    #   is on track to hit 250 or whether OA availability is lower than expected.
    with tqdm(
        total=total_target,
        initial=initial_success,
        desc="Successful PDFs",
        unit="pdf",
    ) as pbar:

        for journal, queue in journal_queues.items():
            target        = JOURNAL_TARGETS.get(journal, 50)
            failure_count = 0

            if journal_success[journal] >= target:
                _vprint(f"[download] {journal}: already at quota ({target} PDFs), skipping.")
                continue

            _vprint(
                f"\n[download] Processing {journal} "
                f"({len(queue)} candidates, need "
                f"{target - journal_success[journal]} more PDFs)..."
            )

            for record in queue:
                doi = record["doi"]

                # STOP-LOSS: quota reached for this journal.
                if journal_success[journal] >= target:
                    break

                # FAIL-CAP: too many consecutive failures — stop and flag for supplement.
                if failure_count >= MAX_FAILED_PER_JOURNAL:
                    _vprint(
                        f"[download] {journal}: MAX_FAILED_PER_JOURNAL "
                        f"({MAX_FAILED_PER_JOURNAL}) reached, stopping early."
                    )
                    break

                # Idempotency: already counted in journal_success above.
                if (RAW_DIR / _doi_to_filename(doi)).exists():
                    continue

                ok = download_paper(doi)
                if ok:
                    journal_success[journal] += 1
                    pbar.update(1)
                else:
                    failure_count += 1

            # Per-journal summary (always printed regardless of VERBOSE).
            acquired = journal_success[journal]
            print(
                f"[download] {journal}: "
                f"{acquired}/{target} PDFs acquired."
            )

            # Detect and record shortfall for this journal.
            if acquired < target:
                deficit = target - acquired
                total_shortfall += deficit
                _log_insufficient_oa(journal, acquired, deficit)

                # Collect un-downloaded records for the CSV report.
                downloaded_dois = {
                    r["doi"] for r in queue
                    if (RAW_DIR / _doi_to_filename(r["doi"])).exists()
                }
                for record in queue:
                    if record["doi"] not in downloaded_dois:
                        missing_papers.append({
                            "journal":        journal,
                            "doi":            record.get("doi", ""),
                            "title":          record.get("title", "No title available"),
                            "reason_missing": "No OA version found",
                        })

    total_success = sum(journal_success.values())

    print(f"\n[download] Complete.")
    print(f"  Total PDFs acquired : {total_success} / {total_target}")

    if total_shortfall > 0:
        print(f"  OA shortfall       : {total_shortfall} papers unavailable as OA")
        print(f"  See data/missing_papers.csv for manual supplement instructions.")
        # Write missing_papers.csv using the shared function from supplement.py.
        # WHY CALL supplement.write_missing_report HERE?
        #   download.py detects the shortfall and already has all the metadata
        #   (journal, doi, title) in memory.  Calling write_missing_report here
        #   means the researcher gets the CSV immediately after the download run,
        #   without needing to run supplement.py separately.  The researcher can
        #   still re-run supplement.py later to regenerate the report.
        write_missing_report(missing_papers)
    else:
        print(f"  All journal quotas met!")

    return total_success, total_shortfall


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    success, shortfall = run_downloads()
    print(
        f"Download run finished. "
        f"{success} PDFs acquired, "
        f"{shortfall} papers need manual supplementation (OA-only pipeline)."
    )
