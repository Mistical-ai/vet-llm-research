"""
src/supplement.py — Manual Supplement Helper
==============================================

WHY DOES THIS MODULE EXIST?
-----------------------------
Even with 200 OA candidates per journal, some journals may fall short of the
50-PDF quota because their content is predominantly paywalled, embargoed, or
not yet deposited in PMC.  This module helps the researcher manually fill
those gaps without automating any access to paywalled content.

It does three things:
  1. Scans the manifest and data/raw/ to identify which papers are still
     missing for each journal (comparing acquired PDFs against the 50-PDF quota).
  2. Writes data/missing_papers.csv — a machine-readable report that a
     librarian or research assistant can use to track manual progress.
  3. Prints human-readable instructions: "Check UoG library" or "Email author"
     for each missing DOI/title pair.

WHAT THIS MODULE DOES NOT DO
------------------------------
- It does NOT download any PDFs.
- It does NOT access paywalled URLs.
- It does NOT interact with publisher authentication systems.

The researcher (or a librarian) manually places PDFs in data/raw/ and adds
corresponding entries to data/manual_manifest.jsonl.  pipeline.py then merges
OA and manual PDFs into a unified corpus.

SHARED FUNCTION: write_missing_report()
-----------------------------------------
download.py imports write_missing_report() from this module so that the CSV
format is defined in exactly one place.  If the CSV format changes (e.g.
adding a "priority" column), only this function needs updating — download.py
and supplement.py will both pick up the change automatically.

WHY DEFINE THE SHARED FUNCTION HERE (not in utils.py)?
    utils.py is the "Governor" — it handles budget, rate limits, and error
    logging.  CSV report generation is a corpus-management concern, not a
    safety concern.  Keeping it in supplement.py makes the dependency graph
    clearer: download → supplement → collect, never supplement → download.
"""

import csv
import json
import os
from pathlib import Path

from dotenv import load_dotenv

from collect import JOURNAL_TARGETS
from file_paths import legacy_doi_filename, resolve_existing_pdf_path
from utils import log_error

load_dotenv()

# ---------------------------------------------------------------------------
# Article-type patterns (read from the same env var as download.py)
# ---------------------------------------------------------------------------
# WHY NOT IMPORT FROM download.py?
#   download.py imports write_missing_report() from this module, so importing
#   back from download.py would create a circular import.  Reading the env var
#   directly gives the same values without the dependency cycle.

_EXCLUDE_ARTICLE_TYPE_PATTERNS: list[str] = [
    phrase.strip().lower()
    for phrase in os.getenv(
        "EXCLUDE_ARTICLE_TYPE_PATTERNS",
        "short communication,brief communication,brief report,"
        "case report,case series,case study,"
        "imaging diagnosis,imaging findings,"
        "rapid communication",
    ).split(",")
    if phrase.strip()
]

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MANIFEST_PATH    = Path("data") / "manifest.jsonl"
RAW_DIR          = Path("data") / "raw"
MISSING_CSV_PATH = Path("data") / "missing_papers.csv"
ERROR_LOG_PATH   = Path("data") / "error_log.jsonl"

# Suggested actions shown for each missing paper.
# WHY TWO ACTIONS?
#   Some papers are behind a paywall that the University of Guelph subscribes
#   to; a librarian can download them via institutional access.  Others are not
#   accessible even via UoG — for those, emailing the corresponding author for
#   a preprint or accepted manuscript is the fallback.  Both paths are legal
#   and preserve reproducibility if the researcher documents the acquisition
#   method in the paper's manual_manifest.jsonl entry.
SUPPLEMENT_ACTIONS: list[str] = [
    "Check University of Guelph library access (https://lib.uoguelph.ca)",
    "Email corresponding author for preprint / accepted manuscript",
]


# ---------------------------------------------------------------------------
# Helper: DOI → filename (must match download.py exactly)
# ---------------------------------------------------------------------------

def _doi_to_filename(doi: str) -> str:
    """
    Return the legacy DOI-only filename for backward compatibility.
    """
    return legacy_doi_filename(doi)


# ---------------------------------------------------------------------------
# Shared report writer (also imported by download.py)
# ---------------------------------------------------------------------------

