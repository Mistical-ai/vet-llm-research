#!/usr/bin/env python3
"""
Script: ingest_manual_pdfs.py
Purpose: Move legally obtained PDFs from data/manual_inbox/ into data/raw/ using
         manifest.jsonl metadata (read-only) and append data/manual_manifest.jsonl.

Design choices explained (deeper detail in README § Design Decisions):
- Why require manifest linkage? Filenames from browsers are meaningless; the manifest
  is the single source of truth for DOI, journal, and descriptive filenames.
- Why DOI first, then PDF Title? Embedded DOIs are the most reliable key; title
  matching is a fallback when the DOI in the PDF is missing or not in the manifest.
- Why a manual_inbox/failed/ folder instead of deleting? Unmatched PDFs are evidence
  for debugging (wrong manifest, bad metadata, scan with no text). Researchers can
  fix the root cause and re-run auto_ingest_workflow.py, which retries failed/.
- Why never edit manifest.jsonl here? collect.py and enrich_manifest_from_pdfs.py
  own manifest writes; ingest only reads so automated and manual paths stay auditable.
- Why skipped_existing_in_raw/ vs failed/? Duplicates already in raw/ are success,
  not errors — they are quarantined separately so failed/ stays actionable.

Filename DOI fallback: when publishers save files as ``10.1177_1098612X….pdf`` (slash
replaced by underscore) and the PDF has no text layer, we parse the stem before
failing — see ``_doi_from_filename``.

Operator guide: README \"Guide: Manually adding papers to your corpus\".
"""

from __future__ import annotations

import argparse
import html
import json
import re
import shutil
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from file_paths import descriptive_pdf_filename, resolve_existing_pdf_path
from utils import log_error

load_dotenv()

MANIFEST_PATH = Path("data") / "manifest.jsonl"
MANUAL_MANIFEST_PATH = Path("data") / "manual_manifest.jsonl"
RAW_DIR = Path("data") / "raw"


# Mirrors the user-authored pattern ``10.{4..9 registrar}/suffix`` with greedy \S suffix.
_RAW_DOI = re.compile(r"10\.\d{4,9}/\S+", flags=re.IGNORECASE)


def _strip_trailing_noise(token: str) -> str:
    """Remove parentheses/quotes/etc. leaked from bibliography lines."""
    t = token
    endings = '),.;:\"' + "”“’]>}"
    while t and t[-1] in endings:
        t = t[:-1]
    return t


def _normalize_match_doi(candidate: str) -> str | None:
    """Return sane lowercase DOI or ``None`` if unusable."""
    d = candidate.strip()
    if not d:
        return None
    d = re.sub(r"^doi:\s*", "", d, flags=re.IGNORECASE).strip()
    d = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", d, flags=re.IGNORECASE)
    frag = re.split(r"[\s,;]", d, maxsplit=1)[0]
    frag = frag.split(";")[0]
    frag = _strip_trailing_noise(frag)
    frag = re.sub(r"\s+", "", frag)
    m = _RAW_DOI.fullmatch(frag)
    if m is None:
        m = _RAW_DOI.match(frag)
    if not m:
        return None
    canon = _strip_trailing_noise(m.group(0)).lower()
    canon = canon.rstrip(".")
    checker = _RAW_DOI.fullmatch(canon)
    return checker.group(0) if checker else None


def _collect_dois_from_plaintext(blob: str) -> list[str]:
    results: list[str] = []
    for hit in _RAW_DOI.findall(blob):
        doi = _normalize_match_doi(hit)
        if doi and doi not in results:
            results.append(doi)
    return results


def _doi_from_filename(pdf_path: Path) -> str | None:
    """
    Parse a DOI encoded in the PDF filename (e.g. ``10.1177_1098612X231170159.pdf``).

    Why safe here? The researcher or download tool named the file after one paper;
    unlike page-text scans, this is not a reference-list DOI. Shared with
    enrich_manifest_from_pdfs.py so both steps agree on the same identifier.
    """
    stem = pdf_path.stem
    registrar_match = re.match(r"^(10\.\d{4,9})_(.+)$", stem, flags=re.IGNORECASE)
    if registrar_match:
        return _normalize_match_doi(f"{registrar_match.group(1)}/{registrar_match.group(2)}")
    return _normalize_match_doi(stem)


