import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

SRC_DIR = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC_DIR))

import enrich_manifest_from_pdfs as enrich  # noqa: E402
from auto_ingest_workflow import _retry_failed_pdfs  # noqa: E402
from ingest_manual_pdfs import _doi_from_filename, _gather_dois_from_pdf  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_crossref_item(doi="10.1111/test.001", title="Test Article") -> dict:
    """Minimal CrossRef /works/{doi} message dict."""
    return {
        "DOI": doi,
        "title": [title],
        "container-title": ["Journal of Veterinary Testing"],
        "ISSN": ["1234-5678"],
        "published": {"date-parts": [[2024, 1, 15]]},
        "author": [{"family": "Smith", "given": "J"}],
        "abstract": "This is a test abstract about canine health.",
        "type": "journal-article",
    }


# ---------------------------------------------------------------------------
# _item_to_manifest_record
# ---------------------------------------------------------------------------

def test_item_to_manifest_record_maps_all_fields() -> None:
    item = _make_crossref_item()
    record = enrich._item_to_manifest_record(item, "10.1111/test.001")

    assert record is not None
    assert record["doi"] == "10.1111/test.001"
    assert "test article" in record["title"].lower()
    assert record["year"] == 2024
    assert record["pub_year"] == 2024
    assert record["journal"] == "Journal of Veterinary Testing"
    assert record["issn"] == "1234-5678"
    # OA stub fields must be present
    assert record["is_oa"] is False
    assert record["oa_locations_count"] == 0
    assert record["has_direct_pdf_url"] is False
    assert record["candidate_quality_score"] == 0


def test_item_to_manifest_record_missing_container_title() -> None:
    item = _make_crossref_item()
    item["container-title"] = []

    record = enrich._item_to_manifest_record(item, "10.1111/test.001")

    assert record is not None
    assert record["journal"] == "Unknown Journal"


def test_item_to_manifest_record_missing_issn() -> None:
    item = _make_crossref_item()
    item["ISSN"] = []

    record = enrich._item_to_manifest_record(item, "10.1111/test.001")

    assert record is not None
    assert record["issn"] == ""


# ---------------------------------------------------------------------------
# _fetch_crossref_single
# ---------------------------------------------------------------------------

def test_fetch_crossref_single_404() -> None:
    mock_resp = MagicMock()
    mock_resp.status_code = 404

    with patch("requests.get", return_value=mock_resp):
        result = enrich._fetch_crossref_single("10.9999/fake", mailto="test@example.com")

    assert result is None


def test_fetch_crossref_single_timeout() -> None:
    with patch("requests.get", side_effect=requests.exceptions.Timeout):
        result = enrich._fetch_crossref_single("10.9999/fake", mailto="test@example.com")

    assert result is None


def test_fetch_crossref_single_network_error() -> None:
    with patch("requests.get", side_effect=requests.exceptions.ConnectionError("refused")):
        result = enrich._fetch_crossref_single("10.9999/fake", mailto="test@example.com")

    assert result is None


def test_fetch_crossref_single_success() -> None:
    item = _make_crossref_item()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"status": "ok", "message": item}
    mock_resp.raise_for_status = MagicMock()

    with patch("requests.get", return_value=mock_resp):
        result = enrich._fetch_crossref_single("10.1111/test.001", mailto="test@example.com")

    assert result == item


# ---------------------------------------------------------------------------
# enrich_manifest_from_folder
# ---------------------------------------------------------------------------

def test_enrich_manifest_skips_existing_dois(tmp_path: Path) -> None:
    doi = "10.1111/already.exists"
    manifest = tmp_path / "manifest.jsonl"
    manifest.write_text(json.dumps({"doi": doi, "title": "Existing Paper"}) + "\n", encoding="utf-8")

    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")

    with (
        patch.object(enrich, "_get_best_doi_for_enrichment", return_value=(doi, "metadata")),
        patch.object(enrich, "_fetch_crossref_single") as mock_fetch,
    ):
        _, _, appended = enrich.enrich_manifest_from_folder(
            tmp_path, manifest, mailto="test@example.com"
        )

    assert appended == 0
    mock_fetch.assert_not_called()


def test_enrich_manifest_appends_new_entry(tmp_path: Path) -> None:
    doi = "10.1111/new.paper"
    manifest = tmp_path / "manifest.jsonl"
    manifest.write_text("", encoding="utf-8")  # empty manifest

    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")

    crossref_item = _make_crossref_item(doi=doi)

    with (
        patch.object(enrich, "_get_best_doi_for_enrichment", return_value=(doi, "metadata")),
        patch.object(enrich, "_fetch_crossref_single", return_value=crossref_item),
        patch("time.sleep"),  # skip rate-limit delay
    ):
        _, _, appended = enrich.enrich_manifest_from_folder(
            tmp_path, manifest, mailto="test@example.com"
        )

    assert appended == 1
    lines = manifest.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["doi"] == doi


