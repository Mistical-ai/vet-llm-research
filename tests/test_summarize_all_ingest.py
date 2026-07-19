"""
Tests for llm-sum/summarize_all_ingest.py — feeding evaluate() from
summarize-all's readable .txt comparison files instead of summaries.jsonl.

Critical assertions:
    The parser round-trips the exact format summarizer._format_folder_entry_as_text
    writes: header fields, per-provider status/metadata, and body text.
    A failed provider slot yields no candidate summary (never judged).
    A 'DOI: Not recorded' header is treated as no DOI (article skipped).
    Repeated runs of the same article collapse to only the latest.
    Only source_type == "processed_text" files are turned into instances —
    a PDF-input comparison file must never reach the judge.
"""

from __future__ import annotations

from pathlib import Path

import summarize_all_ingest as ingest
from summarize_all_ingest import (
    iter_summarize_all_instances,
    parse_summarize_all_txt,
    select_latest_txt_files,
)

SEP = "=" * 78


def _txt(
    *,
    source_type: str = "Processed Text",
    doi: str = "10.1111/jvim.16872",
    source_file: str = "javma__some_title__10_1111_jvim_16872.jsonl",
    providers: dict[str, dict] | None = None,
) -> str:
    """Build a synthetic .txt file matching _format_folder_entry_as_text()'s
    exact output shape, so the parser is tested against the real contract."""
    providers = providers if providers is not None else {
        "openai": {"status": "success", "model_version": "gpt-5.4-test",
                   "input_tokens": "100", "output_tokens": "50",
                   "body": "1. Objective\nDescribe the thing.\n\n2. Results\nIt worked."},
    }
    lines = [
        f"Summary Source: {source_type}",
        f"Source File: {source_file}",
        f"DOI: {doi}",
        "Slug: 10_1111_jvim_16872",
        "Generated At: 2026-07-07T01:50:57.522999+00:00",
        "",
        "This file contains one summary from each configured model provider.",
        "Compare the provider sections below against the same article source.",
    ]
    for provider, info in providers.items():
        lines += ["", SEP, f"{provider.upper()} SUMMARY", SEP,
                  f"Status: {info['status']}"]
        if info["status"] == "success":
            lines += [
                f"Model Version: {info.get('model_version', 'Not recorded')}",
                "Timestamp: 2026-07-07T01:51:20.853179+00:00",
                f"Input Tokens: {info.get('input_tokens', 'Not recorded')}",
                f"Output Tokens: {info.get('output_tokens', 'Not recorded')}",
                "",
                info.get("body", ""),
            ]
        else:
            lines += [
                "Model Version: Not recorded",
                "Timestamp: 2026-07-07T01:51:20.853179+00:00",
                "",
                f"No readable summary was produced. Error: {info.get('error', 'boom')}",
            ]
    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------------------
# parse_summarize_all_txt
# ---------------------------------------------------------------------------

def test_parses_header_and_single_provider() -> None:
    parsed = parse_summarize_all_txt(_txt())
    assert parsed["source_type"] == "processed_text"
    assert parsed["doi"] == "10.1111/jvim.16872"
    assert parsed["source_filename"] == "javma__some_title__10_1111_jvim_16872.jsonl"
    assert set(parsed["providers"]) == {"openai"}
    openai = parsed["providers"]["openai"]
    assert openai["status"] == "success"
    assert openai["model_version"] == "gpt-5.4-test"
    assert openai["input_tokens"] == 100
    assert openai["output_tokens"] == 50
    assert "Describe the thing." in openai["summary"]


def test_parses_three_providers_independently() -> None:
    text = _txt(providers={
        "openai": {"status": "success", "model_version": "gpt-5.4", "body": "Openai text."},
        "anthropic": {"status": "success", "model_version": "claude-4.6", "body": "Anthropic text."},
        "gemini": {"status": "success", "model_version": "gemini-3.5", "body": "Gemini text."},
    })
    parsed = parse_summarize_all_txt(text)
    assert set(parsed["providers"]) == {"openai", "anthropic", "gemini"}
    assert parsed["providers"]["anthropic"]["summary"] == "Anthropic text."
    assert parsed["providers"]["gemini"]["model_version"] == "gemini-3.5"


def test_failed_provider_has_no_summary() -> None:
    text = _txt(providers={
        "openai": {"status": "failed", "error": "rate limited"},
    })
    parsed = parse_summarize_all_txt(text)
    assert parsed["providers"]["openai"]["status"] == "failed"
    assert parsed["providers"]["openai"]["summary"] is None


def test_not_recorded_doi_becomes_empty_string() -> None:
    parsed = parse_summarize_all_txt(_txt(doi="Not recorded"))
    assert parsed["doi"] == ""


