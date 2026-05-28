"""
llm-sum/prepare_texts.py — Cache cleaned paper text for the summariser
=======================================================================

Walks data/raw/*.pdf directly (one file = one paper), extracts the FULL
cleaned text (references stripped, nothing else truncated), and caches to:

    data/processed/<descriptive_stem>.jsonl

The cache filename mirrors the PDF stem exactly so a PDF and its cleaned-
text cache always share a name. The ``slug`` field inside the JSON line is
still ``doi_to_slug(doi)`` — that is the stable key used by summariser
custom_id, cost_estimator, and check_batch_status; only the on-disk
filename gained the descriptive prefix.

Why walk PDFs directly instead of the manifest?
    The manifest may have multiple records per DOI, duplicate entries, or
    records for papers that were never downloaded. Walking the filesystem
    guarantees exactly one output file per downloaded PDF.

Why JSONL (not .txt + .meta.json)?
    One file vs two. Consistent with the rest of the pipeline (manifest.jsonl,
    summaries.jsonl). The text and metadata travel together.

The cache holds the FULL paper body (no character limit). The summariser
applies MAX_INPUT_CHARS when building the LLM prompt, so the limit can be
tuned independently of re-extraction.

Skip rule:
    if {slug}.jsonl exists AND its mtime >= PDF mtime  →  skip (cached)
"""

from __future__ import annotations

from _bootstrap import *  # noqa: F401,F403  (adds llm-sum + src to sys.path)

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from extract import (  # noqa: E402
    extract_text_from_pdf,
    remove_references_section,
    clean_publisher_noise,
    REMOVE_REFERENCES,
)
from file_paths import (  # noqa: E402
    doi_to_slug,
    descriptive_stem,
    pdf_path_candidates,
)
from utils import log_error  # noqa: E402

RAW_DIR = DATA_DIR / "raw"
MANIFEST_PATH = DATA_DIR / "manifest.jsonl"
VALID_INPUT_SOURCES = ("processed", "raw_text")

MIN_WORD_COUNT_WARN = int(os.getenv("MIN_WORD_COUNT_WARN", "1000"))


# ---------------------------------------------------------------------------
# Processed-cache readers (shared by cost_estimator, summarizer, evaluator)
# ---------------------------------------------------------------------------

def read_processed_jsonl(path: Path) -> str | None:
    """Return the `text` field from one JSONL text cache file."""
    if not path.exists():
        return None
    line = path.read_text(encoding="utf-8").strip()
    if not line:
        return None
    try:
        text = json.loads(line).get("text")
    except (json.JSONDecodeError, AttributeError):
        return None
    return text if isinstance(text, str) and text.strip() else None


def processed_jsonl_path(record_or_doi) -> Path:
    """
    Return the descriptive ``data/processed/*.jsonl`` path for a paper.

    Prefers ``<descriptive_stem>.jsonl`` when the input is a record (or
    mapping) containing journal + title; falls back to the legacy
    ``<doi_to_slug(doi)>.jsonl`` when only a DOI string is available.
    The returned path may not exist on disk — callers should check.
    """
    if (not isinstance(record_or_doi, str)
            and record_or_doi.get("journal")
            and record_or_doi.get("title")):
        return PROCESSED_DIR / f"{descriptive_stem(record_or_doi)}.jsonl"

    doi = record_or_doi if isinstance(record_or_doi, str) else str(
        record_or_doi.get("doi", "") or ""
    )
    return PROCESSED_DIR / f"{doi_to_slug(doi)}.jsonl"


def raw_text_jsonl_path(record_or_doi) -> Path:
    """
    Return the descriptive ``data/raw_text/*.jsonl`` path for a paper.

    This mirrors processed_jsonl_path(), but points at the raw column-aware
    extraction before publisher-noise and reference stripping.
    """
    if (not isinstance(record_or_doi, str)
            and record_or_doi.get("journal")
            and record_or_doi.get("title")):
        return RAW_TEXT_DIR / f"{descriptive_stem(record_or_doi)}.jsonl"

    doi = record_or_doi if isinstance(record_or_doi, str) else str(
        record_or_doi.get("doi", "") or ""
    )
    return RAW_TEXT_DIR / f"{doi_to_slug(doi)}.jsonl"