def test_enrich_manifest_dry_run_does_not_write(tmp_path: Path) -> None:
    doi = "10.1111/dry.run"
    manifest = tmp_path / "manifest.jsonl"
    manifest.write_text("", encoding="utf-8")

    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")

    crossref_item = _make_crossref_item(doi=doi)

    with (
        patch.object(enrich, "_get_best_doi_for_enrichment", return_value=(doi, "metadata")),
        patch.object(enrich, "_fetch_crossref_single", return_value=crossref_item),
        patch("time.sleep"),
    ):
        _, _, appended = enrich.enrich_manifest_from_folder(
            tmp_path, manifest, mailto="test@example.com", dry_run=True
        )

    assert appended == 0
    assert manifest.read_text(encoding="utf-8") == ""


def test_enrich_manifest_no_doi_found_skips_crossref(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.jsonl"
    manifest.write_text("", encoding="utf-8")

    pdf = tmp_path / "scan.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")

    with (
        patch.object(enrich, "_get_best_doi_for_enrichment", return_value=(None, "none")),
        patch.object(enrich, "_fetch_crossref_single") as mock_fetch,
    ):
        _, dois_found, appended = enrich.enrich_manifest_from_folder(
            tmp_path, manifest, mailto="test@example.com"
        )

    assert dois_found == 0
    assert appended == 0
    mock_fetch.assert_not_called()


# ---------------------------------------------------------------------------
# _retry_failed_pdfs
# ---------------------------------------------------------------------------

def test_retry_failed_pdfs_moves_files(tmp_path: Path) -> None:
    failed_dir = tmp_path / "failed"
    failed_dir.mkdir()
    inbox_dir = tmp_path / "inbox"
    inbox_dir.mkdir()

    pdf = failed_dir / "article.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")

    retried = _retry_failed_pdfs(failed_dir, inbox_dir)

    assert len(retried) == 1
    assert (inbox_dir / "article.pdf").exists()
    assert not pdf.exists()


def test_retry_failed_pdfs_empty_dir(tmp_path: Path) -> None:
    failed_dir = tmp_path / "failed"
    failed_dir.mkdir()
    inbox_dir = tmp_path / "inbox"
    inbox_dir.mkdir()

    retried = _retry_failed_pdfs(failed_dir, inbox_dir)

    assert retried == []


def test_retry_failed_pdfs_missing_dir(tmp_path: Path) -> None:
    failed_dir = tmp_path / "failed"  # does not exist
    inbox_dir = tmp_path / "inbox"
    inbox_dir.mkdir()

    retried = _retry_failed_pdfs(failed_dir, inbox_dir)

    assert retried == []


# ---------------------------------------------------------------------------
# _doi_from_filename / _get_best_doi_for_enrichment
# ---------------------------------------------------------------------------

def test_doi_from_filename_valid() -> None:
    doi = _doi_from_filename(Path("10.1177_1098612X231170159.pdf"))
    assert doi == "10.1177/1098612x231170159"


def test_doi_from_filename_invalid() -> None:
    doi = _doi_from_filename(Path("some_random_paper.pdf"))
    assert doi is None


def test_gather_dois_prefers_filename_when_pdf_unreadable(tmp_path: Path) -> None:
    pdf = tmp_path / "10.1177_1098612X231170159.pdf"
    pdf.write_bytes(b"not a real pdf")

    dois, title, trace = _gather_dois_from_pdf(pdf)

    assert dois == ["10.1177/1098612x231170159"]
    assert title is None
    assert "filename" in trace


def test_get_best_doi_falls_back_to_filename(tmp_path: Path) -> None:
    pdf = tmp_path / "10.1177_1098612X231170159.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")

    with patch.object(enrich, "_get_pdf_metadata_doi", return_value=None):
        doi, source = enrich._get_best_doi_for_enrichment(pdf)

    assert doi == "10.1177/1098612x231170159"
    assert source == "filename"


def test_get_best_doi_falls_back_to_frequency_scan_multi_page(tmp_path: Path) -> None:
    """DOI appearing on multiple pages (header/footer) is returned with freq label."""
    pdf = tmp_path / "no_doi_in_name.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")

    with (
        patch.object(enrich, "_get_pdf_metadata_doi", return_value=None),
        patch.object(
            enrich,
            "_get_page_dois_with_frequency",
            return_value=[("10.1111/paper.doi", 4), ("10.9999/ref.doi", 1)],
        ),
    ):
        doi, source = enrich._get_best_doi_for_enrichment(pdf)

    assert doi == "10.1111/paper.doi"
    assert "pages-freq" in source


def test_get_best_doi_falls_back_to_frequency_scan_single_page(tmp_path: Path) -> None:
    """Single-occurrence DOI is still returned but labelled page-text."""
    pdf = tmp_path / "no_doi_in_name.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")

    with (
        patch.object(enrich, "_get_pdf_metadata_doi", return_value=None),
        patch.object(
            enrich,
            "_get_page_dois_with_frequency",
            return_value=[("10.1111/single.doi", 1)],
        ),
    ):
        doi, source = enrich._get_best_doi_for_enrichment(pdf)

    assert doi == "10.1111/single.doi"
    assert source == "page-text"


