"""
pipeline.py — Corpus Orchestration
=====================================

WHY DOES THIS MODULE EXIST?
-----------------------------
The pipeline currently has no single entry point that shows the researcher
the overall state of the corpus.  This module fills that gap: it merges OA-
downloaded PDFs with any manually supplied PDFs, deduplicates by DOI, and
prints a structured status report.

It does NOT replace the individual stage scripts:
  - python src/collect.py   → builds data/manifest.jsonl
  - python src/download.py  → downloads OA PDFs to data/raw/
  - python src/supplement.py → reports and guides manual supplementation
  - python pipeline.py      → merges sources and reports corpus health

This separation-of-concerns means each stage can be run independently,
re-run after a crash, or replaced with a different implementation without
touching the others.

TWO-SOURCE CORPUS MODEL
------------------------
The final corpus is the union of:
  1. OA papers downloaded automatically by download.py
     → tracked in data/manifest.jsonl
     → PDFs in data/raw/

  2. Manually supplemented papers placed by the researcher
     → tracked in data/manual_manifest.jsonl (same schema as manifest.jsonl)
     → PDFs in data/raw/ (same naming convention as download.py uses)

pipeline.py merges both sources, deduplicates by DOI, validates that every
manual entry actually has a PDF in data/raw/, and reports the effective corpus
size (250 − total_missing).

WHY VALIDATE MANUAL PDFs BEFORE COUNTING THEM?
    data/manual_manifest.jsonl is edited by hand.  A researcher might add an
    entry before copying the PDF, or the filename might be mis-typed.  Counting
    an entry without a PDF would inflate the corpus size metric and hide the
    gap from downstream stages (extract.py, summarise.py).

SUCCESS THRESHOLD
------------------
OA_THRESHOLD = 200 means: if OA alone yields ≥ 200 PDFs, the run is
considered acceptable and the researcher can proceed with manual supplementation
to fill the remaining ≤ 50 papers.  Below 200, something is wrong (network
failure, wrong ISSNs, etc.) and manual supplementation alone is insufficient.

This threshold is set at 200 (80% of 250) rather than 250 because:
  - Not all papers are OA — some supplementation is expected.
  - Running extract.py and summarise.py on 200 papers still yields enough
    data for a meaningful Phase 4 analysis.
  - A threshold of 250 would require perfect OA coverage, which is unrealistic.
"""

import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

# Add src/ to the Python path so pipeline.py (at workspace root) can import
# from src/collect.py, src/utils.py, etc. without an installed package.
sys.path.insert(0, str(Path(__file__).parent / "src"))

from collect import JOURNAL_TARGETS   # noqa: E402 (after sys.path insert)
from utils import log_error           # noqa: E402

load_dotenv()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MANIFEST_PATH        = Path("data") / "manifest.jsonl"
MANUAL_MANIFEST_PATH = Path("data") / "manual_manifest.jsonl"
RAW_DIR              = Path("data") / "raw"

# Total corpus target (5 journals × 50 papers each).
CORPUS_TARGET: int = sum(JOURNAL_TARGETS.values())  # 250

# Minimum OA-sourced PDFs for the run to be considered acceptable without
# requiring extensive manual intervention.  See module docstring for rationale.
OA_THRESHOLD: int = 200


# ---------------------------------------------------------------------------
# Helper: DOI → filename (mirrors download.py exactly)
# ---------------------------------------------------------------------------

def _doi_to_filename(doi: str) -> str:
    """
    Convert a DOI to the same safe filename as download.py uses.

    WHY DUPLICATE (not import from download.py)?
        Importing download.py would transitively import supplement.py and
        collect.py, executing all their module-level code including load_dotenv
        calls and constant definitions.  For a thin orchestration script that
        only needs one utility function, a local copy is less surprising and
        avoids side effects at import time.
    """
    safe = doi.replace("/", "_").replace(":", "_").replace(".", "_")
    return f"{safe}.pdf"


# ---------------------------------------------------------------------------
# Corpus loader: merge OA + manual manifests
# ---------------------------------------------------------------------------

