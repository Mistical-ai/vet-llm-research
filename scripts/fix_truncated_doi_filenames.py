#!/usr/bin/env python3
"""
scripts/fix_truncated_doi_filenames.py — rename orphaned raw/processed/
raw_text files back onto today's filename convention.

``scripts/reconcile_raw_orphans.py`` finds ``data/raw/*.pdf`` files whose
DOI-suffix can't be recovered even by ``file_paths.doi_suffix_glob_candidates``
— because, for some papers downloaded under an older, less careful version of
``descriptive_pdf_filename()``, the DOI suffix segment was truncated away
entirely (or never appended) when a long journal + title overflowed the
180-char filename budget. Those files are otherwise perfectly good: the PDF
and its extracted text are on disk, and the paper's real DOI is confirmed
by reading the PDF itself (page-frequency scan — the DOI repeating on every
page 1-5 is the paper's own header/footer DOI, not a citation), and that DOI
already has a matching ``manifest.jsonl`` row.

This script renames the ``.pdf`` (in ``data/raw/``), ``.jsonl`` (in
``data/processed/``), and ``.jsonl`` (in ``data/raw_text/``) for each such
orphan to the filename ``descriptive_pdf_filename()``/``descriptive_stem()``
computes *today* from that DOI's manifest record — bringing the on-disk name
back in sync with the naming convention every other lookup in this project
expects, so no code fallback is needed to find them going forward.

Only orphans whose resolved DOI is already a known manifest row are
renamed. Anything else (no DOI found on the page scan, or a genuinely new
DOI) is left untouched and reported — run
``scripts/reconcile_raw_orphans.py`` for that case instead.

Every rename is logged (old path -> new path) to stdout and to
``data/raw_orphan_renames.jsonl`` (append-only) so the operation is
reversible by hand if needed.

No live LLM API calls, no network at all — pure local file + PDF reads.

Run:
    python scripts/fix_truncated_doi_filenames.py            # dry-run report
    python scripts/fix_truncated_doi_filenames.py --apply     # perform the renames
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from enrich_manifest_from_pdfs import _get_page_dois_with_frequency  # noqa: E402
from file_paths import descriptive_stem  # noqa: E402
from reconcile_raw_orphans import find_orphans  # noqa: E402
from scenarios import ScenarioPaths  # noqa: E402

RENAME_LOG_PATH = REPO_ROOT / "data" / "raw_orphan_renames.jsonl"

# Mirrors data/raw/*.pdf's sibling caches. (extension, directory)
SIBLING_DIRS = (
    (".pdf", "raw"),
    (".jsonl", "processed"),
    (".jsonl", "raw_text"),
)


def _doi_to_record(corpus: dict) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for record in corpus["records"]:
        doi = str(record.get("doi", "")).strip().lower()
        if doi and doi not in out:
            out[doi] = record
    return out


def plan_renames(paths: ScenarioPaths) -> tuple[list[tuple[str, str, str]], list[Path], list[Path]]:
    """
    Return (renames, unresolved, not_in_manifest).

    ``renames`` is a list of ``(doi, old_stem, new_stem)``. ``unresolved`` are
    orphans with no DOI found on pages 1-5. ``not_in_manifest`` are orphans
    with a resolved DOI that isn't a manifest row at all (out of scope here —
    see reconcile_raw_orphans.py --apply).
    """
    orphans, corpus = find_orphans(paths)
    doi_to_record = _doi_to_record(corpus)

    renames: list[tuple[str, str, str]] = []
    unresolved: list[Path] = []
    not_in_manifest: list[Path] = []

    for pdf in orphans:
        by_freq = _get_page_dois_with_frequency(pdf, max_pages=5)
        if not by_freq:
            unresolved.append(pdf)
            continue
        doi = by_freq[0][0].strip().lower()
        record = doi_to_record.get(doi)
        if record is None:
            not_in_manifest.append(pdf)
            continue
        old_stem = pdf.stem
        new_stem = descriptive_stem(record)
        if old_stem == new_stem:
            # Already correct under today's convention; nothing to rename.
            continue
        renames.append((doi, old_stem, new_stem))

    return renames, unresolved, not_in_manifest


def _rename_sibling(directory: Path, old_stem: str, extension: str, new_stem: str) -> str | None:
    old_path = directory / f"{old_stem}{extension}"
    if not old_path.exists():
        return None
    new_path = directory / f"{new_stem}{extension}"
    if new_path.exists():
        print(f"[fix-truncated]   WARNING: target already exists, skipping: {new_path}")
        return None
    old_path.rename(new_path)
    return str(new_path)


def run(*, apply: bool) -> int:
    paths = ScenarioPaths()
    renames, unresolved, not_in_manifest = plan_renames(paths)

    print(f"[fix-truncated] {len(renames)} file-triple(s) to rename onto today's naming convention.")
    for doi, old_stem, new_stem in renames:
        print(f"  {doi}")
        print(f"    old: {old_stem}")
        print(f"    new: {new_stem}")

    if unresolved:
        print(f"\n[fix-truncated] {len(unresolved)} orphan(s) with no DOI found on pages 1-5 (skipped):")
        for pdf in unresolved:
            print(f"    {pdf.name}")

    if not_in_manifest:
        print(
            f"\n[fix-truncated] {len(not_in_manifest)} orphan(s) resolved to a DOI not yet in "
            "manifest.jsonl (skipped — run reconcile_raw_orphans.py --apply for these):"
        )
        for pdf in not_in_manifest:
            print(f"    {pdf.name}")

    if not renames:
        print("\n[fix-truncated] Nothing to rename.")
        return 0

    if not apply:
        print(f"\n[fix-truncated] DRY RUN — re-run with --apply to perform {len(renames)} rename(s).")
        return 0

    log_entries = []
    done = 0
    for doi, old_stem, new_stem in renames:
        entry = {"doi": doi, "old_stem": old_stem, "new_stem": new_stem, "renamed": {}}
        for extension, subdir in SIBLING_DIRS:
            directory = REPO_ROOT / "data" / subdir
            new_path = _rename_sibling(directory, old_stem, extension, new_stem)
            if new_path is not None:
                entry["renamed"][subdir] = new_path
        if entry["renamed"]:
            entry["timestamp"] = datetime.now(timezone.utc).isoformat()
            log_entries.append(entry)
            done += 1
            print(f"[fix-truncated] Renamed {doi}: {list(entry['renamed'].keys())}")

    if log_entries:
        RENAME_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with RENAME_LOG_PATH.open("a", encoding="utf-8") as fh:
            for entry in log_entries:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        print(f"\n[fix-truncated] Logged {len(log_entries)} rename(s) to {RENAME_LOG_PATH}")

    print(f"\n[fix-truncated] Done. {done}/{len(renames)} paper(s) renamed.")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Rename data/raw + data/processed + data/raw_text files for orphaned "
            "papers whose DOI-suffix was truncated away at download time, back "
            "onto today's filename convention."
        )
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Perform the renames. Default is a dry-run report only.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = _build_parser().parse_args(argv)
    return run(apply=args.apply)


if __name__ == "__main__":
    sys.exit(main())
