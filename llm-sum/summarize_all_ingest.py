"""
llm-sum/summarize_all_ingest.py — Feed evaluate() from summarize-all's .txt files
==================================================================================

WHY THIS MODULE EXISTS
-----------------------
`run_phase3.py summarize-all` writes one human-readable ``.txt`` comparison
file per article to ``data/summaries_txt/`` (or ``data/dev_tests/summaries_txt/``
in dev mode) — each file holds every provider's summary of the same processed
article text, side by side, for manual reading. That workflow is independent
of `run_phase3.py summarize`, which writes the machine-readable
``data/summaries.jsonl`` that `evaluate` has always read.

This module lets `evaluate` read directly from those ``.txt`` files instead,
for a researcher who has been using `summarize-all` and does not have a
populated `summaries.jsonl`. It only reads the *processed-text* comparison
files (never the PDF-input side in ``summaries_pdf/``) — see
docs/phase3/evaluator.md for why PDF-input evaluation is out of scope here.

It does not change how judging works: this module only builds the same
``EvaluationInstance`` objects that `eval_instances.py` builds from
`summaries.jsonl`, so `evaluator.run_evaluation()` scores them identically
either way (same judge calls, same parsing, same rubric).

FILE FORMAT
-----------
Written by ``summarizer._format_folder_entry_as_text()``. A header block of
``Key: value`` lines, then one ``===`` delimited section per provider:

    Summary Source: Processed Text
    Source File: <processed jsonl filename>
    DOI: <doi, or 'Not recorded'>
    Slug: <slug>
    Generated At: <ISO-8601 timestamp>

    ==============================================================================
    OPENAI SUMMARY
    ==============================================================================
    Status: success
    Model Version: <id>
    Timestamp: <ISO-8601>
    Input Tokens: <int>
    Output Tokens: <int>

    <summary body text>

REPEATED RUNS OF THE SAME ARTICLE
----------------------------------
When ``SUMMARIZE_ALL_UNIQUE_OUTPUT=true`` (the default), each run writes a
new timestamped file instead of overwriting the last one, so the same article
can have several files on disk (e.g. ``<stem>__run_20260703T....txt`` and
``<stem>__run_20260707T....txt``). Judging every copy would double-count the
same paper and waste judge calls, so this module keeps only the most recent
run per article (comparing the run suffix, which sorts correctly as plain
text because it is a zero-padded timestamp).
"""

from __future__ import annotations

from _bootstrap import *  # noqa: F401,F403

import re
from pathlib import Path
from typing import Any, Iterable

from eval_instances import EvaluationInstance, build_strata, load_manifest_index  # noqa: E402
from prepare_texts import read_cached_text, read_processed_jsonl  # noqa: E402

_SEPARATOR = "=" * 78
_KV_LINE = re.compile(r"^([A-Za-z][A-Za-z0-9 _-]*):\s*(.*)$")
_RUN_SUFFIX = re.compile(r"^(?P<base>.+)__run_\d{8}T\d{6,}Z$")


# ---------------------------------------------------------------------------
# File selection: dedupe repeated runs, keep the latest per article
# ---------------------------------------------------------------------------

def _base_stem(stem: str) -> str:
    """Strip a ``__run_<timestamp>Z`` suffix so repeated runs of the same
    article group together. Files without the suffix are their own group."""
    match = _RUN_SUFFIX.match(stem)
    return match.group("base") if match else stem


def select_latest_txt_files(folder: Path, *, paper_limit: int | None = None) -> list[Path]:
    """Return one ``.txt`` file per article — the most recent run — sorted by
    name for deterministic ordering, optionally capped to ``paper_limit``
    articles (``None`` = every article found).
    """
    if not folder.exists():
        return []
    latest: dict[str, Path] = {}
    for path in sorted(folder.glob("*.txt")):
        base = _base_stem(path.stem)
        # Filenames sort correctly as plain text because the run suffix is a
        # zero-padded timestamp, so the lexicographically larger stem is the
        # more recent run.
        if base not in latest or path.stem > latest[base].stem:
            latest[base] = path
    files = sorted(latest.values(), key=lambda p: p.stem)
    return files[:paper_limit]


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def _parse_kv_block(lines: list[str]) -> tuple[dict[str, str], list[str]]:
    """Parse leading ``Key: value`` lines up to the first blank line.

    Returns (fields, remaining lines after that blank line) so the caller can
    keep reading the rest of the block (the summary body or error text).
    """
    fields: dict[str, str] = {}
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            break
        match = _KV_LINE.match(line)
        if match:
            fields[match.group(1).strip()] = match.group(2).strip()
        i += 1
    return fields, lines[i:]


