from pathlib import Path

from core.run_manifest import build_run_manifest, write_run_manifest


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
