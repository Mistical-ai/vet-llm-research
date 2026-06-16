"""
Tests for llm-sum/summarizer.py.

Critical assertions:
    Drift test: model_version reflects the EXACT version returned by the
        provider, not the alias we requested.
    Penny test: BudgetGuard.add_cost() called with response-derived tokens
        matches the per-MTok pricing in models_config.
    DRY_RUN: mock summary has the same dict shape as a real success.
    PHASE3_MODE=dev: paper_limit honours PHASE3_DEV_LIMIT.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import summarizer
from models_config import compute_cost, get_model_spec
from utils import BudgetGuard


@pytest.fixture(autouse=True)
def _force_dry_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    test_pdf_validation.py (Phase 2) sets `os.environ["DRY_RUN"] = "false"`
    at import time. When pytest collects files alphabetically, that runs
    before Phase 3 tests and leaks DRY_RUN=false into our test process.
    This fixture re-asserts DRY_RUN=true for every Phase 3 summariser test
    that doesn't explicitly override it.
    """
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setattr(summarizer, "DRY_RUN", True)


def _valid_summary_dict(**overrides) -> dict:
    """Return a complete VeterinarySummary-shaped dict for provider mocks."""
    data = {
        "headline": "Treatment improved clinical signs in dogs with disease X.",
        "objective": "Evaluate treatment response in client-owned dogs.",
        "study_design": "Retrospective cohort study.",
        "species": "Dogs",
        "sample_size": 42,
        "key_methods": ["Medical records were reviewed.", "Outcomes were compared."],
        "key_findings": ["Clinical signs improved in 30 of 42 dogs."],
        "clinical_significance": "The findings may help clinicians counsel owners.",
        "limitations": ["Retrospective design.", "Single-center population."],
        "summary_text": (
            "Objective: Evaluate treatment response in client-owned dogs. "
            "Key Methods: Medical records were reviewed. Primary Results: "
            "Clinical signs improved in 30 of 42 dogs. Clinical Significance: "
            "The findings may help clinicians counsel owners."
        ),
    }
    data.update(overrides)
    return data


# ---------------------------------------------------------------------------
# DRY_RUN mock summary shape
# ---------------------------------------------------------------------------

def test_dry_run_summary_has_full_schema() -> None:
    result = summarizer.generate_summary("openai", "The cat sat on the mat. " * 50)
    assert result["status"] == "success"
    assert isinstance(result["summary"], str) and result["summary"].startswith("[MOCK")
    assert isinstance(result["structured_summary"], dict)
    assert result["structured_summary"]["summary_text"] == result["summary"]
    assert isinstance(result["input_tokens"], int)
    assert isinstance(result["output_tokens"], int)
    assert "DRYRUN" in result["model_version"]
    assert "timestamp" in result


def test_veterinary_summary_schema_accepts_valid_object() -> None:
    parsed = summarizer.VeterinarySummary(**_valid_summary_dict())
    assert parsed.sample_size == 42
    assert parsed.summary_text.startswith("Objective:")
    assert parsed.model_dump()["headline"].startswith("Treatment improved")


def test_veterinary_summary_allows_missing_sample_size() -> None:
    parsed = summarizer.VeterinarySummary(**_valid_summary_dict(sample_size=None))
    assert parsed.sample_size is None
    assert parsed.species == "Dogs"


def test_guide_summary_prompt_is_format_only() -> None:
    template = "Rules first.\n\nArticle text:\n{ARTICLE_TEXT}"
    guide = "Objective: Example paper about llamas. Primary Results: 97% improved."

    guided_template = summarizer.apply_guide_summary_to_prompt(template, guide)
    message = summarizer.build_user_message("Target article about cats.", guided_template)

    assert "<FORMAT_GUIDE_SUMMARY>" in message
    assert guide in message
    assert "Do NOT copy its species" in message
    assert "Every fact in your output must come from the target article/PDF" in message
    assert message.index("<FORMAT_GUIDE_SUMMARY>") < message.index("Article text:")
    assert message.endswith("Target article about cats.")


def test_missing_guide_summary_file_leaves_prompt_unchanged(tmp_path: Path) -> None:
    missing = tmp_path / "not_created.txt"
    assert summarizer.load_optional_guide_summary(missing) is None

    template = "Article text:\n{ARTICLE_TEXT}"
    assert summarizer.apply_guide_summary_to_prompt(template, None) == template


def test_default_prompt_file_loads_and_keeps_article_placeholder() -> None:
    template = summarizer.load_prompt()
    assert "{ARTICLE_TEXT}" in template
    prompt_template, _guide_summary, _guide_path = summarizer.load_prompt_with_optional_guide(None)
    assert "{ARTICLE_TEXT}" in prompt_template


# ---------------------------------------------------------------------------
# Drift test: model_version comes from the response, not the request
# ---------------------------------------------------------------------------

def _fake_openai_response(specific_version: str = "gpt-5.4-0325-preview") -> SimpleNamespace:
    parsed = summarizer.VeterinarySummary(**_valid_summary_dict())
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(parsed=parsed))],
        usage=SimpleNamespace(prompt_tokens=1234, completion_tokens=456),
        model=specific_version,
    )