def load_corpus() -> dict:
    """
    Load and merge the OA manifest and optional manual manifest into a single
    deduplicated view of the corpus.

    MERGE LOGIC
    ------------
    1. Load every line from data/manifest.jsonl (OA records).
    2. Load every line from data/manual_manifest.jsonl (if it exists).
    3. Deduplicate by DOI — if the same DOI appears in both, the OA record
       takes precedence (manual record is silently skipped).
    4. For manual records only: validate that the PDF exists in data/raw/.
       Records without a confirmed PDF are reported as invalid and excluded
       from the downloaded count.

    WHY OA RECORD TAKES PRECEDENCE?
        The OA record was fetched automatically and has consistent metadata
        (covariates, abstract, etc.).  A manual record for the same DOI is
        likely a duplicate entry by the researcher; the OA version is more
        reliable.

    Returns
    -------
    dict with keys:
        records        : list[dict] — All deduplicated records (OA + manual).
        downloaded     : list[str]  — DOIs with PDFs confirmed in data/raw/.
        missing_pdfs   : list[str]  — DOIs in manifest but no PDF found.
        oa_count       : int        — Records loaded from data/manifest.jsonl.
        manual_count   : int        — Valid records from data/manual_manifest.jsonl.
        invalid_manual : list[dict] — Manual entries without confirmed PDFs.
    """
    records: list[dict] = []
    seen_dois: set[str] = set()

    # --- Load OA manifest ---
    oa_count = 0
    if MANIFEST_PATH.exists():
        with open(MANIFEST_PATH, encoding="utf-8") as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                    doi    = record.get("doi", "").strip()
                    if doi and doi not in seen_dois:
                        seen_dois.add(doi)
                        record["_source"] = "oa"
                        records.append(record)
                        oa_count += 1
                except json.JSONDecodeError:
                    log_error("N/A", "pipeline", f"Malformed OA manifest line {line_num}")
    else:
        print(f"[pipeline] OA manifest not found at {MANIFEST_PATH}. Run collect.py first.")

    # --- Load manual manifest (optional) ---
    manual_count   = 0
    invalid_manual: list[dict] = []

    if MANUAL_MANIFEST_PATH.exists():
        with open(MANUAL_MANIFEST_PATH, encoding="utf-8") as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                    doi    = record.get("doi", "").strip()
                    if not doi:
                        log_error("N/A", "pipeline", f"Manual manifest line {line_num}: missing doi")
                        continue

                    # VALIDATION: the PDF must exist before this DOI counts.
                    # WHY VALIDATE HERE?
                    #   data/manual_manifest.jsonl is edited by hand and may
                    #   contain entries added before the PDF was copied, or with
                    #   a typo in the DOI.  Counting an entry without a PDF would
                    #   silently inflate the corpus size metric.
                    pdf_path = RAW_DIR / _doi_to_filename(doi)
                    if not pdf_path.exists():
                        invalid_manual.append({
                            "doi":    doi,
                            "title":  record.get("title", ""),
                            "reason": f"PDF not found at {pdf_path}",
                        })
                        continue

                    if doi in seen_dois:
                        # OA record already loaded; skip duplicate.
                        continue

                    seen_dois.add(doi)
                    record["_source"] = "manual"
                    records.append(record)
                    manual_count += 1

                except json.JSONDecodeError:
                    log_error("N/A", "pipeline", f"Malformed manual manifest line {line_num}")

        if invalid_manual:
            print(
                f"\n[pipeline] WARNING: {len(invalid_manual)} manual manifest "
                f"entries have no PDF in data/raw/:"
            )
            for item in invalid_manual[:5]:
                print(f"  {item['doi']}: {item['reason']}")
            if len(invalid_manual) > 5:
                print(f"  ... and {len(invalid_manual) - 5} more")
            print(
                "  Add entries to data/manual_manifest.jsonl only AFTER placing "
                "PDFs in data/raw/."
            )

    # --- Determine which DOIs have PDFs on disk ---
    downloaded:   list[str] = []
    missing_pdfs: list[str] = []

    for record in records:
        doi = record["doi"]
        if (RAW_DIR / _doi_to_filename(doi)).exists():
            downloaded.append(doi)
        else:
            missing_pdfs.append(doi)

    return {
        "records":        records,
        "downloaded":     downloaded,
        "missing_pdfs":   missing_pdfs,
        "oa_count":       oa_count,
        "manual_count":   manual_count,
        "invalid_manual": invalid_manual,
    }


# ---------------------------------------------------------------------------
# Status reporter
# ---------------------------------------------------------------------------