def _normalize_pdf_title(raw: str) -> str:
    txt = html.unescape(str(raw or "")).strip()
    txt = re.sub(r"<[^>]+>", " ", txt)
    txt = re.sub(r"\s+", " ", txt).strip()
    return txt.casefold()


def _metadata_blob(meta: dict[str, Any] | None) -> str:
    if not meta:
        return ""
    return "\n".join(str(val) for val in meta.values() if val)


def _gather_dois_from_pdf(pdf_path: Path) -> tuple[list[str], str | None, str]:
    """
    Collect DOI candidates ordered by reliability: metadata → filename →
    multi-page DOIs → single-page DOIs.

    The paper's own DOI is printed in the header or footer of every page (high
    page-frequency), while DOIs from the reference list appear on one page only.
    Sorting by frequency before falling back to document order means the paper's
    own DOI is tried first, preventing accidental matching against a cited paper
    whose DOI coincidentally appears earlier in the text.
    """
    import pdfplumber  # Lazy import — see README \"Why pdfplumber?\"

    filename_hit = _doi_from_filename(pdf_path)
    filename_hits = [filename_hit] if filename_hit else []

    try:
        with pdfplumber.open(pdf_path) as pdf:
            meta_blob = _metadata_blob(pdf.metadata)
            embedded_title = (
                str(pdf.metadata["Title"]).strip()
                if pdf.metadata and pdf.metadata.get("Title")
                else None
            )
            meta_hits = _collect_dois_from_plaintext(meta_blob)

            # Scan each page individually so we can rank DOIs by how many pages
            # they appear on.  Ties are broken by first-appearance order.
            scan_pages = min(5, len(pdf.pages))
            page_doi_counts: dict[str, int] = {}
            doi_first_seen: dict[str, int] = {}
            page_dois: list[list[str]] = []
            for idx in range(scan_pages):
                text = pdf.pages[idx].extract_text() or ""
                hits = _collect_dois_from_plaintext(text)
                page_dois.append(hits)
                for doi in hits:
                    page_doi_counts[doi] = page_doi_counts.get(doi, 0) + 1
                    if doi not in doi_first_seen:
                        doi_first_seen[doi] = idx

            # High-frequency DOIs (appear on 2+ pages) = paper's own header/footer DOI.
            multi_page = [
                doi
                for doi, cnt in sorted(
                    page_doi_counts.items(),
                    key=lambda x: (-x[1], doi_first_seen.get(x[0], 999)),
                )
                if cnt >= 2
            ]

            # Single-page DOIs in document order (page 1 first, preserving position).
            seen: set[str] = set(multi_page)
            single_page: list[str] = []
            for page_doi_list in page_dois:
                for doi in page_doi_list:
                    if doi not in seen and page_doi_counts[doi] == 1:
                        single_page.append(doi)
                        seen.add(doi)

            ordered = list(dict.fromkeys(meta_hits + filename_hits + multi_page + single_page))
            hint = embedded_title or (ordered[0] if ordered else "unknown")
            trace = f"[pdf]={pdf_path.name} hint={hint!r}"
            return ordered, embedded_title, trace

    except Exception as exc:  # noqa: BLE001
        if filename_hits:
            trace = f"[pdf]={pdf_path.name} hint={filename_hits[0]!r} (filename; pdfplumber error={exc!r})"
            return filename_hits, None, trace
        log_error(
            str(pdf_path.name),
            "manual_ingest",
            f"pdfplumber could not inspect PDF: {exc}",
        )
        return [], None, f"[pdf]={pdf_path.name} error={exc!r}"


def _unique_child(path: Path) -> Path:
    base = path.stem
    suffix = path.suffix.lower() or ".pdf"
    folder = path.parent
    candidate = folder / f"{base}{suffix}"
    counter = 1
    while candidate.exists():
        candidate = folder / f"{base}__dup{counter}{suffix}"
        counter += 1
    return candidate