def test_drift_openai_model_version_from_response(monkeypatch: pytest.MonkeyPatch) -> None:
    # Drop both safeguards so the real-time path is exercised. PHASE3_MODE=single
    # is the lightest live mode; without flipping it the test-mode last-guardrail
    # in _is_dry_run() would short-circuit to a mock and the drift assertion
    # would fail with "gpt-5.4-DRYRUN".
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("PHASE3_MODE", "single")
    monkeypatch.setattr(summarizer, "DRY_RUN", False)

    fake_response = _fake_openai_response("gpt-5.4-0325-preview")
    captured_kwargs = {}

    class _FakeCompletions:
        def parse(self, **kwargs):
            captured_kwargs.update(kwargs)
            return fake_response

    class _FakeClient:
        def __init__(self):
            self.beta = SimpleNamespace(
                chat=SimpleNamespace(completions=_FakeCompletions())
            )

    with patch.dict("sys.modules", {"openai": SimpleNamespace(OpenAI=_FakeClient)}):
        result = summarizer.generate_summary("openai", "Article body.",
                                             prompt_template="X {ARTICLE_TEXT} Y")

    assert result["status"] == "success"
    # Exact-version string preserved — NOT the alias "gpt-5.4".
    assert result["model_version"] == "gpt-5.4-0325-preview"
    assert result["model_version"] != "gpt-5"
    assert result["input_tokens"] == 1234
    assert result["output_tokens"] == 456
    assert result["summary"] == result["structured_summary"]["summary_text"]
    assert captured_kwargs["response_format"] is summarizer.VeterinarySummary
    assert captured_kwargs["max_completion_tokens"] == summarizer.MAX_OUTPUT_TOKENS
    assert "max_tokens" not in captured_kwargs


