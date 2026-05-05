"""
tests/test_pdf_validation.py -- PDF text validation gate tests
=============================================================

These tests generate tiny synthetic PDFs locally.  They do not make network
calls and do not depend on publisher content, which keeps the validation gate
reproducible.
"""

import os
import sys
from pathlib import Path

import pytest


SRC_DIR = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC_DIR))

# Validation must be active in this test module.
os.environ["DRY_RUN"] = "false"
os.environ["SKIP_VALIDATION_FOR_TESTING"] = "false"
os.environ["MIN_EXTRACTED_WORDS"] = "3000"

pdfplumber = pytest.importorskip("pdfplumber")

import download  # noqa: E402


def _pdf_escape(text: str) -> str:
    """Escape text for a simple PDF literal string."""
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _write_pdf(path: Path, words: list[str]) -> None:
    """
    Write a minimal single-page text PDF that pdfplumber can extract.

    This avoids adding a PDF-generation dependency just for tests.
    """
    lines = [" ".join(words[i:i + 12]) for i in range(0, len(words), 12)]
    text_ops = ["BT", "/F1 10 Tf", "72 760 Td", "12 TL"]
    for line in lines:
        text_ops.append(f"({_pdf_escape(line)}) Tj")
        text_ops.append("T*")
    text_ops.append("ET")
    stream = "\n".join(text_ops).encode("latin-1")
    _write_pdf_with_stream(path, stream)


def _write_blank_pdf(path: Path) -> None:
    """Write a valid PDF page with no extractable text."""
    _write_pdf_with_stream(path, b"")


def _write_pdf_with_stream(path: Path, stream: bytes) -> None:
    """Write a minimal PDF around a caller-provided page content stream."""
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>"
        ),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode("ascii") + b" >>\nstream\n"
        + stream + b"\nendstream",
    ]

    chunks = [b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"]
    offsets = []
    for index, obj in enumerate(objects, start=1):
        offsets.append(sum(len(chunk) for chunk in chunks))
        chunks.append(f"{index} 0 obj\n".encode("ascii"))
        chunks.append(obj)
        chunks.append(b"\nendobj\n")

    xref_offset = sum(len(chunk) for chunk in chunks)
    chunks.append(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    chunks.append(b"0000000000 65535 f \n")
    for offset in offsets:
        chunks.append(f"{offset:010d} 00000 n \n".encode("ascii"))
    chunks.append(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_offset}\n%%EOF\n"
        ).encode("ascii")
    )

    path.write_bytes(b"".join(chunks))


def test_valid_text_pdf_passes(tmp_path: Path) -> None:
    pdf_path = tmp_path / "valid.pdf"
    _write_pdf(pdf_path, ["canine"] * 3600)

    result = download._validate_pdf_text(pdf_path)

    assert result.is_valid is True
    assert result.word_count >= download.MIN_EXTRACTED_WORDS


def test_short_text_pdf_fails(tmp_path: Path) -> None:
    pdf_path = tmp_path / "short.pdf"
    _write_pdf(pdf_path, ["feline"] * 400)

    is_valid, word_count, reason = download.validate_pdf_text(pdf_path)
    result = download._validate_pdf_text(pdf_path)

    assert is_valid is False
    assert word_count < download.MIN_EXTRACTED_WORDS
    assert result.error_type == "TEXT_TOO_SHORT"
    assert "minimum" in reason


def test_blank_pdf_has_no_extractable_text(tmp_path: Path) -> None:
    pdf_path = tmp_path / "blank.pdf"
    _write_blank_pdf(pdf_path)

    result = download._validate_pdf_text(pdf_path)

    assert result.is_valid is False
    assert result.word_count == 0
    assert result.error_type == "NO_EXTRACTABLE_TEXT"
