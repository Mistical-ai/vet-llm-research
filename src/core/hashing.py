"""
src/core/hashing.py — reproducibility hashes
============================================

Hashes are the cheapest way to prove that a prompt, frozen set, dependency
lock, or report input has not changed between runs. All helpers use SHA-256 and
stable JSON serialization so the same data produces the same digest everywhere.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any


def sha256_bytes(data: bytes) -> str:
    """Return a SHA-256 hex digest for raw bytes."""
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    """Return a SHA-256 hex digest for UTF-8 text."""
    return sha256_bytes(text.encode("utf-8"))


def sha256_file(path: Path) -> str:
    """Hash a file in chunks so large artifacts do not need to fit in memory."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json(data: Any) -> str:
    """Serialize data deterministically for hashing and manifests."""
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_json(data: Any) -> str:
    """Return the SHA-256 digest of deterministic JSON data."""
    return sha256_text(canonical_json(data))


def dataset_hash(rows: Iterable[dict[str, Any]]) -> str:
    """Hash a dataset independent of original JSON key order.

    Rows are sorted by ``instance_id``, then DOI, then title. The explicit
    fallbacks match frozen-set loading so checksums do not depend on the module
    that prepared the rows.
    """
    materialized = list(rows)
    materialized.sort(
        key=lambda row: (
            str(row.get("instance_id") or ""),
            str(row.get("doi") or ""),
            str(row.get("title") or ""),
        )
    )
    return sha256_json(materialized)
