"""
Tests for scripts/migrate_processed_filenames.py.

Critical guarantees:
    Files with a manifest entry (journal + title) get renamed to the
        descriptive name.
    Files without a manifest entry (or with no journal/title) are LEFT
        ALONE at their legacy slug name — the readers fall back to that.
    The migration is idempotent (running twice changes nothing).
    Renaming preserves mtime explicitly (the readers' cache-freshness
        check depends on jsonl.mtime vs pdf.mtime not jumping).
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import time
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))


@pytest.fixture
def migrate_module():
    """Import the script as a module (scripts/ is not a package)."""
    return importlib.import_module("migrate_processed_filenames")


def _write_processed(processed: Path, slug: str, doi: str) -> Path:
    path = processed / f"{slug}.jsonl"
    path.write_text(
        json.dumps({"doi": doi, "slug": slug, "text": "body"}) + "\n",
        encoding="utf-8",
    )
    return path


def _write_manifest(manifest: Path, records: list[dict]) -> None:
    with open(manifest, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def test_file_with_record_gets_renamed(tmp_path: Path, migrate_module) -> None:
    processed = tmp_path / "processed"
    processed.mkdir()
    manifest = tmp_path / "manifest.jsonl"

    doi = "10.1111/jvim.16872"
    old = _write_processed(processed, "10_1111_jvim_16872", doi)
    _write_manifest(manifest, [{
        "doi": doi, "journal": "JVIM", "title": "A clinical study",
    }])

    rows = migrate_module.plan(processed, manifest)
    assert len(rows) == 1
    old_path, new_path, reason = rows[0]
    assert reason == "rename"
    assert new_path is not None
    assert new_path.name != old.name
    assert new_path.name.endswith(".jsonl")
    assert "jvim" in new_path.name.lower()
    assert "16872" in new_path.name

    counts = migrate_module.apply(rows)
    assert counts.get("rename") == 1
    assert not old.exists()
    assert new_path.exists()


def test_file_with_no_manifest_record_is_left_alone(tmp_path: Path, migrate_module) -> None:
    processed = tmp_path / "processed"
    processed.mkdir()
    manifest = tmp_path / "manifest.jsonl"
    manifest.write_text("", encoding="utf-8")

    doi = "10.1111/nowhere.0001"
    path = _write_processed(processed, "10_1111_nowhere_0001", doi)

    rows = migrate_module.plan(processed, manifest)
    assert len(rows) == 1
    _, new_path, reason = rows[0]
    assert reason == "no_manifest_record"

    migrate_module.apply(rows)
    assert path.exists()  # unchanged


def test_file_with_record_missing_title_is_left_alone(tmp_path: Path, migrate_module) -> None:
    processed = tmp_path / "processed"
    processed.mkdir()
    manifest = tmp_path / "manifest.jsonl"

    doi = "10.1111/missingtitle.0001"
    path = _write_processed(processed, "10_1111_missingtitle_0001", doi)
    # Record has journal but no title.
    _write_manifest(manifest, [{"doi": doi, "journal": "JVIM"}])

    rows = migrate_module.plan(processed, manifest)
    assert len(rows) == 1
    _, _, reason = rows[0]
    assert reason == "no_journal_or_title"

    migrate_module.apply(rows)
    assert path.exists()  # unchanged


def test_migration_is_idempotent(tmp_path: Path, migrate_module) -> None:
    processed = tmp_path / "processed"
    processed.mkdir()
    manifest = tmp_path / "manifest.jsonl"

    doi = "10.1111/jvim.99001"
    _write_processed(processed, "10_1111_jvim_99001", doi)
    _write_manifest(manifest, [{
        "doi": doi, "journal": "JVIM", "title": "Some study",
    }])

    # First pass renames.
    rows = migrate_module.plan(processed, manifest)
    migrate_module.apply(rows)

    # Second pass: nothing to do.
    rows2 = migrate_module.plan(processed, manifest)
    assert len(rows2) == 1
    _, _, reason = rows2[0]
    assert reason == "already_correct"

    counts = migrate_module.apply(rows2)
    assert counts.get("rename", 0) == 0


def test_rename_preserves_mtime(tmp_path: Path, migrate_module) -> None:
    """The cache-freshness check in prepare_texts.py compares jsonl.mtime to
    pdf.mtime. A migration that bumped mtime would silently invalidate
    every cache file."""
    processed = tmp_path / "processed"
    processed.mkdir()
    manifest = tmp_path / "manifest.jsonl"

    doi = "10.1111/jvim.50001"
    old = _write_processed(processed, "10_1111_jvim_50001", doi)
    _write_manifest(manifest, [{
        "doi": doi, "journal": "JVIM", "title": "Renamed study",
    }])

    # Force a known old mtime well in the past.
    past = time.time() - 12345.0
    os.utime(old, (past, past))
    original_mtime = old.stat().st_mtime

    rows = migrate_module.plan(processed, manifest)
    migrate_module.apply(rows)

    # Find the renamed file.
    survivors = list(processed.glob("*.jsonl"))
    assert len(survivors) == 1
    new_path = survivors[0]
    assert new_path.name != old.name  # actually got renamed
    # mtime must match the original within filesystem resolution.
    assert abs(new_path.stat().st_mtime - original_mtime) < 1.0


def test_collision_skips_rename(tmp_path: Path, migrate_module) -> None:
    """If the descriptive target name already exists, don't overwrite it."""
    processed = tmp_path / "processed"
    processed.mkdir()
    manifest = tmp_path / "manifest.jsonl"

    doi = "10.1111/jvim.77777"
    legacy = _write_processed(processed, "10_1111_jvim_77777", doi)
    _write_manifest(manifest, [{
        "doi": doi, "journal": "JVIM", "title": "Conflicting study",
    }])

    # Pre-create the target so apply() sees a collision.
    from file_paths import descriptive_stem
    target = processed / f"{descriptive_stem({'doi': doi, 'journal': 'JVIM', 'title': 'Conflicting study'})}.jsonl"
    target.write_text("conflicting body\n", encoding="utf-8")

    rows = migrate_module.plan(processed, manifest)
    # plan returns both files; the legacy one is flagged "collision".
    collision_rows = [r for r in rows if r[2] == "collision"]
    assert len(collision_rows) == 1

    migrate_module.apply(rows)
    # Both original files should still exist (no destructive overwrite).
    assert legacy.exists()
    assert target.exists()
    assert target.read_text(encoding="utf-8") == "conflicting body\n"
