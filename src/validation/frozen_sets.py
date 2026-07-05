"""
src/validation/frozen_sets.py — immutable benchmark-set validation
==================================================================

Frozen sets are the research answer to "which exact papers were evaluated?" A
frozen set is a JSONL file committed to the repo or archived with a run. Before
use, this module verifies its SHA-256 dataset hash and returns rows in stable
order so reruns select the same instances.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from core.hashing import dataset_hash
from core.jsonl import iter_jsonl
from core.schemas import FrozenSetManifest, utc_now_iso


class FrozenSetChecksumError(ValueError):
    """Raised when a frozen set does not match its expected checksum."""


def stable_instance_sort(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return rows sorted by instance_id, then DOI, then title.

    Sorting in one shared helper prevents accidental order changes from
    affecting reports or bootstrap sampling.
    """
    return sorted(
        rows,
        key=lambda row: (
            str(row.get("instance_id") or ""),
            str(row.get("doi") or ""),
            str(row.get("title") or ""),
        ),
    )


def load_frozen_set(path: Path, expected_sha256: str | None = None) -> list[dict[str, Any]]:
    """Load and verify a frozen JSONL benchmark set.

    If ``expected_sha256`` is provided, the function fails before returning data
    when the content hash differs. This fail-fast behavior prevents accidental
    evaluation on a modified benchmark set.
    """
    rows = stable_instance_sort(list(iter_jsonl(path)))
    actual_hash = dataset_hash(rows)
    if expected_sha256 and actual_hash != expected_sha256:
        raise FrozenSetChecksumError(
            "Frozen set checksum mismatch for "
            f"{path}: expected {expected_sha256}, got {actual_hash}"
        )
    return rows


def build_frozen_set_manifest(path: Path, *, name: str, description: str = "") -> FrozenSetManifest:
    """Build a manifest sidecar for a frozen set."""
    rows = stable_instance_sort(list(iter_jsonl(path)))
    return FrozenSetManifest(
        name=name,
        path=str(path),
        sha256=dataset_hash(rows),
        row_count=len(rows),
        created_utc=utc_now_iso(),
        description=description,
    )
