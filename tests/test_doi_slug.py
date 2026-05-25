"""Tests for the doi_to_slug() helper in src/file_paths.py."""

from file_paths import doi_to_slug, legacy_doi_filename


def test_doi_to_slug_standard_doi() -> None:
    assert doi_to_slug("10.1111/jvim.16872") == "10_1111_jvim_16872"


def test_doi_to_slug_javma_format() -> None:
    assert doi_to_slug("10.2460/javma.23.03.0157") == "10_2460_javma_23_03_0157"


def test_doi_to_slug_handles_colons() -> None:
    # Some DOIs include colons in subject identifiers (rare).
    assert doi_to_slug("10.1234:abc/test.45") == "10_1234_abc_test_45"


def test_doi_to_slug_no_leading_or_trailing_underscores() -> None:
    # Defensive: a leading slash shouldn't leak a leading underscore.
    assert not doi_to_slug("/10.1111/x.y").startswith("_")
    assert not doi_to_slug("10.1111/x.y/").endswith("_")


def test_legacy_filename_still_built_from_slug() -> None:
    assert legacy_doi_filename("10.1111/jvim.16872") == "10_1111_jvim_16872.pdf"