def _parse_int(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def parse_summarize_all_txt(text: str) -> dict[str, Any]:
    """Parse one summarize-all ``.txt`` comparison file into a structured dict.

    Returns::

        {
          "source_type": "processed_text" | "pdf" | "unknown",
          "source_filename": str | None,
          "doi": str,                    # "" when not recorded
          "slug": str | None,
          "generated_at": str | None,
          "providers": {
             "openai": {"status": "success", "model_version": ..., ...,
                        "summary": str | None},
             ...
          },
        }

    Tolerant of hand-edited or truncated files: an unparseable field is left
    absent/None rather than raising, since this only ever feeds a read-only
    evaluation pass, never a write path.
    """
    lines = text.splitlines()
    sep_indices = [i for i, ln in enumerate(lines) if ln == _SEPARATOR]

    header_lines = lines[: sep_indices[0]] if sep_indices else lines
    header_fields, _ = _parse_kv_block(header_lines)

    source_type = header_fields.get("Summary Source", "").strip().lower().replace(" ", "_") or "unknown"
    doi_raw = header_fields.get("DOI", "")
    doi = "" if doi_raw.strip().lower() == "not recorded" else doi_raw.strip()

    providers: dict[str, dict[str, Any]] = {}
    i = 0
    while i + 1 < len(sep_indices):
        top, bottom = sep_indices[i], sep_indices[i + 1]
        provider_name = lines[top + 1].strip()
        if provider_name.upper().endswith(" SUMMARY"):
            provider_name = provider_name[: -len(" SUMMARY")]
        provider_name = provider_name.strip().lower()

        content_end = sep_indices[i + 2] if i + 2 < len(sep_indices) else len(lines)
        block_lines = lines[bottom + 1: content_end]
        meta, body_lines = _parse_kv_block(block_lines)
        body = "\n".join(body_lines).strip()
        status = meta.get("Status", "").strip().lower()

        providers[provider_name] = {
            "status": status or "unknown",
            "model_version": meta.get("Model Version"),
            "timestamp": meta.get("Timestamp"),
            "input_tokens": _parse_int(meta.get("Input Tokens")),
            "output_tokens": _parse_int(meta.get("Output Tokens")),
            "summary": body if status == "success" and body else None,
        }
        i += 2

    return {
        "source_type": source_type,
        "source_filename": header_fields.get("Source File") or None,
        "doi": doi,
        "slug": header_fields.get("Slug") or None,
        "generated_at": header_fields.get("Generated At") or None,
        "providers": providers,
    }


# ---------------------------------------------------------------------------
# Reference text lookup
# ---------------------------------------------------------------------------

def _read_reference_text(parsed: dict[str, Any]) -> str | None:
    """Resolve the cleaned article text this summary was built from.

    Prefers an exact match on ``source_filename`` (the descriptive
    ``data/processed/*.jsonl`` name summarize-all recorded at generation
    time) and falls back to the DOI-based lookup used everywhere else in the
    pipeline, in case the processed cache was regenerated/renamed since.
    """
    source_filename = parsed.get("source_filename")
    if source_filename:
        text = read_processed_jsonl(PROCESSED_DIR / source_filename)
        if text is not None:
            return text
    doi = parsed.get("doi")
    if doi:
        return read_cached_text({"doi": doi}, input_source="processed")
    return None


# ---------------------------------------------------------------------------
# Instance iterator — same EvaluationInstance shape as eval_instances.py
# ---------------------------------------------------------------------------

def iter_summarize_all_instances(
    folder: Path,
    *,
    manifest_path: Path | None = None,
    manual_manifest_path: Path | None = None,
    paper_limit: int | None = None,
    files: list[Path] | None = None,
) -> Iterable[EvaluationInstance]:
    """Yield one EvaluationInstance per successful provider slot found in
    ``folder``'s summarize-all ``.txt`` files.

    Only ``source_type == "processed_text"`` files are scored — a direct-PDF
    comparison file (``source_type == "pdf"``) is silently skipped, since PDF
    summaries are intentionally out of scope for this input mode (see the
    module docstring). Pass ``files`` to judge an already-resolved file list
    (e.g. one computed once by ``select_latest_txt_files`` and reused for
    both provenance hashing and instance building).
    """
    selected = files if files is not None else select_latest_txt_files(folder, paper_limit=paper_limit)
    manifest_index = load_manifest_index(manifest_path, manual_manifest_path)

    for path in selected:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        parsed = parse_summarize_all_txt(text)
        if parsed["source_type"] != "processed_text":
            continue
        doi = parsed["doi"]
        if not doi:
            continue

        reference_text = _read_reference_text(parsed)
        if reference_text is None:
            continue

        summary_record = {
            "doi": doi,
            "input_source": "processed",
            "source_filename": parsed.get("source_filename"),
        }
        manifest_record = manifest_index.get(doi, {})
        strata = build_strata(summary_record, manifest_record)

        for provider, slot in parsed["providers"].items():
            if slot.get("status") != "success" or not slot.get("summary"):
                continue
            yield EvaluationInstance(
                doi=doi,
                summarizer=provider,
                reference_text=reference_text,
                candidate_summary=str(slot["summary"]),
                input_source="processed",
                strata=strata,
                summary_record=summary_record,
                manifest_record=manifest_record,
            )
