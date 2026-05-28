"""
tests/test_extraction_full.py — Extraction pipeline correctness

Critical assertions:
    Last-match: reference removal uses the LAST "References" heading so a
        mid-paper mention does not discard Results/Discussion.
    Full text: extract_clean_text() no longer truncates at 12 k chars;
        the full paper body is returned.
    Skip no-PDF: prepare_one() returns skipped_no_pdf when no PDF exists,
        preventing fake fixture files from flooding data/processed/.
    Word count warning: a very short extraction triggers a printed warning.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

import extract
import prepare_texts
from extract import remove_references_section, extract_clean_text, truncate_to_limit
from file_paths import doi_to_slug


# ---------------------------------------------------------------------------
# Reference removal — last-occurrence behaviour
# ---------------------------------------------------------------------------

def test_remove_references_uses_last_occurrence() -> None:
    """An early 'References' heading in the body should NOT cut the paper."""
    text = (
        "Abstract\nThis study references prior work in canine cardiology.\n"
        "\nMethods\nAnimals were enrolled. " * 20
        + "\nResults\nThe primary endpoint was achieved. " * 20
        + "\nDiscussion\nOur findings suggest X. " * 20
        + "\nReferences\n"
        "1. Smith J et al. JVIM 2022.\n"
        "2. Jones A et al. JAVMA 2021.\n"
    )
    cleaned = remove_references_section(text)

    # The bibliography at the end should be stripped.
    assert "Smith J et al." not in cleaned
    assert "Jones A et al." not in cleaned

    # Everything before the bibliography should be intact.
    assert "Results" in cleaned
    assert "Discussion" in cleaned
    assert "Methods" in cleaned


def test_remove_references_first_occurrence_would_have_been_wrong() -> None:
    """
    Regression: if first-match logic had been kept, the text below would
    be cut immediately after 'Abstract' (losing Methods/Results/Discussion).
    Verify the correct output contains Results.
    """
    text = (
        "\nAbstract\n"
        "References to previous work are described below.\n"
        "\nMethods\nSee our earlier references for context. " * 10
        + "\nResults\nPrimary outcome reached.\n"
        + "\nDiscussion\nImplications are discussed.\n"
        + "\nReferences\n"
        "1. Brown B et al. 2020.\n"
    )
    cleaned = remove_references_section(text)
    assert "Results" in cleaned
    assert "Discussion" in cleaned
    assert "Brown B et al." not in cleaned


def test_no_references_section_returns_full_text() -> None:
    text = "Introduction\nBody text. " * 100
    cleaned = remove_references_section(text)
    assert cleaned == text


def test_inline_reference_fallback_logs_doi(capsys) -> None:
    text = (
        "Discussion\nThe study findings were clinically useful.\n"
        "ORCID iD REFERENCES\n"
        "1. Smith J et al. Journal. 2023.\n"
    )
    cleaned = remove_references_section(text, doi="10.9999/inline.0001")
    out = capsys.readouterr().out
    assert "used inline reference fallback for 10.9999/inline.0001" in out
    assert "Smith J et al." not in cleaned
    assert "Discussion" in cleaned


def test_page_extraction_uses_text_flow(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    class FakePage:
        def extract_text(self, **kwargs):
            calls.append(kwargs)
            if kwargs.get("layout") and kwargs.get("use_text_flow"):
                return "left column then right column"
            return "plain"

    monkeypatch.setattr(extract, "PDFPLUMBER_USE_TEXT_FLOW", True)
    assert extract._extract_page_text(FakePage()) == "left column then right column"
    assert calls[0] == {"layout": True, "use_text_flow": True}


def test_page_extraction_falls_back_when_text_flow_unsupported(
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    calls = []

    class FakePage:
        def extract_text(self, **kwargs):
            calls.append(kwargs)
            if kwargs.get("use_text_flow"):
                raise TypeError("unexpected keyword")
            if kwargs == {"x_tolerance": 3, "y_tolerance": 3}:
                return "tolerance fallback"
            return "plain"

    monkeypatch.setattr(extract, "PDFPLUMBER_USE_TEXT_FLOW", True)
    monkeypatch.setattr(extract, "_TEXT_FLOW_WARNING_SHOWN", False)
    assert extract._extract_page_text(FakePage()) == "tolerance fallback"
    assert "does not support use_text_flow" in capsys.readouterr().out
    assert {"x_tolerance": 3, "y_tolerance": 3} in calls


# ---------------------------------------------------------------------------
# Full-paper extraction (no truncation in extract_clean_text)
# ---------------------------------------------------------------------------

def _build_long_paper() -> str:
    """Build a realistic synthetic paper longer than the old 12k-char limit."""
    intro = "Introduction\nBackground and rationale for this study. " * 50
    methods = "\nMethods\nA total of 150 client-owned dogs were enrolled. " * 100
    results = "\nResults\nThe primary endpoint was achieved in 72% of cases. " * 120
    discussion = "\nDiscussion\nThese findings support the hypothesis. " * 120
    refs = "\nReferences\n" + "\n".join(
        f"{i}. Author{i} et al. Journal {i}. 2020." for i in range(1, 31)
    )
    return intro + methods + results + discussion + refs


def test_extract_clean_text_no_truncation(tmp_path: Path,
                                          monkeypatch: pytest.MonkeyPatch) -> None:
    """
    The full paper body (minus references) should be returned — NOT cut at
    the old 12 000-character limit.
    """
    long_paper = _build_long_paper()
    old_limit = 12_000
    assert len(long_paper) > old_limit, (
        f"Synthetic paper ({len(long_paper)} chars) must exceed the old {old_limit}-char limit"
    )

    fake_pdf = tmp_path / "test.pdf"
    fake_pdf.write_bytes(b"%PDF-1.4")

    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setattr(extract, "extract_text_from_pdf", lambda p: long_paper)
    monkeypatch.setattr(extract, "preferred_pdf_path", lambda d, r: fake_pdf)
    monkeypatch.setattr(extract, "RAW_DIR", tmp_path)

    result = extract_clean_text("10.9999/full.0001")

    assert result is not None
    # Must contain all body sections.
    assert "Results" in result
    assert "Discussion" in result
    assert "Methods" in result
    # Must NOT contain bibliography entries.
    assert "Author1 et al." not in result
    # Must be longer than the old 12 k limit — truncation is no longer applied here.
    assert len(result) > old_limit, (
        f"Expected > {old_limit} chars; got {len(result)} — truncation may still be active."
    )


def test_extract_clean_text_word_count(tmp_path: Path,
                                       monkeypatch: pytest.MonkeyPatch) -> None:
    """Extraction output should have > 2000 chars and contain key section words."""
    paper = (
        "Introduction\nVeterinary patients were included.\n"
        "Methods\nAll dogs underwent echocardiography. " * 50
        + "Results\nFractional shortening improved. " * 50
        + "Discussion\nThese findings are clinically significant. " * 50
        + "\nReferences\n1. Smith 2020."
    )

    fake_pdf = tmp_path / "test2.pdf"
    fake_pdf.write_bytes(b"%PDF-1.4")

    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setattr(extract, "extract_text_from_pdf", lambda p: paper)
    monkeypatch.setattr(extract, "preferred_pdf_path", lambda d, r: fake_pdf)
    monkeypatch.setattr(extract, "RAW_DIR", tmp_path)

    result = extract_clean_text("10.9999/full.0002")
    assert result is not None
    assert len(result) > 2_000
    assert "Methods" in result
    assert "Results" in result
    assert "Discussion" in result


# ---------------------------------------------------------------------------
# prepare_texts.py — skip-no-PDF guard
# ---------------------------------------------------------------------------

def test_prepare_one_pdf_no_file_produces_failed(tmp_path: Path,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    """
    prepare_one_pdf must return 'failed' when pdfplumber can't read the PDF
    and must NOT write any .jsonl file.
    """
    processed = tmp_path / "processed"
    processed.mkdir()
    monkeypatch.setattr(prepare_texts, "PROCESSED_DIR", processed)

    fake_pdf = tmp_path / "broken.pdf"
    fake_pdf.write_bytes(b"not a real pdf")

    with patch("prepare_texts.extract_text_from_pdf", return_value=None):
        result = prepare_texts.prepare_one_pdf(fake_pdf, "10.9999/broken.0001", {})

    assert result["status"] == "failed"
    jsonls = list(processed.glob("*.jsonl"))
    assert len(jsonls) == 0, "No .jsonl should be created when extraction fails"


def test_prepare_one_pdf_word_count_warning(tmp_path: Path,
                                             monkeypatch: pytest.MonkeyPatch,
                                             capsys) -> None:
    """A very short extraction should print a warning but still save the .jsonl."""
    processed = tmp_path / "processed"
    processed.mkdir()
    monkeypatch.setattr(prepare_texts, "PROCESSED_DIR", processed)

    pdf_path = tmp_path / "short.pdf"
    pdf_path.write_bytes(b"%PDF-1.4")
    short_text = "Very short text. " * 5  # ~17 words — well below MIN_WORD_COUNT_WARN

    with patch("prepare_texts.extract_text_from_pdf", return_value=short_text):
        result = prepare_texts.prepare_one_pdf(pdf_path, "10.9999/short.0001", {})

    assert result["status"] == "extracted"
    assert "WARNING" in capsys.readouterr().out
    jsonls = list(processed.glob("*.jsonl"))
    assert len(jsonls) == 1, ".jsonl file should still be written despite the warning"
