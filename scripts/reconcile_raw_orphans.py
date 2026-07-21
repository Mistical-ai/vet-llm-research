#!/usr/bin/env python3
"""
scripts/reconcile_raw_orphans.py — diagnose data/raw/ PDFs that pipeline.py
and ``summarize --mode batch`` can't discover under today's recomputed
filename, and report/fix why.

Background
----------
``file_paths.descriptive_pdf_filename()``/``descriptive_stem()`` recompute a
paper's expected filename from its *current* manifest ``journal`` + ``title``
every time they're called. If title-truncation constants change, or a
manifest title gets edited, after a paper was downloaded, the recomputed name
silently stops matching the file actually on disk — the PDF and its cached
text are still there, just invisible to any exact-name lookup. That's exactly
what happened to 43 papers in this corpus (title-length drift from an older
``TITLE_MAX_LENGTH``), which is why ``pipeline.py`` reported 207/250 primary
PDFs even though ``data/raw/`` and ``data/processed/`` both had 250 files.

``src/file_paths.py::doi_suffix_glob_candidates`` (used by
``resolve_existing_pdf_path``, ``corpus_status.pdf_path_for``, and
``prepare_texts.find_processed_jsonl``/``find_cached_jsonl``) now recovers
these automatically via a DOI-suffix glob, so most orphans need no manifest
change at all. This script exists to:

1. Report which ``data/raw/*.pdf`` files are still orphans (invisible even
   to the glob fallback — e.g. the DOI suffix itself got truncated on disk).
2. For each orphan, resolve its real DOI directly from the PDF content
   (page-frequency scan: the DOI that repeats on every page 1-5 is the
   paper's own DOI, not a reference-list citation — the same heuristic
   ``enrich_manifest_from_pdfs.py`` uses, but tried FIRST here instead of
   after PDF metadata, which is unreliable for this corpus — see that
   module's ``_ISSN_PLACEHOLDER_DOI`` guard for why).
3. Split results into: already a manifest.jsonl row (no action needed —
   the glob fallback or a filename fix in
   ``scripts/fix_truncated_doi_filenames.py`` handles it) vs. genuinely new
   (backfilled into ``data/manifest.jsonl`` via CrossRef, same pattern as
   ``src/audit_raw.py``'s orphan backfill).

No live LLM API calls — only the public CrossRef REST API, same as
``audit_raw.py`` and ``enrich_manifest_from_pdfs.py`` already use.

Run:
    python scripts/reconcile_raw_orphans.py            # dry-run report only
    python scripts/reconcile_raw_orphans.py --apply     # backfill genuinely new DOIs into manifest.jsonl
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
sys.path.insert(0, str(REPO_ROOT / "src"))

from enrich_manifest_from_pdfs import (  # noqa: E402
    _fetch_crossref_single,
    _get_page_dois_with_frequency,
    _item_to_manifest_record,
)
from scenarios import PrimaryResearchCorpusScenario, ScenarioPaths  # noqa: E402
from utils import log_error  # noqa: E402

DEFAULT_MAILTO = os.getenv("UNPAYWALL_EMAIL", "pshah10@uoguelph.ca")
INTER_REQUEST_DELAY = 1.0  # seconds between CrossRef calls (polite pool)


def find_orphans(paths: ScenarioPaths) -> tuple[list[Path], dict]:
    """
    Return (orphan PDF paths, merged corpus dict).

    An orphan is a file in ``data/raw/`` that doesn't match any manifest
    record's resolved PDF path — i.e. even ``pdf_path_for()``'s DOI-suffix
    glob fallback can't find a manifest row that claims it.
    """
    scenario = PrimaryResearchCorpusScenario(paths)
    corpus = scenario.load_corpus(error_logger=log_error)

    matched_names: set[str] = set()
    for record in corpus["records"]:
        _classification, pdf_path = scenario.classify_record_pdf(record)
        if pdf_path is not None:
            matched_names.add(pdf_path.name)

    raw_files = sorted(paths.raw_dir.glob("*.pdf")) if paths.raw_dir.exists() else []
    orphans = [f for f in raw_files if f.name not in matched_names]
    return orphans, corpus


def _existing_dois(corpus: dict) -> set[str]:
    return {
        str(r.get("doi", "")).strip().lower()
        for r in corpus["records"]
        if r.get("doi")
    }


def run(*, apply: bool, mailto: str) -> int:
    paths = ScenarioPaths()
    orphans, corpus = find_orphans(paths)

    print(
        f"[reconcile] {len(orphans)} orphan PDF(s) in {paths.raw_dir} "
        "(on disk, but no manifest record's resolved path — including the "
        "DOI-suffix fallback — points at them)."
    )
    if not orphans:
        print("[reconcile] Nothing to do.")
        return 0

    existing_dois = _existing_dois(corpus)
    already_known: list[tuple[Path, str]] = []
    genuinely_new: list[tuple[Path, str]] = []
    unresolved: list[Path] = []

    for pdf in orphans:
        by_freq = _get_page_dois_with_frequency(pdf, max_pages=5)
        if not by_freq:
            unresolved.append(pdf)
            continue
        doi = by_freq[0][0].strip().lower()
        if doi in existing_dois:
            already_known.append((pdf, doi))
        else:
            genuinely_new.append((pdf, doi))

    print(
        f"\n[reconcile] Already a manifest.jsonl row — likely a truncated-DOI-suffix "
        f"filename that even the glob fallback can't recover; see "
        f"scripts/fix_truncated_doi_filenames.py: {len(already_known)}"
    )
    for pdf, doi in already_known:
        print(f"    {doi}  <-  {pdf.name[:90]}")

    print(f"\n[reconcile] No DOI found on pages 1-5 — needs manual review: {len(unresolved)}")
    for pdf in unresolved:
        print(f"    {pdf.name}")

    print(f"\n[reconcile] Genuinely new DOI, not in manifest.jsonl or manual_manifest.jsonl: {len(genuinely_new)}")
    for pdf, doi in genuinely_new:
        print(f"    {doi}  <-  {pdf.name[:90]}")

    if not genuinely_new:
        print("\n[reconcile] No manifest.jsonl changes needed.")
        return 0

    if not apply:
        print(
            f"\n[reconcile] DRY RUN — would query CrossRef and append "
            f"{len(genuinely_new)} new row(s) to {paths.manifest_path}. Re-run with --apply."
        )
        return 0

    appended = 0
    for pdf, doi in genuinely_new:
        print(f"[reconcile] Querying CrossRef for {doi} ...")
        item = _fetch_crossref_single(doi, mailto=mailto)
        if item is None:
            continue
        record = _item_to_manifest_record(item, doi)
        if record is None:
            continue
        with paths.manifest_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        appended += 1
        print(f"[reconcile]   Appended: {doi} | {record.get('journal', '?')}")
        time.sleep(INTER_REQUEST_DELAY)

    print(
        f"\n[reconcile] Done. {appended}/{len(genuinely_new)} new row(s) "
        f"appended to {paths.manifest_path}."
    )
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Diagnose data/raw/ PDFs invisible to today's filename-recomputation "
            "lookup, and backfill manifest.jsonl only for genuinely new DOIs."
        )
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write CrossRef backfill rows to manifest.jsonl. Default is a dry-run report only.",
    )
    parser.add_argument(
        "--mailto",
        default=DEFAULT_MAILTO,
        help="Email for CrossRef polite-pool User-Agent (default: $UNPAYWALL_EMAIL).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = _build_parser().parse_args(argv)
    return run(apply=args.apply, mailto=args.mailto)


if __name__ == "__main__":
    sys.exit(main())