def test_pdf_source_type_parses_as_pdf() -> None:
    parsed = parse_summarize_all_txt(_txt(source_type="Pdf"))
    assert parsed["source_type"] == "pdf"


# ---------------------------------------------------------------------------
# select_latest_txt_files
# ---------------------------------------------------------------------------

def test_select_latest_files_dedupes_repeated_runs(tmp_path: Path) -> None:
    (tmp_path / "paperA__run_20260703T144553932153Z.txt").write_text("old", encoding="utf-8")
    (tmp_path / "paperA__run_20260707T013457897955Z.txt").write_text("new", encoding="utf-8")
    (tmp_path / "paperB__run_20260703T144553932153Z.txt").write_text("only", encoding="utf-8")

    files = select_latest_txt_files(tmp_path)
    names = sorted(f.name for f in files)
    assert names == [
        "paperA__run_20260707T013457897955Z.txt",
        "paperB__run_20260703T144553932153Z.txt",
    ]
    # The kept paperA file is the later run, not the earlier one.
    kept = next(f for f in files if f.name.startswith("paperA"))
    assert kept.read_text(encoding="utf-8") == "new"


def test_select_latest_files_respects_paper_limit(tmp_path: Path) -> None:
    for i in range(5):
        (tmp_path / f"paper{i}.txt").write_text("x", encoding="utf-8")
    files = select_latest_txt_files(tmp_path, paper_limit=3)
    assert len(files) == 3


def test_select_latest_files_missing_folder_returns_empty(tmp_path: Path) -> None:
    assert select_latest_txt_files(tmp_path / "does_not_exist") == []


def test_select_latest_files_no_run_suffix_each_is_its_own_group(tmp_path: Path) -> None:
    """SUMMARIZE_ALL_UNIQUE_OUTPUT=false writes plain <stem>.txt with no run
    suffix — each file is its own article, no dedup should happen."""
    (tmp_path / "paperA.txt").write_text("a", encoding="utf-8")
    (tmp_path / "paperB.txt").write_text("b", encoding="utf-8")
    files = select_latest_txt_files(tmp_path)
    assert len(files) == 2


# ---------------------------------------------------------------------------
# iter_summarize_all_instances
# ---------------------------------------------------------------------------

def _write_processed_cache(
    processed_dir: Path, filename: str, text: str, doi: str = "10.1111/jvim.16872",
) -> None:
    import json
    processed_dir.mkdir(parents=True, exist_ok=True)
    (processed_dir / filename).write_text(
        json.dumps({"doi": doi, "text": text}) + "\n", encoding="utf-8"
    )


# This input mode judges the flowing summary_text out of data/summaries.jsonl,
# not the bulleted body carried by the summarize-all .txt (see
# ingest._load_prose_by_doi_provider). So every test that expects instances to
# come back has to seed a summaries.jsonl too.
def _write_summaries_jsonl(
    path: Path, entries: list[tuple[str, dict[str, str]]],
) -> Path:
    """Write a summaries.jsonl of ``(doi, {provider: prose})`` rows."""
    import json
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for doi, prose_by_provider in entries:
            f.write(json.dumps({
                "doi": doi,
                "input_source": "processed",
                "models": {
                    provider: {"status": "success", "summary": prose}
                    for provider, prose in prose_by_provider.items()
                },
            }) + "\n")
    return path


def test_iter_instances_builds_one_per_successful_provider(
    tmp_path: Path, monkeypatch,
) -> None:
    processed_dir = tmp_path / "processed"
    monkeypatch.setattr(ingest, "PROCESSED_DIR", processed_dir)
    import prepare_texts
    monkeypatch.setattr(prepare_texts, "PROCESSED_DIR", processed_dir)

    txt_dir = tmp_path / "summaries_txt"
    txt_dir.mkdir()
    filename = "javma__some_title__10_1111_jvim_16872.jsonl"
    _write_processed_cache(processed_dir, filename, "Full cleaned article body. " * 20)
    (txt_dir / "paperA.txt").write_text(
        _txt(source_file=filename, providers={
            "openai": {"status": "success", "body": "Openai summary text."},
            "anthropic": {"status": "failed", "error": "timeout"},
        }),
        encoding="utf-8",
    )
    summaries = _write_summaries_jsonl(
        tmp_path / "summaries.jsonl",
        [("10.1111/jvim.16872", {"openai": "Openai PROSE summary_text."})],
    )

    instances = list(iter_summarize_all_instances(txt_dir, summaries_path=summaries))
    assert len(instances) == 1  # anthropic failed -> not judged
    inst = instances[0]
    assert inst.doi == "10.1111/jvim.16872"
    assert inst.summarizer == "openai"
    # The prose from summaries.jsonl, NOT the bulleted body in the .txt.
    assert inst.candidate_summary == "Openai PROSE summary_text."
    assert "Full cleaned article body." in inst.reference_text
    assert inst.input_source == "processed"