def _cache_path(record_or_doi, input_source: str) -> Path:
    if input_source == "processed":
        return processed_jsonl_path(record_or_doi)
    if input_source == "raw_text":
        return raw_text_jsonl_path(record_or_doi)
    raise ValueError(f"Unknown input_source={input_source!r}. Valid: {VALID_INPUT_SOURCES}")


def find_processed_jsonl(record_or_doi) -> Path | None:
    """
    Locate the cache file for a paper on disk, trying both naming schemes.

    Order: descriptive name (new) → legacy ``{slug}.jsonl``. Returns None
    if neither exists. This is the lookup that summariser/evaluator use,
    so a partially-migrated ``data/processed/`` directory still works.
    """
    preferred = processed_jsonl_path(record_or_doi)
    if preferred.exists():
        return preferred

    if not isinstance(record_or_doi, str):
        doi = str(record_or_doi.get("doi", "") or "")
        if doi:
            legacy = PROCESSED_DIR / f"{doi_to_slug(doi)}.jsonl"
            if legacy.exists() and legacy != preferred:
                return legacy
    return None


def find_cached_jsonl(record_or_doi, input_source: str = "processed") -> Path | None:
    """
    Locate a text cache for a paper, trying descriptive then legacy names.

    ``input_source='processed'`` returns cleaned body text. ``'raw_text'``
    returns the raw column-aware extraction used only for small comparison
    experiments.
    """
    if input_source == "processed":
        return find_processed_jsonl(record_or_doi)

    if input_source != "raw_text":
        raise ValueError(f"Unknown input_source={input_source!r}. Valid: {VALID_INPUT_SOURCES}")

    preferred = raw_text_jsonl_path(record_or_doi)
    if preferred.exists():
        return preferred

    if not isinstance(record_or_doi, str):
        doi = str(record_or_doi.get("doi", "") or "")
        if doi:
            legacy = RAW_TEXT_DIR / f"{doi_to_slug(doi)}.jsonl"
            if legacy.exists() and legacy != preferred:
                return legacy
    return None


def read_cached_text(record_or_doi, input_source: str = "processed") -> str | None:
    """
    Return the cleaned-text body of the cache file for a paper, or None.

    Convenience wrapper around find_processed_jsonl + read_processed_jsonl
    so callers don't need to know which filename convention is on disk.
    """
    path = find_cached_jsonl(record_or_doi, input_source=input_source)
    return read_processed_jsonl(path) if path is not None else None


def iter_processed_texts(processed_dir: Path | None = None) -> Iterable[tuple[str, str]]:
    """
    Yield ``(slug, text)`` for every non-empty cached paper.

    ``slug`` is read from the JSON line itself (the stable ``doi_to_slug``
    value), NOT from ``path.stem`` — under descriptive naming the stem is
    the journal/title/DOI mash, but the in-file slug stays canonical so
    downstream cost-estimation and batch-merging keep working.
    """
    root = processed_dir if processed_dir is not None else PROCESSED_DIR
    if not root.exists():
        return
    for path in sorted(root.glob("*.jsonl")):
        if not path.exists():
            continue
        line = path.read_text(encoding="utf-8").strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        text = obj.get("text") if isinstance(obj, dict) else None
        slug = obj.get("slug") if isinstance(obj, dict) else None
        if not (isinstance(text, str) and text.strip()):
            continue
        if not (isinstance(slug, str) and slug):
            slug = path.stem  # last resort for malformed lines
        yield slug, text


