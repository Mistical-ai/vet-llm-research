"""
src/supplement.py — Manual Supplement Helper
==============================================

WHY DOES THIS MODULE EXIST?
-----------------------------
Even with 80 OA candidates per journal, some journals may fall short of the
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
from utils import log_error

load_dotenv()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MANIFEST_PATH    = Path("data") / "manifest.jsonl"
RAW_DIR          = Path("data") / "raw"
MISSING_CSV_PATH = Path("data") / "missing_papers.csv"

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
    Convert a DOI to the same safe filename used by download.py.

    WHY DUPLICATE THIS (not import from download.py)?
        Importing download.py from supplement.py would create a circular
        import: download → supplement → download.  Duplicating this tiny
        pure function (no logic, no state) is cheaper than the circular-import
        workaround, and the function is trivial enough that divergence is
        extremely unlikely.  The two implementations are kept identical by a
        comment in both files.
    """
    safe = doi.replace("/", "_").replace(":", "_").replace(".", "_")
    return f"{safe}.pdf"


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
    missing_papers: list[dict] = []
    total_missing  = 0

    print("\n[supplement] Per-journal acquisition status:")
    print(f"  {'Journal':<25} {'Have':>5}  {'Target':>6}  {'Missing':>7}")
    print("  " + "-" * 50)

    for journal, records in journal_records.items():
        target = JOURNAL_TARGETS.get(journal, 50)

        # Determine which DOIs from this journal already have a PDF on disk.
        downloaded_dois: set[str] = {
            r["doi"] for r in records
            if (RAW_DIR / _doi_to_filename(r["doi"])).exists()
        }

        have    = len(downloaded_dois)
        deficit = max(0, target - have)
        total_missing += deficit

        print(f"  {journal:<25} {have:>5}  {target:>6}  {deficit:>7}")

        if deficit > 0:
            # Candidates for manual supplementation: manifest records that
            # do NOT yet have a PDF, listed newest-first (manifest order).
            not_downloaded = [r for r in records if r["doi"] not in downloaded_dois]
            for record in not_downloaded[:deficit]:
                missing_papers.append({
                    "journal":        journal,
                    "doi":            record.get("doi", ""),
                    "title":          record.get("title", "No title available"),
                    "reason_missing": "No OA version found by download.py",
                })

    print("  " + "-" * 50)
    print(f"  {'TOTAL':<25} {'':>5}  {'250':>6}  {total_missing:>7}")

    if not missing_papers:
        print("\n[supplement] All journal quotas met. No manual supplementation needed.")
        return []

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