def test_iter_instances_skips_pdf_source_type(tmp_path: Path, monkeypatch) -> None:
    processed_dir = tmp_path / "processed"
    monkeypatch.setattr(ingest, "PROCESSED_DIR", processed_dir)
    import prepare_texts
    monkeypatch.setattr(prepare_texts, "PROCESSED_DIR", processed_dir)

    txt_dir = tmp_path / "summaries_pdf"
    txt_dir.mkdir()
    (txt_dir / "paperA.txt").write_text(_txt(source_type="Pdf"), encoding="utf-8")

    instances = list(iter_summarize_all_instances(txt_dir))
    assert instances == []


def test_iter_instances_skips_when_reference_text_missing(
    tmp_path: Path, monkeypatch,
) -> None:
    processed_dir = tmp_path / "processed"
    processed_dir.mkdir()
    monkeypatch.setattr(ingest, "PROCESSED_DIR", processed_dir)
    import prepare_texts
    monkeypatch.setattr(prepare_texts, "PROCESSED_DIR", processed_dir)

    txt_dir = tmp_path / "summaries_txt"
    txt_dir.mkdir()
    # No matching processed/*.jsonl cache written -> reference text unresolved.
    (txt_dir / "paperA.txt").write_text(_txt(), encoding="utf-8")

    instances = list(iter_summarize_all_instances(txt_dir))
    assert instances == []


def test_iter_instances_skips_not_recorded_doi(tmp_path: Path, monkeypatch) -> None:
    processed_dir = tmp_path / "processed"
    monkeypatch.setattr(ingest, "PROCESSED_DIR", processed_dir)
    import prepare_texts
    monkeypatch.setattr(prepare_texts, "PROCESSED_DIR", processed_dir)

    txt_dir = tmp_path / "summaries_txt"
    txt_dir.mkdir()
    (txt_dir / "paperA.txt").write_text(_txt(doi="Not recorded"), encoding="utf-8")

    instances = list(iter_summarize_all_instances(txt_dir))
    assert instances == []


def test_iter_instances_respects_paper_limit(tmp_path: Path, monkeypatch) -> None:
    processed_dir = tmp_path / "processed"
    monkeypatch.setattr(ingest, "PROCESSED_DIR", processed_dir)
    import prepare_texts
    monkeypatch.setattr(prepare_texts, "PROCESSED_DIR", processed_dir)

    txt_dir = tmp_path / "summaries_txt"
    txt_dir.mkdir()
    rows: list[tuple[str, dict[str, str]]] = []
    for i in range(5):
        doi = f"10.9999/paper.{i:04d}"
        filename = f"journal__title{i}__{doi.replace('.', '_').replace('/', '_')}.jsonl"
        _write_processed_cache(processed_dir, filename, f"Body of paper {i}. " * 20, doi=doi)
        text = _txt(doi=doi, source_file=filename)
        (txt_dir / f"paper{i}.txt").write_text(text, encoding="utf-8")
        rows.append((doi, {"openai": f"Prose for paper {i}."}))
    summaries = _write_summaries_jsonl(tmp_path / "summaries.jsonl", rows)

    instances = list(
        iter_summarize_all_instances(txt_dir, paper_limit=2, summaries_path=summaries)
    )
    assert len({i.doi for i in instances}) == 2


# The point of routing this mode through summaries.jsonl: a provider slot with
# no prose is dropped (with a warning) rather than silently judged on the
# bulleted body, which would make this mode disagree with every other one.
def test_iter_instances_skips_slots_with_no_prose(tmp_path: Path, monkeypatch, capsys) -> None:
    processed_dir = tmp_path / "processed"
    monkeypatch.setattr(ingest, "PROCESSED_DIR", processed_dir)
    import prepare_texts
    monkeypatch.setattr(prepare_texts, "PROCESSED_DIR", processed_dir)

    txt_dir = tmp_path / "summaries_txt"
    txt_dir.mkdir()
    filename = "javma__some_title__10_1111_jvim_16872.jsonl"
    _write_processed_cache(processed_dir, filename, "Full cleaned article body. " * 20)
    (txt_dir / "paperA.txt").write_text(
        _txt(source_file=filename, providers={
            "openai": {"status": "success", "body": "Bulleted body only."},
        }),
        encoding="utf-8",
    )
    # summaries.jsonl exists but has no row for this DOI -> nothing to judge.
    summaries = _write_summaries_jsonl(tmp_path / "summaries.jsonl", [])

    instances = list(iter_summarize_all_instances(txt_dir, summaries_path=summaries))
    assert instances == []
    out = capsys.readouterr().out
    assert "no prose summary_text" in out
    assert "10.1111/jvim.16872 (openai)" in out
