from pathlib import Path

import sys


SRC_DIR = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC_DIR))

from file_paths import (  # noqa: E402
    SECONDARY_PREFIX,
    descriptive_pdf_filename,
    doi_suffix_glob_candidates,
    legacy_doi_filename,
    pdf_path_candidates,
    preferred_pdf_path,
    resolve_existing_pdf_path,
)

_RECORD = {
    "doi": "10.1111/jvim.70254",
    "journal": "JVIM",
    "title": "Effect of N-Butylscopolammonium Bromide on Horses",
}


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


# download._classify_article_type renames a secondary-research PDF in place by
# prefixing "2_". If lookups can't see that name, the paper is treated as never
# downloaded: it is re-fetched every run, never counts toward its journal quota,
# and on Windows the next rename() onto the existing target raises
# FileExistsError and aborts the whole download loop.
def test_resolve_finds_secondary_research_prefixed_descriptive_pdf(tmp_path: Path) -> None:
    secondary = tmp_path / f"{SECONDARY_PREFIX}{descriptive_pdf_filename(_RECORD)}"
    secondary.write_bytes(b"%PDF-1.4")

    assert resolve_existing_pdf_path(tmp_path, _RECORD) == secondary


def test_resolve_finds_secondary_research_prefixed_legacy_pdf(tmp_path: Path) -> None:
    secondary = tmp_path / f"{SECONDARY_PREFIX}{legacy_doi_filename(_RECORD['doi'])}"
    secondary.write_bytes(b"%PDF-1.4")

    assert resolve_existing_pdf_path(tmp_path, _RECORD) == secondary


def test_unprefixed_names_are_still_preferred_over_secondary(tmp_path: Path) -> None:
    """A primary-research PDF must not be shadowed by a stale 2_ file."""
    primary = tmp_path / descriptive_pdf_filename(_RECORD)
    primary.write_bytes(b"%PDF-1.4")
    (tmp_path / f"{SECONDARY_PREFIX}{descriptive_pdf_filename(_RECORD)}").write_bytes(b"%PDF-1.4")

    assert resolve_existing_pdf_path(tmp_path, _RECORD) == primary


def test_pdf_path_candidates_offers_both_bare_and_prefixed_names(tmp_path: Path) -> None:
    candidates = [p.name for p in pdf_path_candidates(tmp_path, _RECORD)]

    assert descriptive_pdf_filename(_RECORD) in candidates
    assert f"{SECONDARY_PREFIX}{descriptive_pdf_filename(_RECORD)}" in candidates
    assert f"{SECONDARY_PREFIX}{legacy_doi_filename(_RECORD['doi'])}" in candidates
    # Bare names first, so the common case matches on the first candidate.
    assert not candidates[0].startswith(SECONDARY_PREFIX)
    assert len(candidates) == len(set(candidates)), "duplicate candidates"


# --- DOI-suffix glob fallback --------------------------------------------
#
# descriptive_pdf_filename() recomputes a paper's expected filename from the
# manifest record's *current* journal + title every call. If title-length
# truncation constants change, or a manifest title is edited, after a paper
# was already downloaded, the recomputed name stops matching the file that's
# actually on disk even though nothing about the paper changed. These tests
# cover the DOI-suffix glob fallback that recovers such files without a
# rename — see doi_suffix_glob_candidates()'s docstring for the real-world
# case (43 papers in this corpus) that motivated it.

def test_exact_match_wins_before_glob_fallback_runs(tmp_path: Path, monkeypatch) -> None:
    """The glob fallback must never even execute when a primary candidate exists."""
    primary = tmp_path / descriptive_pdf_filename(_RECORD)
    primary.write_bytes(b"%PDF-1.4")

    def _boom(*args, **kwargs):
        raise AssertionError("doi_suffix_glob_candidates should not run when an exact match exists")

    monkeypatch.setattr("file_paths.doi_suffix_glob_candidates", _boom)

    assert resolve_existing_pdf_path(tmp_path, _RECORD) == primary


def test_glob_fallback_recovers_title_truncated_bare_file(tmp_path: Path) -> None:
    doi_suffix = "10_1111_jvim_70254"
    drifted = tmp_path / f"jvim__a_much_shorter_title_than_today__{doi_suffix}.pdf"
    drifted.write_bytes(b"%PDF-1.4")

    # Today's record recomputes a DIFFERENT (longer/current) title, so the
    # exact-match candidates all miss — only the DOI suffix still lines up.
    assert resolve_existing_pdf_path(tmp_path, _RECORD) == drifted


def test_glob_fallback_recovers_title_truncated_secondary_prefixed_file(tmp_path: Path) -> None:
    doi_suffix = "10_1111_jvim_70254"
    drifted = tmp_path / f"{SECONDARY_PREFIX}jvim__old_title__{doi_suffix}.pdf"
    drifted.write_bytes(b"%PDF-1.4")

    assert resolve_existing_pdf_path(tmp_path, _RECORD) == drifted


def test_glob_fallback_collision_is_deterministic_not_arbitrary(tmp_path: Path, capsys) -> None:
    doi_suffix = "10_1111_jvim_70254"
    first = tmp_path / f"aaa__old_title__{doi_suffix}.pdf"
    second = tmp_path / f"zzz__other_title__{doi_suffix}.pdf"
    first.write_bytes(b"%PDF-1.4")
    second.write_bytes(b"%PDF-1.4")

    matches = doi_suffix_glob_candidates(tmp_path, _RECORD["doi"], ".pdf")

    assert matches == sorted(matches)
    assert matches[0] == first
    assert "WARNING" in capsys.readouterr().out


def test_glob_fallback_returns_none_when_doi_suffix_absent(tmp_path: Path) -> None:
    """A DOI-suffix that was itself truncated away can't be recovered by glob."""
    (tmp_path / "jvim__title_with_no_doi_suffix_at_all.pdf").write_bytes(b"%PDF-1.4")

    assert resolve_existing_pdf_path(tmp_path, _RECORD) is None
    assert doi_suffix_glob_candidates(tmp_path, _RECORD["doi"], ".pdf") == []
