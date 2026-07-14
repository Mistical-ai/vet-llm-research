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
import utils


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
    monkeypatch.setenv("PROMPT_MODE", "shared")
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


def test_shared_prompt_mode_returns_same_template_for_each_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PROMPT_MODE", "shared")

    templates, _guide_summary, _guide_path, prompt_paths = (
        summarizer.load_provider_prompt_templates_with_optional_guide(
            ["openai", "anthropic", "gemini"], None
        )
    )

    assert templates["openai"] == templates["anthropic"] == templates["gemini"]
    assert prompt_paths["openai"] == prompt_paths["anthropic"] == prompt_paths["gemini"]
    assert "{ARTICLE_TEXT}" in templates["openai"]


def test_provider_specific_prompt_mode_loads_distinct_templates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    openai_prompt = tmp_path / "openai.txt"
    anthropic_prompt = tmp_path / "anthropic.txt"
    gemini_prompt = tmp_path / "gemini.txt"
    openai_prompt.write_text("OpenAI prompt {ARTICLE_TEXT}", encoding="utf-8")
    anthropic_prompt.write_text("Anthropic prompt {ARTICLE_TEXT}", encoding="utf-8")
    gemini_prompt.write_text("Gemini prompt {ARTICLE_TEXT}", encoding="utf-8")

    monkeypatch.setenv("PROMPT_MODE", "provider_specific")
    monkeypatch.setenv("OPENAI_PROMPT_FILE", str(openai_prompt))
    monkeypatch.setenv("ANTHROPIC_PROMPT_FILE", str(anthropic_prompt))
    monkeypatch.setenv("GEMINI_PROMPT_FILE", str(gemini_prompt))

    templates, _guide_summary, _guide_path, prompt_paths = (
        summarizer.load_provider_prompt_templates_with_optional_guide(
            ["openai", "anthropic", "gemini"], None
        )
    )

    assert templates["openai"].startswith("OpenAI prompt")
    assert templates["anthropic"].startswith("Anthropic prompt")
    assert templates["gemini"].startswith("Gemini prompt")
    assert prompt_paths["openai"] == openai_prompt


def test_provider_specific_prompt_validation_requires_article_placeholder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bad_prompt = tmp_path / "bad_openai.txt"
    bad_prompt.write_text("No article placeholder here.", encoding="utf-8")

    monkeypatch.setenv("PROMPT_MODE", "provider_specific")
    monkeypatch.setenv("OPENAI_PROMPT_FILE", str(bad_prompt))

    with pytest.raises(ValueError, match="ARTICLE_TEXT"):
        summarizer.load_provider_prompt_templates_with_optional_guide(["openai"], None)


def test_summarize_all_processed_texts_routes_provider_specific_prompt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    processed_dir = tmp_path / "processed"
    processed_dir.mkdir()
    (processed_dir / "paper.jsonl").write_text(
        json.dumps({"doi": "10.9999/prompt", "slug": "paper", "text": "Clinical body."})
        + "\n",
        encoding="utf-8",
    )

    captured_prompts: dict[str, str | None] = {}

    def _fake_generate(provider: str, text: str, **kwargs) -> dict:
        captured_prompts[provider] = kwargs["prompt_template"]
        return summarizer._mock_summary(provider, text)

    monkeypatch.setattr(summarizer, "generate_summary", _fake_generate)
    monkeypatch.setattr(summarizer, "sleep_for_model", lambda _provider: None)

    summarizer.summarize_all_processed_texts(
        output_dir=tmp_path / "summaries_txt",
        processed_dir=processed_dir,
        providers=["openai", "gemini"],
        prompt_template={
            "openai": "OpenAI only {ARTICLE_TEXT}",
            "gemini": "Gemini only {ARTICLE_TEXT}",
        },
    )

    assert captured_prompts == {
        "openai": "OpenAI only {ARTICLE_TEXT}",
        "gemini": "Gemini only {ARTICLE_TEXT}",
    }


# ---------------------------------------------------------------------------
# Drift test: model_version comes from the response, not the request
# ---------------------------------------------------------------------------

