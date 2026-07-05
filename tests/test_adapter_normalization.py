from adapters.base import normalize_response
from adapters.registry import get_adapter
from core.schemas import DatasetInstance


def test_normalize_response_schema():
    response = normalize_response(
        provider="openai",
        raw_text="hello",
        model_version="gpt-test",
        input_tokens=1,
        output_tokens=2,
    )

    assert response.provider == "openai"
    assert response.input_tokens == 1


def test_adapter_dry_run_summary(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "true")
    adapter = get_adapter("openai", model_id="gpt-test")

    response = adapter.summarize(
        DatasetInstance(instance_id="i1", doi="10.123/test"),
        {"dry_run": True},
    )

    assert response.provider == "openai"
    assert "10.123/test" in response.raw_text
