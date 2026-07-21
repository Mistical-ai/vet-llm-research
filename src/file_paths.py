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


def descriptive_stem(record_or_doi: Mapping[str, object] | str) -> str:
    """
    Return the descriptive filename without any extension.

    Same logic as ``descriptive_pdf_filename`` but with the ``.pdf`` suffix
    removed, so callers can append whatever extension they need (``.pdf``,
    ``.jsonl``, ``.txt``). Keeping the stem-builder in one place is what
    lets ``data/raw/<stem>.pdf`` and ``data/processed/<stem>.jsonl`` stay
    in lockstep — a PDF and its cleaned-text cache always share a name.

    Example
    -------
    >>> descriptive_stem({"doi": "10.1111/jvim.16872",
    ...                   "journal": "JVIM",
    ...                   "title": "Pre-illness dietary risk factors"})
    'jvim__pre_illness_dietary_risk_factors__10_1111_jvim_16872'
    """
    return descriptive_pdf_filename(record_or_doi).removesuffix(".pdf")


# Prefix stamped onto a PDF that passed validation but was classified as
# secondary research (review, meta-analysis, commentary). See
# download._classify_article_type, which renames the file in place.
SECONDARY_PREFIX = "2_"


def pdf_path_candidates(
    raw_dir: Path,
    record_or_doi: Mapping[str, object] | str,
) -> list[Path]:
    """
    Return possible paths, preferring the new descriptive name over legacy DOI.

    Each name is offered both bare and with the ``2_`` secondary-research
    prefix, because that rename happens *after* download and is otherwise
    invisible to every lookup that goes through here. Without the prefixed
    variants a reclassified paper looks permanently missing: it gets
    re-downloaded on every run, never counts toward its journal's success
    quota, and on Windows the second download raises FileExistsError when
    rename() finds the target already there.

    Bare names come first so an ordinary primary-research PDF is still matched
    on the first candidate.
    """
    doi = _record_value(record_or_doi, "doi").strip()
    names: list[str] = []
    if not isinstance(record_or_doi, str):
        names.append(descriptive_pdf_filename(record_or_doi))
    if doi:
        names.append(legacy_doi_filename(doi))

    candidates: list[Path] = [raw_dir / name for name in names]
    candidates += [raw_dir / f"{SECONDARY_PREFIX}{name}" for name in names]

    unique: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        if candidate not in seen:
            seen.add(candidate)
            unique.append(candidate)
    return unique


def doi_suffix_glob_candidates(
    root_dir: Path,
    doi: str,
    extension: str,
) -> list[Path]:
    """
    Fallback lookup: find files ending in this DOI's filename suffix.

    ``descriptive_pdf_filename``/``descriptive_stem`` recompute a file's name
    from the manifest record's *current* journal + title every time they're
    called. If ``TITLE_MAX_LENGTH``/``PDF_FILENAME_MAX_LENGTH`` change, or a
    manifest title gets edited, after a paper was downloaded, the recomputed
    name silently stops matching the file that's actually on disk even
    though nothing about the paper itself changed. The DOI suffix segment is
    derived only from the DOI, so it stays stable across any title/journal
    drift — globbing for it recovers the file without needing a rename.

    Matches both the bare name (``...__{doi_suffix}{extension}``) and the
    ``2_`` secondary-research-prefixed variant, since a reclassified review
    PDF is just as exposed to this drift as a primary one.
    """
    if not doi or not root_dir.exists():
        return []
    doi_suffix = sanitize_filename_part(doi, max_length=DOI_SUFFIX_MAX_LENGTH)
    if not doi_suffix:
        return []

    patterns = [f"*__{doi_suffix}{extension}", f"{SECONDARY_PREFIX}*__{doi_suffix}{extension}"]
    matches: list[Path] = []
    seen: set[Path] = set()
    for pattern in patterns:
        for path in sorted(root_dir.glob(pattern)):
            if path not in seen:
                seen.add(path)
                matches.append(path)

    if len(matches) > 1:
        # A 48-char truncated suffix could theoretically collide across two
        # different DOIs. Never guess silently — surface it and fall back to
        # a deterministic choice rather than picking whichever glob() happened
        # to return first.
        print(
            f"[file_paths] WARNING: {len(matches)} files matched DOI suffix "
            f"'{doi_suffix}' in {root_dir}: {[p.name for p in matches]}. "
            f"Using {matches[0].name}."
        )
    return matches


def resolve_existing_pdf_path(
    raw_dir: Path,
    record_or_doi: Mapping[str, object] | str,
) -> Path | None:
    """
    Return the first existing PDF path for a manifest record or DOI.

    Falls back to a DOI-suffix glob (see ``doi_suffix_glob_candidates``) only
    when none of the exact recomputed candidates exist on disk.
    """
    for candidate in pdf_path_candidates(raw_dir, record_or_doi):
        if candidate.exists():
            return candidate

    doi = _record_value(record_or_doi, "doi").strip()
    fallback_matches = doi_suffix_glob_candidates(raw_dir, doi, ".pdf")
    return fallback_matches[0] if fallback_matches else None


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
