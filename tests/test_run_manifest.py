"""
Tests for llm-sum/run_manifest.py.

All offline and deterministic: no live API calls, only local `git` subprocess
calls against this repo and filesystem operations under tmp_path.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from run_manifest import (
    build_run_manifest,
    collect_git_metadata,
    create_run_id,
    finalize_run_manifest,
    resolve_model_versions,
    sha256_bytes,
    sha256_file,
    sha256_file_or_empty,
    write_run_manifest,
)


def _base_kwargs(**overrides) -> dict:
    kwargs = dict(
        run_id="run-test",
        dataset_path="data/summaries.jsonl",
        dataset_hash_sha256="abc123",
        judges=["openai"],
        model_ids={"openai": "gpt-5.4"},
        prompt_template_id="judge_medhelm_v1.txt",
        prompt_path="llm-sum/prompts/judge_medhelm_v1.txt",
        prompt_sha256="def456",
        temperature=0.0,
        max_output_tokens=1500,
        seed=42,
        evaluation_config={"rubric_version": "vet_medhelm_score_v1.0"},
        selected_instance_ids=["10.1111/example"],
    )
    kwargs.update(overrides)
    return kwargs


# ---------------------------------------------------------------------------
# Judge-count scaling (default 1, switchable to N)
# ---------------------------------------------------------------------------

def test_build_run_manifest_single_judge_default() -> None:
    manifest = build_run_manifest(**_base_kwargs(
        judges=["openai"], model_ids={"openai": "gpt-5.4"},
    ))
    assert manifest.judges == ["openai"]
    assert manifest.model_ids == {"openai": "gpt-5.4"}
    assert len(manifest.model_ids) == 1


def test_build_run_manifest_scales_to_multiple_judges() -> None:
    manifest = build_run_manifest(**_base_kwargs(
        judges=["openai", "anthropic", "gemini"],
        model_ids={"openai": "gpt-5.4", "anthropic": "claude-x", "gemini": "gemini-y"},
    ))
    assert set(manifest.model_ids) == {"openai", "anthropic", "gemini"}
    assert len(manifest.model_ids) == 3


# ---------------------------------------------------------------------------
# Fail-fast validation
# ---------------------------------------------------------------------------

def test_run_manifest_missing_required_field_raises() -> None:
    kwargs = _base_kwargs()
    del kwargs["dataset_hash_sha256"]
    with pytest.raises(TypeError):
        build_run_manifest(**kwargs)


def test_run_manifest_pydantic_validation_missing_field() -> None:
    """Constructing RunManifest directly (bypassing the builder) still fails
    fast via Pydantic when a required field is absent."""
    from run_manifest import RunManifest

    kwargs = build_run_manifest(**_base_kwargs()).model_dump()
    del kwargs["dataset_hash_sha256"]
    with pytest.raises(ValidationError):
        RunManifest(**kwargs)


def test_run_manifest_schema_rejects_unknown_field() -> None:
    from run_manifest import RunManifest

    valid = build_run_manifest(**_base_kwargs()).model_dump()
    valid["totally_made_up_field"] = "oops"
    with pytest.raises(ValidationError):
        RunManifest.model_validate(valid)


# ---------------------------------------------------------------------------
# Atomic write + expected keys
# ---------------------------------------------------------------------------

def test_write_run_manifest_creates_file_with_expected_keys(tmp_path: Path) -> None:
    manifest = build_run_manifest(**_base_kwargs())
    path = tmp_path / "run_manifest_run-test.json"
    written = write_run_manifest(manifest, path)

    assert written == path
    assert path.exists()
    assert not path.with_suffix(path.suffix + ".tmp").exists()

    data = json.loads(path.read_text(encoding="utf-8"))
    for key in (
        "run_id", "timestamp_utc", "status", "git_commit_sha", "branch",
        "code_version", "dataset_path", "dataset_hash_sha256",
        "selected_instance_ids", "judges", "model_ids",
        "resolved_model_versions", "prompt_template_id", "prompt_path",
        "prompt_sha256", "temperature", "max_output_tokens", "seed",
        "evaluation_config",
    ):
        assert key in data, f"missing expected key: {key}"


# ---------------------------------------------------------------------------
# Dataset hashing
# ---------------------------------------------------------------------------

def test_dataset_hash_changes_when_dataset_content_changes(tmp_path: Path) -> None:
    file_a = tmp_path / "summaries_a.jsonl"
    file_b = tmp_path / "summaries_b.jsonl"
    file_a.write_text('{"doi": "10.1/a"}\n', encoding="utf-8")
    file_b.write_text('{"doi": "10.1/b"}\n', encoding="utf-8")

    assert sha256_file(file_a) != sha256_file(file_b)


def test_dataset_hash_is_deterministic_for_same_content(tmp_path: Path) -> None:
    file_a = tmp_path / "summaries.jsonl"
    file_a.write_text('{"doi": "10.1/a"}\n', encoding="utf-8")

    assert sha256_file(file_a) == sha256_file(file_a)
    assert sha256_file(file_a) == sha256_bytes(file_a.read_bytes())


def test_sha256_file_or_empty_handles_missing_file(tmp_path: Path) -> None:
    missing = tmp_path / "does_not_exist.jsonl"
    assert sha256_file_or_empty(missing) == sha256_bytes(b"")


# ---------------------------------------------------------------------------
# Finalize: terminal status + preserved fields
# ---------------------------------------------------------------------------

def test_finalize_run_manifest_sets_completed_status(tmp_path: Path) -> None:
    manifest = build_run_manifest(**_base_kwargs())
    path = write_run_manifest(manifest, tmp_path / "run_manifest.json")

    finalized = finalize_run_manifest(
        path, resolved_model_versions={"openai": "gpt-5.4-0325-preview"}, status="completed",
    )

    assert finalized.status == "completed"
    assert finalized.finished_utc is not None
    assert finalized.resolved_model_versions == {"openai": "gpt-5.4-0325-preview"}
    # Start-time provenance is preserved, not clobbered.
    assert finalized.run_id == manifest.run_id
    assert finalized.dataset_hash_sha256 == manifest.dataset_hash_sha256


def test_finalize_run_manifest_sets_failed_status(tmp_path: Path) -> None:
    manifest = build_run_manifest(**_base_kwargs())
    path = write_run_manifest(manifest, tmp_path / "run_manifest.json")

    finalized = finalize_run_manifest(path, resolved_model_versions={}, status="failed")

    assert finalized.status == "failed"
    assert finalized.finished_utc is not None


def test_finalize_run_manifest_rejects_invalid_status(tmp_path: Path) -> None:
    manifest = build_run_manifest(**_base_kwargs())
    path = write_run_manifest(manifest, tmp_path / "run_manifest.json")

    with pytest.raises(ValidationError):
        finalize_run_manifest(path, status="not-a-real-status")


# ---------------------------------------------------------------------------
# Resolved model version fallback
# ---------------------------------------------------------------------------

def test_resolved_model_version_falls_back_to_model_id() -> None:
    rows = [{"judge": "anthropic", "judge_model_version": "claude-observed"}]
    resolved = resolve_model_versions(
        judges=["openai", "anthropic"],
        model_ids={"openai": "gpt-5.4", "anthropic": "claude-x"},
        rows=rows,
    )
    # openai never appears in rows -> falls back to the configured model id.
    assert resolved["openai"] == "gpt-5.4"
    # anthropic has an observed row -> uses the observed version, not the config.
    assert resolved["anthropic"] == "claude-observed"


def test_resolved_model_version_uses_last_matching_row() -> None:
    rows = [
        {"judge": "openai", "judge_model_version": "gpt-old"},
        {"judge": "openai", "judge_model_version": "gpt-new"},
    ]
    resolved = resolve_model_versions(
        judges=["openai"], model_ids={"openai": "gpt-5.4"}, rows=rows,
    )
    assert resolved["openai"] == "gpt-new"


# ---------------------------------------------------------------------------
# Git metadata robustness
# ---------------------------------------------------------------------------

def test_collect_git_metadata_is_robust() -> None:
    branch, sha = collect_git_metadata()
    assert sha is None or (len(sha) == 40 and all(c in "0123456789abcdef" for c in sha))
    assert branch is None or len(branch) > 0


def test_create_run_id_is_filesystem_safe() -> None:
    run_id = create_run_id()
    assert "/" not in run_id and "\\" not in run_id and " " not in run_id
    assert run_id.startswith("run_")
