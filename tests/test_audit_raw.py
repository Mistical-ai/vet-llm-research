"""
Tests for src/audit_raw.py's manifest deduplication.

Critical assertion:
    A line that does not parse is never counted as a duplicate, and its presence
    blocks the rewrite. Getting this wrong deletes manifest rows permanently
    while reporting the deletion as routine deduplication.

No network: _load_and_dedup_manifest is pure file I/O. The CrossRef enrichment
paths in this module are not exercised here.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import audit_raw  # noqa: E402


def _write_manifest(path: Path, lines: list[str]) -> None:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _record(doi: str, **extra) -> str:
    return json.dumps({"doi": doi, "journal": "JVIM", **extra})


def test_clean_manifest_reports_no_duplicates(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.jsonl"
    _write_manifest(manifest, [_record("10.1/a"), _record("10.1/b")])

    _index, unique, dupes, bad = audit_raw._load_and_dedup_manifest(manifest)

    assert len(unique) == 2
    assert dupes == 0
    assert bad == 0


def test_repeated_doi_counts_as_a_duplicate(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.jsonl"
    _write_manifest(manifest, [
        _record("10.1/a", title="first"),
        _record("10.1/a", title="second"),
        _record("10.1/b"),
    ])

    _index, unique, dupes, bad = audit_raw._load_and_dedup_manifest(manifest)

    assert dupes == 1
    assert bad == 0
    assert len(unique) == 2
    # Last occurrence wins, as documented.
    assert unique[0]["title"] == "second"


# The bug this pins: total_lines used to increment BEFORE json.loads, so an
# unparseable line inflated the duplicate count. A non-zero count then triggered
# an atomic rewrite built only from the records that parsed — silently and
# permanently deleting the corrupt line, reported as "duplicates removed".
def test_unparseable_line_is_not_counted_as_a_duplicate(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.jsonl"
    _write_manifest(manifest, [
        _record("10.1/a"),
        "{this is not valid json",
        _record("10.1/b"),
    ])

    _index, unique, dupes, bad = audit_raw._load_and_dedup_manifest(manifest)

    assert bad == 1
    assert dupes == 0, (
        "an unparseable line was miscounted as a duplicate, which would trigger "
        "a rewrite that deletes it permanently"
    )
    assert len(unique) == 2


def test_null_doi_does_not_crash_the_audit(tmp_path: Path) -> None:
    """A record with an explicit "doi": null used to raise AttributeError."""
    manifest = tmp_path / "manifest.jsonl"
    _write_manifest(manifest, [json.dumps({"doi": None}), _record("10.1/a")])

    _index, unique, dupes, bad = audit_raw._load_and_dedup_manifest(manifest)

    assert bad == 0
    assert len(unique) == 2


def test_audit_refuses_to_rewrite_a_manifest_with_unparseable_lines(
        tmp_path: Path, monkeypatch) -> None:
    """The corrupt line must survive an audit that also finds real duplicates."""
    manifest = tmp_path / "manifest.jsonl"
    corrupt = "{truncated record"
    _write_manifest(manifest, [
        _record("10.1/a", title="first"),
        _record("10.1/a", title="second"),   # a genuine duplicate
        corrupt,
    ])
    before = manifest.read_text(encoding="utf-8")

    wrote: list[Path] = []
    monkeypatch.setattr(
        audit_raw, "_write_manifest_safe",
        lambda path, records: wrote.append(path),
    )

    _index, unique, dupes, bad = audit_raw._load_and_dedup_manifest(manifest)
    assert dupes == 1 and bad == 1

    # Rewriting here would drop `corrupt`, so the guard must refuse.
    assert manifest.read_text(encoding="utf-8") == before
    assert corrupt in manifest.read_text(encoding="utf-8")
