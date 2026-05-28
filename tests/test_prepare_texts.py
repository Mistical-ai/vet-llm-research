"""
Tests for llm-sum/prepare_texts.py — cache freshness and JSONL output.

We mock extract.extract_text_from_pdf so pdfplumber isn't required.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest

import prepare_texts
from file_paths import doi_to_slug


@pytest.fixture
def fake_pdf_and_processed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    raw = tmp_path / "raw"
    processed = tmp_path / "processed"
    raw.mkdir()
    processed.mkdir()

    monkeypatch.setattr(prepare_texts, "RAW_DIR", raw)
    monkeypatch.setattr(prepare_texts, "PROCESSED_DIR", processed)
    monkeypatch.setattr(prepare_texts, "RAW_TEXT_DIR", tmp_path / "raw_text")

    doi = "10.9999/test.0001"
    slug = doi_to_slug(doi)
    pdf_path = raw / f"{slug}.pdf"
    pdf_path.write_bytes(b"%PDF-1.4")

    return {"doi": doi, "slug": slug, "pdf_path": pdf_path,
            "processed": processed, "raw": raw}


def test_extracts_when_no_cache_exists(fake_pdf_and_processed) -> None:
    fp = fake_pdf_and_processed
    cleaned_text = "Cleaned body text from PDF." * 100  # >1000 words to avoid warning

    with patch("prepare_texts.extract_text_from_pdf", return_value=cleaned_text):
        result = prepare_texts.prepare_one_pdf(
            fp["pdf_path"], fp["doi"], {"doi": fp["doi"], "journal": "TEST"}
        )

    assert result["status"] == "extracted"
    jsonl = fp["processed"] / f"{fp['slug']}.jsonl"
    assert jsonl.exists(), ".jsonl cache file must be created"
    raw_jsonl = fp["raw"].parent / "raw_text" / f"{fp['slug']}.jsonl"
    assert raw_jsonl.exists(), "raw extracted-text cache file must be created"

    entry = json.loads(jsonl.read_text(encoding="utf-8"))
    assert entry["doi"] == fp["doi"]
    assert entry["text"] == cleaned_text
    assert entry["input_source"] == "processed"
    raw_entry = json.loads(raw_jsonl.read_text(encoding="utf-8"))
    assert raw_entry["text"] == cleaned_text
    assert raw_entry["input_source"] == "raw_text"
    assert "word_count" in entry
    assert "char_count" in entry
    assert "extracted_at" in entry


def test_cache_hit_when_jsonl_newer_than_pdf(fake_pdf_and_processed) -> None:
    fp = fake_pdf_and_processed
    jsonl = fp["processed"] / f"{fp['slug']}.jsonl"
    entry = {"doi": fp["doi"], "slug": fp["slug"], "text": "cached body"}
    jsonl.write_text(json.dumps(entry) + "\n", encoding="utf-8")
    raw_jsonl = fp["raw"].parent / "raw_text" / f"{fp['slug']}.jsonl"
    raw_jsonl.parent.mkdir()
    raw_jsonl.write_text(json.dumps(entry) + "\n", encoding="utf-8")

    # Make .jsonl mtime strictly after the PDF mtime.
    pdf_mtime = fp["pdf_path"].stat().st_mtime
    os.utime(jsonl, (pdf_mtime + 10, pdf_mtime + 10))
    os.utime(raw_jsonl, (pdf_mtime + 10, pdf_mtime + 10))

    with patch("prepare_texts.extract_text_from_pdf") as mock_extract:
        result = prepare_texts.prepare_one_pdf(fp["pdf_path"], fp["doi"], {})

    assert result["status"] == "cached"
    mock_extract.assert_not_called()


def test_re_extracts_when_pdf_newer_than_cache(fake_pdf_and_processed) -> None:
    fp = fake_pdf_and_processed
    jsonl = fp["processed"] / f"{fp['slug']}.jsonl"
    jsonl.write_text(json.dumps({"doi": fp["doi"], "text": "stale"}) + "\n",
                     encoding="utf-8")

    # Make PDF mtime newer than .jsonl.
    jsonl_mtime = jsonl.stat().st_mtime
    time.sleep(0.01)
    os.utime(fp["pdf_path"], (jsonl_mtime + 10, jsonl_mtime + 10))

    fresh_text = "fresh body text " * 100
    with patch("prepare_texts.extract_text_from_pdf", return_value=fresh_text) as m:
        result = prepare_texts.prepare_one_pdf(fp["pdf_path"], fp["doi"], {})

    assert result["status"] == "extracted"
    m.assert_called_once()
    # Verify the .jsonl was updated.
    entry = json.loads(jsonl.read_text(encoding="utf-8"))
    assert entry["text"] == fresh_text


def test_iter_processed_texts_reads_jsonl_cache(tmp_path: Path) -> None:
    processed = tmp_path / "processed"
    processed.mkdir()
    text = "Cached paper body for cost estimate."
    (processed / "slug_a.jsonl").write_text(
        json.dumps({"slug": "slug_a", "text": text}) + "\n", encoding="utf-8"
    )
    (processed / "empty.jsonl").write_text("\n", encoding="utf-8")

    rows = list(prepare_texts.iter_processed_texts(processed))
    assert rows == [("slug_a", text)]


def test_run_produces_one_jsonl_per_pdf(tmp_path: Path,
                                        monkeypatch: pytest.MonkeyPatch) -> None:
    """run() walks data/raw/*.pdf — exactly one .jsonl output per PDF."""
    raw = tmp_path / "raw"
    processed = tmp_path / "processed"
    raw.mkdir()
    processed.mkdir()
    monkeypatch.setattr(prepare_texts, "RAW_DIR", raw)
    monkeypatch.setattr(prepare_texts, "PROCESSED_DIR", processed)
    monkeypatch.setattr(prepare_texts, "RAW_TEXT_DIR", tmp_path / "raw_text")
    monkeypatch.setattr(prepare_texts, "MANIFEST_PATH", tmp_path / "manifest.jsonl")

    # Create 3 fake PDFs.
    for i in range(3):
        (raw / f"10_9999_run_{i:04d}.pdf").write_bytes(b"%PDF-1.4")

    fake_text = "Paper body text. " * 200  # well above word-count warning threshold

    with patch("prepare_texts.extract_text_from_pdf", return_value=fake_text):
        counts = prepare_texts.run(tmp_path / "manifest.jsonl", limit=None)

    assert counts["extracted"] == 3
    assert counts["cached"] == 0
    assert counts["failed"] == 0
    jsonls = list(processed.glob("*.jsonl"))
    assert len(jsonls) == 3, f"Expected 3 .jsonl files, got {len(jsonls)}"
    raw_jsonls = list((tmp_path / "raw_text").glob("*.jsonl"))
    assert len(raw_jsonls) == 3, f"Expected 3 raw_text files, got {len(raw_jsonls)}"
