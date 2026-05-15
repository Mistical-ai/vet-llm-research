"""
src/audit_article_types.py — Retroactive article-type audit for data/raw/
=========================================================================

WHY DOES THIS SCRIPT EXIST?
    Before article-type filtering was added to download.py, some non-primary-
    research PDFs (case reports, short communications, imaging diagnoses) were
    downloaded and saved to data/raw/ because they passed the text-quality gate
    (≥3 pages, ≥3 section headings, ≥3,000 words).

    This script scans every PDF already in data/raw/, classifies each one using
    the same logic as download.py's _classify_article_type(), and reports the
    findings.  With the --remove flag it moves excluded PDFs to
    data/quarantine/rejected_types/ (keeping them as evidence) and with
    --tag-secondary it renames systematic reviews with the "2_" prefix.

    After running this script you should re-run supplement.py to get a fresh
    missing_papers.csv that reflects the new open quota slots.

HOW TO USE:
    # 1. Preview only (no changes to files):
    python src/audit_article_types.py

    # 2. Move excluded PDFs + rename secondary:
    python src/audit_article_types.py --remove --tag-secondary

    # 3. After cleanup, regenerate the missing-papers list:
    python src/supplement.py

WHAT IS THE "2_" PREFIX?
    Files starting with 2_ are secondary research (systematic reviews, meta-
    analyses).  They are kept in data/raw/ because they are peer-reviewed and
    content-rich, but they do NOT count toward the 50-paper primary-research
    quota per journal.  pipeline.py reports them separately so you always know
    how many primary vs secondary papers you have.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Path setup — allows importing from src/ when this script is run from the
# repo root or from within src/.
# ---------------------------------------------------------------------------

_SRC_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SRC_DIR.parent
sys.path.insert(0, str(_SRC_DIR))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from download import (  # noqa: E402
    EXCLUDE_ARTICLE_TYPE_PATTERNS,
    SECONDARY_ARTICLE_TYPE_PATTERNS,
    _classify_article_type,
)

RAW_DIR         = _REPO_ROOT / "data" / "raw"
QUARANTINE_DIR  = _REPO_ROOT / "data" / "quarantine" / "rejected_types"
MISSING_CSV     = _REPO_ROOT / "data" / "missing_papers.csv"


# ---------------------------------------------------------------------------
# Core scan
# ---------------------------------------------------------------------------

def scan_raw_directory() -> list[dict]:
    """
    Classify every PDF in data/raw/ and return a list of result dicts.

    Each result dict has:
        path           : Path    — full path to the PDF
        classification : str     — "primary", "secondary", or "excluded"
        matched_phrase : str     — the phrase that triggered the classification
                                   (empty string for primary)

    Skips files that already have the "2_" prefix — they have been classified
    as secondary in a previous run and do not need re-processing.
    """
    if not RAW_DIR.exists():
        print(f"[audit] data/raw/ not found at {RAW_DIR}.  Nothing to audit.")
        return []

    # Collect all PDF files, but skip files that are already tagged secondary.
    pdf_files = sorted(
        p for p in RAW_DIR.iterdir()
        if p.is_file() and p.suffix.lower() == ".pdf" and not p.name.startswith("2_")
    )

    if not pdf_files:
        print("[audit] No untagged PDF files found in data/raw/.")
        return []

    print(f"[audit] Scanning {len(pdf_files)} PDF(s) in data/raw/ ...")
    results = []
    for pdf_path in pdf_files:
        classification, matched_phrase = _classify_article_type(pdf_path)
        results.append({
            "path":           pdf_path,
            "classification": classification,
            "matched_phrase": matched_phrase,
        })

    return results


# ---------------------------------------------------------------------------
# Report printer
# ---------------------------------------------------------------------------

def print_audit_report(results: list[dict]) -> None:
    """
    Print a human-readable classification table to the terminal.

    Shows each PDF filename alongside its classification and the phrase (if any)
    that determined it.  Totals are printed at the end.
    """
    primary_count   = sum(1 for r in results if r["classification"] == "primary")
    secondary_count = sum(1 for r in results if r["classification"] == "secondary")
    excluded_count  = sum(1 for r in results if r["classification"] == "excluded")

    print()
    print(f"  {'Filename':<70}  {'Class':<12}  {'Matched phrase'}")
    print("  " + "-" * 110)

    for r in results:
        fname  = r["path"].name
        cls    = r["classification"].upper()
        phrase = r["matched_phrase"] or "—"
        # Truncate long filenames so the table stays readable.
        if len(fname) > 68:
            fname = fname[:65] + "..."
        print(f"  {fname:<70}  {cls:<12}  {phrase}")

    print()
    print(f"  Primary (keep as-is):   {primary_count}")
    print(f"  Secondary (2_ prefix):  {secondary_count}")
    print(f"  Excluded (remove):      {excluded_count}")
    print()

    if excluded_count == 0 and secondary_count == 0:
        print("[audit] All PDFs are primary research.  No action needed.")
    else:
        print(
            "[audit] Run with --remove and/or --tag-secondary to apply changes.\n"
            "[audit] Then run:  python src/supplement.py\n"
            "[audit] to regenerate data/missing_papers.csv with the freed slots."
        )


# ---------------------------------------------------------------------------
# Action: move excluded PDFs to quarantine
# ---------------------------------------------------------------------------

def remove_excluded(results: list[dict]) -> int:
    """
    Move PDFs classified as "excluded" to data/quarantine/rejected_types/.

    WHY MOVE INSTEAD OF DELETE?
        Moving preserves evidence for the research record.  If a PDF was
        incorrectly classified (a false positive), you can inspect the file in
        quarantine and restore it manually.

    WHY DELETE missing_papers.csv AFTERWARD?
        Deleting it forces supplement.py to regenerate it from the current
        state of data/raw/ on the next run.  This is simpler and more reliable
        than trying to append rows to an existing CSV.

    Returns the number of PDFs moved.
    """
    excluded = [r for r in results if r["classification"] == "excluded"]
    if not excluded:
        print("[audit] --remove: No excluded PDFs to move.")
        return 0

    QUARANTINE_DIR.mkdir(parents=True, exist_ok=True)

    moved = 0
    for r in excluded:
        pdf_path = r["path"]
        dest     = QUARANTINE_DIR / pdf_path.name
        # Avoid overwriting an existing quarantine file.
        counter = 1
        while dest.exists():
            dest = QUARANTINE_DIR / f"{pdf_path.stem}__dup{counter}{pdf_path.suffix}"
            counter += 1
        pdf_path.rename(dest)
        print(f"[audit] Moved excluded: {pdf_path.name} → quarantine/rejected_types/")
        moved += 1

    # Delete the stale missing_papers.csv so supplement.py regenerates it fresh.
    # The next call to supplement.py will rescan data/raw/ and produce an
    # accurate list of missing slots, including the ones just freed.
    if MISSING_CSV.exists():
        MISSING_CSV.unlink()
        print(
            f"[audit] Deleted {MISSING_CSV.name} — run  python src/supplement.py "
            "to regenerate with updated quota counts."
        )

    return moved


# ---------------------------------------------------------------------------
# Action: rename secondary PDFs with "2_" prefix
# ---------------------------------------------------------------------------

def tag_secondary(results: list[dict]) -> int:
    """
    Rename PDFs classified as "secondary" by prepending "2_" to the filename.

    The "2_" prefix is a lightweight convention that lets pipeline.py and
    analysis scripts distinguish primary research from secondary research
    (systematic reviews, meta-analyses) without reading every file's contents.

    Returns the number of PDFs renamed.
    """
    secondary = [r for r in results if r["classification"] == "secondary"]
    if not secondary:
        print("[audit] --tag-secondary: No secondary PDFs to rename.")
        return 0

    renamed = 0
    for r in secondary:
        pdf_path = r["path"]
        new_path = pdf_path.parent / ("2_" + pdf_path.name)
        if new_path.exists():
            # Already renamed (can happen if the script is run twice without --remove).
            print(f"[audit] Already tagged: {new_path.name}")
            continue
        pdf_path.rename(new_path)
        print(f"[audit] Tagged secondary: {pdf_path.name} → {new_path.name}")
        renamed += 1

    return renamed


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Audit PDFs in data/raw/ for article type (primary / secondary / excluded). "
            "Without flags, prints a report and makes no changes."
        ),
    )
    parser.add_argument(
        "--remove",
        action="store_true",
        help=(
            "Move excluded PDFs (case reports, short comms, etc.) to "
            "data/quarantine/rejected_types/ and delete missing_papers.csv "
            "so supplement.py regenerates it with the freed slots."
        ),
    )
    parser.add_argument(
        "--tag-secondary",
        action="store_true",
        help=(
            "Rename secondary PDFs (systematic reviews, meta-analyses) "
            "by adding a '2_' prefix to their filename."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    print(f"[audit] Article-type patterns to EXCLUDE: {EXCLUDE_ARTICLE_TYPE_PATTERNS}")
    print(f"[audit] Article-type patterns for SECONDARY: {SECONDARY_ARTICLE_TYPE_PATTERNS}")
    print()

    results = scan_raw_directory()
    if not results:
        return 0

    print_audit_report(results)

    if args.remove:
        moved = remove_excluded(results)
        print(f"[audit] Moved {moved} excluded PDF(s) to quarantine.")

    if args.tag_secondary:
        renamed = tag_secondary(results)
        print(f"[audit] Renamed {renamed} secondary PDF(s) with 2_ prefix.")

    if not args.remove and not args.tag_secondary:
        print(
            "[audit] Preview-only run — no files changed.\n"
            "[audit] Re-run with --remove --tag-secondary to apply changes."
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