def test_get_best_doi_no_doi_found(tmp_path: Path) -> None:
    """Returns (None, 'none') when all sources come up empty."""
    pdf = tmp_path / "no_doi_in_name.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")

    with (
        patch.object(enrich, "_get_pdf_metadata_doi", return_value=None),
        patch.object(enrich, "_get_page_dois_with_frequency", return_value=[]),
    ):
        doi, source = enrich._get_best_doi_for_enrichment(pdf)

    assert doi is None
    assert source == "none"


# ---------------------------------------------------------------------------
# _get_pdf_metadata_doi ISSN-placeholder guard
# ---------------------------------------------------------------------------
#
# Some publishers (observed on Wiley PDFs in this corpus) stamp the journal's
# container/ISSN identifier into the DOI-shaped metadata field instead of the
# article's real DOI, e.g. "10.1111/(issn)1740-8261". Because bulk CrossRef
# crawls sometimes also pick up a stray container-level manifest row under
# that same fake DOI, trusting it as "the" metadata DOI made 43 real, already-
# downloaded papers in this corpus read as "already matched" to audit_raw.py
# even though the actual article had never been registered.

def _mock_pdfplumber(metadata: dict) -> MagicMock:
    mock_pdf = MagicMock()
    mock_pdf.__enter__ = MagicMock(return_value=mock_pdf)
    mock_pdf.__exit__ = MagicMock(return_value=False)
    mock_pdf.metadata = metadata
    mock_pdf.pages = []
    return mock_pdf


def test_metadata_doi_rejects_issn_placeholder(tmp_path: Path) -> None:
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")

    mock_pdf = _mock_pdfplumber({"Subject": "10.1111/(issn)1740-8261"})
    with patch("pdfplumber.open", return_value=mock_pdf):
        doi = enrich._get_pdf_metadata_doi(pdf)

    assert doi is None


def test_metadata_doi_falls_through_to_next_metadata_hit_past_issn_placeholder(tmp_path: Path) -> None:
    """If a real DOI is ALSO present in metadata, it's still returned."""
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")

    mock_pdf = _mock_pdfplumber({
        "Subject": "10.1111/(issn)1740-8261",
        "doi": "10.1111/vsu.14160",
    })
    with patch("pdfplumber.open", return_value=mock_pdf):
        doi = enrich._get_pdf_metadata_doi(pdf)

    assert doi == "10.1111/vsu.14160"


def test_metadata_doi_still_returns_normal_dois_unchanged(tmp_path: Path) -> None:
    """The guard must not regress the common case of a legitimate metadata DOI."""
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")

    mock_pdf = _mock_pdfplumber({"doi": "10.1111/jvim.70254"})
    with patch("pdfplumber.open", return_value=mock_pdf):
        doi = enrich._get_pdf_metadata_doi(pdf)

    assert doi == "10.1111/jvim.70254"


def test_get_best_doi_falls_through_issn_placeholder_to_filename(tmp_path: Path) -> None:
    """Integration: an ISSN-placeholder metadata DOI must not shadow a good filename DOI.

    Exercises the real (unpatched) _get_pdf_metadata_doi so the whole chain —
    metadata reader -> ISSN guard -> filename fallback — runs end to end.
    """
    pdf = tmp_path / "10.1177_1098612X231170159.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")

    mock_pdf = _mock_pdfplumber({"Subject": "10.1111/(issn)1740-8261"})
    with patch("pdfplumber.open", return_value=mock_pdf):
        doi, source = enrich._get_best_doi_for_enrichment(pdf)

    assert doi == "10.1177/1098612x231170159"
    assert source == "filename"


def test_get_page_dois_with_frequency_prefers_high_count(tmp_path: Path) -> None:
    """DOI appearing on more pages sorts before single-occurrence DOIs."""
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")

    # Simulate: page 0 has both DOIs, pages 1-3 repeat only the paper's own DOI.
    page_texts = [
        "doi:10.1111/own.doi  see also doi:10.9999/ref.doi",
        "doi:10.1111/own.doi",
        "doi:10.1111/own.doi",
        "doi:10.1111/own.doi",
    ]

    mock_page = MagicMock()
    mock_pdf = MagicMock()
    mock_pdf.__enter__ = MagicMock(return_value=mock_pdf)
    mock_pdf.__exit__ = MagicMock(return_value=False)
    mock_pdf.pages = [MagicMock(extract_text=MagicMock(return_value=t)) for t in page_texts]

    with patch("pdfplumber.open", return_value=mock_pdf):
        result = enrich._get_page_dois_with_frequency(pdf, max_pages=4)

    dois = [d for d, _ in result]
    assert dois[0] == "10.1111/own.doi", "high-frequency DOI should rank first"
    assert "10.9999/ref.doi" in dois
