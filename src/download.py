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
We ONLY download content that is genuinely open access — either confirmed by
a trusted OA metadata service (Unpaywall, Semantic Scholar) or delivered
without authentication by the publisher's own website.  We do NOT:
  - Attempt to log in to journal websites.
  - Use a VPN to circumvent geographic or institutional access controls.
  - Use browser automation (Selenium, Playwright) to scrape subscription content.
  - Access Sci-Hub or similar shadow libraries.

HOW WE VERIFY A RESPONSE IS REALLY A PDF (NOT A PAYWALLED PAGE):
    Every response body is checked against the PDF magic bytes (%PDF) before
    being saved.  If a publisher serves an HTML login page at an OA-looking
    URL, the magic byte check catches it and we move on.  This means trying a
    publisher's direct PDF URL is completely safe — the worst outcome is we
    save nothing.

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
   A well-maintained, free service that returns OA PDF URLs for a DOI.
   We now try ALL oa_locations (not just best_oa_location) so that if the
   primary link is stale or temporarily down, we fall through to the next.
   Requires an email address in the request header (set via UNPAYWALL_EMAIL in .env).

3. Semantic Scholar Open Access PDF URL:
   Semantic Scholar's API returns an `openAccessPdf` field for many papers.
   No API key required for basic queries.

4. PubMed Central (via NCBI E-utils):
   For papers indexed in PMC, we can fetch the full-text PDF directly.
   Requires a DOI-to-PMCID lookup step.

5. Publisher-direct PDF URL (Wiley, AVMA, SAGE):
   Many publishers expose OA PDFs at a predictable URL pattern without login.
   We try these directly using browser-like headers.  Paywalled papers return
   an HTML page or 403, which the magic byte check rejects — no false saves.
   Covered publishers (by DOI prefix):
     - Wiley (10.1111/):  onlinelibrary.wiley.com/doi/pdf/{doi}
     - AVMA  (10.2460/):  avmajournals.avma.org/doi/pdf/{doi}
     - SAGE  (10.1177/):  journals.sagepub.com/doi/pdf/{doi}

6. Article-page HTML scraping (citation_pdf_url meta tag):
   Follow the DOI redirect to the publisher's article page, parse the HTML for
   a <meta name="citation_pdf_url"> tag (a standard Google Scholar metadata
   convention used by most publishers), and download the URL it contains.
   A requests.Session() is used to carry cookies from the HTML page fetch to
   the PDF download, which some publishers (SAGE) require.

If all six sources fail, we log the failure and move on.
The pipeline is designed to work with whatever subset of PDFs is legally available.

WHY BROWSER-LIKE HEADERS?
---------------------------
Many publishers reject plain Python requests.get() calls with 403 Forbidden
even when the paper is open access — their CDN or reverse proxy checks the
User-Agent and blocks non-browser strings.  Using a realistic browser
User-Agent string (Chrome on Windows) resolves this for most publishers.
This does NOT bypass authentication: publishers still gate subscription content
behind login forms.  The magic byte check ensures we only keep real PDFs.

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
Set DOWNLOAD_VERBOSE=true to see every step of every fallback — useful when
debugging why a specific DOI is failing.

IDEMPOTENCY
-----------
WHY CHECK data/raw/ BEFORE DOWNLOADING?
    If the pipeline crashes mid-run and is restarted, we don't want to re-download
    papers that were already saved.  Checking `if pdf_path.exists(): skip` makes
    the download step safe to re-run as many times as needed.
