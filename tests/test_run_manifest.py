import json
from pathlib import Path

from core.run_manifest import build_run_manifest, finalize_run_manifest, write_run_manifest
from core.schemas import RunManifest


def test_run_manifest_contains_required_provenance(tmp_path: Path):
    manifest = build_run_manifest(
        run_id="run-test",
        benchmark_name="vet_lit_summary_medhelm",
        mode="test",
        random_seed=42,
        cli_args=["evaluate", "--mode", "test"],
        selected_instance_ids=["tiny-001"],
        prompt_paths=[],
        config_paths=[],
        dataset_hash="abc123",
        model_ids={"openai": "gpt-test"},
    )
    path = write_run_manifest(manifest, tmp_path)

    assert path.exists()
    assert manifest.dataset_hash == "abc123"
    assert manifest.random_seed == 42
    assert manifest.model_ids["openai"] == "gpt-test"


def test_run_manifest_round_trips_and_finalizes_relative_artifacts(
    tmp_path: Path,
    monkeypatch,
):
    from core import run_manifest

    monkeypatch.setattr(run_manifest, "collect_git_metadata", lambda: ("unknown", "unknown"))
    output_path = tmp_path / "evaluations.jsonl"
    manifest = build_run_manifest(
        run_id="run-test",
        benchmark_name="vet_lit_summary_medhelm",
        mode="test",
        random_seed=42,
        cli_args=["evaluate", "--mode", "test"],
        selected_instance_ids=["tiny-001"],
        prompt_paths=[],
        config_paths=[],
        dataset_hash="abc123",
        model_ids={"openai": "gpt-test"},
        artifact_paths={"evaluations": output_path},
    )
    path = write_run_manifest(manifest, tmp_path)

    finalized = finalize_run_manifest(
        path,
        artifact_paths={"evaluations": output_path, "run_manifest": path},
        resolved_model_versions={"openai": "gpt-test-2026"},
    )
    raw = json.loads(path.read_text(encoding="utf-8"))

    assert RunManifest.model_validate(raw).run_id == "run-test"
    assert raw["started_utc"].endswith("Z")
    assert raw["finished_utc"].endswith("Z")
    assert finalized.git_sha == "unknown"
    assert finalized.resolved_model_versions["openai"] == "gpt-test-2026"
    assert raw["artifact_paths"]["evaluations"].endswith("evaluations.jsonl")
    assert finalized.dependency_lock_hash is not None
