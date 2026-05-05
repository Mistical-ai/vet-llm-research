from pathlib import Path

import sys


SRC_DIR = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC_DIR))

from file_paths import (  # noqa: E402
    descriptive_pdf_filename,
    legacy_doi_filename,
    preferred_pdf_path,
    resolve_existing_pdf_path,
)


def test_descriptive_filename_contains_journal_title_and_doi_suffix() -> None:
    record = {
        "doi": "10.1111/jvim.70254",
        "journal": "JVIM",
        "title": "Effect of N-Butylscopolammonium Bromide on Horses",
    }

    filename = descriptive_pdf_filename(record)

    assert filename.startswith("jvim__effect_of_n_butylscopolammonium")
    assert filename.endswith("__10_1111_jvim_70254.pdf")
    assert len(filename) <= 180


def test_legacy_doi_filename_matches_existing_convention() -> None:
    assert legacy_doi_filename("10.2460/javma.25.04.0224") == "10_2460_javma_25_04_0224.pdf"


def test_preferred_path_keeps_existing_legacy_file(tmp_path: Path) -> None:
    record = {
        "doi": "10.1111/jvim.70254",
        "journal": "JVIM",
        "title": "Effect of N-Butylscopolammonium Bromide on Horses",
    }
    legacy_path = tmp_path / legacy_doi_filename(record["doi"])
    legacy_path.write_bytes(b"%PDF-1.4")

    assert resolve_existing_pdf_path(tmp_path, record) == legacy_path
    assert preferred_pdf_path(tmp_path, record) == legacy_path
