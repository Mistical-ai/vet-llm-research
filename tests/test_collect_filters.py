from pathlib import Path

import sys


SRC_DIR = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC_DIR))

import collect  # noqa: E402


def test_candidate_filter_rejects_corrections() -> None:
    record = {
        "doi": "10.1111/jvim.70264",
        "title": "Correction to Characteristics of Dogs With Protein-Losing Enteropathy",
        "authors": [],
        "abstract": "",
    }

    reason = collect._candidate_rejection_reason(record)

    assert reason is not None
    assert "non-article title pattern" in reason


def test_candidate_filter_accepts_real_article() -> None:
    record = {
        "doi": "10.1111/jvim.70254",
        "title": "Effect of N-Butylscopolammonium Bromide on Horses",
        "authors": ["Dufourni A"],
        "abstract": "Background: Evaluate the effect of treatment. Methods: Prospective study.",
    }

    assert collect._candidate_rejection_reason(record) is None


def test_collection_windows_cover_2023_to_2025() -> None:
    windows = collect._collection_windows()

    labels = [label for label, *_ in windows]
    assert labels == ["2023", "2024", "2025"]
    assert sum(quota for *_, quota in windows) == collect.COLLECT_CANDIDATES_PER_JOURNAL