def _fake_openai_response(specific_version: str = "gpt-5.4-0325-preview") -> SimpleNamespace:
    parsed = summarizer.VeterinarySummary(**_valid_summary_dict())
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(parsed=parsed))],
        usage=SimpleNamespace(prompt_tokens=1234, completion_tokens=456),
        model=specific_version,
        system_fingerprint="fp-openai-test",
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
    assert result["system_fingerprint"] == "fp-openai-test"
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
    assert "anyOf" not in json.dumps(response_schema)
    assert "title" not in json.dumps(response_schema)
    assert response_schema["properties"]["sample_size"].get("nullable") is True


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


def test_paid_run_preflight_rejects_zero_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(utils, "BUDGET_HARD_STOP", 0.0)
    with pytest.raises(SystemExit):
        utils.require_positive_budget_for_real_run(
            dry_run=False,
            context="test paid run",
        )


def test_paid_run_preflight_allows_dry_run_with_zero_budget(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(utils, "BUDGET_HARD_STOP", 0.0)
    utils.require_positive_budget_for_real_run(
        dry_run=True,
        context="test dry run",
    )


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


def test_doi_filter_restricts_run_and_ignores_paper_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A doi_filter should process only the filtered DOIs, and a paper_limit
    passed alongside it must be ignored (the filter set already defines
    exactly which/how-many papers run) — this is what lets run_phase3.py's
    dev-mode journal-random selection pick precise DOIs out of manifest.jsonl
    without fighting the sequential paper_limit slicing.
    """
    import prepare_texts

    manifest = tmp_path / "manifest.jsonl"
    processed = tmp_path / "processed"
    processed.mkdir()

    dois = [f"10.9999/filter.{i:04d}" for i in range(5)]
    with open(manifest, "w", encoding="utf-8") as f:
        for d in dois:
            f.write(json.dumps({"doi": d, "journal": "TEST"}) + "\n")

    from file_paths import doi_to_slug
    for d in dois:
        slug = doi_to_slug(d)
        entry = {"doi": d, "slug": slug, "text": "Body of paper. " * 40}
        (processed / f"{slug}.jsonl").write_text(json.dumps(entry) + "\n", encoding="utf-8")

    monkeypatch.setattr(summarizer, "PROCESSED_DIR", processed)
    monkeypatch.setattr(prepare_texts, "PROCESSED_DIR", processed)
    monkeypatch.setattr(summarizer, "SUMMARIES_PATH", tmp_path / "summaries.jsonl")

    selected = {dois[1], dois[3]}
    counts = summarizer.run_realtime(
        manifest_path=manifest,
        providers=["openai"],
        paper_limit=1,  # would cap at 1 paper if it weren't ignored
        doi_filter=selected,
    )

    assert counts["success"] == 2

    rows = [
        json.loads(line)
        for line in (tmp_path / "summaries.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert {row["doi"] for row in rows} == selected


def test_write_dev_summary_jsonl_outputs_writes_one_file_per_doi(
    tmp_path: Path,
) -> None:
    """
    write_dev_summary_jsonl_outputs should render exactly one readable .txt
    per matching (doi, input_source) pair from data/summaries.jsonl, named by
    descriptive_stem, with every configured provider represented.
    """
    from file_paths import descriptive_stem

    summaries_path = tmp_path / "summaries.jsonl"
    output_dir = tmp_path / "dev_summaries_jsonl"

    keep_entry = {
        "doi": "10.9999/dev.0001",
        "input_source": "processed",
        "journal": "jvim",
        "title": "A paper about renal disease",
        "species": "canine",
        "study_design": "retrospective",
        "clinical_topic": "nephrology",
        "models": {
            "openai": {
                "status": "success",
                "summary": "fallback text",
                "structured_summary": {"objective": "Study the thing."},
                "input_tokens": 100,
                "output_tokens": 50,
                "model_version": "gpt-5.4-test",
                "timestamp": "2026-07-13T00:00:00+00:00",
            },
            "anthropic": {
                "status": "failed",
                "error": "simulated failure",
                "summary": None,
                "input_tokens": None,
                "output_tokens": None,
                "model_version": None,
                "timestamp": "2026-07-13T00:00:00+00:00",
            },
        },
    }
    other_doi_entry = {**keep_entry, "doi": "10.9999/dev.0002"}
    other_source_entry = {**keep_entry, "input_source": "raw_text"}

    with open(summaries_path, "w", encoding="utf-8") as f:
        for entry in (keep_entry, other_doi_entry, other_source_entry):
            f.write(json.dumps(entry) + "\n")

    written = summarizer.write_dev_summary_jsonl_outputs(
        {keep_entry["doi"]},
        output_dir=output_dir,
        input_source="processed",
        summaries_path=summaries_path,
    )

    assert written == 1
    files = list(output_dir.glob("*.txt"))
    assert len(files) == 1
    assert files[0].stem == descriptive_stem(keep_entry)

    content = files[0].read_text(encoding="utf-8")
    assert "Journal: jvim" in content
    assert "OPENAI SUMMARY" in content
    assert "ANTHROPIC SUMMARY" in content
    assert "No readable summary was produced. Error: simulated failure" in content


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
                system_fingerprint="fp-pdf-test",
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
    assert result["system_fingerprint"] == "fp-pdf-test"
    assert captured["file_purpose"] == "user_data"
    assert captured["file_name"] == "paper.pdf"
    content = captured["response_kwargs"]["input"][0]["content"]
    assert content[0] == {"type": "input_file", "file_id": "file_test_pdf"}
    assert content[1]["type"] == "input_text"
    assert "attached as a PDF" in content[1]["text"]
    assert captured["response_kwargs"]["text_format"] is summarizer.VeterinarySummary
    assert "seed" not in captured["response_kwargs"]


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
    assert (
        captured["generation_config"]["max_output_tokens"]
        == summarizer.GEMINI_MAX_OUTPUT_TOKENS
    )
    response_schema = captured["generation_config"]["response_schema"]
    assert isinstance(response_schema, dict)
    assert "default" not in json.dumps(response_schema)
    assert captured["contents"][0].name == "uploaded-paper"
    assert "attached as a PDF" in captured["contents"][1]


def test_gemini_pdf_call_uses_parsed_schema_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("PHASE3_MODE", "single")
    monkeypatch.setattr(summarizer, "DRY_RUN", False)

    pdf_path = tmp_path / "paper.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 direct pdf")

    class _FakeFiles:
        def upload(self, file):
            return SimpleNamespace(name="uploaded-paper")

    class _FakeModels:
        def generate_content(self, model, contents, config):
            return SimpleNamespace(
                parsed=_valid_summary_dict(),
                text='{"headline": "truncated',
                usage_metadata=SimpleNamespace(
                    prompt_token_count=5432,
                    candidates_token_count=234,
                ),
            )

    class _FakeGeminiClient:
        def __init__(self, api_key=None):
            self.files = _FakeFiles()
            self.models = _FakeModels()

    class _FakeTypes:
        @staticmethod
        def GenerateContentConfig(**kwargs):
            return kwargs

    fake_genai = SimpleNamespace(Client=_FakeGeminiClient, types=_FakeTypes)

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
    assert result["structured_summary"]["headline"] == _valid_summary_dict()["headline"]


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
    assert counts.budget_spent > 0

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


def test_summarize_all_outputs_can_use_shared_suffix(tmp_path: Path) -> None:
    """A summarize-all CLI run can create paired unique files in both output folders."""
    processed_dir = tmp_path / "processed"
    raw_dir = tmp_path / "raw"
    processed_dir.mkdir()
    raw_dir.mkdir()

    stem = "journal__study_title__10_9999_suffix"
    suffix = "run_20260617T131100000000Z"
    (raw_dir / f"{stem}.pdf").write_bytes(b"%PDF-1.4")
    (processed_dir / f"{stem}.jsonl").write_text(
        json.dumps({
            "doi": "10.9999/suffix",
            "slug": "10_9999_suffix",
            "text": "Clinical trial body. " * 30,
        }) + "\n",
        encoding="utf-8",
    )

    pdf_output = tmp_path / "summaries_pdf"
    txt_output = tmp_path / "summaries_txt"
    summarizer.summarize_all_pdfs(
        output_dir=pdf_output,
        raw_dir=raw_dir,
        providers=["openai"],
        stems={stem},
        output_suffix=suffix,
    )
    summarizer.summarize_all_processed_texts(
        output_dir=txt_output,
        processed_dir=processed_dir,
        providers=["openai"],
        stems={stem},
        output_suffix=suffix,
    )

    assert (pdf_output / f"{stem}__{suffix}.txt").exists()
    assert (txt_output / f"{stem}__{suffix}.txt").exists()
    assert not (pdf_output / f"{stem}.txt").exists()
    assert not (txt_output / f"{stem}.txt").exists()


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
    assert counts.budget_spent > 0

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

def test_run_phase3_paired_summary_stems_randomly_samples(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default one-pair runs should not always select the first sorted article."""
    import run_phase3

    raw_dir = tmp_path / "raw"
    processed_dir = tmp_path / "processed"
    raw_dir.mkdir()
    processed_dir.mkdir()
    for stem in ("alpha", "beta", "gamma"):
        (raw_dir / f"{stem}.pdf").write_bytes(b"%PDF-1.4")
        (processed_dir / f"{stem}.jsonl").write_text(
            json.dumps({"text": "body"}) + "\n", encoding="utf-8"
        )

    captured: dict[str, object] = {}

    def _fake_sample(population, k):
        captured["population"] = population
        captured["k"] = k
        return ["gamma"]

    monkeypatch.setattr(run_phase3, "RAW_DIR", raw_dir)
    monkeypatch.setattr(run_phase3, "PROCESSED_DIR", processed_dir)
    monkeypatch.setattr(run_phase3.random, "sample", _fake_sample)
    monkeypatch.setenv("SUMMARIZE_ALL_RANDOM_MATCH", "true")

    assert run_phase3._paired_summary_stems(1) == {"gamma"}
    assert captured["population"] == ["alpha", "beta", "gamma"]
    assert captured["k"] == 1


def test_run_phase3_paired_summary_stems_can_be_deterministic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Env/config can keep choosing the first sorted matched article."""
    import run_phase3

    raw_dir = tmp_path / "raw"
    processed_dir = tmp_path / "processed"
    raw_dir.mkdir()
    processed_dir.mkdir()
    for stem in ("alpha", "beta", "gamma"):
        (raw_dir / f"{stem}.pdf").write_bytes(b"%PDF-1.4")
        (processed_dir / f"{stem}.jsonl").write_text(
            json.dumps({"text": "body"}) + "\n", encoding="utf-8"
        )

    def _unexpected_sample(_population, _k):
        raise AssertionError("random.sample should not be called")

    monkeypatch.setattr(run_phase3, "RAW_DIR", raw_dir)
    monkeypatch.setattr(run_phase3, "PROCESSED_DIR", processed_dir)
    monkeypatch.setattr(run_phase3.random, "sample", _unexpected_sample)
    monkeypatch.setenv("SUMMARIZE_ALL_RANDOM_MATCH", "false")

    assert run_phase3._paired_summary_stems(1) == {"alpha"}
    assert run_phase3._paired_summary_stems(2) == {"alpha", "beta"}


def test_run_phase3_summarize_all_single_mode_limits_both_sources(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """single mode means 1 PDF + 1 processed text, for 6 summaries total."""
    import run_phase3

    captured: dict[str, object] = {}

    def _fake_load_prompts(_providers=None, _path=None):
        return {"openai": "Prompt {ARTICLE_TEXT}"}, None, Path("guide.txt"), {}

    def _fake_confirm(_profile, force=False):
        return True

    def _fake_pdfs(**kwargs):
        captured["pdf_limit"] = kwargs["limit"]
        captured["pdf_suffix"] = kwargs["output_suffix"]
        return summarizer.SummaryRunStats(
            {"success": 3, "failed": 0, "skipped": 0, "no_source": 0},
            budget_spent=1.25,
        )

    def _fake_texts(**kwargs):
        captured["txt_limit"] = kwargs["limit"]
        captured["txt_stems"] = kwargs["stems"]
        captured["txt_suffix"] = kwargs["output_suffix"]
        return summarizer.SummaryRunStats(
            {"success": 3, "failed": 0, "skipped": 0, "no_source": 0},
            budget_spent=0.75,
        )

    monkeypatch.setattr(
        summarizer,
        "load_provider_prompt_templates_with_optional_guide",
        _fake_load_prompts,
    )
    monkeypatch.setattr(summarizer, "confirm_real_batch", _fake_confirm)
    monkeypatch.setattr(summarizer, "summarize_all_pdfs", _fake_pdfs)
    monkeypatch.setattr(summarizer, "summarize_all_processed_texts", _fake_texts)
    monkeypatch.setattr(
        run_phase3,
        "_paired_summary_stems",
        lambda _limit, **_kwargs: {"same_article"},
    )
    monkeypatch.setattr(run_phase3, "_summary_run_suffix", lambda: "run_test")

    ret = run_phase3.main(["summarize-all", "--mode", "single"])
    assert ret == 0
    assert captured["pdf_limit"] == 1
    assert captured["txt_limit"] == 1
    assert captured["txt_stems"] == {"same_article"}
    assert captured["pdf_suffix"] == "run_test"
    assert captured["txt_suffix"] == "run_test"
    out = capsys.readouterr().out
    assert "output run suffix: run_test" in out
    assert "[phase3:summarize-all] COSTS" in out
    assert "PDF/raw summaries:          $1.2500" in out
    assert "Processed JSONL summaries:  $0.7500" in out
    assert "Combined total:             $2.0000" in out


def test_run_phase3_summarize_all_can_disable_unique_output(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """SUMMARIZE_ALL_UNIQUE_OUTPUT=false writes legacy stem.txt filenames."""
    import run_phase3

    captured: dict[str, object] = {}

    def _fake_load_prompts(_providers=None, _path=None):
        return {"openai": "Prompt {ARTICLE_TEXT}"}, None, Path("guide.txt"), {}

    def _fake_pdfs(**kwargs):
        captured["pdf_suffix"] = kwargs["output_suffix"]
        return {"success": 3, "failed": 0, "skipped": 0, "no_source": 0}

    def _fake_texts(**kwargs):
        captured["txt_suffix"] = kwargs["output_suffix"]
        return {"success": 3, "failed": 0, "skipped": 0, "no_source": 0}

    def _unexpected_suffix():
        raise AssertionError("unique suffix should not be generated")

    monkeypatch.setenv("SUMMARIZE_ALL_UNIQUE_OUTPUT", "false")
    monkeypatch.setattr(
        summarizer,
        "load_provider_prompt_templates_with_optional_guide",
        _fake_load_prompts,
    )
    monkeypatch.setattr(summarizer, "summarize_all_pdfs", _fake_pdfs)
    monkeypatch.setattr(summarizer, "summarize_all_processed_texts", _fake_texts)
    monkeypatch.setattr(
        run_phase3,
        "_paired_summary_stems",
        lambda _limit, **_kwargs: {"same_article"},
    )
    monkeypatch.setattr(run_phase3, "_summary_run_suffix", _unexpected_suffix)

    ret = run_phase3.main(["summarize-all", "--mode", "test"])
    assert ret == 0
    assert captured["pdf_suffix"] is None
    assert captured["txt_suffix"] is None
    out = capsys.readouterr().out
    assert "unique output disabled; writing <stem>.txt files" in out


def test_run_phase3_summarize_all_test_mode_defaults_to_one_pair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mock summarize-all should not create one file per corpus article by default."""
    import run_phase3

    captured: dict[str, object] = {}

    def _fake_load_prompts(_providers=None, _path=None):
        return {"openai": "Prompt {ARTICLE_TEXT}"}, None, Path("guide.txt"), {}

    def _fake_pdfs(**kwargs):
        captured["pdf_limit"] = kwargs["limit"]
        captured["pdf_stems"] = kwargs["stems"]
        captured["pdf_output_dir"] = kwargs["output_dir"]
        return {"success": 3, "failed": 0, "skipped": 0, "no_source": 0}

    def _fake_texts(**kwargs):
        captured["txt_limit"] = kwargs["limit"]
        captured["txt_stems"] = kwargs["stems"]
        captured["txt_output_dir"] = kwargs["output_dir"]
        return {"success": 3, "failed": 0, "skipped": 0, "no_source": 0}

    monkeypatch.setattr(
        summarizer,
        "load_provider_prompt_templates_with_optional_guide",
        _fake_load_prompts,
    )
    monkeypatch.setattr(summarizer, "summarize_all_pdfs", _fake_pdfs)
    monkeypatch.setattr(summarizer, "summarize_all_processed_texts", _fake_texts)
    monkeypatch.setattr(
        run_phase3,
        "_paired_summary_stems",
        lambda _limit, **_kwargs: {"same_article"},
    )

    ret = run_phase3.main(["summarize-all", "--mode", "test"])
    assert ret == 0
    assert captured["pdf_limit"] == 1
    assert captured["txt_limit"] == 1
    assert captured["pdf_stems"] == {"same_article"}
    assert captured["txt_stems"] == {"same_article"}
    assert captured["pdf_output_dir"] == run_phase3.DATA_DIR / "summaries_pdf"
    assert captured["txt_output_dir"] == run_phase3.DATA_DIR / "summaries_txt"


def test_run_phase3_summarize_all_dev_mode_can_use_regular_outputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--output-set regular keeps a dev comparison in the original output folders."""
    import run_phase3

    captured: dict[str, object] = {}

    def _fake_load_prompts(_providers=None, _path=None):
        return {"openai": "Prompt {ARTICLE_TEXT}"}, None, Path("guide.txt"), {}

    def _fake_confirm(_profile, force=False):
        return True

    def _fake_pdfs(**kwargs):
        captured["pdf_output_dir"] = kwargs["output_dir"]
        return {"success": 3, "failed": 0, "skipped": 0, "no_source": 0}

    def _fake_texts(**kwargs):
        captured["txt_output_dir"] = kwargs["output_dir"]
        return {"success": 3, "failed": 0, "skipped": 0, "no_source": 0}

    monkeypatch.setattr(
        summarizer,
        "load_provider_prompt_templates_with_optional_guide",
        _fake_load_prompts,
    )
    monkeypatch.setattr(summarizer, "confirm_real_batch", _fake_confirm)
    monkeypatch.setattr(summarizer, "summarize_all_pdfs", _fake_pdfs)
    monkeypatch.setattr(summarizer, "summarize_all_processed_texts", _fake_texts)
    monkeypatch.setattr(
        run_phase3,
        "_paired_summary_stems",
        lambda _limit, **_kwargs: {"same_article"},
    )

    ret = run_phase3.main([
        "summarize-all",
        "--mode",
        "dev",
        "--output-set",
        "regular",
    ])

    assert ret == 0
    assert captured["pdf_output_dir"] == run_phase3.DATA_DIR / "summaries_pdf"
    assert captured["txt_output_dir"] == run_phase3.DATA_DIR / "summaries_txt"


def test_run_phase3_summarize_all_env_can_use_regular_outputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SUMMARIZE_ALL_OUTPUT_SET in .env can send dev outputs to regular folders."""
    import run_phase3

    captured: dict[str, object] = {}

    def _fake_load_prompts(_providers=None, _path=None):
        return {"openai": "Prompt {ARTICLE_TEXT}"}, None, Path("guide.txt"), {}

    def _fake_confirm(_profile, force=False):
        return True

    def _fake_pdfs(**kwargs):
        captured["pdf_output_dir"] = kwargs["output_dir"]
        return {"success": 3, "failed": 0, "skipped": 0, "no_source": 0}

    def _fake_texts(**kwargs):
        captured["txt_output_dir"] = kwargs["output_dir"]
        return {"success": 3, "failed": 0, "skipped": 0, "no_source": 0}

    monkeypatch.setenv("SUMMARIZE_ALL_OUTPUT_SET", "regular")
    monkeypatch.setattr(
        summarizer,
        "load_provider_prompt_templates_with_optional_guide",
        _fake_load_prompts,
    )
    monkeypatch.setattr(summarizer, "confirm_real_batch", _fake_confirm)
    monkeypatch.setattr(summarizer, "summarize_all_pdfs", _fake_pdfs)
    monkeypatch.setattr(summarizer, "summarize_all_processed_texts", _fake_texts)
    monkeypatch.setattr(
        run_phase3,
        "_paired_summary_stems",
        lambda _limit, **_kwargs: {"same_article"},
    )

    ret = run_phase3.main(["summarize-all", "--mode", "dev"])

    assert ret == 0
    assert captured["pdf_output_dir"] == run_phase3.DATA_DIR / "summaries_pdf"
    assert captured["txt_output_dir"] == run_phase3.DATA_DIR / "summaries_txt"


def test_run_phase3_summarize_all_dev_mode_uses_phase3_dev_limit(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """summarize-all dev mode uses PHASE3_DEV_LIMIT matched article pairs."""
    import run_phase3

    captured: dict[str, object] = {}
    matched_stems = {f"article_{i}" for i in range(5)}

    def _fake_load_prompts(_providers=None, _path=None):
        return {"openai": "Prompt {ARTICLE_TEXT}"}, None, Path("guide.txt"), {}

    def _fake_confirm(_profile, force=False):
        return True

    def _fake_pdfs(**kwargs):
        captured["pdf_limit"] = kwargs["limit"]
        captured["pdf_stems"] = kwargs["stems"]
        captured["pdf_output_dir"] = kwargs["output_dir"]
        return {"success": 3, "failed": 0, "skipped": 0, "no_source": 0}

    def _fake_texts(**kwargs):
        captured["txt_limit"] = kwargs["limit"]
        captured["txt_stems"] = kwargs["stems"]
        captured["txt_output_dir"] = kwargs["output_dir"]
        return {"success": 3, "failed": 0, "skipped": 0, "no_source": 0}

    monkeypatch.setattr(
        summarizer,
        "load_provider_prompt_templates_with_optional_guide",
        _fake_load_prompts,
    )
    monkeypatch.setattr(summarizer, "confirm_real_batch", _fake_confirm)
    monkeypatch.setattr(summarizer, "summarize_all_pdfs", _fake_pdfs)
    monkeypatch.setattr(summarizer, "summarize_all_processed_texts", _fake_texts)
    monkeypatch.setenv("PHASE3_DEV_LIMIT", "5")

    def _fake_paired_summary_stems(limit, **_kwargs):
        captured["paired_limit"] = limit
        return matched_stems

    monkeypatch.setattr(run_phase3, "_paired_summary_stems", _fake_paired_summary_stems)

    ret = run_phase3.main(["summarize-all", "--mode", "dev"])
    assert ret == 0
    assert captured["paired_limit"] == 5
    assert captured["pdf_limit"] == 5
    assert captured["txt_limit"] == 5
    assert captured["pdf_stems"] == matched_stems
    assert captured["txt_stems"] == matched_stems
    assert captured["pdf_output_dir"] == run_phase3.DATA_DIR / "dev_tests" / "summaries_pdf"
    assert captured["txt_output_dir"] == run_phase3.DATA_DIR / "dev_tests" / "summaries_txt"
    out = capsys.readouterr().out
    assert "dev mode defaults to PHASE3_DEV_LIMIT=5 matched article pairs" in out


def test_run_phase3_summarize_all_warns_when_fewer_pairs_than_requested(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The CLI explains when the paired corpus has fewer matches than requested."""
    import run_phase3

    def _fake_load_prompts(_providers=None, _path=None):
        return {"openai": "Prompt {ARTICLE_TEXT}"}, None, Path("guide.txt"), {}

    def _fake_confirm(_profile, force=False):
        return True

    def _fake_pdfs(**_kwargs):
        return {"success": 6, "failed": 0, "skipped": 0, "no_source": 0}

    def _fake_texts(**_kwargs):
        return {"success": 6, "failed": 0, "skipped": 0, "no_source": 0}

    monkeypatch.setattr(
        summarizer,
        "load_provider_prompt_templates_with_optional_guide",
        _fake_load_prompts,
    )
    monkeypatch.setattr(summarizer, "confirm_real_batch", _fake_confirm)
    monkeypatch.setattr(summarizer, "summarize_all_pdfs", _fake_pdfs)
    monkeypatch.setattr(summarizer, "summarize_all_processed_texts", _fake_texts)
    monkeypatch.setenv("PHASE3_DEV_LIMIT", "5")
    monkeypatch.setattr(
        run_phase3,
        "_paired_summary_stems",
        lambda _limit, **_kwargs: {"article_1", "article_2"},
    )

    ret = run_phase3.main(["summarize-all", "--mode", "dev"])

    assert ret == 0
    out = capsys.readouterr().out
    assert "WARNING: requested 5 matched pairs, but only found 2" in out
    assert "Only stems present in both data/raw/*.pdf and data/processed/*.jsonl" in out


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