def test_non_dry_mode_ignores_stale_module_dry_run_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    A stale module-level DRY_RUN=True must not force mocks after the active
    environment says this is a paid single-mode run.
    """
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("PHASE3_MODE", "single")
    monkeypatch.setattr(summarizer, "DRY_RUN", True)

    def _fake_provider(_text: str, **_kwargs) -> dict:
        parsed = summarizer.VeterinarySummary(**_valid_summary_dict())
        return summarizer._successful_summary_result(
            parsed=parsed,
            input_tokens=10,
            output_tokens=5,
            model_version="real-provider-version",
        )

    monkeypatch.setitem(summarizer.PROVIDER_CALLERS, "openai", _fake_provider)

    result = summarizer.generate_summary("openai", "Article body.")

    assert result["status"] == "success"
    assert result["model_version"] == "real-provider-version"
    assert not result["model_version"].endswith("-DRYRUN")


def test_gemini_structured_output_validates_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("PHASE3_MODE", "single")
    monkeypatch.setattr(summarizer, "DRY_RUN", False)

    captured_generation_config = {}

    class _FakeModels:
        def generate_content(self, model, contents, config):
            captured_generation_config.update(config)
            captured_generation_config["model"] = model
            captured_generation_config["contents"] = contents
            return SimpleNamespace(
                text=json.dumps(_valid_summary_dict()),
                usage_metadata=SimpleNamespace(
                    prompt_token_count=111,
                    candidates_token_count=22,
                ),
            )

    class _FakeGeminiClient:
        def __init__(self, api_key=None):
            self.api_key = api_key
            self.models = _FakeModels()

    class _FakeTypes:
        @staticmethod
        def GenerateContentConfig(**kwargs):
            return kwargs

    fake_genai = SimpleNamespace(
        Client=_FakeGeminiClient,
        types=_FakeTypes,
    )

    with patch.dict("sys.modules", {
        "google": SimpleNamespace(genai=fake_genai),
        "google.genai": fake_genai,
        "google.genai.types": _FakeTypes,
    }):
        result = summarizer.generate_summary("gemini", "Article body.",
                                             prompt_template="X {ARTICLE_TEXT} Y")

    assert result["status"] == "success"
    assert result["input_tokens"] == 111
    assert result["output_tokens"] == 22
    assert result["structured_summary"]["sample_size"] == 42
    assert captured_generation_config["response_mime_type"] == "application/json"
    assert captured_generation_config["contents"] == "X Article body. Y"
    response_schema = captured_generation_config["response_schema"]
    assert isinstance(response_schema, dict)
    assert response_schema["properties"]["headline"]["type"] == "string"
    assert "default" not in json.dumps(response_schema)


def test_anthropic_tool_call_returns_validated_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("PHASE3_MODE", "single")
    monkeypatch.setattr(summarizer, "DRY_RUN", False)

    captured_kwargs = {}

    class _FakeAnthropicClient:
        def __init__(self):
            self.messages = self

        def create(self, **kwargs):
            captured_kwargs.update(kwargs)
            return SimpleNamespace(
                content=[
                    SimpleNamespace(
                        type="tool_use",
                        name="VeterinarySummary",
                        input=_valid_summary_dict(),
                    )
                ],
                usage=SimpleNamespace(input_tokens=222, output_tokens=33),
                model="claude-sonnet-4-6-test",
            )

    with patch.dict("sys.modules", {
        "anthropic": SimpleNamespace(Anthropic=_FakeAnthropicClient),
    }):
        result = summarizer.generate_summary("anthropic", "Article body.",
                                             prompt_template="X {ARTICLE_TEXT} Y")

    assert result["status"] == "success"
    assert result["input_tokens"] == 222
    assert result["output_tokens"] == 33
    assert result["structured_summary"]["headline"].startswith("Treatment improved")
    assert captured_kwargs["tool_choice"] == {"type": "tool", "name": "VeterinarySummary"}
    assert captured_kwargs["tools"][0]["name"] == "VeterinarySummary"
    assert "input_schema" in captured_kwargs["tools"][0]


def test_partial_provider_payload_is_repaired_without_inventing_facts() -> None:
    parsed = summarizer.coerce_veterinary_summary({
        "headline": "Dietary carbohydrates were associated with disease.",
        "objective": "Assess diet associations.",
        "study_design": "Case-control study.",
        "species": "Dogs",
        "sample_size": 42,
        "key_methods": ["Compared owner-reported diets."],
    })

    assert parsed.key_findings == []
    assert parsed.clinical_significance == "Not reported"
    assert parsed.limitations == []
    assert parsed.summary_text.startswith("Objective: Assess diet associations.")


# ---------------------------------------------------------------------------
# Penny test: BudgetGuard total matches the price formula
# ---------------------------------------------------------------------------

def test_penny_budget_matches_models_config_pricing() -> None:
    """compute_cost(...) is the single source of truth for $/token; verify
    the formula here so BudgetGuard.total_spent cannot silently drift."""
    spec = get_model_spec("openai")
    in_tokens, out_tokens = 1234, 456

    # Manual calc using the SAME numbers BudgetGuard would charge.
    expected = (in_tokens / 1_000_000) * spec.price_input_per_mtok + \
               (out_tokens / 1_000_000) * spec.price_output_per_mtok

    got = compute_cost("openai", in_tokens, out_tokens, batched=False)
    assert abs(got - expected) < 1e-9

    # Round-trip through BudgetGuard.
    guard = BudgetGuard(hard_stop=10.0)
    guard.add_cost(got)
    assert abs(guard.total_spent - expected) < 1e-9


# ---------------------------------------------------------------------------
# PHASE3_MODE=dev paper cap
# ---------------------------------------------------------------------------

def test_dev_mode_caps_paper_count(tmp_path: Path,
                                    monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Replaces the old DEVELOPMENT_MODE=True cap test. With ``PHASE3_MODE=dev``
    and ``PHASE3_DEV_LIMIT=2`` the run should process exactly 2 papers
    regardless of how many are in the manifest.
    """
    import prepare_texts
    from phase3_mode import resolve_mode

    monkeypatch.setenv("PHASE3_MODE", "dev")
    monkeypatch.setenv("PHASE3_DEV_LIMIT", "2")

    # Sanity: the resolved profile reports limit=2.
    profile = resolve_mode()
    assert profile.name == "dev"
    assert profile.paper_limit == 2

    # Build a fake manifest with 5 papers.
    manifest = tmp_path / "manifest.jsonl"
    processed = tmp_path / "processed"
    processed.mkdir()

    dois = [f"10.9999/test.{i:04d}" for i in range(5)]
    with open(manifest, "w", encoding="utf-8") as f:
        for d in dois:
            f.write(json.dumps({"doi": d, "journal": "TEST"}) + "\n")

    # Pre-populate the legacy {slug}.jsonl cache (these records lack a title
    # so the descriptive path resolves back to the legacy slug name).
    from file_paths import doi_to_slug
    for d in dois:
        slug = doi_to_slug(d)
        entry = {"doi": d, "slug": slug, "text": "Body of paper. " * 40}
        (processed / f"{slug}.jsonl").write_text(json.dumps(entry) + "\n", encoding="utf-8")

    monkeypatch.setattr(summarizer, "PROCESSED_DIR", processed)
    monkeypatch.setattr(prepare_texts, "PROCESSED_DIR", processed)
    monkeypatch.setattr(summarizer, "SUMMARIES_PATH", tmp_path / "summaries.jsonl")
    monkeypatch.setattr(summarizer, "MANIFEST_PATH", manifest)

    counts = summarizer.run_realtime(
        manifest_path=manifest,
        resume=False,
        paper_limit=profile.paper_limit,
        providers=["openai"],
    )

    assert counts["success"] == 2
    assert counts["failed"] == 0

    out_lines = (tmp_path / "summaries.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(out_lines) == 2


def test_raw_text_input_source_writes_separate_summary_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import prepare_texts

    manifest = tmp_path / "manifest.jsonl"
    processed = tmp_path / "processed"
    raw_text = tmp_path / "raw_text"
    processed.mkdir()
    raw_text.mkdir()

    doi = "10.9999/source.0001"
    manifest.write_text(json.dumps({"doi": doi, "journal": "TEST"}) + "\n", encoding="utf-8")

    from file_paths import doi_to_slug
    slug = doi_to_slug(doi)
    (processed / f"{slug}.jsonl").write_text(
        json.dumps({"doi": doi, "slug": slug, "text": "processed body " * 20}) + "\n",
        encoding="utf-8",
    )
    (raw_text / f"{slug}.jsonl").write_text(
        json.dumps({"doi": doi, "slug": slug, "text": "raw body with references " * 20}) + "\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(summarizer, "SUMMARIES_PATH", tmp_path / "summaries.jsonl")
    monkeypatch.setattr(prepare_texts, "PROCESSED_DIR", processed)
    monkeypatch.setattr(prepare_texts, "RAW_TEXT_DIR", raw_text)

    summarizer.run_realtime(
        manifest_path=manifest,
        providers=["openai"],
        paper_limit=1,
        input_source="processed",
    )
    summarizer.run_realtime(
        manifest_path=manifest,
        providers=["openai"],
        paper_limit=1,
        input_source="raw_text",
    )

    rows = [
        json.loads(line)
        for line in (tmp_path / "summaries.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert {row["input_source"] for row in rows} == {"processed", "raw_text"}
    assert {row["custom_id"] for row in rows} == {slug, f"{slug}__raw_text"}


def test_pdf_input_source_writes_separate_summary_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = tmp_path / "manifest.jsonl"
    raw = tmp_path / "raw"
    raw.mkdir()

    doi = "10.9999/pdf.0001"
    manifest.write_text(json.dumps({"doi": doi, "journal": "TEST"}) + "\n", encoding="utf-8")

    from file_paths import doi_to_slug
    slug = doi_to_slug(doi)
    (raw / f"{slug}.pdf").write_bytes(b"%PDF-1.4 fake test pdf")

    monkeypatch.setattr(summarizer, "RAW_DIR", raw)
    monkeypatch.setattr(summarizer, "SUMMARIES_PATH", tmp_path / "summaries.jsonl")

    counts = summarizer.run_realtime(
        manifest_path=manifest,
        providers=["openai"],
        paper_limit=1,
        input_source="pdf",
    )

    assert counts["success"] == 1
    rows = [
        json.loads(line)
        for line in (tmp_path / "summaries.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert len(rows) == 1
    assert rows[0]["input_source"] == "pdf"
    assert rows[0]["custom_id"] == f"{slug}__pdf"
    assert rows[0]["models"]["openai"]["status"] == "success"


def test_pdf_input_source_rejected_outside_test_and_single() -> None:
    assert summarizer.main(["--mode", "dev", "--input-source", "pdf"]) == 1
    assert summarizer.main(["--mode", "batch", "--input-source", "pdf"]) == 1


def test_openai_pdf_call_uploads_file_and_parses_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("PHASE3_MODE", "single")
    monkeypatch.setattr(summarizer, "DRY_RUN", False)

    pdf_path = tmp_path / "paper.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 direct pdf")
    captured = {}

    class _FakeFiles:
        def create(self, **kwargs):
            captured["file_purpose"] = kwargs["purpose"]
            captured["file_name"] = Path(kwargs["file"].name).name
            return SimpleNamespace(id="file_test_pdf")

    class _FakeResponses:
        def parse(self, **kwargs):
            captured["response_kwargs"] = kwargs
            return SimpleNamespace(
                output_parsed=summarizer.VeterinarySummary(**_valid_summary_dict()),
                usage=SimpleNamespace(input_tokens=3210, output_tokens=210),
                model="gpt-5.4-pdf-test",
            )

    class _FakeClient:
        def __init__(self):
            self.files = _FakeFiles()
            self.responses = _FakeResponses()

    with patch.dict("sys.modules", {"openai": SimpleNamespace(OpenAI=_FakeClient)}):
        result = summarizer.generate_summary_from_pdf(
            "openai",
            pdf_path,
            prompt_template="Summarize this: {ARTICLE_TEXT}",
        )

    assert result["status"] == "success"
    assert result["input_tokens"] == 3210
    assert result["output_tokens"] == 210
    assert result["model_version"] == "gpt-5.4-pdf-test"
    assert captured["file_purpose"] == "user_data"
    assert captured["file_name"] == "paper.pdf"
    content = captured["response_kwargs"]["input"][0]["content"]
    assert content[0] == {"type": "input_file", "file_id": "file_test_pdf"}
    assert content[1]["type"] == "input_text"
    assert "attached as a PDF" in content[1]["text"]
    assert captured["response_kwargs"]["text_format"] is summarizer.VeterinarySummary


def test_anthropic_pdf_call_sends_document_block(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("PHASE3_MODE", "single")
    monkeypatch.setattr(summarizer, "DRY_RUN", False)

    pdf_path = tmp_path / "paper.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 direct pdf")
    captured = {}

    class _FakeAnthropicClient:
        def __init__(self):
            self.messages = self

        def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                content=[
                    SimpleNamespace(
                        type="tool_use",
                        name="VeterinarySummary",
                        input=_valid_summary_dict(),
                    )
                ],
                usage=SimpleNamespace(input_tokens=4321, output_tokens=321),
                model="claude-opus-pdf-test",
            )

    with patch.dict("sys.modules", {
        "anthropic": SimpleNamespace(Anthropic=_FakeAnthropicClient),
    }):
        result = summarizer.generate_summary_from_pdf(
            "anthropic",
            pdf_path,
            prompt_template="Summarize this: {ARTICLE_TEXT}",
        )

    assert result["status"] == "success"
    assert result["input_tokens"] == 4321
    assert result["output_tokens"] == 321
    content = captured["messages"][0]["content"]
    assert content[0]["type"] == "document"
    assert content[0]["source"]["media_type"] == "application/pdf"
    assert content[1]["type"] == "text"
    assert "attached as a PDF" in content[1]["text"]


def test_gemini_pdf_call_uploads_pdf(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("PHASE3_MODE", "single")
    monkeypatch.setattr(summarizer, "DRY_RUN", False)

    pdf_path = tmp_path / "paper.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 direct pdf")
    captured = {}

    class _FakeFiles:
        def upload(self, file):
            captured["upload"] = {"file": file}
            return SimpleNamespace(name="uploaded-paper")

    class _FakeModels:
        def generate_content(self, model, contents, config):
            captured["model"] = model
            captured["contents"] = contents
            captured["generation_config"] = config
            return SimpleNamespace(
                text=json.dumps(_valid_summary_dict()),
                usage_metadata=SimpleNamespace(
                    prompt_token_count=5432,
                    candidates_token_count=234,
                ),
            )

    class _FakeGeminiClient:
        def __init__(self, api_key=None):
            self.api_key = api_key
            self.files = _FakeFiles()
            self.models = _FakeModels()

    class _FakeTypes:
        @staticmethod
        def GenerateContentConfig(**kwargs):
            return kwargs

    fake_genai = SimpleNamespace(
        Client=_FakeGeminiClient,
        types=_FakeTypes,
    )

    with patch.dict("sys.modules", {
        "google": SimpleNamespace(genai=fake_genai),
        "google.genai": fake_genai,
        "google.genai.types": _FakeTypes,
    }):
        result = summarizer.generate_summary_from_pdf(
            "gemini",
            pdf_path,
            prompt_template="Summarize this: {ARTICLE_TEXT}",
        )

    assert result["status"] == "success"
    assert result["input_tokens"] == 5432
    assert result["output_tokens"] == 234
    assert captured["upload"]["file"] == str(pdf_path)
    response_schema = captured["generation_config"]["response_schema"]
    assert isinstance(response_schema, dict)
    assert "default" not in json.dumps(response_schema)
    assert captured["contents"][0].name == "uploaded-paper"
    assert "attached as a PDF" in captured["contents"][1]


# ---------------------------------------------------------------------------
# Gemini schema: additionalProperties must also be stripped
# ---------------------------------------------------------------------------

def test_gemini_schema_strips_additional_properties() -> None:
    schema = summarizer.gemini_response_schema()
    schema_str = json.dumps(schema)
    assert "additionalProperties" not in schema_str, (
        "Gemini schema must not contain 'additionalProperties'; "
        "it causes SDK validation errors."
    )
    assert "default" not in schema_str
    # Core VeterinarySummary fields must still be present.
    assert "properties" in schema
    assert "headline" in schema["properties"]
    assert "key_methods" in schema["properties"]


# ---------------------------------------------------------------------------
# OpenAI temperature retry
# ---------------------------------------------------------------------------

def test_openai_temperature_retry_omits_temperature_on_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When OpenAI rejects temperature, the caller retries without it."""
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("PHASE3_MODE", "single")
    monkeypatch.setattr(summarizer, "DRY_RUN", False)

    call_count = [0]
    captured_kwargs: list[dict] = []

    class _FakeCompletions:
        def parse(self, **kwargs):
            call_count[0] += 1
            captured_kwargs.append(dict(kwargs))
            if call_count[0] == 1:
                raise Exception(
                    "Unsupported parameter: temperature is not supported with this model"
                )
            return SimpleNamespace(
                choices=[SimpleNamespace(
                    message=SimpleNamespace(
                        parsed=summarizer.VeterinarySummary(**_valid_summary_dict())
                    )
                )],
                usage=SimpleNamespace(prompt_tokens=100, completion_tokens=50),
                model="gpt-o1-test",
            )

    class _FakeClient:
        def __init__(self):
            self.beta = SimpleNamespace(
                chat=SimpleNamespace(completions=_FakeCompletions())
            )

    with patch.dict("sys.modules", {"openai": SimpleNamespace(OpenAI=_FakeClient)}):
        result = summarizer.generate_summary(
            "openai", "Article body.", prompt_template="X {ARTICLE_TEXT} Y"
        )

    assert result["status"] == "success"
    assert call_count[0] == 2, "Expected exactly two calls: one failed, one retry"
    assert "temperature" in captured_kwargs[0], "First call must include temperature"
    assert "temperature" not in captured_kwargs[1], "Retry must omit temperature"


def test_openai_is_temperature_error_detection() -> None:
    """Helper correctly identifies temperature-related errors only."""
    assert summarizer._openai_is_temperature_error(
        Exception("Unsupported parameter: temperature is not supported with this model")
    )
    assert summarizer._openai_is_temperature_error(
        Exception("invalid_request_error: temperature is not supported")
    )
    assert not summarizer._openai_is_temperature_error(
        Exception("Authentication error: invalid API key")
    )
    assert not summarizer._openai_is_temperature_error(
        Exception("Rate limit exceeded")
    )


# ---------------------------------------------------------------------------
# Human-readable format
# ---------------------------------------------------------------------------

def test_format_human_readable_produces_numbered_sections() -> None:
    structured = _valid_summary_dict()
    text = summarizer._format_human_readable(structured)

    assert "1. Objective" in text
    assert "2. Key Methods" in text
    assert "3. Primary Results" in text
    assert "4. Clinical Significance" in text
    assert "5. Limitations" in text
    # Lists should be bullet-pointed.
    assert "- Medical records were reviewed." in text
    assert "- Clinical signs improved in 30 of 42 dogs." in text
    # Sections must appear in order.
    assert text.index("1. Objective") < text.index("2. Key Methods")
    assert text.index("2. Key Methods") < text.index("3. Primary Results")
    assert text.index("3. Primary Results") < text.index("4. Clinical Significance")
    assert text.index("4. Clinical Significance") < text.index("5. Limitations")


def test_enrich_result_adds_human_readable_on_success() -> None:
    result = summarizer._mock_summary("openai", "Some article text.")
    enriched = summarizer._enrich_result_with_human_readable(result)
    assert "human_readable" in enriched
    assert "1. Objective" in enriched["human_readable"]


def test_enrich_result_skips_human_readable_on_failure() -> None:
    failed = {
        "status": "failed",
        "error": "API error",
        "summary": None,
        "structured_summary": None,
        "input_tokens": None,
        "output_tokens": None,
        "model_version": None,
        "timestamp": "2026-06-14T00:00:00+00:00",
    }
    enriched = summarizer._enrich_result_with_human_readable(failed)
    assert "human_readable" not in enriched


# ---------------------------------------------------------------------------
# Folder-based summarize_all_pdfs (test mode — no API calls)
# ---------------------------------------------------------------------------

def test_summarize_all_pdfs_test_mode_writes_output_files(
    tmp_path: Path,
) -> None:
    """In test (DRY_RUN) mode every PDF gets 3 provider sections and a text output."""
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    (raw_dir / "paper1.pdf").write_bytes(b"%PDF-1.4 fake pdf 1")
    (raw_dir / "paper2.pdf").write_bytes(b"%PDF-1.4 fake pdf 2")

    pdf_output = tmp_path / "summaries_pdf"

    counts = summarizer.summarize_all_pdfs(
        output_dir=pdf_output,
        raw_dir=raw_dir,
        providers=["openai", "anthropic", "gemini"],
    )

    # 2 PDFs × 3 providers = 6 successes
    assert counts["success"] == 6
    assert counts["failed"] == 0

    # One readable text file per PDF
    assert (pdf_output / "paper1.txt").exists()
    assert (pdf_output / "paper2.txt").exists()

    for stem in ("paper1", "paper2"):
        text = (pdf_output / f"{stem}.txt").read_text(encoding="utf-8")
        assert "Summary Source: Pdf" in text
        assert f"Source File: {stem}.pdf" in text
        assert "OPENAI SUMMARY" in text
        assert "ANTHROPIC SUMMARY" in text
        assert "GEMINI SUMMARY" in text
        assert "1. Objective" in text


def test_summarize_all_pdfs_each_pdf_has_all_three_providers(
    tmp_path: Path,
) -> None:
    """Every PDF output has all three provider keys regardless of which providers were requested."""
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    (raw_dir / "study.pdf").write_bytes(b"%PDF-1.4")

    pdf_output = tmp_path / "summaries_pdf"
    summarizer.summarize_all_pdfs(
        output_dir=pdf_output,
        raw_dir=raw_dir,
        providers=["openai", "anthropic", "gemini"],
    )

    text = (pdf_output / "study.txt").read_text(encoding="utf-8")
    assert "OPENAI SUMMARY" in text
    assert "ANTHROPIC SUMMARY" in text
    assert "GEMINI SUMMARY" in text


def test_summarize_all_pdfs_resume_skips_successful_slots(
    tmp_path: Path,
) -> None:
    """Resume still skips successful slots from legacy JSON folder outputs."""
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    (raw_dir / "paper.pdf").write_bytes(b"%PDF-1.4")
    pdf_output = tmp_path / "summaries_pdf"

    pdf_output.mkdir()
    legacy_entry = {
        "source_type": "pdf",
        "source_filename": "paper.pdf",
        "doi": None,
        "slug": "paper",
        "generated_at": "2026-06-14T00:00:00+00:00",
        "models": {
            provider: {"status": "success"}
            for provider in ("openai", "anthropic", "gemini")
        },
    }
    (pdf_output / "paper.json").write_text(json.dumps(legacy_entry), encoding="utf-8")

    counts_resume = summarizer.summarize_all_pdfs(
        output_dir=pdf_output,
        raw_dir=raw_dir,
        providers=["openai", "anthropic", "gemini"],
        resume=True,
    )
    assert counts_resume["skipped"] == 3
    assert counts_resume["success"] == 0


def test_summarize_all_pdfs_limit_respected(tmp_path: Path) -> None:
    """limit= processes only the first N PDFs."""
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    for i in range(4):
        (raw_dir / f"paper{i}.pdf").write_bytes(b"%PDF-1.4")

    pdf_output = tmp_path / "summaries_pdf"
    counts = summarizer.summarize_all_pdfs(
        output_dir=pdf_output,
        raw_dir=raw_dir,
        providers=["openai"],
        limit=2,
    )
    assert counts["success"] == 2
    txt_files = list(pdf_output.glob("*.txt"))
    assert len(txt_files) == 2


# ---------------------------------------------------------------------------
# Folder-based summarize_all_processed_texts (test mode — no API calls)
# ---------------------------------------------------------------------------

def test_summarize_all_processed_texts_test_mode_writes_output_files(
    tmp_path: Path,
) -> None:
    """In test mode every processed JSONL gets 3 provider sections by source stem."""
    from file_paths import doi_to_slug

    processed_dir = tmp_path / "processed"
    processed_dir.mkdir()

    dois = ["10.9999/alpha.0001", "10.9999/beta.0002"]
    source_records = []
    for doi in dois:
        slug = doi_to_slug(doi)
        source_stem = f"journal__study_title__{slug}"
        source_records.append((doi, slug, source_stem))
        entry = {"doi": doi, "slug": slug, "text": "Clinical trial body. " * 30}
        (processed_dir / f"{source_stem}.jsonl").write_text(
            json.dumps(entry) + "\n", encoding="utf-8"
        )

    txt_output = tmp_path / "summaries_txt"
    counts = summarizer.summarize_all_processed_texts(
        output_dir=txt_output,
        processed_dir=processed_dir,
        providers=["openai", "anthropic", "gemini"],
    )

    assert counts["success"] == 6
    assert counts["failed"] == 0

    txt_files = list(txt_output.glob("*.txt"))
    assert len(txt_files) == 2

    for doi, slug, source_stem in source_records:
        text = (txt_output / f"{source_stem}.txt").read_text(encoding="utf-8")
        assert "Summary Source: Processed Text" in text
        assert f"Source File: {source_stem}.jsonl" in text
        assert f"DOI: {doi}" in text
        assert f"Slug: {slug}" in text
        assert "OPENAI SUMMARY" in text
        assert "ANTHROPIC SUMMARY" in text
        assert "GEMINI SUMMARY" in text


def test_summarize_all_processed_texts_writes_to_summaries_txt_dir(
    tmp_path: Path,
) -> None:
    """Processed outputs mirror the processed source stem in summaries_txt."""
    from file_paths import doi_to_slug

    processed_dir = tmp_path / "processed"
    processed_dir.mkdir()
    doi = "10.9999/gamma.0003"
    slug = doi_to_slug(doi)
    source_stem = f"jvim__gamma_article__{slug}"
    entry = {"doi": doi, "slug": slug, "text": "Some text. " * 40}
    (processed_dir / f"{source_stem}.jsonl").write_text(
        json.dumps(entry) + "\n", encoding="utf-8"
    )

    txt_output = tmp_path / "summaries_txt"
    summarizer.summarize_all_processed_texts(
        output_dir=txt_output,
        processed_dir=processed_dir,
        providers=["openai"],
    )
    assert (txt_output / f"{source_stem}.txt").exists()
    assert not (txt_output / f"{slug}.txt").exists()


# ---------------------------------------------------------------------------
# Failed provider does not stop other providers or other files
# ---------------------------------------------------------------------------

def test_failed_provider_does_not_stop_other_providers_or_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    If one provider raises an unexpected error for one paper, the other
    providers and other papers must still run and produce output.
    """
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("PHASE3_MODE", "single")
    monkeypatch.setattr(summarizer, "DRY_RUN", False)

    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    (raw_dir / "paper1.pdf").write_bytes(b"%PDF-1.4")
    (raw_dir / "paper2.pdf").write_bytes(b"%PDF-1.4")

    call_tracker: list[tuple[str, str]] = []

    def _fake_pdf_summary(provider: str, pdf_path: Path, **_kwargs) -> dict:
        call_tracker.append((provider, pdf_path.name))
        if provider == "anthropic" and pdf_path.name == "paper1.pdf":
            raise RuntimeError("Simulated Anthropic failure")
        return summarizer._mock_pdf_summary(provider, pdf_path)

    monkeypatch.setattr(summarizer, "generate_summary_from_pdf", _fake_pdf_summary)

    pdf_output = tmp_path / "summaries_pdf"
    counts = summarizer.summarize_all_pdfs(
        output_dir=pdf_output,
        raw_dir=raw_dir,
        providers=["openai", "anthropic", "gemini"],
    )

    # 2 papers × 3 providers = 6 calls total; 1 failed
    assert len(call_tracker) == 6
    assert counts["success"] == 5
    assert counts["failed"] == 1

    # paper1: anthropic section is failed, others success
    text1 = (pdf_output / "paper1.txt").read_text(encoding="utf-8")
    assert "OPENAI SUMMARY" in text1
    assert "ANTHROPIC SUMMARY" in text1
    assert "Status: failed" in text1
    assert "GEMINI SUMMARY" in text1

    # paper2: all providers produced readable success sections
    text2 = (pdf_output / "paper2.txt").read_text(encoding="utf-8")
    assert text2.count("Status: success") == 3


# ---------------------------------------------------------------------------
# summarize-all CLI command (test mode)
# ---------------------------------------------------------------------------

def test_run_phase3_summarize_all_single_mode_limits_both_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """single mode means 1 PDF + 1 processed text, for 6 summaries total."""
    import run_phase3

    captured: dict[str, object] = {}

    def _fake_load_prompt(_path=None):
        return "Prompt {ARTICLE_TEXT}", None, Path("guide.txt")

    def _fake_confirm(_profile, force=False):
        return True

    def _fake_pdfs(**kwargs):
        captured["pdf_limit"] = kwargs["limit"]
        return {"success": 3, "failed": 0, "skipped": 0, "no_source": 0}

    def _fake_texts(**kwargs):
        captured["txt_limit"] = kwargs["limit"]
        captured["txt_stems"] = kwargs["stems"]
        return {"success": 3, "failed": 0, "skipped": 0, "no_source": 0}

    monkeypatch.setattr(summarizer, "load_prompt_with_optional_guide", _fake_load_prompt)
    monkeypatch.setattr(summarizer, "confirm_real_batch", _fake_confirm)
    monkeypatch.setattr(summarizer, "summarize_all_pdfs", _fake_pdfs)
    monkeypatch.setattr(summarizer, "summarize_all_processed_texts", _fake_texts)
    monkeypatch.setattr(run_phase3, "_paired_summary_stems", lambda _limit: {"same_article"})

    ret = run_phase3.main(["summarize-all", "--mode", "single"])
    assert ret == 0
    assert captured["pdf_limit"] == 1
    assert captured["txt_limit"] == 1
    assert captured["txt_stems"] == {"same_article"}


def test_run_phase3_summarize_all_test_mode_defaults_to_one_pair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mock summarize-all should not create one file per corpus article by default."""
    import run_phase3

    captured: dict[str, object] = {}

    def _fake_load_prompt(_path=None):
        return "Prompt {ARTICLE_TEXT}", None, Path("guide.txt")

    def _fake_pdfs(**kwargs):
        captured["pdf_limit"] = kwargs["limit"]
        captured["pdf_stems"] = kwargs["stems"]
        return {"success": 3, "failed": 0, "skipped": 0, "no_source": 0}

    def _fake_texts(**kwargs):
        captured["txt_limit"] = kwargs["limit"]
        captured["txt_stems"] = kwargs["stems"]
        return {"success": 3, "failed": 0, "skipped": 0, "no_source": 0}

    monkeypatch.setattr(summarizer, "load_prompt_with_optional_guide", _fake_load_prompt)
    monkeypatch.setattr(summarizer, "summarize_all_pdfs", _fake_pdfs)
    monkeypatch.setattr(summarizer, "summarize_all_processed_texts", _fake_texts)
    monkeypatch.setattr(run_phase3, "_paired_summary_stems", lambda _limit: {"same_article"})

    ret = run_phase3.main(["summarize-all", "--mode", "test"])
    assert ret == 0
    assert captured["pdf_limit"] == 1
    assert captured["txt_limit"] == 1
    assert captured["pdf_stems"] == {"same_article"}
    assert captured["txt_stems"] == {"same_article"}


def test_run_phase3_summarize_all_dev_mode_defaults_to_one_pair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """summarize-all dev mode is also a one-article PDF-vs-processed smoke test."""
    import run_phase3

    captured: dict[str, object] = {}

    def _fake_load_prompt(_path=None):
        return "Prompt {ARTICLE_TEXT}", None, Path("guide.txt")

    def _fake_confirm(_profile, force=False):
        return True

    def _fake_pdfs(**kwargs):
        captured["pdf_limit"] = kwargs["limit"]
        captured["pdf_stems"] = kwargs["stems"]
        return {"success": 3, "failed": 0, "skipped": 0, "no_source": 0}

    def _fake_texts(**kwargs):
        captured["txt_limit"] = kwargs["limit"]
        captured["txt_stems"] = kwargs["stems"]
        return {"success": 3, "failed": 0, "skipped": 0, "no_source": 0}

    monkeypatch.setattr(summarizer, "load_prompt_with_optional_guide", _fake_load_prompt)
    monkeypatch.setattr(summarizer, "confirm_real_batch", _fake_confirm)
    monkeypatch.setattr(summarizer, "summarize_all_pdfs", _fake_pdfs)
    monkeypatch.setattr(summarizer, "summarize_all_processed_texts", _fake_texts)
    monkeypatch.setattr(run_phase3, "_paired_summary_stems", lambda _limit: {"same_article"})

    ret = run_phase3.main(["summarize-all", "--mode", "dev"])
    assert ret == 0
    assert captured["pdf_limit"] == 1
    assert captured["txt_limit"] == 1
    assert captured["pdf_stems"] == {"same_article"}
    assert captured["txt_stems"] == {"same_article"}


def test_run_phase3_summarize_all_rejects_batch_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """summarize-all must not silently turn batch mode into real-time PDF calls."""
    import run_phase3

    called = {"pdfs": False, "texts": False}

    monkeypatch.setattr(
        summarizer,
        "summarize_all_pdfs",
        lambda **_kwargs: called.__setitem__("pdfs", True),
    )
    monkeypatch.setattr(
        summarizer,
        "summarize_all_processed_texts",
        lambda **_kwargs: called.__setitem__("texts", True),
    )

    ret = run_phase3.main(["summarize-all", "--mode", "batch"])
    assert ret == 1
    assert called == {"pdfs": False, "texts": False}


# ---------------------------------------------------------------------------
# Folder I/O helpers
# ---------------------------------------------------------------------------

def test_write_folder_output_creates_readable_text(tmp_path: Path) -> None:
    entry = {
        "source_type": "pdf",
        "source_filename": "test.pdf",
        "doi": None,
        "slug": "test",
        "generated_at": "2026-06-14T00:00:00+00:00",
        "models": {"openai": {"status": "success", "summary": "A mock summary."}},
    }
    out_path = summarizer._write_folder_output(tmp_path, "test", entry)
    text = out_path.read_text(encoding="utf-8")
    assert out_path == tmp_path / "test.txt"
    assert "Summary Source: Pdf" in text
    assert "OPENAI SUMMARY" in text
    assert "A mock summary." in text


def test_load_folder_output_reads_legacy_json(tmp_path: Path) -> None:
    entry = {
        "source_type": "pdf",
        "source_filename": "test.pdf",
        "doi": None,
        "slug": "test",
        "generated_at": "2026-06-14T00:00:00+00:00",
        "models": {"openai": {"status": "success", "summary": "A mock summary."}},
    }
    (tmp_path / "test.json").write_text(json.dumps(entry), encoding="utf-8")
    loaded = summarizer._load_folder_output(tmp_path, "test")
    assert loaded == entry


def test_load_folder_output_returns_none_when_missing(tmp_path: Path) -> None:
    assert summarizer._load_folder_output(tmp_path, "nonexistent") is None
