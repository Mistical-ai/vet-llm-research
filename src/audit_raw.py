#!/usr/bin/env python3
"""
Script: audit_raw.py
Purpose: Audit data/raw/ against both manifests, deduplicate manifest entries,
         and backfill CrossRef records for any PDFs with no manifest entry.

Run:
    python src/audit_raw.py --dry-run   # safe preview, no writes
    python src/audit_raw.py             # apply fixes
    python src/audit_raw.py --report    # also save data/audit_raw_report.txt
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

REPO_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_RAW_DIR          = REPO_ROOT / "data" / "raw"
DEFAULT_MANIFEST         = REPO_ROOT / "data" / "manifest.jsonl"
DEFAULT_MANUAL_MANIFEST  = REPO_ROOT / "data" / "manual_manifest.jsonl"
DEFAULT_REPORT_PATH      = REPO_ROOT / "data" / "audit_raw_report.txt"

INTER_REQUEST_DELAY = 1.0  # seconds between CrossRef calls (polite pool)

_SRC_DIR = Path(__file__).resolve().parent
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from enrich_manifest_from_pdfs import (  # noqa: E402
    _fetch_crossref_single,
    _get_best_doi_for_enrichment,
    _item_to_manifest_record,
)
from utils import log_error  # noqa: E402


# ---------------------------------------------------------------------------
# Manifest loading and deduplication
# ---------------------------------------------------------------------------

def _load_and_dedup_manifest(
    manifest_path: Path,
) -> tuple[dict[str, dict], list[dict], int]:
    """
    Read manifest, return (doi_index, ordered_unique_records, duplicate_count).

    Last occurrence of each DOI wins (most recent append is most complete).
    Original insertion order of first appearances is preserved in output list.
    """
    if not manifest_path.exists():
        return {}, [], 0

    first_order: list[str] = []  # DOIs in first-seen order
    records_by_doi: dict[str, dict] = {}
    total_lines = 0

    with manifest_path.open(encoding="utf-8") as fh:
        for raw_line in fh:
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            total_lines += 1
            try:
                rec = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            doi = rec.get("doi", "").lower().strip()
            if doi not in records_by_doi:
                first_order.append(doi)
            records_by_doi[doi] = rec  # last occurrence wins

    unique_records = [records_by_doi[d] for d in first_order if d in records_by_doi]
    duplicates_removed = total_lines - len(unique_records)
    return records_by_doi, unique_records, duplicates_removed


def _write_manifest_safe(manifest_path: Path, records: list[dict]) -> None:
    """Write records to a .tmp file then atomically replace the original."""
    tmp_path = manifest_path.with_suffix(".jsonl.tmp")
    with tmp_path.open("w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    os.replace(tmp_path, manifest_path)


# ---------------------------------------------------------------------------
# Core audit logic
# ---------------------------------------------------------------------------

def run_audit(
    raw_dir: Path,
    manifest_path: Path,
    manual_manifest_path: Path,
    *,
    mailto: str,
    dry_run: bool,
) -> dict:
    """
    Run the full audit and return a results dict for reporting.
    """
    results: dict = {
        "pdfs_total": 0,
        "matched": 0,
        "no_doi": [],      # list of filenames
        "orphans_backfilled": [],
        "orphans_crossref_failed": [],
        "manifest_lines_before": 0,
        "manifest_lines_after": 0,
        "manifest_dupes": 0,
        "manual_lines_before": 0,
        "manual_lines_after": 0,
        "manual_dupes": 0,
    }

    # --- Phase 1: load & deduplicate manifests ---
    print("[audit] Loading manifests …")

    doi_index, unique_records, dupes = _load_and_dedup_manifest(manifest_path)
    results["manifest_lines_before"] = len(unique_records) + dupes
    results["manifest_lines_after"] = len(unique_records)
    results["manifest_dupes"] = dupes
    print(
        f"[audit] manifest.jsonl: {results['manifest_lines_before']} lines, "
        f"{dupes} duplicates"
    )

    manual_index, manual_unique, manual_dupes = _load_and_dedup_manifest(manual_manifest_path)
    results["manual_lines_before"] = len(manual_unique) + manual_dupes
    results["manual_lines_after"] = len(manual_unique)
    results["manual_dupes"] = manual_dupes
    print(
        f"[audit] manual_manifest.jsonl: {results['manual_lines_before']} lines, "
        f"{manual_dupes} duplicates"
    )

    combined_dois: set[str] = set(doi_index.keys()) | set(manual_index.keys())

    # --- Phase 2: write deduplicated manifests ---
    if not dry_run:
        if dupes > 0:
            _write_manifest_safe(manifest_path, unique_records)
            print(f"[audit] Wrote deduplicated manifest.jsonl ({len(unique_records)} lines)")
        if manual_dupes > 0:
            _write_manifest_safe(manual_manifest_path, manual_unique)
            print(f"[audit] Wrote deduplicated manual_manifest.jsonl ({len(manual_unique)} lines)")
        if dupes == 0 and manual_dupes == 0:
            print("[audit] No duplicates — manifests unchanged.")
    else:
        if dupes > 0 or manual_dupes > 0:
            print("[audit] DRY_RUN — would write deduplicated manifests (no changes made)")

    # --- Phase 3: scan raw/ ---
    pdf_files = sorted(p for p in raw_dir.iterdir() if p.is_file() and p.suffix.lower() == ".pdf")
    results["pdfs_total"] = len(pdf_files)
    print(f"\n[audit] Scanning {len(pdf_files)} PDF(s) in {raw_dir.name}/ …")

    orphan_dois: list[tuple[Path, str]] = []  # (path, doi)

    for idx, pdf_path in enumerate(pdf_files, 1):
        doi, source = _get_best_doi_for_enrichment(pdf_path)
        prefix = f"[audit] ({idx}/{len(pdf_files)}) {pdf_path.name[:60]}"

        if doi is None:
            print(f"{prefix} — ⚠️  no DOI found")
            results["no_doi"].append(pdf_path.name)
            continue

        doi_lower = doi.lower().strip()

        if doi_lower in combined_dois:
            results["matched"] += 1
            print(f"{prefix} — ✅  {doi_lower}")
        else:
            print(f"{prefix} — ❌  orphan via {source}: {doi_lower}")
            orphan_dois.append((pdf_path, doi_lower))

    # --- Phase 4: backfill orphans ---
    if orphan_dois:
        print(f"\n[audit] Backfilling {len(orphan_dois)} orphan(s) via CrossRef …")

    for pdf_path, doi_lower in orphan_dois:
        print(f"[audit] Querying CrossRef: {doi_lower}")

        if dry_run:
            print(f"[audit]   DRY_RUN — would append: {doi_lower}")
            results["orphans_backfilled"].append(doi_lower)
            continue

        item = _fetch_crossref_single(doi_lower, mailto=mailto)
        if item is None:
            results["orphans_crossref_failed"].append(doi_lower)
            continue

        record = _item_to_manifest_record(item, doi_lower)
        if record is None:
            results["orphans_crossref_failed"].append(doi_lower)
            continue

        with manifest_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

        combined_dois.add(doi_lower)
        results["orphans_backfilled"].append(doi_lower)
        results["manifest_lines_after"] += 1
        print(f"[audit]   Appended: {doi_lower} | {record.get('journal','?')} | {record.get('year','?')}")

        time.sleep(INTER_REQUEST_DELAY)

    return results


# ---------------------------------------------------------------------------
# Report formatting
# ---------------------------------------------------------------------------

def _format_report(results: dict, dry_run: bool) -> str:
    suffix = " (DRY RUN — no files written)" if dry_run else ""
    lines = [
        f"=== audit_raw summary{suffix} ===",
        f"PDFs in raw/:           {results['pdfs_total']}",
        f"  Matched in manifest:  {results['matched']}",
        f"  No DOI extractable:   {len(results['no_doi'])}",
        f"  Orphans backfilled:   {len(results['orphans_backfilled'])}",
        f"  Orphans (CR failed):  {len(results['orphans_crossref_failed'])}",
        "",
        f"manifest.jsonl:         {results['manifest_lines_before']} → {results['manifest_lines_after']} lines "
        f"({results['manifest_dupes']} duplicates removed)",
        f"manual_manifest.jsonl:  {results['manual_lines_before']} → {results['manual_lines_after']} lines "
        f"({results['manual_dupes']} duplicates removed)",
    ]
    if results["no_doi"]:
        lines += ["", "PDFs with no DOI extracted:"]
        for name in results["no_doi"]:
            lines.append(f"  {name}")
    if results["orphans_crossref_failed"]:
        lines += ["", "Orphans where CrossRef lookup failed (still not in manifest):"]
        for doi in results["orphans_crossref_failed"]:
            lines.append(f"  {doi}")
    if results["orphans_backfilled"]:
        lines += ["", f"Backfilled DOIs ({'would-be' if dry_run else 'appended'}):"]
        for doi in results["orphans_backfilled"]:
            lines.append(f"  {doi}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Audit data/raw/ against manifests: deduplicate manifest entries "
            "and backfill CrossRef records for any orphaned PDFs."
        )
    )
    parser.add_argument("--raw-dir",         type=Path, default=DEFAULT_RAW_DIR)
    parser.add_argument("--manifest",        type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--manual-manifest", type=Path, default=DEFAULT_MANUAL_MANIFEST)
    parser.add_argument(
        "--mailto",
        type=str,
        default=os.getenv("UNPAYWALL_EMAIL", "pshah10@uoguelph.ca"),
        help="Email for CrossRef polite-pool User-Agent",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report only — do not write any files.",
    )
    parser.add_argument(
        "--report",
        action="store_true",
        help=f"Save summary to {DEFAULT_REPORT_PATH}",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    raw_dir   = args.raw_dir.expanduser().resolve()
    manifest  = args.manifest.expanduser().resolve()
    manual    = args.manual_manifest.expanduser().resolve()

    if not raw_dir.exists():
        print(f"[audit] ERROR: raw dir does not exist: {raw_dir}", file=sys.stderr)
        return 1

    results = run_audit(
        raw_dir, manifest, manual,
        mailto=args.mailto,
        dry_run=args.dry_run,
    )

    report_text = _format_report(results, dry_run=args.dry_run)
    print(f"\n{report_text}")

    if args.report and not args.dry_run:
        DEFAULT_REPORT_PATH.write_text(report_text + "\n", encoding="utf-8")
        print(f"\n[audit] Report saved to {DEFAULT_REPORT_PATH}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