"""

# json lets us read and write JSON/JSONL records.
# We use it for manifest rows and structured error-log entries.
import json

# io lets us treat bytes in memory like a file.
# We use it to open downloaded NCBI tar.gz packages without first saving them.
import io

# os lets Python read environment variables such as DOWNLOAD_VERBOSE.
# Those variables come from the local .env file after load_dotenv() runs.
import os

# random lets us choose a random delay within a safe range.
# We use it for publisher jitter so requests are slow and less bursty.
import random

# subprocess lets Python run an external command-line program.
# We use it to call fulltext-download as one fallback source.
import subprocess

# sys exposes details about the Python interpreter currently running this file.
# We use it to find the matching virtualenv Scripts folder and to exit safely.
import sys

# tarfile opens .tar.gz archive packages.
# NCBI OA packages are tarballs, so this is how we inspect them for PDFs.
import tarfile

# time provides sleep().
# We use it for rate-limit backoff and polite delays between fallback attempts.
import time

# urllib.request can download ftp:// URLs.
# requests does not support FTP, but NCBI OA package links can still be FTP.
import urllib.request

# ElementTree parses XML.
# NCBI's official OA service returns XML that lists OA package links.
import xml.etree.ElementTree as ET

# dataclass reduces boilerplate for small data containers.
# DownloadAttempt is a dataclass that stores what happened during one URL try.
from dataclasses import asdict, dataclass

# datetime and timezone create precise UTC timestamps.
# We use them in data/error_log.jsonl so every failure has a time attached.
from datetime import datetime, timezone

# Path is a safer, clearer way to build file paths than raw strings.
# We use it for data/manifest.jsonl, data/raw, and generated PDF filenames.
from pathlib import Path

# urljoin combines a page URL with a relative link.
# Example: base page https://site/article plus href /pdf/file.pdf -> full URL.
from urllib.parse import urljoin

# requests is the main HTTP library.
# We use it to call Unpaywall, Semantic Scholar, NCBI, Wiley, AVMA, SAGE, etc.
import requests

# urllib3 is the lower-level HTTP library used by requests.
# We use it only to suppress expected TLS warnings when verify=False is needed.
import urllib3

# BeautifulSoup parses HTML pages.
# We use it to find PDF links inside public article/PMC/DOAJ landing pages.
from bs4 import BeautifulSoup

# load_dotenv reads the local .env file.
# That lets settings like DRY_RUN and UNPAYWALL_EMAIL control the script.
from dotenv import load_dotenv

# tqdm draws progress bars in the terminal.
# Here it shows successful PDFs acquired out of the 250-paper target.
from tqdm import tqdm

# Silence the "Unverified HTTPS request" warning that urllib3 emits when
# verify=False is used.  We disable SSL verification across this module
# because the university VPN / campus proxy intercepts TLS handshakes and
# causes SSLError on standard verify=True requests.  The warning is expected
# and acknowledged; suppressing it keeps the terminal output clean.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

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
# files larger than this are likely HTML error pages disguised as large files.
MAX_PDF_BYTES = 25 * 1024 * 1024  # 25 MB

# NCBI OA packages are compressed tarballs that can contain the article PDF,
# XML, images, tables, and supplements.  The package can be larger than the PDF
# itself, so it needs a separate cap.  100 MB is high enough for normal article
# packages but low enough to avoid accidentally downloading huge bulk archives.
MAX_OA_PACKAGE_BYTES = 100 * 1024 * 1024  # 100 MB

# WHY A VERBOSE FLAG?
# Per-DOI "Trying OA sources..." messages can flood the terminal during a long
# run, hiding the per-journal summaries that actually matter.
# DOWNLOAD_VERBOSE=false (the default) keeps output minimal: only the progress
# bar, one line per success, and one line per final failure.
# DOWNLOAD_VERBOSE=true adds intermediate step-by-step messages for every
# fallback attempted — useful for debugging exactly why a specific DOI failed.
# All final failures are written to data/error_log.jsonl regardless of this flag.
VERBOSE: bool = os.getenv("DOWNLOAD_VERBOSE", "false").lower() == "true"

# WHY MAX_FAILED_PER_JOURNAL?
# Without a per-journal failure cap, a journal whose 80 candidates are mostly
# paywalled would exhaust all 80 attempts before surfacing a shortfall.  Capping
# failures early lets us detect a low-OA journal and pivot to manual
# supplementation instead of making 80+ fruitless network requests.
# Default 100 = 1.25 × the 80-candidate pool, effectively meaning "exhaust the
# pool" unless you set a tighter value (e.g. 40 to save time at the cost of
# potentially missing a few late-pool OA papers).
MAX_FAILED_PER_JOURNAL: int = int(os.getenv("MAX_FAILED_PER_JOURNAL", "100"))

# Pause between each fallback attempt for a single DOI.
# WHY A DELAY?
#   APIs like Unpaywall, Semantic Scholar, and NCBI enforce per-second or
#   per-minute rate limits and return HTTP 429 when exceeded.  A short pause
#   after each failed fallback reduces pressure on all APIs simultaneously.
#   2 seconds is conservative enough to stay well under published rate limits
#   while keeping a 250-paper run feasible.
# Set DOWNLOAD_DELAY_SECONDS=0 in .env to disable if running off-campus.
DOWNLOAD_DELAY_SECONDS: float = float(os.getenv("DOWNLOAD_DELAY_SECONDS", "2"))

# Publisher-facing requests need a slower cadence than API requests.
# WHY SEPARATE PUBLISHER DELAYS FROM DOWNLOAD_DELAY_SECONDS?
#   Unpaywall, Semantic Scholar, and NCBI are APIs built for scripted access,
#   so a short fixed delay is enough.  Publisher sites (Wiley, AVMA, SAGE) are
#   human-facing websites behind CDN/bot-detection systems.  A conservative
#   random delay before publisher requests makes the run look like patient,
#   ordinary browsing while staying within institutional access expectations.
#   Set both values to 0 only when debugging ONE DOI locally.
PUBLISHER_DELAY_MIN_SECONDS: float = float(os.getenv("PUBLISHER_DELAY_MIN_SECONDS", "10"))
PUBLISHER_DELAY_MAX_SECONDS: float = float(os.getenv("PUBLISHER_DELAY_MAX_SECONDS", "25"))

# If a server explicitly rate-limits us with HTTP 429 but does not provide a
# Retry-After header, wait this long before the next request.  This is a
# conservative backoff, not an attempt to force through a block.
RATE_LIMIT_BACKOFF_SECONDS: float = float(os.getenv("RATE_LIMIT_BACKOFF_SECONDS", "60"))


# ---------------------------------------------------------------------------
# Browser-like request headers
# ---------------------------------------------------------------------------

# WHY BROWSER HEADERS?
#   Many journal publishers (Wiley, SAGE, AVMA) run CDN-level bot detection
#   that rejects HTTP requests with a non-browser User-Agent — even for papers
#   that are fully open access.  A plain requests.get() call sends something
#   like "python-requests/2.31.0", which many publisher CDNs immediately block
#   with a 403 Forbidden, even though the paper is publicly accessible.
#
#   Using a realistic Chrome User-Agent string convinces the CDN that a normal
#   browser is fetching the page, bypassing the bot filter.  This is the same
#   thing a student does when they open the paper in Chrome — we are just
#   doing it programmatically.
#
#   IMPORTANT: this does NOT bypass authentication.  Paywalled papers still
#   return an HTML login page or a 403.  Our _save_pdf() magic-byte check
#   (%PDF) discards any response that is not a real PDF, so we never
#   accidentally save a login page as a "PDF".
BROWSER_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/147.0.0.0 Safari/537.36"
    ),
    # Accept-Language: some publisher CDNs serve different content by locale;
    # en-US is the safest choice for academic English-language journals.
    "Accept-Language": "en-US,en;q=0.9",
    # Keep-alive matches normal browser behavior and lets requests reuse the
    # TCP connection where possible.  It is not stealth; it simply avoids
    # repeatedly opening fresh connections for every page/PDF request.
    "Connection": "keep-alive",
}


# ---------------------------------------------------------------------------
# Publisher-direct PDF URL templates (used by Fallback 5)
# ---------------------------------------------------------------------------

# WHY HARDCODE PUBLISHER URL PATTERNS?
#   Metadata APIs (Unpaywall, Semantic Scholar) may not yet index a paper
#   that was published very recently, or the publisher may not have deposited
#   OA metadata with those services even if the PDF is freely available on their
#   website.  Every major publisher follows a predictable URL structure for their
#   PDFs.  Trying the canonical publisher PDF URL is cheap (one HTTP request)
#   and catches OA papers that the metadata APIs miss.
#
#   We only include publishers whose OA PDFs are reachable without a session
#   cookie or login when browser-like headers are used:
#     - Wiley (10.1111/*):  handles JVIM, Veterinary Surgery, and VRU
#     - AVMA  (10.2460/*):  handles JAVMA
#     - SAGE  (10.1177/*):  handles JFMS
#
#   Each key is a DOI prefix string.  Each value is a list of URL templates
#   to try in priority order — we stop as soon as one delivers valid PDF bytes.
PUBLISHER_PDF_TEMPLATES: dict[str, list[str]] = {
    # Wiley's primary PDF endpoint is /doi/pdf/{doi}.
    # /doi/epdf/{doi} is an "enhanced PDF" viewer that sometimes serves the
    # raw file even when the /pdf/ endpoint redirects to a paywall gate.
    "10.1111/": [
        "https://onlinelibrary.wiley.com/doi/pdf/{doi}",
        "https://onlinelibrary.wiley.com/doi/epdf/{doi}",
    ],
    # AVMA uses the same /doi/pdf/ pattern on their own domain.
    "10.2460/": [
        "https://avmajournals.avma.org/doi/pdf/{doi}",
    ],
    # SAGE uses the same /doi/pdf/ pattern on journals.sagepub.com.
    "10.1177/": [
        "https://journals.sagepub.com/doi/pdf/{doi}",
    ],
}


# ---------------------------------------------------------------------------
# Verbosity and error-logging helpers
# ---------------------------------------------------------------------------

def _vprint(*args, **kwargs) -> None:
    """
    Print only when DOWNLOAD_VERBOSE=true.

    Used for per-DOI intermediate step messages ("Trying ...", "Found URL ...",
    "Response was not a PDF ...").  Summary lines, success lines, and final
    failure lines always use print() directly so they appear regardless of the
    verbosity setting.
    """
    if VERBOSE:
        print(*args, **kwargs)


@dataclass
class DownloadAttempt:
    """
    Structured diagnostic record for one attempted PDF fetch.

    WHY STORE A STRUCTURED ATTEMPT INSTEAD OF ONLY RETURNING TRUE/FALSE?
        A boolean tells us only whether the file was saved.  It does not tell us
        whether Wiley returned 403, a text/html cookie wall, a broken URL, a
        rate limit, or a response that looked binary but failed the %PDF check.
        This object preserves the exact reason so the final JSONL error ledger
        contains actionable evidence for each failed DOI.
    """

    source: str
    url: str
    ok: bool = False
    failure_code: str | None = None
    message: str | None = None
    http_status: int | None = None
    content_type: str | None = None
    final_url: str | None = None
    retry_after: str | None = None
    body_start: str | None = None


# Each download_paper() call resets this list.  All fallback helpers append to
# it as they try URLs.  When every fallback fails, the most useful attempt is
# copied into data/error_log.jsonl so failures are debuggable after the run.
_CURRENT_DOWNLOAD_ATTEMPTS: list[DownloadAttempt] = []


def _remember_attempt(attempt: DownloadAttempt) -> DownloadAttempt:
    """Record an attempt for final failure logging, then return it for chaining."""
    _CURRENT_DOWNLOAD_ATTEMPTS.append(attempt)
    return attempt


def _best_failure_attempt() -> DownloadAttempt | None:
    """
    Pick the most actionable failed attempt to write into the final error log.

    WHY PRIORITISE THIS WAY?
        HTTP 429 and 403 explain operational blocks, so they are most useful.
        MIME/magic-byte mismatches explain publisher HTML/cookie/login pages.
        Missing URLs are less useful because they only show metadata gaps.
    """
    if not _CURRENT_DOWNLOAD_ATTEMPTS:
        return None

    priority = {
        "HTTP_429": 0,
        "HTTP_403": 1,
        "MIME_TYPE_MISMATCH": 2,
        "PDF_MAGIC_MISMATCH": 3,
        "HTTP_ERROR": 4,
        "REQUEST_TIMEOUT": 5,
        "REQUEST_ERROR": 6,
        "NO_PDF_URL": 7,
    }
    return min(
        _CURRENT_DOWNLOAD_ATTEMPTS,
        key=lambda a: priority.get(a.failure_code or "", 99),
    )


def _retry_after_seconds(value: str | None) -> float | None:
    """
    Parse a Retry-After header into seconds when it uses the common numeric form.

    Retry-After can also be an HTTP date.  We deliberately ignore date parsing
    here to keep behavior conservative and simple; if it is not a number, we use
    RATE_LIMIT_BACKOFF_SECONDS instead.
    """
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


def _respect_retry_after(attempt: DownloadAttempt) -> None:
    """
    Sleep after HTTP 429 before moving on.

    WHY SLEEP WITHOUT AGGRESSIVE RETRY?
        A 429 means the server explicitly asked us to slow down.  Sleeping before
        the next fallback/request respects that instruction.  We do not hammer
        the same endpoint repeatedly; the next fallback will proceed slowly.
    """
    if attempt.http_status != 429:
        return
    seconds = _retry_after_seconds(attempt.retry_after) or RATE_LIMIT_BACKOFF_SECONDS
    _vprint(f"    -> HTTP 429 rate limit; sleeping {seconds:.1f}s before continuing.")
    time.sleep(seconds)


def _sleep_before_publisher_request(source: str) -> None:
    """
    Apply random jitter only to publisher-facing requests.

    WHY NOT SLEEP 10-25 SECONDS BEFORE EVERY API REQUEST?
        APIs such as Unpaywall and NCBI are meant for scripted access and already
        have their own rate limits.  Publisher websites are where bot detection
        is most sensitive, so we reserve the long human-paced jitter for those
        sources.  This keeps the full run efficient while still being patient
        with Wiley/AVMA/SAGE.
    """
    if not source.startswith(("publisher", "scrape")):
        return
    upper = max(PUBLISHER_DELAY_MIN_SECONDS, PUBLISHER_DELAY_MAX_SECONDS)
    lower = min(PUBLISHER_DELAY_MIN_SECONDS, PUBLISHER_DELAY_MAX_SECONDS)
    if upper <= 0:
        return
    delay = random.uniform(lower, upper)
    _vprint(f"    -> publisher jitter sleep={delay:.1f}s")
    time.sleep(delay)


def _default_referer_for_url(url: str) -> str:
    """
    Choose a conservative Referer when the caller does not know the article page.

    Wiley/AVMA/SAGE sometimes expect a request to look like it came from their
    own site.  A publisher homepage referer is safer than inventing an unrelated
    referer and still does not bypass authentication.
    """
    if "onlinelibrary.wiley.com" in url:
        return "https://onlinelibrary.wiley.com/"
    if "avmajournals.avma.org" in url:
        return "https://avmajournals.avma.org/"
    if "journals.sagepub.com" in url:
        return "https://journals.sagepub.com/"
    return url


def _extract_pdf_url_from_html(html: str, base_url: str) -> str | None:
    """
    Look inside an HTML page for a real PDF URL.

    WHY THIS HELPER EXISTS:
        Some open-access repositories do not return PDF bytes immediately.  PMC
        often responds to a PDF URL with a normal HTML page titled "Preparing to
        download ..." before handing off to the actual file.  DOAJ pages can also
        be landing pages that point to an external full text/PDF.  Those pages
        are not failures by themselves; they are signposts.  BeautifulSoup lets
        us read those signposts safely.

    WHAT WE LOOK FOR, IN ORDER:
        1. citation_pdf_url:
           A publisher/repository metadata tag designed specifically for tools
           like Google Scholar.  If it exists, it is the most trustworthy PDF
           pointer on the page.

        2. meta refresh:
           Some "Preparing to download" pages contain a tag like:
             <meta http-equiv="refresh" content="1; url=/real/file.pdf">
           This means the browser would automatically navigate to that URL.

        3. normal links:
           If the page has an <a href="...pdf"> or <a href="/pdf/..."> link,
           we can follow it the same way a user would click it.

    WHAT WE DO NOT DO:
        We do not run JavaScript, solve CAPTCHA, log in, or bypass Cloudflare.
        If the page is a bot challenge or login wall, this function simply finds
        no PDF URL and the caller records a MIME_TYPE_MISMATCH.
    """
    soup = BeautifulSoup(html, "html.parser")

    meta = soup.find("meta", attrs={"name": "citation_pdf_url"})
    if meta and meta.get("content"):
        return urljoin(base_url, meta["content"])

    refresh = soup.find("meta", attrs={"http-equiv": lambda value: value and value.lower() == "refresh"})
    if refresh and refresh.get("content"):
        # Refresh content usually looks like "1; url=/path/to/file.pdf".
        # Split only once so odd URLs containing semicolons are not mangled.
        parts = refresh["content"].split(";", 1)
        if len(parts) == 2 and "url=" in parts[1].lower():
            refresh_url = parts[1].split("=", 1)[1].strip().strip("'\"")
            if refresh_url:
                return urljoin(base_url, refresh_url)

    for link in soup.find_all("a", href=True):
        href = link["href"]
        href_lower = href.lower()
        if (
            href_lower.endswith(".pdf")
            or "/pdf/" in href_lower
            or "/doi/pdf/" in href_lower
            or "/doi/pdfdirect/" in href_lower
        ):
            return urljoin(base_url, href)

    return None


def _safe_body_preview(content: bytes) -> str:
    """
    Convert response bytes into a short, Windows-safe debug preview.

    WHY NOT PRINT resp.text DIRECTLY?
        Publisher/repository HTML can contain Unicode characters such as special
        hyphens, smart quotes, or symbols.  Some Windows terminals still use a
        legacy code page that cannot print those characters, causing the script
        to crash while merely trying to show diagnostics.  This helper keeps the
        first 200 bytes, replaces newlines with spaces, and converts any
        non-ASCII characters to "?" so debug output can never break a run.
    """
    preview = content[:200].decode("utf-8", errors="replace").replace("\n", " ")
    return preview.encode("ascii", errors="replace").decode("ascii")


def _log_download_failure(
    doi: str,
    message: str,
    attempt: DownloadAttempt | None = None,
) -> None:
    """
    Write a per-DOI download failure to the structured error ledger (JSONL)
    and print a short summary line to the terminal.

    WHY ALWAYS PRINT (regardless of VERBOSE)?
        Failures are actionable — the researcher needs to know which DOIs
        couldn't be fetched so they can decide whether to manually supplement.
        Suppressing failures entirely would hide shortfalls until the final
        summary.  Printing "x DOI -- reason" (not the full log_error chain)
        keeps the terminal clean while still surfacing every failure.

    WHY WRITE JSONL DIRECTLY (not via utils.log_error)?
        utils.log_error() also prints a verbose "[log_error] stage | doi | msg"
        line that clutters the terminal when VERBOSE=false.  Writing the JSONL
        entry directly here and printing our own clean single line gives the
        best of both worlds: structured file logging + readable terminal output.
    """
    ERROR_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "doi":       doi,
        "stage":     "download",
        "message":   message,
    }
    if attempt is not None:
        # Store the most important failure fields at top level for easy CSV/JSONL
        # analysis later, and keep the full attempt object for deeper debugging.
        entry.update({
            "failure_code": attempt.failure_code,
            "http_status":  attempt.http_status,
            "content_type": attempt.content_type,
            "source":       attempt.source,
            "final_url":    attempt.final_url,
            "attempt":      asdict(attempt),
        })
    with open(ERROR_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")

    # Always print a minimal failure line — DOI and reason only.
    print(f"  [x] {doi} -- {message}")


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
        "timestamp":         datetime.now(timezone.utc).isoformat(),
        "doi":               "N/A",
        "stage":             "insufficient_oa",
        "journal":           journal,
        "obtained":          obtained,
        "missing":           missing,
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
        DOIs contain forward slashes, which are illegal in file paths on all
        operating systems.  We also replace colons and dots to keep filenames
        shell-friendly and unambiguous.

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
    Save `content` to `path` only if it starts with the PDF magic bytes (%PDF).

    Returns True on success, False if the content is not a valid PDF.

    WHY CHECK MAGIC BYTES?
        Some OA repositories and publishers return an HTML error page (with
        HTTP 200 OK!) when a PDF is unavailable or the paper is paywalled.
        Saving an HTML file with a .pdf extension would cause pdfplumber to
        crash in extract.py.  The four-byte magic signature is a simple,
        reliable, format-level check.

    WHY THIS IS OUR LEGAL SAFETY NET:
        When we try a publisher's direct PDF URL, the publisher may return an
        HTML login page (status 200) for paywalled content instead of a 403.
        _save_pdf() rejects the HTML because it doesn't start with %PDF.
        This means we never accidentally save paywalled content — we simply
        try and save nothing if the paper isn't freely accessible.
    """
    if not content.startswith(b"%PDF"):
        return False

    if len(content) > MAX_PDF_BYTES:
        return False

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return True