def _load_manifest_indexes(
    manifest_path: Path,
) -> tuple[dict[str, dict[str, Any]], dict[str, list[str]]]:
    doi_map: dict[str, dict[str, Any]] = {}
    title_map: defaultdict[str, list[str]] = defaultdict(list)

    if not manifest_path.exists():
        return doi_map, dict(title_map)

    with open(manifest_path, encoding="utf-8") as handle:
        for lineno, raw in enumerate(handle, 1):
            line = raw.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                log_error(
                    "N/A",
                    "manual_ingest",
                    f"Skipping malformed manifest line {lineno}",
                )
                continue
            doi = str(record.get("doi", "") or "").strip().lower()
            if not doi:
                continue
            doi_map.setdefault(doi, record)
            title_key = _normalize_pdf_title(str(record.get("title", "") or ""))
            if title_key:
                bucket = title_map[title_key]
                if doi not in bucket:
                    bucket.append(doi)
    return doi_map, dict(title_map)


def _manual_manifest_existing_dois(manual_path: Path) -> set[str]:
    dois: set[str] = set()
    if not manual_path.exists():
        return dois
    with open(manual_path, encoding="utf-8") as handle:
        for lineno, raw in enumerate(handle, 1):
            line = raw.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                log_error(
                    "N/A",
                    "manual_ingest",
                    f"Skipping malformed manual manifest line {lineno}",
                )
                continue
            d = str(record.get("doi", "") or "").strip().lower()
            if d:
                dois.add(d)
    return dois


def _canonical_json_line(record: dict[str, Any]) -> str:
    payload = dict(record)
    payload.pop("_source", None)
    return json.dumps(payload, ensure_ascii=False)


def ingest_one_pdf(
    pdf_path: Path,
    *,
    manifest_by_doi: dict[str, dict[str, Any]],
    title_buckets: dict[str, list[str]],
    manual_registered: set[str],
    skipped_existing_root: Path,
) -> tuple[str, Path | None, str]:
    """
    Match one inbox PDF to a manifest row and move it into data/raw/.

    Outcomes starting with failed_* move the file to manual_inbox/failed/ so the
    researcher can inspect without losing the PDF. See README § Manual ingest failures.
    """
    doi_candidates, embedded_title, trace = _gather_dois_from_pdf(pdf_path)
    doi: str | None = None

    for candidate in doi_candidates:
        if candidate in manifest_by_doi:
            doi = candidate
            break
    else:
        if doi_candidates:
            print(
                f"[manual_ingest] WARN {pdf_path.name} scraped DOIs {doi_candidates}"
                " not manifest-backed; relying on metadata title linkage…",
            )

    if doi is None and embedded_title:
        key = _normalize_pdf_title(embedded_title)
        candidates = title_buckets.get(key)
        if not candidates:
            log_error(str(pdf_path.name), "manual_ingest", f"{trace} unmatched title fallback")
            return "failed_match", pdf_path, "No DOI or metadata title linkage."

        if len(candidates) > 1:
            log_error(
                str(pdf_path.name),
                "manual_ingest",
                f"{trace} ambiguous title collisions DOIs={candidates}",
            )
            return "failed_ambiguous_title", pdf_path, f"ambiguous title linkage {candidates}"

        doi = candidates[0]

    if doi is None:
        log_error(str(pdf_path.name), "manual_ingest", f"{trace} missing linkage clues")
        return "failed_unknown", pdf_path, "Neither DOI nor unique title linkage."

    record = manifest_by_doi.get(doi)
    if not record:
        log_error(doi, "manual_ingest", "DOI not present in OA manifest.")
        return "failed_manifest_miss", pdf_path, f"No manifest entry for DOI={doi}"

    dest_path = RAW_DIR / descriptive_pdf_filename(record)

    if resolve_existing_pdf_path(RAW_DIR, record) is not None:
        target = skipped_existing_root / pdf_path.name
        target = _unique_child(target.parent / target.name)
        shutil.move(str(pdf_path), str(target))
        note = (
            f"{trace} corpus already owns {resolve_existing_pdf_path(RAW_DIR, record)!s}; "
            f"duplicate inbox copy moved aside to skipped/{target.name}"
        )
        print(f"[manual_ingest] SKIP_EXISTS doi={doi} file={pdf_path.name}")
        return "skipped_existing_raw", None, note

    if dest_path.exists():
        alt = skipped_existing_root / pdf_path.name
        alt = _unique_child(alt.parent / alt.name)
        shutil.move(str(pdf_path), str(alt))
        note = (
            f"{trace} conflicting destination {dest_path.name}; inbox copy moved to skipped/{alt.name}"
        )
        print(f"[manual_ingest] SKIP_DEST_CONFLICT doi={doi} dest={dest_path.name}")
        return "skipped_conflict", None, note

    shutil.move(str(pdf_path), str(dest_path))
    print(f"[manual_ingest] IMPORTED doi={doi} → {dest_path.name}")

    if doi not in manual_registered:
        with open(MANUAL_MANIFEST_PATH, "a", encoding="utf-8") as mh:
            mh.write(_canonical_json_line(record) + "\n")
        manual_registered.add(doi)
        print("[manual_ingest] appended manual_manifest.jsonl")

    else:
        print(f"[manual_ingest] manual_manifest.jsonl already records {doi}")

    return "imported", None, trace


