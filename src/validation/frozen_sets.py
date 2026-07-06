"""
src/validation/frozen_sets.py — immutable benchmark-set validation
==================================================================

Frozen sets are the research answer to "which exact papers were evaluated?" A
frozen set is a JSONL file committed to the repo or archived with a run. Before
use, this module verifies its SHA-256 dataset hash and returns rows in stable
order so reruns select the same instances.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.hashing import dataset_hash
from core.jsonl import iter_jsonl
from core.schemas import FrozenSetManifest, utc_now_iso


class FrozenSetChecksumError(ValueError):
    """Raised when a frozen set does not match its expected checksum."""


def frozen_set_manifest_path(path: Path) -> Path:
    """Return the conventional checksum sidecar path for a frozen JSONL set."""
    return path.with_suffix(".manifest.json")


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


def _read_sidecar_sha256(path: Path, *, require_manifest: bool) -> str | None:
    """Read the frozen-set checksum sidecar when it exists."""
    sidecar_path = frozen_set_manifest_path(path)
    if not sidecar_path.exists():
        if require_manifest:
            raise FrozenSetChecksumError(
                f"Frozen set manifest sidecar not found for {path}: expected {sidecar_path}"
            )
        return None
    try:
        manifest = json.loads(sidecar_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise FrozenSetChecksumError(
            f"Frozen set manifest sidecar is not valid JSON: {sidecar_path}"
        ) from exc
    expected = manifest.get("sha256")
    if not isinstance(expected, str) or not expected:
        raise FrozenSetChecksumError(
            f"Frozen set manifest sidecar is missing sha256: {sidecar_path}"
        )
    return expected


def load_frozen_set(
    path: Path,
    expected_sha256: str | None = None,
    *,
    require_manifest: bool = False,
) -> list[dict[str, Any]]:
    """Load and verify a frozen JSONL benchmark set.

    If ``expected_sha256`` is omitted and a conventional sidecar exists, the
    sidecar checksum is used. ``require_manifest`` is used by production
    reproducibility runs to fail before evaluation when the sidecar is missing.
    """
    expected = expected_sha256 or _read_sidecar_sha256(path, require_manifest=require_manifest)
    rows = stable_instance_sort(list(iter_jsonl(path)))
    actual_hash = dataset_hash(rows)
    if expected and actual_hash != expected:
        raise FrozenSetChecksumError(
            "Frozen set checksum mismatch for " f"{path}: expected {expected}, got {actual_hash}"
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