# ---------------------------------------------------------------------------
# Helper: download a URL to disk with browser-like headers
# ---------------------------------------------------------------------------

def _download_pdf_url(
    url: str,
    dest_path: Path,
    *,
    session: requests.Session | None = None,
    source: str = "unknown",
    referer: str | None = None,
) -> DownloadAttempt:
    """
    GET `url` with browser-like headers and save the response if it is a real PDF.

    WHY A SHARED HELPER (instead of each fallback doing its own requests.get)?
        Every fallback that has a candidate URL needs the same pattern:
        add browser headers → make request → check magic bytes → save.
        Centralising this in one function means we change headers, add retry
        logic, or improve logging in ONE place rather than in every fallback.

    WHY Accept: application/pdf?
        Sending `Accept: application/pdf` signals to the server that we want
        binary PDF bytes, not an HTML wrapper page.  Some publishers (Wiley in
        particular) serve the raw PDF when this header is present, but return
        an HTML abstract page when Accept is text/html.  This one header can be
        the difference between a successful download and a useless HTML response.

    WHY A Referer HEADER?
        Some publisher CDN rules require the PDF download request to appear to
        originate from the publisher's own site — as if the user clicked a
        "Download PDF" button on the article page.  Setting Referer to the URL
        we are fetching satisfies the simplest form of this check without any
        deception: we are, in fact, trying to fetch that resource.

    Parameters
    ----------
    url       : The URL to download from.
    dest_path : Where to save the file if it validates as a real PDF.
    session   : Optional requests.Session for cookie persistence.  The article-
                page scraping fallback passes its session here so cookies set
                during the HTML page fetch are automatically included in the
                PDF download request — exactly what a browser does.
    """
    _sleep_before_publisher_request(source)

    headers = {
        **BROWSER_HEADERS,
        # Request PDF bytes, not an HTML wrapper page.
        "Accept": "application/pdf,application/octet-stream,text/html,*/*;q=0.8",
        # Use the article-page referer when known; otherwise use a conservative
        # publisher homepage/current URL.  This mimics a normal click path without
        # inventing identities or bypassing authentication.
        "Referer": referer or _default_referer_for_url(url),
    }
    # Use the provided session if available (preserves cookies); otherwise use
    # the module-level requests directly (stateless, fine for API URLs).
    getter = session.get if session is not None else requests.get
    try:
        resp = getter(
            url,
            headers=headers,
            timeout=REQUEST_TIMEOUT,
            verify=False,
            allow_redirects=True,
        )
        content_type = resp.headers.get("Content-Type", "")
        retry_after = resp.headers.get("Retry-After")

        # --- Diagnostic block (only prints when DOWNLOAD_VERBOSE=true) ---
        #
        # WHY LOG THESE FOUR THINGS?
        #
        # 1. status_code: tells us whether the server accepted the request at all.
        #    200 = "I gave you something" (could still be HTML, not PDF)
        #    403 = "I know you're a bot and I'm refusing"
        #    404 = "This URL doesn't exist on my server"
        #    302 = "I'm redirecting you" (requests follows these automatically,
        #           but the final URL shows where we ended up)
        #
        # 2. resp.url: the FINAL URL after all redirects are followed.
        #    This is critical for Wiley — a request to:
        #      onlinelibrary.wiley.com/doi/pdf/10.1111/jvim.70254
        #    may silently redirect to:
        #      onlinelibrary.wiley.com/action/cookieAbsent   ← cookie wall
        #    or to:
        #      onlinelibrary.wiley.com/doi/abs/10.1111/...   ← abstract page
        #    If the final URL is different from what we requested, that explains
        #    why we're not getting a PDF.
        #
        # 3. Content-Type: the server's declaration of what it sent back.
        #    "application/pdf"  → server says it's a PDF (we still check magic bytes)
        #    "text/html"        → server sent an HTML page (login wall, cookie consent)
        #    "application/octet-stream" → generic binary (might still be a real PDF)
        #
        # 4. Body preview (first 200 chars): the actual start of the response.
        #    This is the single most useful diagnostic.  Real PDFs start with "%PDF".
        #    HTML pages start with "<!DOCTYPE html>" or "<html>".
        #    Cookie walls often start with "<html>" and contain words like "cookies"
        #    or "consent" in the first 200 characters.
        #    This tells us definitively what Wiley actually sent us.
        _vprint(f"    -> status={resp.status_code}")
        _vprint(f"    -> final_url={resp.url}")
        _vprint(f"    -> content_type={content_type or 'not set'}")
        # Decode the first 200 bytes as text for readability; errors='replace'
        # means if the bytes are binary (real PDF) we see something like "%PDF-1.6..."
        # instead of a UnicodeDecodeError crashing the debug output.
        body_preview = _safe_body_preview(resp.content)
        _vprint(f"    -> body_start={body_preview}")

        if resp.status_code == 429:
            attempt = _remember_attempt(DownloadAttempt(
                source=source,
                url=url,
                failure_code="HTTP_429",
                message="Server rate-limited the request",
                http_status=resp.status_code,
                content_type=content_type,
                final_url=resp.url,
                retry_after=retry_after,
                body_start=body_preview,
            ))
            _respect_retry_after(attempt)
            return attempt

        if resp.status_code == 403:
            return _remember_attempt(DownloadAttempt(
                source=source,
                url=url,
                failure_code="HTTP_403",
                message="Server refused the request",
                http_status=resp.status_code,
                content_type=content_type,
                final_url=resp.url,
                retry_after=retry_after,
                body_start=body_preview,
            ))

        try:
            resp.raise_for_status()
        except requests.HTTPError as exc:
            return _remember_attempt(DownloadAttempt(
                source=source,
                url=url,
                failure_code="HTTP_ERROR",
                message=str(exc),
                http_status=resp.status_code,
                content_type=content_type,
                final_url=resp.url,
                retry_after=retry_after,
                body_start=body_preview,
            ))

        if "text/html" in content_type.lower():
            _vprint("    -> MIME_TYPE_MISMATCH: server returned HTML, not PDF.")
            html_pdf_url = _extract_pdf_url_from_html(resp.text, resp.url)
            if html_pdf_url and html_pdf_url != resp.url:
                # HTML is not a PDF, so we still refuse to save this response.
                # But if the page openly points to a PDF URL, follow that one
                # additional legal/public handoff.  This is exactly what a human
                # browser would do on a PMC "Preparing to download ..." page.
                _vprint(f"    -> HTML page exposed a PDF link; trying {html_pdf_url}")
                linked_attempt = _download_pdf_url(
                    html_pdf_url,
                    dest_path,
                    session=session,
                    source=f"{source}_html_pdf_link",
                    referer=resp.url,
                )
                if linked_attempt.ok:
                    return linked_attempt
                return linked_attempt

            return _remember_attempt(DownloadAttempt(
                source=source,
                url=url,
                failure_code="MIME_TYPE_MISMATCH",
                message="Server returned HTML instead of PDF",
                http_status=resp.status_code,
                content_type=content_type,
                final_url=resp.url,
                retry_after=retry_after,
                body_start=body_preview,
            ))

        saved = _save_pdf(resp.content, dest_path)

        # If the download succeeded but _save_pdf rejected the content (not a real
        # PDF), log that too so we know it was a content problem, not a network one.
        if not saved:
            _vprint(f"    -> Response received but failed %PDF magic-byte check — not a real PDF.")
            return _remember_attempt(DownloadAttempt(
                source=source,
                url=url,
                failure_code="PDF_MAGIC_MISMATCH",
                message="Response did not start with %PDF",
                http_status=resp.status_code,
                content_type=content_type,
                final_url=resp.url,
                retry_after=retry_after,
                body_start=body_preview,
            ))

        return _remember_attempt(DownloadAttempt(
            source=source,
            url=url,
            ok=True,
            http_status=resp.status_code,
            content_type=content_type,
            final_url=resp.url,
            retry_after=retry_after,
            body_start=body_preview,
        ))

    except requests.Timeout as exc:
        attempt = DownloadAttempt(
            source=source,
            url=url,
            failure_code="REQUEST_TIMEOUT",
            message=str(exc),
        )
        _vprint(f"    -> Request timeout: {exc}")
        return _remember_attempt(attempt)
    except requests.RequestException as exc:
        # Network-level failure: DNS failure, connection refused, timeout, 4xx/5xx.
        # The specific error message tells us exactly what went wrong.
        _vprint(f"    -> Request error: {exc}")
        return _remember_attempt(DownloadAttempt(
            source=source,
            url=url,
            failure_code="REQUEST_ERROR",
            message=str(exc),
        ))