def _process_inbox(inbox_root: Path) -> defaultdict[str, int]:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    failed_root = inbox_root / "failed"
    skipped_existing_root = inbox_root / "skipped_existing_in_raw"
    failed_root.mkdir(parents=True, exist_ok=True)
    skipped_existing_root.mkdir(parents=True, exist_ok=True)

    counters: defaultdict[str, int] = defaultdict(int)
    manifest_by_doi, title_buckets = _load_manifest_indexes(MANIFEST_PATH)

    MANUAL_MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    manual_registered = _manual_manifest_existing_dois(MANUAL_MANIFEST_PATH)

    inbox_pdfs = sorted(
        path
        for path in inbox_root.iterdir()
        if path.is_file() and path.suffix.lower() == ".pdf"
    )

    print(f"[manual_ingest] inbox_root={inbox_root} pdf_count={len(inbox_pdfs)}")
    if not inbox_pdfs:
        print("[manual_ingest] No PDF candidates at inbox root.")
        return counters

    for pdf_path in inbox_pdfs:
        outcome, failure_source, detail = ingest_one_pdf(
            pdf_path,
            manifest_by_doi=manifest_by_doi,
            title_buckets=title_buckets,
            manual_registered=manual_registered,
            skipped_existing_root=skipped_existing_root,
        )

        if outcome.startswith("failed"):
            tomb = failed_root / pdf_path.name
            tomb = _unique_child(tomb.parent / tomb.name)
            movable = failure_source if failure_source and failure_source.exists() else pdf_path
            try:
                shutil.move(str(movable), str(tomb))
                print(f"[manual_ingest] FAILED({outcome}) {pdf_path.name} → failed/{tomb.name}")
            except Exception as exc:  # noqa: BLE001
                log_error(
                    str(movable.name),
                    "manual_ingest",
                    f"unable to relocate failure {movable}: {exc} ({detail})",
                )

        counters[outcome] += 1

    print("[manual_ingest] ===== summary =====")
    for key in sorted(counters.keys()):
        print(f"{'': >10}{key:>35}: {counters[key]}")
    return counters


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Ingest legal OA acquisitions from data/manual_inbox into data/raw/.",
    )
    parser.add_argument(
        "--inbox",
        type=Path,
        default=Path("data") / "manual_inbox",
        help="Folder containing arbitrarily named PDF acquisitions (default: data/manual_inbox)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    inbox = args.inbox.expanduser().resolve()
    inbox.mkdir(parents=True, exist_ok=True)

    if not MANIFEST_PATH.exists():
        print(
            "[manual_ingest] manifest missing at data/manifest.jsonl — collect first.",
            file=sys.stderr,
        )
        return 1

    counts = _process_inbox(inbox)
    failures = sum(value for key, value in counts.items() if key.startswith("failed"))

    if failures:
        print(
            "[manual_ingest] Some rows landed in inbox/failed/; review data/error_log.jsonl "
            "for structured notes.",
        )
        return 1

    print("[manual_ingest] All inbox PDFs completed without linkage failures.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