def iter_cached_texts(input_source: str = "processed",
                      root: Path | None = None) -> Iterable[tuple[str, str]]:
    """
    Yield ``(slug, text)`` from either processed or raw extracted-text caches.
    """
    if input_source == "processed":
        yield from iter_processed_texts(root if root is not None else PROCESSED_DIR)
        return
    if input_source != "raw_text":
        raise ValueError(f"Unknown input_source={input_source!r}. Valid: {VALID_INPUT_SOURCES}")
    yield from iter_processed_texts(root if root is not None else RAW_TEXT_DIR)


# ---------------------------------------------------------------------------
# Manifest helpers (used only for DOI enrichment, not for PDF selection)
# ---------------------------------------------------------------------------

def _iter_manifest(manifest_path: Path) -> Iterable[dict]:
    if not manifest_path.exists():
        return
    with open(manifest_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _build_pdf_doi_map(manifest_path: Path, raw_dir: Path) -> dict[Path, tuple[str, dict]]:
    """
    Build a {pdf_path: (doi, record)} mapping from the manifest.

    Covers both descriptive and legacy filenames so whichever naming the
    file system uses, the DOI lookup works. PDFs not matched by the manifest
    fall back to slug-from-filename in _slug_from_pdf().
    """
    mapping: dict[Path, tuple[str, dict]] = {}
    for record in _iter_manifest(manifest_path):
        doi = str(record.get("doi", "")).strip()
        if not doi:
            continue
        # Map ALL candidate paths (existing or not) — the glob iteration will
        # only actually process files that exist on disk.
        for candidate in pdf_path_candidates(raw_dir, record):
            if candidate not in mapping:
                mapping[candidate] = (doi, record)
    return mapping


def _slug_from_pdf(pdf_path: Path) -> str:
    """
    Derive a slug from the PDF filename when no manifest entry is found.

    Legacy:      10_1111_jvim_16872.pdf            → 10_1111_jvim_16872
    Descriptive: jvim__outcomes__10_1111_jvim_16872.pdf → 10_1111_jvim_16872

    The slug is the stable join key used by summariser custom_id and the
    batch result merger; the filename is a separate concern handled by
    ``_output_filename`` below.
    """
    stem = pdf_path.stem
    parts = stem.split("__")
    return parts[-1] if len(parts) > 1 else stem


def _output_filename(pdf_path: Path, doi: str, record: dict) -> str:
    """
    Build the ``data/processed/*.jsonl`` filename for one PDF.

    Preferred (manifest record with journal + title + doi available):
        ``<descriptive_stem(record)>.jsonl`` — mirrors the PDF stem exactly.
    Fallback (orphan PDF, no manifest match):
        ``<pdf_path.stem>.jsonl`` — still mirrors the PDF, just from disk.

    Either way, the on-disk cache file is one rename away from its source
    PDF, which is what makes ``scripts/verify_extraction.py`` work without
    a separate manifest lookup.
    """
    if record and doi and record.get("journal") and record.get("title"):
        return f"{descriptive_stem(record)}.jsonl"
    return f"{pdf_path.stem}.jsonl"


# ---------------------------------------------------------------------------
# Cache check
# ---------------------------------------------------------------------------

def _jsonl_is_fresh(jsonl_path: Path, pdf_path: Path) -> bool:
    if not jsonl_path.exists():
        return False
    return jsonl_path.stat().st_mtime >= pdf_path.stat().st_mtime


def _write_text_cache(
    *,
    path: Path,
    doi: str,
    slug: str,
    text: str,
    pdf_path: Path,
    source: str,
) -> None:
    """Write one text cache JSONL row with shared metadata."""
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "doi": doi,
        "slug": slug,
        "text": text,
        "word_count": len(text.split()),
        "char_count": len(text),
        "pdf_filename": pdf_path.name,
        "pdf_source": str(pdf_path),
        "input_source": source,
        "extracted_at": datetime.now(timezone.utc).isoformat(),
    }
    path.write_text(json.dumps(entry, ensure_ascii=False) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Core extraction
# ---------------------------------------------------------------------------

def prepare_one_pdf(pdf_path: Path, doi: str, record: dict) -> dict:
    """
    Extract and cache one paper's cleaned text from a known PDF path.

    Output: data/processed/{slug}.jsonl — one JSON line with text + metadata.
    Returns a result dict; never raises.
    """
    slug = doi_to_slug(doi) if doi else _slug_from_pdf(pdf_path)
    jsonl_path = PROCESSED_DIR / _output_filename(pdf_path, doi, record)
    raw_jsonl_path = RAW_TEXT_DIR / _output_filename(pdf_path, doi, record)

    if _jsonl_is_fresh(jsonl_path, pdf_path) and _jsonl_is_fresh(raw_jsonl_path, pdf_path):
        return {"doi": doi, "slug": slug, "status": "cached"}

    raw_text = extract_text_from_pdf(pdf_path)
    if raw_text is None:
        log_error(doi or slug, "extract", f"pdfplumber failed for {pdf_path.name}")
        return {"doi": doi, "slug": slug, "status": "failed"}

    _write_text_cache(
        path=raw_jsonl_path,
        doi=doi,
        slug=slug,
        text=raw_text,
        pdf_path=pdf_path,
        source="raw_text",
    )

    raw_text = clean_publisher_noise(raw_text)
    cleaned = remove_references_section(raw_text, doi or slug) if REMOVE_REFERENCES else raw_text
    word_count = len(cleaned.split())

    if word_count < MIN_WORD_COUNT_WARN:
        print(f"  [prepare] WARNING: {slug} has only {word_count} words after "
              "extraction — check PDF quality or set REMOVE_REFERENCES=false to debug.")

    _write_text_cache(
        path=jsonl_path,
        doi=doi,
        slug=slug,
        text=cleaned,
        pdf_path=pdf_path,
        source="processed",
    )

    return {"doi": doi, "slug": slug, "status": "extracted",
            "chars": len(cleaned), "words": word_count}


# ---------------------------------------------------------------------------
# Run loop — walks data/raw/*.pdf
# ---------------------------------------------------------------------------

def run(manifest_path: Path = MANIFEST_PATH, limit: int | None = None) -> dict:
    """
    Walk data/raw/*.pdf and cache cleaned text for each PDF.

    One {slug}.jsonl per PDF — exactly as many output files as there are PDFs.
    Returns counts per outcome.
    """
    if not RAW_DIR.exists():
        print(f"[phase3:extract] Raw directory not found at {RAW_DIR}.")
        return {"extracted": 0, "cached": 0, "failed": 0}

    pdf_doi_map = _build_pdf_doi_map(manifest_path, RAW_DIR)

    counts: dict[str, int] = {"extracted": 0, "cached": 0, "failed": 0}
    seen = 0

    for pdf_path in sorted(RAW_DIR.glob("*.pdf")):
        if limit is not None and seen >= limit:
            break
        seen += 1

        doi, record = pdf_doi_map.get(pdf_path, ("", {}))
        result = prepare_one_pdf(pdf_path, doi, record)
        counts[result["status"]] = counts.get(result["status"], 0) + 1

        if result["status"] == "extracted":
            print(f"[phase3:extract] {result['slug']} "
                  f"({result['chars']:,} chars / {result.get('words', '?')} words)")
        elif result["status"] == "failed":
            print(f"[phase3:extract] FAILED {pdf_path.name}")

    print(
        f"[phase3:extract] done — extracted={counts['extracted']} "
        f"cached={counts['cached']} failed={counts['failed']}"
    )
    return counts


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Extract and cache full cleaned text for every PDF in data/raw/."
    )
    parser.add_argument("--manifest", type=Path, default=MANIFEST_PATH,
                        help="Manifest for DOI enrichment (optional; does not filter PDFs).")
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Process only the first N PDFs (handy for smoke tests).",
    )
    args = parser.parse_args(argv)
    run(args.manifest, args.limit)
    return 0


if __name__ == "__main__":
    sys.exit(main())
