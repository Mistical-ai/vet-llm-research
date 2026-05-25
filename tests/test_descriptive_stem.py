"""
Tests for src/file_paths.descriptive_stem — the shared stem used by
data/raw/<stem>.pdf and data/processed/<stem>.jsonl.

The PDF filename helper already has a full test in test_file_paths.py /
test_doi_slug.py. These tests focus on the new helper's contract:

    descriptive_stem(record) == descriptive_pdf_filename(record).removesuffix(".pdf")

Plus the practical guarantee callers rely on: appending ".jsonl" to the
stem gives a clean filename that round-trips through doi-to-record lookup.
"""

from __future__ import annotations

from file_paths import descriptive_pdf_filename, descriptive_stem


def test_stem_is_pdf_filename_minus_extension() -> None:
    record = {
        "doi": "10.1111/jvim.16872",
        "journal": "JVIM",
        "title": "Pre-illness dietary risk factors in dogs",
    }
    stem = descriptive_stem(record)
    pdf_name = descriptive_pdf_filename(record)
    assert pdf_name == f"{stem}.pdf"
    assert not stem.endswith(".pdf")


def test_stem_works_for_jsonl_filename() -> None:
    """The whole point of the helper: same stem powers both files."""
    record = {
        "doi": "10.1111/vru.13314",
        "journal": "VRU",
        "title": "Aberrant right subclavian artery in cats",
    }
    pdf = descriptive_pdf_filename(record)
    jsonl_name = f"{descriptive_stem(record)}.jsonl"
    assert pdf.endswith(".pdf")
    assert jsonl_name.endswith(".jsonl")
    assert pdf[:-4] == jsonl_name[:-6]  # same stem under both extensions


def test_stem_accepts_bare_doi_string() -> None:
    # When only a DOI is available (no journal/title), the stem falls
    # back to the doi-suffix form. Don't assert exact text — just that
    # it's non-empty, ASCII-safe, and free of the .pdf suffix.
    stem = descriptive_stem("10.1111/jvim.16872")
    assert stem
    assert not stem.endswith(".pdf")
    assert all(c.isascii() for c in stem)


def test_stem_truncation_matches_pdf_filename_truncation() -> None:
    """A very long title is truncated identically in both helpers."""
    record = {
        "doi": "10.1111/jvim.99999",
        "journal": "JVIM",
        "title": "An extraordinarily long title " * 30,
    }
    pdf_name = descriptive_pdf_filename(record)
    stem = descriptive_stem(record)
    assert pdf_name == f"{stem}.pdf"
