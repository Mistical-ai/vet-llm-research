"""
src/core/run_manifest.py — run provenance capture
=================================================

Every manuscript-grade run should produce a ``run_manifest.json``. The manifest
answers: which code, dependencies, prompts, dataset, seed, and models produced
these outputs?
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

from core.hashing import sha256_file
from core.paths import REPO_ROOT, RUNS_DIR, resolve_repo_path
from core.schemas import RunManifest, utc_now_iso


def create_run_id(prefix: str = "run") -> str:
    """Return a filesystem-safe UTC run id."""
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{prefix}_{stamp}"


def _git_value(args: list[str]) -> str | None:
    """Return a git command result, or None when git metadata is unavailable."""
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
    except Exception:
        return None
    value = result.stdout.strip()
    return value or None


def collect_git_metadata() -> tuple[str | None, str | None]:
    """Return (branch, sha) for the current checkout."""
    branch = _git_value(["branch", "--show-current"]) or os.getenv("GITHUB_REF_NAME")
    sha = (
        _git_value(["rev-parse", "HEAD"])
        or os.getenv("GITHUB_SHA")
        or os.getenv("GIT_COMMIT")
        or os.getenv("SOURCE_VERSION")
    )
    return branch or "unknown", sha or "unknown"


def repo_relative_path(path: str | Path) -> str:
    """Return a stable repo-relative path when possible."""
    resolved = resolve_repo_path(path)
    try:
        return str(resolved.relative_to(REPO_ROOT))
    except ValueError:
        return str(resolved)


def _normalize_artifact_paths(paths: dict[str, str] | None) -> dict[str, str]:
    """Store artifact locations in a cross-machine form where possible."""
    return {name: repo_relative_path(path) for name, path in (paths or {}).items()}


def hash_existing_files(paths: Iterable[str | Path]) -> dict[str, str]:
    """Hash files that exist, keyed by repo-relative path when possible."""
    hashes: dict[str, str] = {}
    for raw_path in paths:
        path = resolve_repo_path(raw_path)
        if not path.exists() or not path.is_file():
            continue
        try:
            key = str(path.relative_to(REPO_ROOT))
        except ValueError:
            key = str(path)
        hashes[key] = sha256_file(path)
    return hashes


def build_run_manifest(
    *,
    run_id: str,
    benchmark_name: str,
    mode: str,
    random_seed: int,
    cli_args: list[str],
    selected_instance_ids: list[str] | None = None,
    prompt_paths: Iterable[str | Path] = (),
    config_paths: Iterable[str | Path] = (),
    dataset_hash: str | None = None,
    model_ids: dict[str, str] | None = None,
    artifact_paths: dict[str, str] | None = None,
) -> RunManifest:
    """Build a run manifest with environment and artifact provenance."""
    branch, sha = collect_git_metadata()
    lock_path = REPO_ROOT / "requirements-lock.txt"
    return RunManifest(
        run_id=run_id,
        benchmark_name=benchmark_name,
        started_utc=utc_now_iso(),
        git_sha=sha,
        git_branch=branch,
        python_version=sys.version.replace("\n", " "),
        platform=platform.platform(),
        dependency_lock_hash=sha256_file(lock_path) if lock_path.exists() else None,
        prompt_hashes=hash_existing_files(prompt_paths),
        config_hashes=hash_existing_files(config_paths),
        dataset_hash=dataset_hash,
        random_seed=random_seed,
        mode=mode,
        cli_args=cli_args,
        selected_instance_ids=selected_instance_ids or [],
        model_ids=model_ids or {},
        artifact_paths=_normalize_artifact_paths(artifact_paths),
    )


def write_run_manifest(manifest: RunManifest, run_dir: Path) -> Path:
    """Atomically write ``run_manifest.json`` into a run directory."""
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / "run_manifest.json"
    tmp_path = path.with_suffix(".json.tmp")
    tmp_path.write_text(manifest.model_dump_json(indent=2), encoding="utf-8")
    tmp_path.replace(path)
    return path


def finalize_run_manifest(path: Path, **updates: object) -> RunManifest:
    """Load, update, and rewrite an existing run manifest.

    This is used after a run completes to add finish time, observed model
    versions, or artifact paths without losing the start-time provenance.
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    if "artifact_paths" in updates and isinstance(updates["artifact_paths"], dict):
        updates["artifact_paths"] = _normalize_artifact_paths(updates["artifact_paths"])
    data.update(updates)
    data["finished_utc"] = utc_now_iso()
    manifest = RunManifest.model_validate(data)
    write_run_manifest(manifest, path.parent)
    return manifest


def resolve_run_dir(run_id: str | None = None, run_dir: str | Path | None = None) -> Path:
    """Return the immutable output directory for a run."""
    if run_dir is not None:
        return resolve_repo_path(run_dir)
    return RUNS_DIR / (run_id or create_run_id("phase4_medhelm"))