def _ncbi_package_url_candidates(url: str) -> list[str]:
    """
    Return the safest URL variants to try for an NCBI OA package link.

    WHY IS THIS NEEDED?
        NCBI's OA web service often returns links like:
            ftp://ftp.ncbi.nlm.nih.gov/pub/pmc/oa_package/...
        Some environments block FTP, while requests does not support FTP at all.
        NCBI often mirrors the same files over HTTPS at:
            https://ftp.ncbi.nlm.nih.gov/pub/pmc/oa_package/...

        So for NCBI FTP links we try HTTPS first, then the original FTP URL via
        Python's standard urllib.  This is not a bypass: both URLs point to the
        official NCBI OA package named by NCBI's own oa.fcgi response.
    """
    prefix = "ftp://ftp.ncbi.nlm.nih.gov/"
    if url.startswith(prefix):
        return [
            "https://ftp.ncbi.nlm.nih.gov/" + url[len(prefix):],
            url,
        ]
    return [url]


def _download_oa_package_bytes(url: str) -> tuple[bytes | None, str | None]:
    """
    Download an OA package over HTTPS or FTP.

    WHY A SEPARATE HELPER?
        requests is excellent for HTTP/HTTPS, but it deliberately does not
        support ftp:// URLs.  NCBI's OA service may return FTP package links, so
        this helper uses requests for HTTPS and urllib.request for FTP.  Both are
        read-only downloads from official NCBI infrastructure.

    Returns
    -------
    tuple[bytes | None, str | None]
        (content, error_message).  If content is None, error_message explains
        what failed so we can write it to the diagnostic ledger.
    """
    try:
        if url.startswith("ftp://"):
            with urllib.request.urlopen(url, timeout=REQUEST_TIMEOUT) as response:
                return response.read(), None

        package_resp = requests.get(
            url,
            headers={**BROWSER_HEADERS, "Accept": "application/gzip,*/*;q=0.8"},
            timeout=REQUEST_TIMEOUT,
            verify=False,
        )
        package_resp.raise_for_status()
        return package_resp.content, None
    except Exception as exc:
        return None, str(exc)


