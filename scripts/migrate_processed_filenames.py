"""
scripts/migrate_processed_filenames.py — one-time rename of data/processed/*.jsonl
====================================================================================

Phase 3 cleanup. Old cache filenames were ``{doi_to_slug(doi)}.jsonl`` (e.g.
``10_1111_jvim_16872.jsonl``). They are now ``{descriptive_stem}.jsonl`` so a
PDF and its cleaned-text cache share a stem. This script walks
``data/processed/``, looks up each file's DOI in the manifest, computes the
new descriptive name, and renames in place.

Behaviour
---------
* Reads each ``.jsonl`` line, pulls ``doi`` from the JSON body (not the
  filename), looks up the manifest record, and computes the target name.
* If a file is already at the descriptive name → skipped (idempotent).
* If the DOI has no manifest record OR the record lacks journal/title →
  left at the legacy name (the readers fall back to legacy automatically).
* mtime is explicitly preserved after rename. On NTFS this is the default
  for ``os.rename``, but the explicit ``os.utime`` keeps the contract
  portable so ``prepare_texts.py``'s cache-freshness check (jsonl.mtime
  vs pdf.mtime) keeps behaving identically before and after migration.

Usage
-----
    python scripts/migrate_processed_filenames.py --dry-run   # preview
    python scripts/migrate_processed_filenames.py             # apply

Re-running is safe — already-migrated files are skipped.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Iterable

# Repo layout: scripts/ is a sibling of src/ and llm-sum/. Inject src/ so
# we can reuse the canonical filename helpers without copy-pasting them.
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from file_paths import descriptive_stem, doi_to_slug  # noqa: E402

DATA_DIR = REPO_ROOT / "data"
PROCESSED_DIR = DATA_DIR / "processed"
MANIFEST_PATH = DATA_DIR / "manifest.jsonl"


def _iter_jsonl(path: Path) -> Iterable[dict]:
    if not path.exists():
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _load_doi_to_record(manifest_path: Path) -> dict[str, dict]:
    """Last-write-wins index of DOI → manifest record."""
    index: dict[str, dict] = {}
    for record in _iter_jsonl(manifest_path):
        doi = str(record.get("doi", "")).strip()
        if doi:
            index[doi.lower()] = record
    return index


def _read_doi_from_jsonl(jsonl_path: Path) -> str | None:
    """Pull the ``doi`` field from the cached JSON line."""
    try:
        line = jsonl_path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not line:
        return None
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    doi = obj.get("doi")
    return str(doi).strip() if doi else None


def _target_name(doi: str, record: dict | None) -> str | None:
    """
    Return the descriptive filename for this DOI's cache, or None if we
    can't build one (missing journal/title in the manifest record).
    """
    if not record or not record.get("journal") or not record.get("title"):
        return None
    return f"{descriptive_stem(record)}.jsonl"


def plan(processed_dir: Path = PROCESSED_DIR,
         manifest_path: Path = MANIFEST_PATH) -> list[tuple[Path, Path | None, str]]:
    """
    Compute the rename plan as a list of ``(old_path, new_path, reason)`` tuples.

    ``new_path`` is None when no rename is possible/needed. ``reason`` is one
    of: ``"rename"``, ``"already_correct"``, ``"no_doi_in_file"``,
    ``"no_manifest_record"``, ``"no_journal_or_title"``, ``"collision"``.
    """
    if not processed_dir.exists():
        return []

    doi_index = _load_doi_to_record(manifest_path)
    plan_rows: list[tuple[Path, Path | None, str]] = []

    for old_path in sorted(processed_dir.glob("*.jsonl")):
        doi = _read_doi_from_jsonl(old_path)
        if not doi:
            plan_rows.append((old_path, None, "no_doi_in_file"))
            continue

        record = doi_index.get(doi.lower())
        target_name = _target_name(doi, record)

        if target_name is None:
            if record is None:
                plan_rows.append((old_path, None, "no_manifest_record"))
            else:
                plan_rows.append((old_path, None, "no_journal_or_title"))
            continue

        new_path = processed_dir / target_name
        if new_path == old_path:
            plan_rows.append((old_path, new_path, "already_correct"))
            continue

        if new_path.exists():
            plan_rows.append((old_path, new_path, "collision"))
            continue

        plan_rows.append((old_path, new_path, "rename"))

    return plan_rows


def apply(plan_rows: list[tuple[Path, Path | None, str]]) -> dict[str, int]:
    """Execute the rename plan. mtime/atime are preserved explicitly."""
    counts: dict[str, int] = {}
    for old_path, new_path, reason in plan_rows:
        counts[reason] = counts.get(reason, 0) + 1
        if reason != "rename" or new_path is None:
            continue
        stat = os.stat(old_path)
        os.rename(old_path, new_path)
        os.utime(new_path, (stat.st_atime, stat.st_mtime))
    return counts


def print_plan(plan_rows: list[tuple[Path, Path | None, str]], *, verbose: bool) -> None:
    counts: dict[str, int] = {}
    for old_path, new_path, reason in plan_rows:
        counts[reason] = counts.get(reason, 0) + 1
        if verbose or reason == "rename":
            new_name = new_path.name if new_path is not None else "—"
            print(f"  [{reason:<22}] {old_path.name}  ->  {new_name}")

    print("\nSummary:")
    for reason, n in sorted(counts.items()):
        print(f"  {reason:<22} {n}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="One-time rename of data/processed/*.jsonl to descriptive filenames."
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the plan without renaming anything.")
    parser.add_argument("--verbose", action="store_true",
                        help="Print every file, including already-correct ones.")
    parser.add_argument("--processed-dir", type=Path, default=PROCESSED_DIR)
    parser.add_argument("--manifest", type=Path, default=MANIFEST_PATH)
    args = parser.parse_args(argv)

    plan_rows = plan(args.processed_dir, args.manifest)
    if not plan_rows:
        print(f"[migrate] No .jsonl files found in {args.processed_dir}.")
        return 0

    print(f"[migrate] {len(plan_rows)} file(s) inspected in {args.processed_dir}\n")
    print_plan(plan_rows, verbose=args.verbose)

    if args.dry_run:
        print("\n[migrate] --dry-run set; no files were renamed.")
        return 0

    rename_count = sum(1 for _, _, reason in plan_rows if reason == "rename")
    if rename_count == 0:
        print("\n[migrate] Nothing to rename.")
        return 0

    print(f"\n[migrate] Applying {rename_count} rename(s)...")
    counts = apply(plan_rows)
    print(f"[migrate] Done. Renamed {counts.get('rename', 0)} file(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
