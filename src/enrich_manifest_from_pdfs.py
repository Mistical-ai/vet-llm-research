#!/usr/bin/env python3
"""
Script: enrich_manifest_from_pdfs.py
Purpose: Append manifest.jsonl rows for inbox PDFs whose DOI is not yet in the manifest.

Design choices explained (deeper detail in README § Design Decisions):
- Why query CrossRef per PDF instead of re-running collect.py? Bulk collect walks
  ISSN/date windows; a manually downloaded paper may never appear in that slice.
  One /works/{doi} call is precise and cheap for a handful of files.
- Why metadata, then filename, then page 1? Reference lists pollute multi-page scans;
  filename DOIs (``10.1177_1098612X….pdf``) are trusted when metadata is empty.
  ingest_manual_pdfs uses the same filename helper and may also scan pages 1–5.
- Why append-only? Never overwrite manifest lines — preserves collect.py history and
  any manual annotations on existing rows.
- Why non-fatal in auto_ingest_workflow? Ingest can still succeed for PDFs already
  in the manifest if enrichment fails (network, 404 DOI).

Run standalone:
    python src/enrich_manifest_from_pdfs.py
    python src/enrich_manifest_from_pdfs.py --inbox data/manual_inbox --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

REPO_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_INBOX    = REPO_ROOT / "data" / "manual_inbox"
DEFAULT_MANIFEST = REPO_ROOT / "data" / "manifest.jsonl"

CROSSREF_WORKS_URL  = "https://api.crossref.org/works/{doi}"
REQUEST_TIMEOUT     = 20   # seconds
INTER_REQUEST_DELAY = 1.0  # seconds between CrossRef calls (polite pool)

# Ensure src/ siblings are importable regardless of working directory
_SRC_DIR = Path(__file__).resolve().parent
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from ingest_manual_pdfs import (  # noqa: E402
    _collect_dois_from_plaintext,
    _doi_from_filename,
    _load_manifest_indexes,
    _metadata_blob,
)
from collect import _parse_crossref_item  # noqa: E402
from utils import log_error  # noqa: E402


# ---------------------------------------------------------------------------
# PDF metadata DOI extraction
# ---------------------------------------------------------------------------

# Some publishers (observed on Wiley PDFs) stamp the journal's own
# container/ISSN identifier into the PDF metadata's DOI-shaped field instead
# of the article's real DOI, e.g. "10.1111/(issn)1740-8261". That string is
# a well-formed DOI as far as _RAW_DOI's regex is concerned, so without this
# guard it gets treated as "the" metadata DOI — and because bulk CrossRef
# crawls often also pick up a stray container-level row under that same
# fake DOI, the PDF reads as "already matched" even when the actual article
# it belongs to has never been registered in the manifest.
_ISSN_PLACEHOLDER_DOI = re.compile(r"^10\.\d{4,9}/\(issn\)", flags=re.IGNORECASE)


def _get_pdf_metadata_doi(pdf_path: Path) -> str | None:
    """
    Return the first DOI in PDF document metadata only.

    Why not page text here? Enrichment *adds* manifest rows; a reference-list DOI
    would register the wrong paper. Ingest only *matches* existing rows and can
    use page text more safely (see ingest_manual_pdfs.py).

    A metadata hit that looks like a journal-level ISSN placeholder (see
    ``_ISSN_PLACEHOLDER_DOI``) is treated as no hit at all, so callers fall
    through to the filename/page-frequency sources instead of trusting it.
    """
    import pdfplumber  # lazy import mirrors the rest of the pipeline

    try:
        with pdfplumber.open(pdf_path) as pdf:
            blob = _metadata_blob(pdf.metadata)
            hits = _collect_dois_from_plaintext(blob)
            for hit in hits:
                if not _ISSN_PLACEHOLDER_DOI.match(hit):
                    return hit
            return None
    except Exception as exc:  # noqa: BLE001
        log_error(str(pdf_path.name), "enrich_manifest", f"pdfplumber metadata read failed: {exc}")
        return None


def _get_page_dois_with_frequency(
    pdf_path: Path, max_pages: int = 5
) -> list[tuple[str, int]]:
    """
    Scan pages 0..(max_pages-1) and return [(doi, page_count)] sorted by page_count desc.

    page_count = number of distinct pages on which the DOI appears.  A paper's
    own DOI is printed in the header or footer of every page (high count), while
    DOIs from the reference list appear on at most one page (count = 1).

    Ties at the same frequency are broken by first-appearance order (earlier page
    wins), which matches the document-order preference used in ingest_manual_pdfs.
    """
    import pdfplumber

    page_counts: dict[str, int] = {}
    first_seen: dict[str, int] = {}
    try:
        with pdfplumber.open(pdf_path) as pdf:
            for idx in range(min(max_pages, len(pdf.pages))):
                text = pdf.pages[idx].extract_text() or ""
                for doi in _collect_dois_from_plaintext(text):
                    page_counts[doi] = page_counts.get(doi, 0) + 1
                    if doi not in first_seen:
                        first_seen[doi] = idx
    except Exception as exc:  # noqa: BLE001
        log_error(
            str(pdf_path.name),
            "enrich_manifest",
            f"pdfplumber page-frequency scan failed: {exc}",
        )
    return sorted(
        page_counts.items(),
        key=lambda x: (-x[1], first_seen.get(x[0], 999)),
    )


def _get_best_doi_for_enrichment(pdf_path: Path) -> tuple[str | None, str]:
    """Try DOI sources in order of reliability and return (doi, source_label).

    1. PDF embedded metadata dict (safest — identifies the file itself when present)
    2. Filename-based DOI (safe — user/tool named the file after the DOI)
    3. Page-frequency scan across pages 1–5: the paper's own DOI appears in every
       page header/footer (high count); cited DOIs appear only once.  The most
       frequent DOI wins; ties are broken by earliest page of appearance.
    """
    doi = _get_pdf_metadata_doi(pdf_path)
    if doi:
        return doi, "metadata"
    doi = _doi_from_filename(pdf_path)
    if doi:
        return doi, "filename"

    by_freq = _get_page_dois_with_frequency(pdf_path, max_pages=5)
    if not by_freq:
        return None, "none"

    best_doi, best_count = by_freq[0]
    source = f"pages-freq{best_count}" if best_count >= 2 else "page-text"
    return best_doi, source


# ---------------------------------------------------------------------------
# Manifest helpers
# ---------------------------------------------------------------------------

def _load_existing_dois(manifest_path: Path) -> set[str]:
    """Return the set of lowercase DOIs already present in manifest.jsonl."""
    if not manifest_path.exists():
        return set()
    manifest_by_doi, _ = _load_manifest_indexes(manifest_path)
    return set(manifest_by_doi.keys())


# ---------------------------------------------------------------------------
# CrossRef single-DOI lookup
# ---------------------------------------------------------------------------

def _fetch_crossref_single(doi: str, *, mailto: str) -> dict | None:
    """Query CrossRef /works/{doi} and return the message dict, or None on failure.

    All failures are non-fatal: they are logged and return None so the pipeline
    continues with remaining PDFs.
    """
    url = CROSSREF_WORKS_URL.format(doi=doi)
    headers = {"User-Agent": f"vet-llm-research/1.0 (mailto:{mailto})"}

    try:
        response = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)

        if response.status_code == 404:
            print(f"[enrich]   CrossRef 404 — DOI not registered: {doi}")
            log_error(doi, "enrich_manifest", "CrossRef 404 — DOI not registered in CrossRef")
            return None

        response.raise_for_status()
        payload = response.json()

    except requests.exceptions.Timeout:
        print(f"[enrich]   CrossRef timeout for {doi} — skipping.")
        log_error(doi, "enrich_manifest", "CrossRef request timed out")
        return None
    except requests.exceptions.RequestException as exc:
        print(f"[enrich]   CrossRef network error for {doi}: {exc} — skipping.")
        log_error(doi, "enrich_manifest", f"CrossRef network error: {exc}")
        return None
    except ValueError as exc:
        print(f"[enrich]   Malformed CrossRef JSON for {doi}: {exc} — skipping.")
        log_error(doi, "enrich_manifest", f"CrossRef malformed JSON: {exc}")
        return None

    if payload.get("status") != "ok":
        print(f"[enrich]   CrossRef status={payload.get('status')} for {doi} — skipping.")
        log_error(doi, "enrich_manifest", f"CrossRef status={payload.get('status')}")
        return None

    return payload.get("message")


# ---------------------------------------------------------------------------
# CrossRef item → manifest record
# ---------------------------------------------------------------------------

def _item_to_manifest_record(item: dict, doi: str) -> dict | None:
    """Convert a CrossRef /works/{doi} message dict to a manifest row.

    Extracts journal_name and issn from the item itself (unlike collect.py
    which knows the journal ISSN in advance). Missing ISSN is safe — neither
    ingest_manual_pdfs.py nor file_paths.py use it for anything functional;
    it is stored as metadata only.
    """
    container_titles = item.get("container-title") or []
    journal_name = container_titles[0] if container_titles else "Unknown Journal"

    issns = item.get("ISSN") or []
    issn = issns[0] if issns else ""

    record = _parse_crossref_item(item, journal_name, issn)

    if record is None:
        log_error(doi, "enrich_manifest", "_parse_crossref_item returned None")
        return None

    # Normalise DOI to lowercase to match the rest of the manifest
    record["doi"] = record["doi"].lower().strip()

    # OA-metadata stub fields — collect.py always writes these; we replicate
    # the same defaults so enrich-added entries are schema-compatible.
    record.setdefault("is_oa", False)
    record.setdefault("oa_locations_count", 0)
    record.setdefault("has_direct_pdf_url", False)
    record.setdefault("candidate_quality_score", 0)

    return record


# ---------------------------------------------------------------------------
# Main enrichment logic
# ---------------------------------------------------------------------------

def enrich_manifest_from_folder(
    inbox_path: Path,
    manifest_path: Path,
    *,
    mailto: str,
    dry_run: bool = False,
) -> tuple[int, int, int]:
    """
    Scan top-level inbox PDFs, fetch unknown DOIs from CrossRef, append manifest rows.

    Returns (pdfs_scanned, dois_found, records_appended). Subfolders failed/ and
    skipped_existing_in_raw/ are ignored so quarantined copies are not re-enriched.
    """
    pdf_files = sorted(
        p for p in inbox_path.iterdir()
        if p.is_file() and p.suffix.lower() == ".pdf"
    )

    print(f"[enrich] Scanning {len(pdf_files)} PDF(s) in {inbox_path.name}/")

    if not pdf_files:
        print("[enrich] No PDFs found — nothing to enrich.")
        return 0, 0, 0

    existing_dois = _load_existing_dois(manifest_path)
    print(f"[enrich] Manifest currently has {len(existing_dois)} DOIs.")

    # Track DOIs processed this run to deduplicate across multiple PDFs that
    # share the same DOI (e.g. a duplicate download with a different filename).
    seen_this_run: set[str] = set()

    pdfs_scanned    = 0
    dois_found      = 0
    records_appended = 0

    for pdf_path in pdf_files:
        pdfs_scanned += 1
        print(f"[enrich] ({pdfs_scanned}/{len(pdf_files)}) {pdf_path.name}")

        doi, doi_source = _get_best_doi_for_enrichment(pdf_path)

        if doi is None:
            print(f"[enrich]   No DOI found (tried metadata, filename, page-1) — skipping enrichment.")
            continue

        print(f"[enrich]   DOI found via {doi_source}: {doi}")

        dois_found += 1
        doi_lower = doi.lower().strip()

        if doi_lower in existing_dois:
            print(f"[enrich]   DOI already in manifest — skipping: {doi_lower}")
            continue

        if doi_lower in seen_this_run:
            print(f"[enrich]   DOI already queried this run — skipping: {doi_lower}")
            continue

        seen_this_run.add(doi_lower)

        print(f"[enrich]   Querying CrossRef for {doi_lower} …")
        item = _fetch_crossref_single(doi_lower, mailto=mailto)

        if item is None:
            continue

        record = _item_to_manifest_record(item, doi_lower)
        if record is None:
            continue

        title_preview = record.get("title", "")[:60]
        journal_str   = record.get("journal", "?")
        year_str      = str(record.get("year", "?"))

        if dry_run:
            print(f"[enrich]   DRY_RUN — would append: {doi_lower} | {journal_str} | {year_str} | {title_preview}")
        else:
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            with manifest_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            print(f"[enrich]   Appended: {doi_lower} | {journal_str} | {year_str}")
            existing_dois.add(doi_lower)
            records_appended += 1

        time.sleep(INTER_REQUEST_DELAY)

    suffix = " (dry run)" if dry_run else ""
    print(
        f"[enrich] Done{suffix}. "
        f"PDFs scanned: {pdfs_scanned}, "
        f"DOIs found: {dois_found}, "
        f"records appended: {records_appended}."
    )
    return pdfs_scanned, dois_found, records_appended


# ---------------------------------------------------------------------------
# Standalone entry point
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Scan PDFs in manual_inbox/, extract DOIs from PDF metadata, "
            "look up unknown DOIs via CrossRef, and append new entries to "
            "manifest.jsonl before running ingest_manual_pdfs.py."
        )
    )
    parser.add_argument(
        "--inbox",
        type=Path,
        default=DEFAULT_INBOX,
        help="Folder of PDFs to scan (default: data/manual_inbox/)",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=DEFAULT_MANIFEST,
        help="manifest.jsonl to enrich (default: data/manifest.jsonl)",
    )
    parser.add_argument(
        "--mailto",
        type=str,
        default=os.getenv("UNPAYWALL_EMAIL", "pshah10@uoguelph.ca"),
        help="Email for CrossRef polite-pool User-Agent (default: $UNPAYWALL_EMAIL)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be appended without writing anything.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    inbox    = args.inbox.expanduser().resolve()
    manifest = args.manifest.expanduser().resolve()

    if not inbox.exists():
        print(f"[enrich] ERROR: inbox folder does not exist: {inbox}", file=sys.stderr)
        return 1

    enrich_manifest_from_folder(
        inbox,
        manifest,
        mailto=args.mailto,
        dry_run=args.dry_run,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
