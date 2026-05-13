"""
src/ingest_manual_pdfs.py — Bulk ingest legally obtained OA PDFs from disk
===========================================================================

Drop arbitrary PDF filenames into ``data/manual_inbox/``.  This script identifies
each paper (prefer embedded DOI strings), pulls canonical rows from
``data/manifest.jsonl``, saves under descriptive names in ``data/raw/``, and
appends duplicate-free JSON lines into ``data/manual_manifest.jsonl``.

LEGAL USE ONLY — researchers ingest manuscripts they legally obtained via OA,
publisher permissions, library services, authors, etc.  This helper never speaks
HTTP to subscription gateways.

IMPORTANT: Never edit ``manifest.jsonl`` via this script; ingestion only reads it.
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
    """Return ordered DOIs (metadata-first) plus embedding Title hints."""
    import pdfplumber  # Mirrors download/extract lazy strategy.

    try:
        with pdfplumber.open(pdf_path) as pdf:
            meta_blob = _metadata_blob(pdf.metadata)
            embedded_title = (
                str(pdf.metadata["Title"]).strip()
                if pdf.metadata and pdf.metadata.get("Title")
                else None
            )
            meta_hits = _collect_dois_from_plaintext(meta_blob)

            chunks = [meta_blob]
            scan_pages = min(5, len(pdf.pages))
            for idx in range(scan_pages):
                chunks.append(pdf.pages[idx].extract_text() or "")
            corpus = "\n".join(chunks)
            corpus_hits = _collect_dois_from_plaintext(corpus)

            ordered = list(dict.fromkeys(meta_hits + corpus_hits))
            hint = embedded_title or (ordered[0] if ordered else "unknown")
            trace = f"[pdf]={pdf_path.name} hint={hint!r}"
            return ordered, embedded_title, trace

    except Exception as exc:  # noqa: BLE001
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
