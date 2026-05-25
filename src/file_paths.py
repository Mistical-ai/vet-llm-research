"""
Shared filesystem helpers for corpus PDF paths.

This module is intentionally import-light so download.py, extract.py,
supplement.py, and pipeline.py can all use the same filename convention without
creating circular imports.
"""

import html
import re
from pathlib import Path
from typing import Mapping


PDF_FILENAME_MAX_LENGTH = 180
TITLE_MAX_LENGTH = 105
JOURNAL_MAX_LENGTH = 32
DOI_SUFFIX_MAX_LENGTH = 48


def sanitize_filename_part(value: object, *, max_length: int = 80) -> str:
    """
    Convert arbitrary metadata into a Windows-safe ASCII filename component.
    """
    text = html.unescape(str(value or "")).strip()
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.encode("ascii", errors="ignore").decode("ascii")
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", "_", text)
    text = re.sub(r"_+", "_", text).strip("_")
    if not text:
        text = "untitled"
    return text[:max_length].strip("_") or "untitled"


def doi_to_slug(doi: str) -> str:
    """
    Convert a DOI into a filesystem- and batch-API-safe slug.

    Used by Phase 3 to build cache filenames (data/processed/{slug}.jsonl) and
    batch API `custom_id` values that join requests to responses. Keeping this
    in one place means summariser/evaluator/CLI cannot disagree on the format.

    Example: "10.1111/jvim.16872" -> "10_1111_jvim_16872"
    """
    return doi.replace("/", "_").replace(":", "_").replace(".", "_").strip("_")


def legacy_doi_filename(doi: str) -> str:
    """
    Return the original DOI-only filename used by earlier pipeline versions.
    """
    return f"{doi_to_slug(doi)}.pdf"


def _record_value(record_or_doi: Mapping[str, object] | str, key: str) -> str:
    if isinstance(record_or_doi, str):
        return record_or_doi if key == "doi" else ""
    return str(record_or_doi.get(key, "") or "")


def descriptive_pdf_filename(record_or_doi: Mapping[str, object] | str) -> str:
    """
    Build a readable, collision-resistant filename from journal, title, and DOI.
    """
    doi = _record_value(record_or_doi, "doi").strip()
    journal = sanitize_filename_part(
        _record_value(record_or_doi, "journal"),
        max_length=JOURNAL_MAX_LENGTH,
    )
    title = sanitize_filename_part(
        _record_value(record_or_doi, "title"),
        max_length=TITLE_MAX_LENGTH,
    )
    doi_suffix = sanitize_filename_part(
        doi,
        max_length=DOI_SUFFIX_MAX_LENGTH,
    )

    if not doi:
        return f"{journal}__{title}.pdf"[:PDF_FILENAME_MAX_LENGTH]

    filename = f"{journal}__{title}__{doi_suffix}.pdf"
    if len(filename) <= PDF_FILENAME_MAX_LENGTH:
        return filename

    overflow = len(filename) - PDF_FILENAME_MAX_LENGTH
    title = title[:max(20, len(title) - overflow)].strip("_") or "untitled"
    return f"{journal}__{title}__{doi_suffix}.pdf"


def pdf_path_candidates(
    raw_dir: Path,
    record_or_doi: Mapping[str, object] | str,
) -> list[Path]:
    """
    Return possible paths, preferring the new descriptive name over legacy DOI.
    """
    doi = _record_value(record_or_doi, "doi").strip()
    candidates: list[Path] = []
    if not isinstance(record_or_doi, str):
        candidates.append(raw_dir / descriptive_pdf_filename(record_or_doi))
    if doi:
        candidates.append(raw_dir / legacy_doi_filename(doi))

    unique: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        if candidate not in seen:
            seen.add(candidate)
            unique.append(candidate)
    return unique


def resolve_existing_pdf_path(
    raw_dir: Path,
    record_or_doi: Mapping[str, object] | str,
) -> Path | None:
    """
    Return the first existing PDF path for a manifest record or DOI.
    """
    for candidate in pdf_path_candidates(raw_dir, record_or_doi):
        if candidate.exists():
            return candidate
    return None


def preferred_pdf_path(
    raw_dir: Path,
    record_or_doi: Mapping[str, object] | str,
) -> Path:
    """
    Return the path new downloads should use.

    If a legacy file already exists, return that path so idempotent runs do not
    duplicate a paper under two filenames.
    """
    existing = resolve_existing_pdf_path(raw_dir, record_or_doi)
    if existing is not None:
        return existing

    candidates = pdf_path_candidates(raw_dir, record_or_doi)
    if candidates:
        return candidates[0]

    return raw_dir / "untitled.pdf"
