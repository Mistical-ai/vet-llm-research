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


# ---------------------------------------------------------------------------
# DRY_RUN mock summary shape
# ---------------------------------------------------------------------------

def test_dry_run_summary_has_full_schema() -> None:
    result = summarizer.generate_summary("openai", "The cat sat on the mat. " * 50)
    assert result["status"] == "success"
    assert isinstance(result["summary"], str) and result["summary"].startswith("[MOCK")
    assert isinstance(result["input_tokens"], int)
    assert isinstance(result["output_tokens"], int)
    assert "DRYRUN" in result["model_version"]
    assert "timestamp" in result


# ---------------------------------------------------------------------------
# Drift test: model_version comes from the response, not the request
# ---------------------------------------------------------------------------

def _fake_openai_response(specific_version: str = "gpt-5.5-0325-preview") -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="summary text here"))],
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

    class _FakeClient:
        def __init__(self): self.chat = SimpleNamespace(completions=self)
        def create(self, **_kw): return fake_response

    with patch.dict("sys.modules", {"openai": SimpleNamespace(OpenAI=_FakeClient)}):
        result = summarizer.generate_summary("openai", "Article body.",
                                             prompt_template="X {ARTICLE_TEXT} Y")

    assert result["status"] == "success"
    # Exact-version string preserved — NOT the alias "gpt-5.5".
    assert result["model_version"] == "gpt-5.5-0325-preview"
    assert result["model_version"] != "gpt-5"
    assert result["input_tokens"] == 1234
    assert result["output_tokens"] == 456


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
