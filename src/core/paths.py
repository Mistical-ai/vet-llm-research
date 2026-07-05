"""
src/core/paths.py — central repository paths
============================================

WHY THIS MODULE EXISTS
----------------------
Many legacy scripts define their own ``DATA_DIR`` and manifest paths. Central
path helpers reduce drift while still letting old scripts keep their CLI
entrypoints. New reproducibility code should import paths from here.
"""

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = REPO_ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
RAW_TEXT_DIR = DATA_DIR / "raw_text"
PROCESSED_DIR = DATA_DIR / "processed"
MANIFEST_PATH = DATA_DIR / "manifest.jsonl"
MANUAL_MANIFEST_PATH = DATA_DIR / "manual_manifest.jsonl"
SUMMARIES_PATH = DATA_DIR / "summaries.jsonl"
EVALUATIONS_PATH = DATA_DIR / "evaluations.jsonl"

CONFIGS_DIR = REPO_ROOT / "configs"
FROZEN_SETS_DIR = REPO_ROOT / "frozen_sets"
RUNS_DIR = REPO_ROOT / "runs"
PROMPTS_DIR = REPO_ROOT / "llm-sum" / "prompts"


def resolve_repo_path(path: str | Path) -> Path:
    """Return an absolute path, resolving relative paths from the repo root."""
    candidate = Path(path)
    return candidate if candidate.is_absolute() else REPO_ROOT / candidate


def ensure_directory(path: str | Path) -> Path:
    """Create and return a directory path.

    The helper keeps directory creation explicit in caller code and avoids
    scattering ``mkdir(parents=True, exist_ok=True)`` across orchestration
    modules.
    """
    resolved = resolve_repo_path(path)
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved
