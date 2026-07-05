from core.schemas import DatasetInstance, EvaluationRecord, ProviderResponse, RunManifest


def test_core_models_validate_and_serialize():
    instance = DatasetInstance(instance_id="i1", doi="10.123/test", year=2026)
    response = ProviderResponse(provider="openai", raw_text="ok", model_version="gpt-test")
    record = EvaluationRecord(
        benchmark_name="vet_lit_summary_medhelm",
        doi=instance.doi,
        summarizer="openai",
        judge="anthropic",
        evaluator_version="evaluator-test",
        rubric_version="vet_medhelm_score_v1.0",
        jury_score=4.5,
    )
    manifest = RunManifest(
        run_id="run-test",
        benchmark_name="vet_lit_summary_medhelm",
        started_utc="2026-01-01T00:00:00Z",
        python_version="3.12",
        platform="test",
    )

    assert instance.model_dump()["doi"] == "10.123/test"
    assert response.provider == "openai"
    assert record.jury_score == 4.5
    assert "run-test" in manifest.model_dump_json()
