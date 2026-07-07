"""
llm-sum/run_manifest.py — reproducible evaluation run provenance
=================================================================

Every evaluation run should answer: which code, prompt, dataset, and models
produced these scores? A ``RunManifest`` is written to
``data/processed/run_manifest_<run_id>.json`` before any judge is called, and
patched with terminal status after the run finishes (success, partial
failure, or a raised exception).

This module intentionally does not import ``evaluator.py``. Keeping it
dependency-free from the rest of Phase 3 means it can be unit-tested in
isolation and avoids any circular-import risk.

DESIGN NOTE — reused from prior art
-----------------------------------
An earlier, more elaborate version of this idea (``RunManifest`` plus a whole
``src/core/`` package with frozen-dataset-set validation and a dependency-lock
system) exists on the unmerged ``phase/4-medhelm`` branch. That scope is
broader than needed here. This module reuses the proven pieces — the schema
shape, git-sha collection, SHA-256 file hashing, and the atomic-write pattern
— reimplemented as one flat module matching this branch's actual convention
(sibling to ``evaluator.py``, ``eval_report.py``, etc.), rather than pulling in
the other branch's package layout or its frozen-set machinery.
"""

from __future__ import annotations

from _bootstrap import *  # noqa: F401,F403

import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------

def sha256_bytes(data: bytes) -> str:
    """Return a SHA-256 hex digest for raw bytes."""
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    """Hash a file in chunks so large artifacts do not need to fit in memory."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_file_or_empty(path: Path) -> str:
    """Hash a file's contents, or an empty byte string if it does not exist yet.

    Lets a manifest still be written when evaluate runs before any dataset
    exists (e.g. a fresh checkout before the summarize step has ever run) —
    matching eval_instances.py's own tolerance for a missing summaries.jsonl,
    rather than crashing on a case the rest of the pipeline already handles.
    """
    if not path.exists():
        return sha256_bytes(b"")
    return sha256_file(path)


# ---------------------------------------------------------------------------
# Git metadata
# ---------------------------------------------------------------------------

def _git_value(args: list[str]) -> str | None:
    """Return a git command's stdout, or None when git metadata is unavailable.

    Swallows every failure (no git binary, not a git checkout, detached HEAD
    with no branch name) so a missing git environment can never crash an
    evaluation run just to record provenance about it.
    """
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
    return _git_value(["branch", "--show-current"]), _git_value(["rev-parse", "HEAD"])


def _derive_code_version(branch: str | None, sha: str | None) -> str:
    """Best-effort code identity when no formal package version exists.

    This repo has no pyproject.toml/__version__ — Phase 3 is a research
    pipeline, not a packaged library — so git identity is the only honest
    stand-in for "code version."
    """
    if sha is None:
        return "unknown"
    return f"{branch or 'unknown'}@{sha[:12]}"


# ---------------------------------------------------------------------------
# run_id
# ---------------------------------------------------------------------------

def create_run_id(prefix: str = "run") -> str:
    """Return a filesystem-safe UTC-timestamp run id."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{prefix}_{stamp}"


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

class RunManifest(BaseModel):
    """Provenance record for one evaluation run.

    ``extra="forbid"`` makes a typo'd field an immediate construction error
    instead of a silently-dropped one — the whole point of a manifest is to
    be trustworthy, so it should fail loudly when malformed.
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str
    timestamp_utc: str
    finished_utc: str | None = None
    status: Literal["started", "completed", "failed"] = "started"

    git_commit_sha: str | None
    branch: str | None
    code_version: str

    dataset_path: str
    dataset_hash_sha256: str
    selected_instance_ids: list[str] = Field(default_factory=list)

    judges: list[str]
    model_ids: dict[str, str] = Field(default_factory=dict)
    resolved_model_versions: dict[str, str] = Field(default_factory=dict)

    prompt_template_id: str
    prompt_path: str
    prompt_sha256: str

    temperature: float
    max_output_tokens: int
    seed: int
    top_p: float | None = None

    evaluation_config: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Build / write / finalize
# ---------------------------------------------------------------------------

def build_run_manifest(
    *,
    run_id: str,
    dataset_path: str,
    dataset_hash_sha256: str,
    judges: list[str],
    model_ids: dict[str, str],
    prompt_template_id: str,
    prompt_path: str,
    prompt_sha256: str,
    temperature: float,
    max_output_tokens: int,
    seed: int,
    evaluation_config: dict[str, Any],
    selected_instance_ids: list[str],
) -> RunManifest:
    """Build a run manifest with environment and dataset provenance.

    Every provenance-critical argument is required and keyword-only — a
    caller that forgets one gets an immediate ``TypeError``, and Pydantic
    re-validates types on top of that. This is the "fail fast" guarantee:
    there is no code path that produces a manifest silently missing a field.
    """
    branch, sha = collect_git_metadata()
    return RunManifest(
        run_id=run_id,
        timestamp_utc=datetime.now(timezone.utc).isoformat(),
        git_commit_sha=sha,
        branch=branch,
        code_version=_derive_code_version(branch, sha),
        dataset_path=dataset_path,
        dataset_hash_sha256=dataset_hash_sha256,
        selected_instance_ids=selected_instance_ids,
        judges=judges,
        model_ids=model_ids,
        prompt_template_id=prompt_template_id,
        prompt_path=prompt_path,
        prompt_sha256=prompt_sha256,
        temperature=temperature,
        max_output_tokens=max_output_tokens,
        seed=seed,
        evaluation_config=evaluation_config,
    )


def write_run_manifest(manifest: RunManifest, path: Path) -> Path:
    """Atomically write the manifest to ``path`` (write temp file, then rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(manifest.model_dump_json(indent=2), encoding="utf-8")
    tmp_path.replace(path)
    return path


def finalize_run_manifest(path: Path, **updates: object) -> RunManifest:
    """Load, patch, and rewrite a manifest with terminal run info.

    Used once a run completes (successfully, partially, or via exception) to
    add ``resolved_model_versions`` and a terminal ``status`` without losing
    the start-time provenance already on disk.
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    data.update(updates)
    data["finished_utc"] = datetime.now(timezone.utc).isoformat()
    manifest = RunManifest.model_validate(data)
    write_run_manifest(manifest, path)
    return manifest


# ---------------------------------------------------------------------------
# Resolved model version lookup
# ---------------------------------------------------------------------------

def resolve_model_versions(
    judges: list[str],
    model_ids: dict[str, str],
    rows: list[dict[str, Any]],
) -> dict[str, str]:
    """Return the observed judge_model_version per judge from evaluation rows.

    Falls back to the configured model_ids[judge] when no matching row is
    found (every call for that judge failed, or resume skipped everything),
    so the field is populated whenever possible rather than left absent.
    """
    resolved: dict[str, str] = {}
    for judge in judges:
        observed = None
        for row in rows:
            if row.get("judge") == judge and row.get("judge_model_version"):
                observed = str(row["judge_model_version"])
        resolved[judge] = observed or model_ids.get(judge, "unknown")
    return resolved