def report_corpus_status(corpus: dict) -> None:
    """
    Print a structured summary of the current corpus state.

    Shows per-journal breakdown, total PDFs, missing count, and whether the
    OA-only run meets the OA_THRESHOLD (200) for manual supplementation to
    be sufficient.

    Parameters
    ----------
    corpus : dict — Output of load_corpus().
    """
    records      = corpus["records"]
    downloaded   = corpus["downloaded"]
    missing_pdfs = corpus["missing_pdfs"]
    oa_count     = corpus["oa_count"]
    manual_count = corpus["manual_count"]

    total_with_pdf  = len(downloaded)
    effective_total = total_with_pdf           # PDFs we actually have
    total_missing   = max(0, CORPUS_TARGET - total_with_pdf)

    print("\n" + "=" * 62)
    print("  CORPUS STATUS")
    print("=" * 62)
    print(f"  Manifest entries (OA):        {oa_count:>5}")
    print(f"  Manifest entries (manual):    {manual_count:>5}")
    print(f"  Total unique DOIs:             {len(records):>5}")
    print(f"  PDFs confirmed in data/raw/:  {total_with_pdf:>5}")
    print(f"  PDFs still missing:           {len(missing_pdfs):>5}")
    print(f"  Corpus target:                {CORPUS_TARGET:>5}")
    print(f"  Effective corpus size:        {effective_total:>5}  ({effective_total}/{CORPUS_TARGET})")
    print()

    # --- Per-journal breakdown ---
    journal_counts: dict[str, dict] = {}
    for record in records:
        j = record.get("journal", "Unknown")
        if j not in journal_counts:
            journal_counts[j] = {"total": 0, "have_pdf": 0, "source": set()}
        journal_counts[j]["total"] += 1
        if (RAW_DIR / _doi_to_filename(record["doi"])).exists():
            journal_counts[j]["have_pdf"] += 1
        journal_counts[j]["source"].add(record.get("_source", "oa"))

    print(f"  {'Journal':<25} {'Target':>6}  {'PDF':>5}  {'Status'}")
    print("  " + "-" * 57)
    for journal, target in JOURNAL_TARGETS.items():
        counts   = journal_counts.get(journal, {"total": 0, "have_pdf": 0})
        have     = counts["have_pdf"]
        status   = "✓ OK" if have >= target else f"NEED {target - have} MORE"
        print(f"  {journal:<25} {target:>6}  {have:>5}  {status}")
    print("=" * 62)

    # --- Overall assessment ---
    if total_with_pdf >= CORPUS_TARGET:
        print(f"\n[pipeline] Corpus complete: {total_with_pdf}/{CORPUS_TARGET} PDFs acquired.")

    elif total_with_pdf >= OA_THRESHOLD:
        remaining = CORPUS_TARGET - total_with_pdf
        print(
            f"\n[pipeline] OA corpus acceptable: {total_with_pdf} >= {OA_THRESHOLD} threshold.\n"
            f"[pipeline] {remaining} papers still needed for full corpus (250 target).\n"
            f"[pipeline] Manual supplementation steps:\n"
            f"  1. Run: python src/supplement.py  → generates data/missing_papers.csv\n"
            f"  2. Acquire PDFs and place in:     data/raw/\n"
            f"  3. Add entries to:                data/manual_manifest.jsonl\n"
            f"  4. Re-run: python pipeline.py     → verify updated count"
        )

    else:
        print(
            f"\n[pipeline] WARNING: Only {total_with_pdf}/{OA_THRESHOLD} minimum PDFs acquired.\n"
            f"[pipeline] Possible causes:\n"
            f"  - Network errors during download.py run\n"
            f"  - Low OA availability for the target journals in 2023-2025\n"
            f"  - manifest.jsonl is empty or has wrong journal names\n"
            f"[pipeline] Run: python src/supplement.py to diagnose per-journal gaps."
        )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("[pipeline] Loading corpus (OA manifest + manual manifest)...")
    corpus = load_corpus()
    report_corpus_status(corpus)

    # Exit with code 1 if below minimum threshold.
    # WHY sys.exit(1)?
    #   Makes pipeline.py usable in a CI/CD check or a shell script that
    #   should fail fast when the corpus is critically under-populated.
    n_downloaded = len(corpus["downloaded"])
    if n_downloaded < OA_THRESHOLD:
        sys.exit(1)
