"""
src/scenarios/base.py - Minimal scenario contract

WHY THIS MODULE EXISTS
----------------------
MedHELM-style scenarios give each benchmark or pipeline view a stable name,
input locations, and reproducible selection rules. This project keeps the
interface small so pipeline.py and Phase 3 stay script-friendly and testable
without introducing HELM as a dependency.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from utils import env_path, processed_dir_path


@dataclass(frozen=True)
class ScenarioPaths:
    """Filesystem inputs used by lightweight scenarios.

    ``processed_dir`` and ``summaries_path`` follow ``.env`` so researchers can
    switch text caches or summary locations without editing scenario code.
    Manifest and raw PDF paths stay at the conventional ``data/`` locations.
    """

    manifest_path: Path = Path("data") / "manifest.jsonl"
    manual_manifest_path: Path = Path("data") / "manual_manifest.jsonl"
    raw_dir: Path = Path("data") / "raw"
    # Follow PROCESSED_DIR_NAME so pipeline scenarios match Phase 3 text cache.
    processed_dir: Path = field(default_factory=processed_dir_path)
    summaries_path: Path = field(
        default_factory=lambda: env_path("SUMMARIES_JSONL_PATH", "data/summaries.jsonl")
    )


class Scenario(ABC):
    """Base contract for a reproducible veterinary pipeline scenario."""

    name: str
    description: str

    def __init__(self, paths: ScenarioPaths | None = None) -> None:
        self.paths = paths or ScenarioPaths()

    @abstractmethod
    def records(self) -> Iterable[Any]:
        """Yield the scenario's input records or benchmark instances."""
        raise NotImplementedError

    def metadata(self) -> dict[str, Any]:
        """Return stable metadata for logs, reports, and tests."""
        return {
            "name": self.name,
            "description": self.description,
        }