def write_missing_report(missing_papers: list[dict]) -> Path:
    """
    Write data/missing_papers.csv and return its path.

    This is the canonical implementation of the CSV report.  Both download.py
    (called automatically on OA shortfall) and supplement.py (called on demand)
    use this function to guarantee a consistent file format.

    WHY A SHARED FUNCTION RATHER THAN DUPLICATED LOGIC?
        If the CSV format changes (e.g. adding a "doi_url" or "priority"
        column), only this function needs updating.  Duplicate implementations
        would diverge over time, producing inconsistent reports depending on
        which code path triggered the shortfall.

    Parameters
    ----------
    missing_papers : list[dict]
        Each dict must have keys: journal, doi, title, reason_missing.
        Additional keys are silently ignored by extrasaction="ignore".

    Returns
    -------
    Path
        Path to the written CSV file (data/missing_papers.csv).
    """
    MISSING_CSV_PATH.parent.mkdir(parents=True, exist_ok=True)

    fieldnames = ["journal", "doi", "title", "reason_missing"]
    with open(MISSING_CSV_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(missing_papers)

    print(f"[supplement] Missing papers report written: {MISSING_CSV_PATH}")
    print(f"[supplement] {len(missing_papers)} papers need manual supplementation.")
    return MISSING_CSV_PATH


# ---------------------------------------------------------------------------
# Failure reason lookup
# ---------------------------------------------------------------------------

def _load_failure_reasons() -> dict[str, str]:
    """
    Read data/error_log.jsonl and return a dict mapping each DOI to the message
    from its most recent 'download' stage entry.

    WHY ONLY THE DOWNLOAD STAGE?
        Validation and collect_filter entries describe problems with the PDF
        content or metadata — not the download attempt.  We want the reason
        the automated downloader couldn't retrieve the file, since that's what
        tells the researcher how to get it manually (library proxy vs. author
        email vs. genuinely paywalled).

    WHY LAST ENTRY WINS?
        download.py logs one entry per attempt; the last one reflects the final
        state after all fallbacks were exhausted.  Earlier entries for the same
        DOI are intermediate failures that were superseded.
    """
    reasons: dict[str, str] = {}
    if not ERROR_LOG_PATH.exists():
        return reasons
    with open(ERROR_LOG_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                if entry.get("stage") == "download":
                    doi = entry.get("doi", "").strip()
                    msg = entry.get("message", "")
                    if doi:
                        reasons[doi] = msg
            except json.JSONDecodeError:
                continue
    return reasons


# ---------------------------------------------------------------------------
# Core report generation
# ---------------------------------------------------------------------------

def generate_supplement_report() -> list[dict]:
    """
    Scan the manifest and data/raw/ to identify missing papers, then write
    data/missing_papers.csv and print manual supplement instructions.

    LOGIC
    ------
    For each journal in JOURNAL_TARGETS:
      1. Load all manifest records for that journal.
      2. Count how many of those DOIs have PDFs in data/raw/.
      3. If count < 50, the deficit papers are added to the missing list.

    Papers are listed in manifest order (newest-first, because collect.py uses
    sort=published desc), so high-priority recent papers appear at the top of
    the CSV.

    WHY NOT JUST COUNT PDFs IN data/raw/?
        Counting files in data/raw/ without the manifest would miss the
        metadata (title, journal) needed to populate the CSV report.  We need
        the manifest as the source of truth for which DOIs belong to which
        journal; data/raw/ is just the download cache.

    Returns
    -------
    list[dict]
        Missing papers, each with: journal, doi, title, reason_missing.
        Empty list if all quotas are met.
    """
    if not MANIFEST_PATH.exists():
        print(f"[supplement] Manifest not found at {MANIFEST_PATH}. Run collect.py first.")
        return []

    # --- Load manifest records grouped by journal ---
    # WHY GROUP BY JOURNAL (not flat list)?
    #   We need per-journal counts to determine the deficit for each journal.
    #   Grouping at load time avoids a second pass over the data.
    journal_records: dict[str, list[dict]] = {j: [] for j in JOURNAL_TARGETS}
    seen_dois: set[str] = set()

    with open(MANIFEST_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
                doi     = record.get("doi", "").strip()
                journal = record.get("journal", "").strip()
                if doi and journal in journal_records and doi not in seen_dois:
                    seen_dois.add(doi)
                    journal_records[journal].append(record)
            except json.JSONDecodeError:
                continue

    # --- Compare manifest against downloaded PDFs ---
    # Load per-DOI failure reasons from the error log so the CSV tells the
    # researcher WHY each paper wasn't downloaded and how to approach it.
    failure_reasons = _load_failure_reasons()

    missing_papers: list[dict] = []
    total_missing  = 0

    print("\n[supplement] Per-journal acquisition status:")
    print(f"  {'Journal':<25} {'Have':>5}  {'Target':>6}  {'Missing':>7}")
    print("  " + "-" * 50)

    for journal, records in journal_records.items():
        target = JOURNAL_TARGETS.get(journal, 50)

        # Determine which DOIs from this journal already have a PDF on disk.
        # Only primary-research PDFs (no "2_" prefix) count toward the quota.
        downloaded_dois: set[str] = {
            r["doi"] for r in records
            if resolve_existing_pdf_path(RAW_DIR, r) is not None
            and not resolve_existing_pdf_path(RAW_DIR, r).name.startswith("2_")
        }

        have    = len(downloaded_dois)
        deficit = max(0, target - have)
        total_missing += deficit

        print(f"  {journal:<25} {have:>5}  {target:>6}  {deficit:>7}")

        if deficit > 0:
            # Candidates: manifest records that do NOT yet have a primary PDF.
            not_downloaded = [r for r in records if r["doi"] not in downloaded_dois]

            # Filter out non-primary article types by checking the title.
            # This removes case reports, short communications, editorials, and
            # other non-original-research articles that slipped past the title
            # filter in collect.py.  The same phrases live in download.py and
            # audit_article_types.py; here we read them from the same env var
            # (_EXCLUDE_ARTICLE_TYPE_PATTERNS) to avoid a circular import.
            not_downloaded = [
                r for r in not_downloaded
                if not any(
                    phrase in r.get("title", "").lower()
                    for phrase in _EXCLUDE_ARTICLE_TYPE_PATTERNS
                )
            ]

            for record in not_downloaded[:deficit]:
                doi      = record.get("doi", "")
                last_msg = failure_reasons.get(doi, "")

                # Translate the raw error log message into an actionable hint.
                if "CLOUDFLARE_BLOCKED" in last_msg or "cloudflare" in last_msg.lower():
                    # The downloader found an OA URL but publisher CDN blocked it.
                    # Institutional library proxy or a direct author request will
                    # often succeed where the automated downloader cannot.
                    reason = "CLOUDFLARE_BLOCKED — try UoG library proxy or author email"
                elif "ended with" in last_msg:
                    reason = "Download failed: " + last_msg.split("ended with ")[-1].strip()
                elif last_msg:
                    reason = last_msg[:100]
                else:
                    reason = "No OA version found by download.py"

                missing_papers.append({
                    "journal":        journal,
                    "doi":            doi,
                    "title":          record.get("title", "No title available"),
                    "reason_missing": reason,
                })

    print("  " + "-" * 50)
    print(f"  {'TOTAL':<25} {'':>5}  {'250':>6}  {total_missing:>7}")

    if not missing_papers:
        print("\n[supplement] All journal quotas met. No manual supplementation needed.")
        return []

    # Sort so CLOUDFLARE_BLOCKED papers appear first.
    # These are the most actionable: the downloader found an OA URL but publisher
    # CDN blocked it.  Institutional library proxy (UoG) or an author email will
    # usually succeed.  Papers with other failure reasons follow.
    missing_papers.sort(
        key=lambda p: (0 if "CLOUDFLARE_BLOCKED" in p["reason_missing"] else 1)
    )

    # --- Print human-readable supplement instructions ---
    print(
        f"\n[supplement] {total_missing} papers need manual supplementation.\n"
        f"[supplement] After downloading, place PDFs in data/raw/ and add entries\n"
        f"             to data/manual_manifest.jsonl (same schema as manifest.jsonl).\n"
    )

    for paper in missing_papers:
        title_display = paper["title"]
        if len(title_display) > 80:
            title_display = title_display[:77] + "..."
        print(f"  DOI     : {paper['doi']}")
        print(f"  Title   : {title_display}")
        print(f"  Journal : {paper['journal']}")
        for action in SUPPLEMENT_ACTIONS:
            print(f"  -> {action}")
        print()

    # --- Write CSV report ---
    write_missing_report(missing_papers)

    return missing_papers


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    generate_supplement_report()
