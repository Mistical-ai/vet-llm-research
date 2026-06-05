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


# ---------------------------------------------------------------------------
# Drift test: model_version comes from the response, not the request
# ---------------------------------------------------------------------------

def _fake_openai_response(specific_version: str = "gpt-5.5-0325-preview") -> SimpleNamespace:
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
    # would fail with "gpt-5.5-DRYRUN".
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("PHASE3_MODE", "single")
    monkeypatch.setattr(summarizer, "DRY_RUN", False)

    fake_response = _fake_openai_response("gpt-5.5-0325-preview")
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
    # Exact-version string preserved — NOT the alias "gpt-5.5".
    assert result["model_version"] == "gpt-5.5-0325-preview"
    assert result["model_version"] != "gpt-5"
    assert result["input_tokens"] == 1234
    assert result["output_tokens"] == 456
    assert result["summary"] == result["structured_summary"]["summary_text"]
    assert captured_kwargs["response_format"] is summarizer.VeterinarySummary


def test_gemini_structured_output_validates_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("PHASE3_MODE", "single")
    monkeypatch.setattr(summarizer, "DRY_RUN", False)

    captured_generation_config = {}

    class _FakeGeminiModel:
        def __init__(self, model_id):
            self.model_id = model_id

        def generate_content(self, _message, generation_config):
            captured_generation_config.update(generation_config)
            return SimpleNamespace(
                text=json.dumps(_valid_summary_dict()),
                usage_metadata=SimpleNamespace(
                    prompt_token_count=111,
                    candidates_token_count=22,
                ),
            )

    fake_genai = SimpleNamespace(
        configure=lambda api_key=None: None,
        GenerativeModel=_FakeGeminiModel,
    )

    with patch.dict("sys.modules", {
        "google": SimpleNamespace(generativeai=fake_genai),
        "google.generativeai": fake_genai,
    }):
        result = summarizer.generate_summary("gemini", "Article body.",
                                             prompt_template="X {ARTICLE_TEXT} Y")

    assert result["status"] == "success"
    assert result["input_tokens"] == 111
    assert result["output_tokens"] == 22
    assert result["structured_summary"]["sample_size"] == 42
    assert captured_generation_config["response_mime_type"] == "application/json"
    assert captured_generation_config["response_schema"] is summarizer.VeterinarySummary


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
                model="claude-opus-4-6-test",
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
                model="gpt-5.5-pdf-test",
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
    assert result["model_version"] == "gpt-5.5-pdf-test"
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

    class _FakeGeminiModel:
        def __init__(self, model_id):
            self.model_id = model_id

        def generate_content(self, parts, generation_config):
            captured["parts"] = parts
            captured["generation_config"] = generation_config
            return SimpleNamespace(
                text=json.dumps(_valid_summary_dict()),
                usage_metadata=SimpleNamespace(
                    prompt_token_count=5432,
                    candidates_token_count=234,
                ),
            )

    fake_genai = SimpleNamespace(
        configure=lambda api_key=None: None,
        upload_file=lambda path, mime_type: captured.setdefault(
            "upload", {"path": path, "mime_type": mime_type}
        ),
        GenerativeModel=_FakeGeminiModel,
    )

    with patch.dict("sys.modules", {
        "google": SimpleNamespace(generativeai=fake_genai),
        "google.generativeai": fake_genai,
    }):
        result = summarizer.generate_summary_from_pdf(
            "gemini",
            pdf_path,
            prompt_template="Summarize this: {ARTICLE_TEXT}",
        )

    assert result["status"] == "success"
    assert result["input_tokens"] == 5432
    assert result["output_tokens"] == 234
    assert captured["upload"]["path"] == str(pdf_path)
    assert captured["upload"]["mime_type"] == "application/pdf"
    assert captured["generation_config"]["response_schema"] is summarizer.VeterinarySummary
    assert "attached as a PDF" in captured["parts"][1]