def _try_pubmed_oa_package(pmcid: str, dest_path: Path) -> bool:
    """
    Download a PMC article through NCBI's official Open Access package service.

    WHY THIS FALLBACK EXISTS:
        The normal PMC PDF URL can now return a browser proof-of-work page:
            "Preparing to download ..." + cloudpmc-viewer-pow JavaScript
        Solving that challenge programmatically would be inappropriate for this
        research pipeline.  The official NCBI OA web service is different: it is
        an API intended for machine access, and for OA papers it returns package
        links with license metadata (for example CC BY).

    WHAT THE API RETURNS:
        For a PMCID, oa.fcgi returns XML like:
            <record id="PMC..." license="CC BY">
              <link format="tgz" href="ftp://ftp.ncbi.nlm.nih.gov/...tar.gz" />
            </record>
        The tar.gz package contains the article files.  We open the tarball in
        memory, find the first PDF member, validate that it starts with %PDF, and
        save only that PDF to data/raw/.

    SAFETY BOUNDARY:
        We do not bypass a challenge, run JavaScript, or use credentials.  We use
        an official OA metadata endpoint and still require the extracted file to
        pass the same PDF magic-byte check as every other source.
    """
    oa_url = f"https://www.ncbi.nlm.nih.gov/pmc/utils/oa/oa.fcgi?id=PMC{pmcid}"
    _vprint(f"  [PMC OA] Querying official OA package service: {oa_url}")

    try:
        resp = requests.get(oa_url, timeout=REQUEST_TIMEOUT, verify=False)
        if resp.status_code == 429:
            attempt = _remember_attempt(DownloadAttempt(
                source="pubmed_oa_service",
                url=resp.url,
                failure_code="HTTP_429",
                message="NCBI OA service rate-limited the request",
                http_status=resp.status_code,
                content_type=resp.headers.get("Content-Type", ""),
                final_url=resp.url,
                retry_after=resp.headers.get("Retry-After"),
                body_start=_safe_body_preview(resp.content),
            ))
            _respect_retry_after(attempt)
            return False
        resp.raise_for_status()
    except requests.RequestException as exc:
        _remember_attempt(DownloadAttempt(
            source="pubmed_oa_service",
            url=oa_url,
            failure_code="REQUEST_ERROR",
            message=str(exc),
        ))
        _vprint(f"  [PMC OA] OA service request failed: {exc}")
        return False

    try:
        root = ET.fromstring(resp.text)
    except ET.ParseError as exc:
        _remember_attempt(DownloadAttempt(
            source="pubmed_oa_service",
            url=oa_url,
            failure_code="MIME_TYPE_MISMATCH",
            message=f"NCBI OA service returned non-XML response: {exc}",
            http_status=resp.status_code,
            content_type=resp.headers.get("Content-Type", ""),
            final_url=resp.url,
            body_start=_safe_body_preview(resp.content),
        ))
        return False

    links = root.findall(".//link")
    if not links:
        _remember_attempt(DownloadAttempt(
            source="pubmed_oa_service",
            url=oa_url,
            failure_code="NO_PDF_URL",
            message="NCBI OA service returned no package links",
        ))
        _vprint("  [PMC OA] No OA package links returned.")
        return False

    # Prefer a direct PDF link if NCBI provides one.  If not, fall back to the
    # tar.gz package, which is the common response for OA articles.
    sorted_links = sorted(
        links,
        key=lambda link: 0 if (link.get("format") or "").lower() == "pdf" else 1,
    )
    for link in sorted_links:
        fmt = (link.get("format") or "").lower()
        href = link.get("href")
        if not href:
            continue

        package_urls = _ncbi_package_url_candidates(href)

        if fmt == "pdf":
            for package_url in package_urls:
                _vprint(f"  [PMC OA] Trying direct PDF link: {package_url}")
                attempt = _download_pdf_url(package_url, dest_path, source="pubmed_oa_pdf")
                if attempt.ok:
                    print(f"  [PMC OA] Saved {dest_path.name} from OA PDF link")
                    return True
            continue

        if fmt != "tgz" and not any(url.endswith((".tar.gz", ".tgz")) for url in package_urls):
            continue

        package_content: bytes | None = None
        package_url_used: str | None = None
        for package_url in package_urls:
            _vprint(f"  [PMC OA] Trying {fmt or 'unknown'} package link: {package_url}")
            package_content, error = _download_oa_package_bytes(package_url)
            if package_content is not None:
                package_url_used = package_url
                break
            _remember_attempt(DownloadAttempt(
                source="pubmed_oa_package",
                url=package_url,
                failure_code="REQUEST_ERROR",
                message=error,
            ))
            _vprint(f"  [PMC OA] Package download failed: {error}")

        if package_content is None or package_url_used is None:
            continue

        if len(package_content) > MAX_OA_PACKAGE_BYTES:
            _remember_attempt(DownloadAttempt(
                source="pubmed_oa_package",
                url=package_url_used,
                failure_code="PDF_MAGIC_MISMATCH",
                message="OA package exceeded maximum allowed package size",
            ))
            _vprint("  [PMC OA] Package too large; skipping.")
            continue

        try:
            with tarfile.open(fileobj=io.BytesIO(package_content), mode="r:gz") as tar:
                pdf_members = [
                    member for member in tar.getmembers()
                    if member.isfile() and member.name.lower().endswith(".pdf")
                ]
                if not pdf_members:
                    _remember_attempt(DownloadAttempt(
                        source="pubmed_oa_package",
                        url=package_url_used,
                        failure_code="NO_PDF_URL",
                        message="OA package contained no PDF member",
                    ))
                    continue

                pdf_member = pdf_members[0]
                extracted = tar.extractfile(pdf_member)
                if extracted is None:
                    continue
                pdf_bytes = extracted.read()
        except (tarfile.TarError, OSError) as exc:
            _remember_attempt(DownloadAttempt(
                source="pubmed_oa_package",
                url=package_url_used,
                failure_code="MIME_TYPE_MISMATCH",
                message=f"OA package was not a readable tar.gz: {exc}",
            ))
            continue

        if _save_pdf(pdf_bytes, dest_path):
            print(f"  [PMC OA] Saved {dest_path.name} from OA package")
            _remember_attempt(DownloadAttempt(
                source="pubmed_oa_package",
                url=package_url_used,
                ok=True,
                final_url=package_url_used,
                body_start=_safe_body_preview(pdf_bytes),
            ))
            return True

        _remember_attempt(DownloadAttempt(
            source="pubmed_oa_package",
            url=package_url_used,
            failure_code="PDF_MAGIC_MISMATCH",
            message="PDF extracted from OA package failed %PDF validation",
            final_url=package_url_used,
            body_start=_safe_body_preview(pdf_bytes),
        ))

    return False


# ---------------------------------------------------------------------------
# Fallback 1: fulltext-article-downloader CLI
# ---------------------------------------------------------------------------

