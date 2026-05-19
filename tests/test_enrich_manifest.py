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
        patch.object(enrich, "_get_pdf_metadata_doi", return_value=doi),
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
        patch.object(enrich, "_get_pdf_metadata_doi", return_value=doi),
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
        patch.object(enrich, "_get_pdf_metadata_doi", return_value=doi),
        patch.object(enrich, "_fetch_crossref_single", return_value=crossref_item),
        patch("time.sleep"),
    ):
        _, _, appended = enrich.enrich_manifest_from_folder(
            tmp_path, manifest, mailto="test@example.com", dry_run=True
        )

    assert appended == 0
    assert manifest.read_text(encoding="utf-8") == ""


def test_enrich_manifest_no_metadata_doi_skips_crossref(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.jsonl"
    manifest.write_text("", encoding="utf-8")

    pdf = tmp_path / "scan.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")

    with (
        patch.object(enrich, "_get_pdf_metadata_doi", return_value=None),
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
