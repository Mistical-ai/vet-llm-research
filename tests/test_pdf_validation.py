"""
tests/test_pdf_validation.py -- PDF text validation gate tests
=============================================================

These tests generate tiny synthetic PDFs locally.  They do not make network
calls and do not depend on publisher content, which keeps the validation gate
reproducible.
"""

import importlib
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
os.environ.setdefault("MIN_PAGES", "3")
os.environ.setdefault("MIN_SECTIONS", "3")

pdfplumber = pytest.importorskip("pdfplumber")

import download  # noqa: E402


@pytest.fixture(autouse=True)
def _reload_download_after_test() -> None:
    yield
    importlib.reload(download)


def _pdf_escape(text: str) -> str:
    """Escape text for a simple PDF literal string."""
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _words_to_stream(words: list[str]) -> bytes:
    lines = [" ".join(words[i : i + 12]) for i in range(0, len(words), 12)]
    text_ops = ["BT", "/F1 10 Tf", "72 760 Td", "12 TL"]
    for line in lines:
        text_ops.append(f"({_pdf_escape(line)}) Tj")
        text_ops.append("T*")
    text_ops.append("ET")
    return "\n".join(text_ops).encode("latin-1")


def _write_pdf(path: Path, words: list[str]) -> None:
    """
    Write a minimal single-page text PDF that pdfplumber can extract.

    This avoids adding a PDF-generation dependency just for tests.
    """
    _write_pdf_with_stream(path, _words_to_stream(words))


def _write_multipage_pdf(path: Path, page_streams: list[bytes]) -> None:
    """Write a minimal N-page PDF sharing one Helvetica font."""
    n = len(page_streams)
    if n == 0:
        raise ValueError("Need at least one page stream.")

    font_id = n + 3
    kids = " ".join(f"{3 + i} 0 R" for i in range(n))
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{kids}] /Count {n} >>".encode("ascii"),
    ]
    for i in range(n):
        cid = n + 4 + i
        pg = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 {font_id} 0 R >> >> "
            f"/Contents {cid} 0 R >>"
        ).encode()
        objects.append(pg)
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    for stream in page_streams:
        objects.append(
            b"<< /Length " + str(len(stream)).encode("ascii") + b" >>\nstream\n"
            + stream
            + b"\nendstream"
        )

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
    filler = ["canine"] * 1195
    p1 = ["Introduction"] + filler
    p2 = ["Methods"] + filler
    p3 = ["Results", "Discussion"] + filler
    streams = [_words_to_stream(p1), _words_to_stream(p2), _words_to_stream(p3)]

    pdf_path = tmp_path / "valid.pdf"
    _write_multipage_pdf(pdf_path, streams)

    result = download._validate_pdf_text(pdf_path)

    assert result.is_valid is True
    assert result.word_count >= download.MIN_EXTRACTED_WORDS
    assert result.page_count >= download.MIN_PAGES


def test_pdf_rejected_too_few_pages(tmp_path: Path) -> None:
    pdf_path = tmp_path / "one_page_many_words.pdf"
    _write_pdf(pdf_path, ["canine"] * 3600)

    result = download._validate_pdf_text(pdf_path)

    assert result.is_valid is False
    assert result.error_type == "TOO_FEW_PAGES"
    assert result.page_count == 1


def test_pdf_rejected_missing_sections(tmp_path: Path) -> None:
    body = ["omission"] * 1200
    streams = [_words_to_stream(body), _words_to_stream(body), _words_to_stream(body)]
    pdf_path = tmp_path / "no_heading_labels.pdf"
    _write_multipage_pdf(pdf_path, streams)

    result = download._validate_pdf_text(pdf_path)

    assert result.is_valid is False
    assert result.error_type == "MISSING_SECTIONS"
    assert result.section_headers_found is not None


def test_short_text_pdf_fails(tmp_path: Path) -> None:
    p1 = ["Introduction"] + ["alpha"] * 133
    p2 = ["Methods"] + ["beta"] * 133
    p3 = ["Results", "Discussion"] + ["gamma"] * 132
    pdf_path = tmp_path / "short.pdf"
    _write_multipage_pdf(pdf_path, [_words_to_stream(p1), _words_to_stream(p2), _words_to_stream(p3)])

    is_valid, word_count, reason = download.validate_pdf_text(pdf_path)
    result = download._validate_pdf_text(pdf_path)

    assert is_valid is False
    assert word_count < download.MIN_EXTRACTED_WORDS
    assert result.error_type == "TEXT_TOO_SHORT"
    assert "minimum" in reason


def test_blank_pdf_has_no_extractable_text(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIN_PAGES", "1")
    monkeypatch.setenv("MIN_SECTIONS", "0")
    importlib.reload(download)

    pdf_path = tmp_path / "blank.pdf"
    _write_blank_pdf(pdf_path)

    result = download._validate_pdf_text(pdf_path)

    assert result.is_valid is False
    assert result.word_count == 0
    assert result.error_type == "NO_EXTRACTABLE_TEXT"