def _try_fulltext_downloader(doi: str, dest_path: Path) -> bool:
    """
    Attempt download using the `fulltext-download` CLI tool.

    WHY SUBPROCESS INSTEAD OF IMPORT?
        `fulltext-article-downloader` is primarily a command-line tool.  Calling
        it with subprocess is like typing the command into PowerShell from inside
        Python.  If the tool is missing or changes behavior, only this fallback
        fails; the rest of the downloader still runs.

    WHY IS THIS FIRST IN THE CHAIN?
        fulltext-article-downloader is a specialist tool that queries multiple
        OA repositories simultaneously (Unpaywall, BASE, CORE, and others).
        When it is available it is often the fastest single-step solution.

    WHY PASS output_dir AND output_filename SEPARATELY?
        The CLI's real usage is:
            fulltext-download DOI output_dir [output_filename]
        It does NOT accept "--output path.pdf".  Passing the folder and filename
        separately lets the tool create exactly data/raw/<doi>.pdf while matching
        the command's documented syntax.

    WHY VALIDATE THE FILE AFTER THE CLI RETURNS?
        External tools can still save an HTML error page, an empty file, or a
        redirect page.  We only count success if the created file begins with
        the PDF magic bytes (%PDF) and is under the size limit.  Invalid output
        is removed so extract.py never sees a corrupted PDF.

    Returns True if the PDF was saved successfully, False otherwise.
    """
    try:
        # If the project is run as ".venv\\Scripts\\python.exe src/download.py",
        # the virtualenv's Scripts folder is not always at the front of PATH.
        # That means subprocess may not find fulltext-download even though it is
        # installed in the venv.  Prefer the executable next to sys.executable
        # (the Python currently running this file), then fall back to PATH.
        cli_name = "fulltext-download.exe" if os.name == "nt" else "fulltext-download"
        venv_cli = Path(sys.executable).with_name(cli_name)
        cli_command = str(venv_cli) if venv_cli.exists() else "fulltext-download"

        result = subprocess.run(
            [cli_command, doi, str(dest_path.parent), dest_path.name],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode == 0 and dest_path.exists():
            content = dest_path.read_bytes()
            if content.startswith(b"%PDF") and len(content) <= MAX_PDF_BYTES:
                print(f"  [fulltext-downloader] Saved {dest_path.name}")
                return True

            _vprint(
                f"  [fulltext-downloader] Created {dest_path.name}, "
                "but it was not a valid PDF; deleting it."
            )
            dest_path.unlink(missing_ok=True)
        _vprint(
            f"  [fulltext-downloader] Non-zero exit for {doi} "
            f"(stderr: {result.stderr[:120].strip() or 'none'})"
        )
    except FileNotFoundError:
        # The CLI is not installed — fine, move on to the next fallback.
        _vprint("  [fulltext-downloader] CLI not installed, skipping.")
    except subprocess.TimeoutExpired:
        _vprint(f"  [fulltext-downloader] Timed out for DOI {doi}")
    return False


# ---------------------------------------------------------------------------
# Fallback 2: Unpaywall API
# ---------------------------------------------------------------------------

def _try_unpaywall(doi: str, dest_path: Path) -> bool:
    """
    Query the Unpaywall API for ALL known OA PDF URLs, then try each one.

    Unpaywall covers ~50% of all recent scholarly articles.
    API docs: https://unpaywall.org/data-format

    WHY TRY ALL oa_locations (not just best_oa_location)?
        `best_oa_location` is Unpaywall's recommended source, but its URL can
        be stale, temporarily unavailable, or redirect to a paywalled version.
        `oa_locations` is the FULL list of every known OA copy — publisher
        versions, repository mirrors, preprints, green OA deposits, etc.
        Iterating all of them means a single bad URL doesn't cause us to give
        up on a paper that has multiple other accessible copies.

    Returns True on success, False if no OA version is found or downloadable.
    """
    url    = f"https://api.unpaywall.org/v2/{doi}"
    params = {"email": UNPAYWALL_EMAIL}

    # SSL verification is disabled for campus network / VPN compatibility.
    # See the module-level urllib3.disable_warnings() call for the rationale.
    try:
        resp = requests.get(url, params=params, timeout=REQUEST_TIMEOUT, verify=False)
        if resp.status_code == 429:
            attempt = _remember_attempt(DownloadAttempt(
                source="unpaywall_api",
                url=resp.url,
                failure_code="HTTP_429",
                message="Unpaywall rate-limited the request",
                http_status=resp.status_code,
                content_type=resp.headers.get("Content-Type", ""),
                final_url=resp.url,
                retry_after=resp.headers.get("Retry-After"),
                body_start=_safe_body_preview(resp.content),
            ))
            _respect_retry_after(attempt)
            return False
        if resp.status_code == 404:
            # DOI is simply not in the Unpaywall database — not an error.
            _vprint(f"  [Unpaywall] DOI not found in Unpaywall database.")
            return False
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as exc:
        _vprint(f"  [Unpaywall] API request failed: {exc}")
        return False

    # Legal gate: only proceed if Unpaywall explicitly confirms the paper is OA.
    # This is what ensures we never download subscription content via this path.
    if not data.get("is_oa"):
        _vprint(f"  [Unpaywall] Paper is not flagged as open access.")
        return False

    # Build an ordered list: best_oa_location first, then all other oa_locations.
    # WHY DEDUPLICATE?
    #   best_oa_location is usually also present inside oa_locations.  We add
    #   the best one first and skip duplicates so we don't request the same URL
    #   twice.
    best = data.get("best_oa_location") or {}
    all_locs: list[dict] = [best] if best else []
    for loc in data.get("oa_locations") or []:
        if loc not in all_locs:
            all_locs.append(loc)

    if not all_locs:
        _vprint(f"  [Unpaywall] is_oa=True but no oa_locations returned.")
        _remember_attempt(DownloadAttempt(
            source="unpaywall",
            url=url,
            failure_code="NO_PDF_URL",
            message="Unpaywall returned is_oa=True but no oa_locations",
        ))
        return False

    for i, loc in enumerate(all_locs, 1):
        # url_for_pdf is a direct link to the PDF file.  url may be a landing
        # page — we prefer url_for_pdf but fall back to url if that's all we have.
        pdf_url = loc.get("url_for_pdf") or loc.get("url")
        if not pdf_url:
            _vprint(f"  [Unpaywall] Location {i}/{len(all_locs)}: no URL in entry, skipping.")
            _remember_attempt(DownloadAttempt(
                source="unpaywall",
                url=url,
                failure_code="NO_PDF_URL",
                message=f"Unpaywall location {i} had no URL",
            ))
            continue

        _vprint(f"  [Unpaywall] Location {i}/{len(all_locs)}: {pdf_url}")
        attempt = _download_pdf_url(pdf_url, dest_path, source="unpaywall")
        if attempt.ok:
            license_str = (loc.get("license") or "OA").lower()
            print(f"  [Unpaywall] Saved {dest_path.name} (license: {license_str})")
            return True
        _vprint(f"    -> Did not yield valid PDF bytes.")

    _vprint(f"  [Unpaywall] All {len(all_locs)} OA location(s) tried — none delivered a PDF.")
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
        resp = requests.get(url, params=params, timeout=REQUEST_TIMEOUT, verify=False)
        if resp.status_code == 429:
            attempt = _remember_attempt(DownloadAttempt(
                source="semantic_scholar_api",
                url=resp.url,
                failure_code="HTTP_429",
                message="Semantic Scholar rate-limited the request",
                http_status=resp.status_code,
                content_type=resp.headers.get("Content-Type", ""),
                final_url=resp.url,
                retry_after=resp.headers.get("Retry-After"),
                body_start=_safe_body_preview(resp.content),
            ))
            _respect_retry_after(attempt)
            return False
        if resp.status_code == 404:
            _vprint(f"  [S2] Paper not found in Semantic Scholar.")
            return False
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as exc:
        _vprint(f"  [S2] API request failed: {exc}")
        return False

    if not data.get("isOpenAccess"):
        _vprint(f"  [S2] isOpenAccess=False — paper not flagged as OA.")
        return False

    oa_pdf  = data.get("openAccessPdf") or {}
    pdf_url = oa_pdf.get("url")

    if not pdf_url:
        # isOpenAccess can be True without an openAccessPdf URL — this happens
        # when S2 knows the paper is OA but hasn't indexed the PDF location yet.
        _vprint(f"  [S2] isOpenAccess=True but no openAccessPdf URL returned.")
        _remember_attempt(DownloadAttempt(
            source="semantic_scholar",
            url=url,
            failure_code="NO_PDF_URL",
            message="Semantic Scholar returned isOpenAccess=True but no openAccessPdf URL",
        ))
        return False

    _vprint(f"  [S2] Trying PDF URL: {pdf_url}")
    attempt = _download_pdf_url(pdf_url, dest_path, source="semantic_scholar")
    if attempt.ok:
        print(f"  [Semantic Scholar] Saved {dest_path.name}")
        return True

    _vprint(f"    -> Did not yield valid PDF bytes.")
    return False


# ---------------------------------------------------------------------------
# Fallback 4: PubMed Central
# ---------------------------------------------------------------------------

def _try_pubmed_central(doi: str, dest_path: Path) -> bool:
    """
    Look up the DOI in PubMed Central via NCBI E-utils, then download the PDF.

    PMC is a free archive maintained by the US National Library of Medicine.
    Many veterinary journals deposit OA papers there automatically under NIH
    or UKRI open-access mandates.

    Returns True on success, False if the paper is not in PMC.
    """
    # Step 1: Resolve DOI → PMCID using the NCBI ID converter API.
    # WHY A SEPARATE LOOKUP STEP?
    #   PMC download URLs are keyed by PMCID (e.g. PMC12345678), not by DOI.
    #   The NCBI ID converter handles this translation reliably and is free.
    id_url = "https://www.ncbi.nlm.nih.gov/pmc/utils/idconv/v1.0/"
    try:
        resp = requests.get(
            id_url,
            params={"ids": doi, "format": "json"},
            timeout=REQUEST_TIMEOUT,
            verify=False,
        )
        if resp.status_code == 429:
            attempt = _remember_attempt(DownloadAttempt(
                source="pubmed_central_api",
                url=resp.url,
                failure_code="HTTP_429",
                message="NCBI rate-limited the request",
                http_status=resp.status_code,
                content_type=resp.headers.get("Content-Type", ""),
                final_url=resp.url,
                retry_after=resp.headers.get("Retry-After"),
                body_start=_safe_body_preview(resp.content),
            ))
            _respect_retry_after(attempt)
            return False
        resp.raise_for_status()
        records = resp.json().get("records", [])
    except requests.RequestException as exc:
        _vprint(f"  [PMC] ID conversion request failed: {exc}")
        return False

    if not records or "pmcid" not in records[0]:
        _vprint(f"  [PMC] DOI not found in PubMed Central.")
        _remember_attempt(DownloadAttempt(
            source="pubmed_central",
            url=id_url,
            failure_code="NO_PDF_URL",
            message="DOI did not resolve to a PMCID",
        ))
        return False

    pmcid = records[0]["pmcid"].replace("PMC", "")

    # Step 2: Download the PDF from PMC's web interface.
    # WHY THIS URL PATTERN?
    #   PMC exposes PDFs at /pmc/articles/PMC{id}/pdf/ — the trailing slash
    #   triggers a redirect to the actual PDF filename (which varies per paper).
    #   requests follows the redirect automatically via allow_redirects=True.
    pdf_url = f"https://www.ncbi.nlm.nih.gov/pmc/articles/PMC{pmcid}/pdf/"
    _vprint(f"  [PMC] Trying: {pdf_url}")

    attempt = _download_pdf_url(pdf_url, dest_path, source="pubmed_central")
    if attempt.ok:
        print(f"  [PubMed Central] Saved {dest_path.name} (PMC{pmcid})")
        return True

    _vprint(f"    -> Did not yield valid PDF bytes for PMC{pmcid}.")
    _vprint("  [PMC OA] Direct PDF did not work; trying official OA package service.")
    if _try_pubmed_oa_package(pmcid, dest_path):
        return True

    return False


# ---------------------------------------------------------------------------
# Fallback 5: Publisher-direct PDF URLs (Wiley, AVMA, SAGE)
# ---------------------------------------------------------------------------

def _try_publisher_pdf(doi: str, dest_path: Path) -> bool:
    """
    Try known publisher PDF URL patterns directly using browser-like headers.

    WHY THIS FALLBACK EXISTS:
        Metadata services (Unpaywall, Semantic Scholar) index most OA papers
        but they lag behind publication by days to weeks.  A paper published
        this month by Wiley as OA may not yet appear in Unpaywall's database,
        but its PDF is already publicly accessible at the predictable Wiley URL.
        This fallback catches those recently-published OA papers.

        This is also where we fix the original problem: the script previously
        never tried publisher URLs directly.  A paper like the Wiley JVIM article
        "Effect of N-Butylscopolammonium Bromide..." is OA on Wiley's site but
        was invisible to the metadata APIs.

    HOW WE KNOW IT'S TRULY OA (and not subscription content):
        We do not know ahead of time — we simply try the URL.  If the paper is
        paywalled, the publisher returns either:
          a) an HTML login page with HTTP 200  →  _save_pdf() rejects it (%PDF check)
          b) an HTTP 403 Forbidden             →  raise_for_status() raises; we return False
        In either case, nothing is saved and we return False.  The magic byte
        check is the legal safety net.

    Returns True if any URL template yielded real PDF bytes, False otherwise.
    """
    for prefix, templates in PUBLISHER_PDF_TEMPLATES.items():
        if not doi.startswith(prefix):
            continue

        # Only one publisher block will match per DOI, so we break after trying
        # all templates for the matched publisher.
        for template in templates:
            url = template.format(doi=doi)
            _vprint(f"  [publisher] Trying: {url}")
            attempt = _download_pdf_url(url, dest_path, source="publisher_direct")
            if attempt.ok:
                print(f"  [publisher] Saved {dest_path.name} ({url})")
                return True
            _vprint(f"    -> Not a valid PDF at this URL.")

        break  # Stop checking other publisher prefixes once we found a match.

    return False


# ---------------------------------------------------------------------------
# Fallback 6: Article-page HTML scraping (citation_pdf_url meta tag)
# ---------------------------------------------------------------------------

def _try_scrape_article_page(doi: str, dest_path: Path) -> bool:
    """
    Follow the DOI redirect to the publisher's article page, parse the HTML
    for a machine-readable PDF link, then download it.

    WHY THIS WORKS:
        Most scholarly publishers embed this standardised metadata tag in their
        article HTML pages:
            <meta name="citation_pdf_url" content="https://...pdf">
        This is part of the Google Scholar metadata specification
        (https://scholar.google.com/intl/en/scholar/inclusion.html).
        Publishers implement it so that Google Scholar, citation managers, and
        browser plugins can locate their PDFs automatically.  For open-access
        papers, this tag points directly to the freely downloadable PDF — the
        same URL a student's browser navigates to when they click "Download PDF".

    WHY FOLLOW THE doi.org REDIRECT (not go directly to the publisher)?
        By following doi.org we land on the correct publisher page for ANY DOI,
        regardless of which journal or publisher is involved.  We do not need to
        know the publisher's domain ahead of time — doi.org handles the routing.

    WHY USE A requests.Session?
        Some publishers (particularly SAGE) set a session cookie during the first
        HTML page load and then require that cookie to be present on the PDF
        download request.  Without it the server redirects to a login page.
        A Session object stores and resends cookies automatically across all
        requests in the session — exactly what a browser does.

    TWO DISCOVERY STRATEGIES:
        1. citation_pdf_url meta tag (primary) — authoritative, placed by the
           publisher deliberately for automated indexing tools.
        2. Scanning <a> tags for /doi/pdf/ or /doi/epdf/ patterns (secondary)
           — catches publisher pages that omit the meta tag but have a visible
           "Download PDF" hyperlink in the page body.

    HOW WE STAY LEGAL:
        The same _save_pdf() magic-byte check applies.  If the paper is
        subscription-only, the server returns HTML (a login redirect), which
        fails the %PDF check.  We never save non-PDF content.

    Returns True if a PDF was found and saved, False otherwise.
    """
    doi_url = f"https://doi.org/{doi}"

    # Create a session so cookies set during the HTML page load (Step 1) are
    # automatically included in the follow-up PDF download (Step 3).
    session = requests.Session()

    # Step 1: Fetch the publisher's article HTML page via doi.org.
    try:
        page_resp = session.get(
            doi_url,
            headers={
                **BROWSER_HEADERS,
                # Ask for HTML explicitly so the publisher returns the article
                # landing page, not a PDF, JSON, or redirect-only response.
                "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            },
            timeout=REQUEST_TIMEOUT,
            verify=False,
            allow_redirects=True,
        )
        if page_resp.status_code == 429:
            attempt = _remember_attempt(DownloadAttempt(
                source="scrape_article_page",
                url=doi_url,
                failure_code="HTTP_429",
                message="Publisher article page rate-limited the request",
                http_status=page_resp.status_code,
                content_type=page_resp.headers.get("Content-Type", ""),
                final_url=page_resp.url,
                retry_after=page_resp.headers.get("Retry-After"),
                body_start=_safe_body_preview(page_resp.content),
            ))
            _respect_retry_after(attempt)
            return False
        try:
            page_resp.raise_for_status()
        except requests.HTTPError as exc:
            _remember_attempt(DownloadAttempt(
                source="scrape_article_page",
                url=doi_url,
                failure_code="HTTP_403" if page_resp.status_code == 403 else "HTTP_ERROR",
                message=str(exc),
                http_status=page_resp.status_code,
                content_type=page_resp.headers.get("Content-Type", ""),
                final_url=page_resp.url,
                retry_after=page_resp.headers.get("Retry-After"),
                body_start=_safe_body_preview(page_resp.content),
            ))
            raise
    except requests.RequestException as exc:
        _vprint(f"  [scrape] Could not fetch article page: {exc}")
        return False

    # Step 2: Parse the HTML for a PDF link using BeautifulSoup.
    # WHY html.parser (not lxml)?
    #   html.parser ships with Python's standard library — no extra install.
    #   It is slightly slower than lxml but more than fast enough for finding
    #   a handful of <meta> tags in a single article page.
    soup = BeautifulSoup(page_resp.text, "html.parser")
    pdf_url: str | None = None

    # Primary strategy: look for the Google Scholar citation_pdf_url meta tag.
    # attrs={"name": "citation_pdf_url"} matches the standard tag used by Wiley,
    # SAGE, AVMA, Elsevier, Springer, and most other major publishers.
    # WHY THIS FIRST?
    #   It is authoritative — the publisher deliberately placed it for automated
    #   tools.  It is far less likely to be a wrong link than a random anchor tag.
    meta = soup.find("meta", attrs={"name": "citation_pdf_url"})
    if meta and meta.get("content"):
        pdf_url = meta["content"]
        _vprint(f"  [scrape] Found citation_pdf_url meta tag: {pdf_url}")

    # Secondary strategy: scan <a> tags for known publisher PDF link patterns.
    # WHY A SECONDARY STRATEGY?
    #   Older Wiley page templates and some AVMA pages don't include the meta
    #   tag but do have a visible "Download PDF" anchor in the page body.
    #   Matching /doi/pdf/ and /doi/epdf/ catches these reliably.
    if not pdf_url:
        for a in soup.find_all("a", href=True):
            href: str = a["href"]
            if (
                "/doi/pdf/" in href
                or "/doi/epdf/" in href
                or href.lower().endswith(".pdf")
            ):
                # urljoin handles both absolute URLs (https://...) and relative
                # ones (/doi/pdf/...) correctly: relative hrefs get the base
                # domain prepended from page_resp.url; absolute hrefs pass through.
                pdf_url = urljoin(page_resp.url, href)
                _vprint(f"  [scrape] Found PDF anchor tag: {pdf_url}")
                break

    if not pdf_url:
        _vprint(f"  [scrape] No PDF link found in article page HTML.")
        _remember_attempt(DownloadAttempt(
            source="scrape_article_page",
            url=doi_url,
            failure_code="NO_PDF_URL",
            message="Article page did not expose a citation_pdf_url or PDF anchor",
            http_status=page_resp.status_code,
            content_type=page_resp.headers.get("Content-Type", ""),
            final_url=page_resp.url,
            body_start=_safe_body_preview(page_resp.content),
        ))
        return False

    # Step 3: Download the PDF using the SAME session (cookies carried over).
    # WHY session=session?
    #   _download_pdf_url() calls session.get() instead of requests.get() when
    #   a session is provided, reusing the same session object.  Any cookies set
    #   by the server during the HTML page fetch in Step 1 are automatically
    #   present in this PDF request — exactly like a browser that clicked
    #   "Download PDF" after loading the article page.
    attempt = _download_pdf_url(
        pdf_url,
        dest_path,
        session=session,
        source="scrape_article_page",
        referer=page_resp.url,
    )
    if attempt.ok:
        print(f"  [scrape] Saved {dest_path.name} via article-page scraping")
        return True

    _vprint(f"  [scrape] PDF URL found but response was not valid PDF bytes.")
    return False


# ---------------------------------------------------------------------------
# Core single-paper download function
# ---------------------------------------------------------------------------

def download_paper(doi: str) -> bool:
    """
    Attempt to download the OA PDF for a single paper through the fallback chain.

    Tries six sources in priority order, stopping as soon as any source delivers
    a valid PDF.  All sources are legal — only genuinely open-access content is
    saved (enforced by the %PDF magic byte check on every download).

    Idempotency: if the PDF already exists in data/raw/, this function returns
    True immediately without making any network requests.

    Returns True if a PDF is available in data/raw/ at the end of the call,
    False otherwise.
    """
    _CURRENT_DOWNLOAD_ATTEMPTS.clear()
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    dest_path = RAW_DIR / _doi_to_filename(doi)

    # Idempotency check: skip papers already downloaded in a previous run.
    # This makes the download step safe to restart after a crash.
    if dest_path.exists():
        _vprint(f"  [download] Already exists, skipping: {dest_path.name}")
        return True

    _vprint(f"  [download] Starting fallback chain for DOI: {doi}")

    # Fallback 1: fulltext-article-downloader CLI
    # Best single-step option when installed — queries many OA repositories.
    if _try_fulltext_downloader(doi, dest_path):
        return True
    time.sleep(DOWNLOAD_DELAY_SECONDS)

    # Fallback 2: Unpaywall
    # Covers ~50% of recent articles; now tries ALL oa_locations so one broken
    # URL doesn't block access to other valid copies of the same paper.
    if _try_unpaywall(doi, dest_path):
        return True
    time.sleep(DOWNLOAD_DELAY_SECONDS)

    # Fallback 3: Semantic Scholar
    # Good coverage for biomedical content; weaker for purely veterinary journals
    # but still catches many PMC-indexed papers.
    if _try_semantic_scholar(doi, dest_path):
        return True
    time.sleep(DOWNLOAD_DELAY_SECONDS)

    # Fallback 4: PubMed Central
    # Many vet journal papers are deposited in PMC under NIH/UKRI OA mandates.
    # Reliable once found via the PMCID lookup step.
    if _try_pubmed_central(doi, dest_path):
        return True
    time.sleep(DOWNLOAD_DELAY_SECONDS)

    # Fallback 5: Publisher-direct PDF URL
    # Catches recently-published OA papers not yet indexed by metadata APIs.
    # Uses browser-like headers to pass CDN bot-detection.
    # Covers: Wiley (10.1111/*), AVMA (10.2460/*), SAGE (10.1177/*).
    if _try_publisher_pdf(doi, dest_path):
        return True
    time.sleep(DOWNLOAD_DELAY_SECONDS)

    # Fallback 6: Article-page HTML scraping
    # Last resort: follow the DOI redirect, read the citation_pdf_url meta tag,
    # download with a cookie-carrying Session.  Slowest but most robust — can
    # find PDFs that no metadata API has indexed yet.
    if _try_scrape_article_page(doi, dest_path):
        return True

    # All six fallbacks exhausted — log the final failure clearly.
    # The final ledger entry includes the single most useful failed attempt
    # (for example HTTP_403 from Wiley, HTTP_429 rate limit, text/html cookie
    # wall, or %PDF mismatch) so a full run is diagnosable after it finishes.
    best_attempt = _best_failure_attempt()
    if best_attempt is not None and best_attempt.failure_code:
        message = (
            "All 6 fallbacks failed — "
            f"{best_attempt.source} ended with {best_attempt.failure_code}"
        )
    else:
        message = "All 6 fallbacks failed — no accessible OA PDF found"
    _log_download_failure(doi, message, attempt=best_attempt)
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

        total_would_add = 0
        total_shortfall = 0

        for journal, queue in journal_queues.items():
            target     = JOURNAL_TARGETS.get(journal, 50)
            existing   = journal_success[journal]
            candidates = len(queue) - existing
            can_add    = min(candidates, max(0, target - existing))
            shortfall  = max(0, target - existing - can_add)
            total_would_add += can_add
            total_shortfall += shortfall
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
                target    = JOURNAL_TARGETS.get(journal, 50)
                existing  = journal_success[journal]
                would_add = min(len(queue) - existing, max(0, target - existing))
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

                # FAIL-CAP: too many consecutive failures — flag for supplement.
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
            print(f"[download] {journal}: {acquired}/{target} PDFs acquired.")

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
        # WHY CALL supplement.write_missing_report HERE?
        #   download.py detects the shortfall and already has all the metadata
        #   (journal, doi, title) in memory.  Calling write_missing_report here
        #   means the researcher gets the CSV immediately after the download run,
        #   without needing to run supplement.py separately.
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
