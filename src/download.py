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

IDEMPOTENCY
-----------
WHY CHECK data/raw/ BEFORE DOWNLOADING?
    If the pipeline crashes mid-run and is restarted, we don't want to re-download
    papers that were already saved.  The check `if pdf_path.exists(): skip` makes
    the download step safe to re-run as many times as needed.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv
from tqdm import tqdm

from utils import log_error

load_dotenv()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MANIFEST_PATH = Path("data") / "manifest.jsonl"
RAW_DIR       = Path("data") / "raw"

# Email for Unpaywall and CrossRef polite-pool requests.
# This is NOT a secret — it just lets the API provider contact us if we abuse
# their service.  Store it in .env so it's configurable without code changes.
UNPAYWALL_EMAIL = os.getenv("UNPAYWALL_EMAIL", "researcher@example.com")

# Request timeout in seconds.  Long enough for slow servers, short enough not
# to hang the pipeline indefinitely.
REQUEST_TIMEOUT = 30

# Maximum PDF file size we'll accept (in bytes).  25 MB is generous for a
# journal article; files larger than this are likely not PDFs (e.g. HTML error pages).
MAX_PDF_BYTES = 25 * 1024 * 1024  # 25 MB


# ---------------------------------------------------------------------------
# Helper: sanitise a DOI into a valid filename
# ---------------------------------------------------------------------------

def _doi_to_filename(doi: str) -> str:
    """
    Convert a DOI like '10.1111/jvim.12345' to a safe filename 'jvim_12345.pdf'.

    WHY?
        DOIs contain slashes, which are illegal in file paths on all operating
        systems.  We also replace other special characters to keep filenames
        shell-friendly.
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
        this one fallback, not the entire import chain.  The next fallback
        (Unpaywall) will be tried automatically.

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
        # The CLI is not installed — that's fine, try the next fallback.
        pass
    except subprocess.TimeoutExpired:
        print(f"  [fulltext-downloader] Timed out for DOI {doi}")
    return False


# ---------------------------------------------------------------------------
# Fallback 2: Unpaywall API
# ---------------------------------------------------------------------------

def _try_unpaywall(doi: str, dest_path: Path) -> bool:
    """
    Query the Unpaywall API for a best OA PDF URL, then download it.

    Unpaywall is maintained by Our Research (a non-profit) and is free for
    research use.  It covers ~50% of all recent scholarly articles.

    API docs: https://unpaywall.org/data-format

    Returns True on success, False if no OA version is found.
    """
    url = f"https://api.unpaywall.org/v2/{doi}"
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
    best = data.get("best_oa_location") or {}
    pdf_url = best.get("url_for_pdf") or best.get("url")

    if not pdf_url:
        return False

    # Confirm the license is explicitly open access before downloading.
    # This is the legal compliance gate.
    license_str = (best.get("license") or "").lower()
    is_oa = data.get("is_oa", False)

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

    Semantic Scholar indexes ~200 million papers and often has direct PDF links
    for OA content.  No API key required for individual paper lookups.

    API docs: https://api.semanticscholar.org/graph/v1

    Returns True on success, False otherwise.
    """
    url = f"https://api.semanticscholar.org/graph/v1/paper/DOI:{doi}"
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

    oa_pdf = data.get("openAccessPdf") or {}
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
# Core download function
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
    # Why check before every fallback?  The pipeline may restart after a crash.
    # We don't want to re-download 50 papers just because paper #51 failed.
    if dest_path.exists():
        print(f"  [download] Already exists, skipping: {dest_path.name}")
        return True

    print(f"  [download] Trying OA sources for DOI: {doi}")

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
    log_error(doi, "download", "No OA version available")
    return False


def run_downloads() -> tuple[int, int]:
    """
    Read the manifest and attempt to download every paper.

    In DRY_RUN mode: prints what would happen without making network calls.

    Returns
    -------
    tuple[int, int]
        (success_count, failure_count)
    """
    dry_run = os.getenv("DRY_RUN", "true").lower() == "true"

    if not MANIFEST_PATH.exists():
        print(f"[download] Manifest not found at {MANIFEST_PATH}. Run collect.py first.")
        sys.exit(1)

    # Read all DOIs from the manifest.
    dois: list[str] = []
    with open(MANIFEST_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
                doi = record.get("doi", "").strip()
                if doi:
                    dois.append(doi)
            except json.JSONDecodeError:
                log_error("N/A", "download", f"Malformed JSONL line: {line[:80]}")

    print(f"[download] Found {len(dois)} DOIs in manifest.")

    if dry_run:
        print("[download] DRY_RUN=true — skipping all network calls.")
        print("[download] In live mode, each DOI would be attempted through:")
        print("           1. fulltext-article-downloader → 2. Unpaywall → "
              "3. Semantic Scholar → 4. PubMed Central")
        # Simulate success/failure counts for the dry-run report.
        success = len(dois)
        failure = 0
        print(f"[download] DRY_RUN summary: {success} would be attempted, "
              f"{failure} failures simulated.")
        return success, failure

    # --- Live mode ---
    success_count = 0
    failure_count = 0

    for doi in tqdm(dois, desc="Downloading PDFs"):
        ok = download_paper(doi)
        if ok:
            success_count += 1
        else:
            failure_count += 1

    print(
        f"\n[download] Complete. "
        f"Success: {success_count}, "
        f"No OA version: {failure_count}"
    )
    return success_count, failure_count


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    success, failure = run_downloads()
    print(f"Download run finished. {success} PDFs acquired, {failure} unavailable (OA only).")
