"""
Tests for the run-manifest wiring in llm-sum/run_phase3.py::cmd_evaluate().

These are wiring-level tests: they exercise the actual CLI entrypoint (via
the real argparse parser) in PHASE3_MODE=test (mock judge, $0, no live API
calls) and assert a run_manifest_*.json appears with the expected shape.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _default_judge_panel(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the default judge set to the full 3-judge panel.

    These are wiring tests; the default is now the openai,anthropic,gemini
    panel, so we pin it explicitly for determinism (rather than depending on the
    ambient .env JUDGE_MODELS / JURY_PRESET) while still exercising the real
    3-judge default. Tests that opt into a different set via --judges/--jury
    still override this (CLI wins in resolve_judges).
    """
    import evaluator

    monkeypatch.setattr(evaluator, "JUDGE_MODELS", list(evaluator.JURY_PANEL))
    monkeypatch.delenv("JURY_PRESET", raising=False)


def _write_fake_summaries(summaries_path: Path, processed_dir: Path, dois: list[str]) -> None:
    from file_paths import doi_to_slug

    processed_dir.mkdir(parents=True, exist_ok=True)
    with open(summaries_path, "w", encoding="utf-8") as f:
        for doi in dois:
            slug = doi_to_slug(doi)
            (processed_dir / f"{slug}.jsonl").write_text(
                json.dumps({"doi": doi, "slug": slug, "text": "Reference body. " * 30}) + "\n",
                encoding="utf-8",
            )
            f.write(json.dumps({
                "doi": doi,
                "custom_id": slug,
                "models": {
                    "openai": {
                        "status": "success",
                        "summary": f"summary for {doi}",
                        "model_version": "gpt-5.4-test",
                        "input_tokens": 100, "output_tokens": 50,
                    }
                },
            }) + "\n")


@pytest.fixture()
def _wired_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Redirect every module-level path constant this wiring touches into tmp_path."""
    import evaluator
    import eval_report
    import eval_instances
    import prepare_texts
    import run_phase3

    summaries_path = tmp_path / "summaries.jsonl"
    evaluations_path = tmp_path / "evaluations.jsonl"
    processed_dir = tmp_path / "processed"

    monkeypatch.setattr(evaluator, "SUMMARIES_PATH", summaries_path)
    monkeypatch.setattr(evaluator, "EVALUATIONS_PATH", evaluations_path)
    monkeypatch.setattr(evaluator, "PROCESSED_DIR", processed_dir)
    monkeypatch.setattr(prepare_texts, "PROCESSED_DIR", processed_dir)
    monkeypatch.setattr(eval_report, "EVALUATIONS_PATH", evaluations_path)
    monkeypatch.setattr(run_phase3, "RUN_MANIFEST_DIR", tmp_path)
    # write_dev_eval_jsonl_outputs/write_dev_detail_eval_outputs load the real
    # manifest by default when no manifest_index is passed in; point that at
    # tmp (nonexistent) paths too so tests never touch data/manifest.jsonl.
    monkeypatch.setattr(eval_instances, "MANIFEST_PATH", tmp_path / "manifest.jsonl")
    monkeypatch.setattr(eval_instances, "MANUAL_MANIFEST_PATH", tmp_path / "manual_manifest.jsonl")

    return {
        "summaries_path": summaries_path,
        "evaluations_path": evaluations_path,
        "processed_dir": processed_dir,
        "manifest_dir": tmp_path,
    }


def _run_evaluate(argv: list[str]):
    import run_phase3

    parser = run_phase3.build_parser()
    args = parser.parse_args(argv)
    return run_phase3.cmd_evaluate(args)


def _find_manifest(manifest_dir: Path) -> Path:
    manifests = sorted(manifest_dir.glob("run_manifest_*.json"))
    assert len(manifests) == 1, f"expected exactly one manifest, found {manifests}"
    return manifests[0]


def test_evaluate_test_mode_writes_completed_run_manifest(_wired_paths) -> None:
    dois = ["10.9999/manifest.0001", "10.9999/manifest.0002"]
    _write_fake_summaries(_wired_paths["summaries_path"], _wired_paths["processed_dir"], dois)

    result = _run_evaluate(["evaluate", "--mode", "test"])

    assert result == 0
    manifest_path = _find_manifest(_wired_paths["manifest_dir"])
    data = json.loads(manifest_path.read_text(encoding="utf-8"))

    from evaluator import JURY_PANEL

    assert data["status"] == "completed"
    assert data["finished_utc"] is not None
    assert sorted(data["selected_instance_ids"]) == sorted(dois)
    # Default is now the full 3-judge panel.
    assert data["judges"] == JURY_PANEL
    assert all(j in data["model_ids"] for j in JURY_PANEL)
    # DRY_RUN mock mode returns "<model_id>-DRYRUN" as the judge's observed
    # version — this proves resolved_model_versions reads the real judge
    # response, not just echoing the configured model_ids. Every panel judge
    # should carry that mock marker.
    for judge in JURY_PANEL:
        assert data["resolved_model_versions"].get(judge, "").endswith("-DRYRUN")
    assert data["evaluation_config"]["mode"] == "test"
    taxonomy = data["evaluation_config"]["taxonomy"]
    assert taxonomy["taxonomy_id"] == "vet_taxonomy_v1"
    assert taxonomy["task_key"] == "veterinary_summary_quality"


def _write_summarize_all_txt(
    txt_dir: Path, processed_dir: Path, *, doi: str, stem: str = "paperA",
) -> None:
    """Write one summarize-all processed-text comparison file plus its
    matching data/processed/*.jsonl cache, mirroring the real on-disk shape
    of `run_phase3.py summarize-all` output."""
    txt_dir.mkdir(parents=True, exist_ok=True)
    processed_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{stem}.jsonl"
    (processed_dir / filename).write_text(
        json.dumps({"doi": doi, "text": "Reference body text. " * 30}) + "\n",
        encoding="utf-8",
    )
    sep = "=" * 78
    text = "\n".join([
        "Summary Source: Processed Text",
        f"Source File: {filename}",
        f"DOI: {doi}",
        f"Slug: {stem}",
        "Generated At: 2026-07-07T01:50:57.522999+00:00",
        "",
        "This file contains one summary from each configured model provider.",
        "Compare the provider sections below against the same article source.",
        "",
        sep, "OPENAI SUMMARY", sep,
        "Status: success",
        "Model Version: gpt-5.4-test",
        "Timestamp: 2026-07-07T01:51:20.853179+00:00",
        "Input Tokens: 100",
        "Output Tokens: 50",
        "",
        "A candidate summary for this article.",
        "",
    ]) + "\n"
    (txt_dir / f"{stem}.txt").write_text(text, encoding="utf-8")


def test_resolve_eval_input_mode_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    import run_phase3

    monkeypatch.delenv("EVAL_INPUT_MODE", raising=False)
    # Default for non-dev modes: jsonl.
    assert run_phase3._resolve_eval_input_mode("single", None) == "jsonl"
    # Dev mode with no --input-mode always resolves to the folder-driven loop,
    # regardless of EVAL_INPUT_MODE.
    assert run_phase3._resolve_eval_input_mode("dev", None) == "dev-jsonl"

    # .env value applies when there's no CLI override (non-dev mode).
    monkeypatch.setenv("EVAL_INPUT_MODE", "dev")
    assert run_phase3._resolve_eval_input_mode("single", None) == "dev"
    # ...but EVAL_INPUT_MODE never overrides the dev-mode default.
    assert run_phase3._resolve_eval_input_mode("dev", None) == "dev-jsonl"

    # CLI override always wins — including forcing a dev run back to jsonl.
    assert run_phase3._resolve_eval_input_mode("single", "regular") == "regular"
    assert run_phase3._resolve_eval_input_mode("dev", "jsonl") == "jsonl"
    assert run_phase3._resolve_eval_input_mode("dev", "dev") == "dev"

    # auto expands based on the active PHASE3_MODE (explicit CLI, so no dev default).
    assert run_phase3._resolve_eval_input_mode("dev", "auto") == "dev"
    assert run_phase3._resolve_eval_input_mode("single", "auto") == "regular"
    assert run_phase3._resolve_eval_input_mode("batch", "auto") == "regular"

    # Unknown value falls back to jsonl (non-dev mode).
    monkeypatch.setenv("EVAL_INPUT_MODE", "nonsense")
    assert run_phase3._resolve_eval_input_mode("single", None) == "jsonl"


def test_evaluate_dev_input_mode_reads_txt_folder_not_jsonl(
    _wired_paths, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--input-mode dev judges data/dev_tests/summaries_txt directly and
    never touches data/summaries.jsonl (left empty/nonexistent here)."""
    import run_phase3
    import summarize_all_ingest

    txt_dir = _wired_paths["manifest_dir"] / "dev_tests_summaries_txt"
    monkeypatch.setattr(run_phase3, "DEV_TESTS_SUMMARIES_TXT_DIR", txt_dir)
    monkeypatch.setattr(summarize_all_ingest, "PROCESSED_DIR", _wired_paths["processed_dir"])
    _write_summarize_all_txt(
        txt_dir, _wired_paths["processed_dir"], doi="10.9999/txt.0001",
    )

    result = _run_evaluate(["evaluate", "--mode", "test", "--input-mode", "dev"])

    assert result == 0
    assert not _wired_paths["summaries_path"].exists()

    manifest_path = _find_manifest(_wired_paths["manifest_dir"])
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert data["status"] == "completed"
    assert data["selected_instance_ids"] == ["10.9999/txt.0001"]
    assert data["evaluation_config"]["slice_config"]["type"] == "summarize_all_txt"
    assert data["evaluation_config"]["slice_config"]["input_mode"] == "dev"

    from evaluator import JURY_PANEL

    rows = [
        json.loads(line)
        for line in _wired_paths["evaluations_path"].read_text(encoding="utf-8").splitlines()
    ]
    # One article × one summarizer × the 3-judge default = 3 rows.
    assert len(rows) == 3
    assert {row["doi"] for row in rows} == {"10.9999/txt.0001"}
    assert {row["summarizer"] for row in rows} == {"openai"}
    assert {row["judge"] for row in rows} == set(JURY_PANEL)


def test_evaluate_dev_input_mode_no_files_returns_error(
    _wired_paths, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import run_phase3

    empty_txt_dir = _wired_paths["manifest_dir"] / "empty_dev_txt"
    monkeypatch.setattr(run_phase3, "DEV_TESTS_SUMMARIES_TXT_DIR", empty_txt_dir)

    result = _run_evaluate(["evaluate", "--mode", "test", "--input-mode", "dev"])

    assert result == 1
    # No manifest is written when there is nothing to evaluate.
    assert list(_wired_paths["manifest_dir"].glob("run_manifest_*.json")) == []


def _journal_records(journal: str, n: int) -> list[dict]:
    return [{"doi": f"10.1/{journal}.{i}", "journal": journal} for i in range(n)]


def test_sample_round_robin_by_journal_one_per_journal_when_limit_matches() -> None:
    """limit == number of journals -> exactly one paper per journal."""
    import run_phase3

    by_journal = {
        "jvim": _journal_records("jvim", 3),
        "javma": _journal_records("javma", 3),
        "jfms": _journal_records("jfms", 3),
    }

    selected = run_phase3._sample_round_robin_by_journal(by_journal, limit=3, seed=42)

    assert len(selected) == 3
    assert {r["journal"] for r in selected} == {"jvim", "javma", "jfms"}


def test_sample_round_robin_by_journal_limit_below_journal_count() -> None:
    """limit < number of journals -> only that many journals are touched."""
    import run_phase3

    by_journal = {
        "jvim": _journal_records("jvim", 1),
        "javma": _journal_records("javma", 1),
        "jfms": _journal_records("jfms", 1),
    }

    selected = run_phase3._sample_round_robin_by_journal(by_journal, limit=2, seed=1)

    assert len(selected) == 2
    journals = {r["journal"] for r in selected}
    assert len(journals) == 2
    assert journals.issubset({"jvim", "javma", "jfms"})


def test_sample_round_robin_by_journal_spills_over_when_limit_exceeds_journals() -> None:
    """limit > number of journals -> every journal gets 1 before any gets 2."""
    import run_phase3

    by_journal = {
        "jvim": _journal_records("jvim", 3),
        "javma": _journal_records("javma", 3),
    }

    selected = run_phase3._sample_round_robin_by_journal(by_journal, limit=3, seed=7)

    assert len(selected) == 3
    counts: dict[str, int] = {}
    for r in selected:
        counts[r["journal"]] = counts.get(r["journal"], 0) + 1
    # Both journals got at least 1 before either got a 2nd (round-robin, not
    # "drain journal A first").
    assert counts.get("jvim", 0) >= 1
    assert counts.get("javma", 0) >= 1
    assert sum(counts.values()) == 3


def test_sample_round_robin_by_journal_is_deterministic_given_seed() -> None:
    import run_phase3

    by_journal = {
        "jvim": _journal_records("jvim", 5),
        "javma": _journal_records("javma", 5),
    }

    first = run_phase3._sample_round_robin_by_journal(by_journal, limit=2, seed=42)
    second = run_phase3._sample_round_robin_by_journal(by_journal, limit=2, seed=42)

    assert [r["doi"] for r in first] == [r["doi"] for r in second]


def test_sample_round_robin_by_journal_empty_journal_is_dropped() -> None:
    """A journal with no eligible records never gets selected from."""
    import run_phase3

    by_journal = {
        "jvim": _journal_records("jvim", 1),
        "empty": [],
    }

    selected = run_phase3._sample_round_robin_by_journal(by_journal, limit=5, seed=42)

    assert len(selected) == 1
    assert selected[0]["journal"] == "jvim"


def test_load_manifest_by_journal_only_keeps_records_with_cached_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    manifest.jsonl rows without a cached processed-text file (never
    downloaded, or extraction failed) must be excluded — this is what makes
    noise manifest rows fall out of dev-mode journal-random selection
    automatically, without an explicit journal allowlist.
    """
    import prepare_texts
    import run_phase3
    from file_paths import doi_to_slug

    manifest_path = tmp_path / "manifest.jsonl"
    processed = tmp_path / "processed"
    processed.mkdir()
    monkeypatch.setattr(prepare_texts, "PROCESSED_DIR", processed)

    has_text_doi = "10.9999/has_text.0001"
    no_text_doi = "10.9999/no_text.0001"
    no_journal_doi = "10.9999/no_journal.0001"

    with open(manifest_path, "w", encoding="utf-8") as f:
        f.write(json.dumps({"doi": has_text_doi, "journal": "JVIM"}) + "\n")
        f.write(json.dumps({"doi": no_text_doi, "journal": "JVIM"}) + "\n")
        f.write(json.dumps({"doi": no_journal_doi, "journal": ""}) + "\n")

    slug = doi_to_slug(has_text_doi)
    (processed / f"{slug}.jsonl").write_text(
        json.dumps({"doi": has_text_doi, "slug": slug, "text": "body"}) + "\n",
        encoding="utf-8",
    )

    by_journal = run_phase3._load_manifest_by_journal(manifest_path, "processed")

    assert list(by_journal.keys()) == ["jvim"]
    assert [r["doi"] for r in by_journal["jvim"]] == [has_text_doi]


def test_cmd_summarize_dev_mode_selects_one_per_journal_and_writes_readable_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    End-to-end (mocked, PHASE3_MODE=test/DRY_RUN) check of the dev-mode
    journal-random wiring in cmd_summarize: exactly one DOI per journal goes
    through --doi-filter, data/summaries.jsonl gets exactly that many rows,
    and data/dev_summaries_jsonl/ gets one readable .txt per selected paper.
    """
    import prepare_texts
    import run_phase3
    import summarizer
    from file_paths import doi_to_slug

    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setattr(summarizer, "DRY_RUN", True)
    monkeypatch.setenv("PHASE3_DEV_LIMIT", "2")
    monkeypatch.setenv("PHASE3_DEV_SAMPLE_SEED", "42")

    manifest_path = tmp_path / "manifest.jsonl"
    processed = tmp_path / "processed"
    processed.mkdir()
    summaries_path = tmp_path / "summaries.jsonl"
    dev_dir = tmp_path / "dev_summaries_jsonl"

    journals = {"jvim": 3, "javma": 3}
    dois_by_journal: dict[str, list[str]] = {}
    with open(manifest_path, "w", encoding="utf-8") as f:
        for journal, n in journals.items():
            dois = [f"10.9999/{journal}.{i:04d}" for i in range(n)]
            dois_by_journal[journal] = dois
            for doi in dois:
                record = {"doi": doi, "journal": journal, "title": f"Paper {doi}"}
                f.write(json.dumps(record) + "\n")
                slug = doi_to_slug(doi)
                (processed / f"{slug}.jsonl").write_text(
                    json.dumps({"doi": doi, "slug": slug, "text": "Body text. " * 30}) + "\n",
                    encoding="utf-8",
                )

    monkeypatch.setattr(run_phase3, "PROCESSED_DIR", processed)
    monkeypatch.setattr(prepare_texts, "PROCESSED_DIR", processed)
    monkeypatch.setattr(summarizer, "PROCESSED_DIR", processed)
    monkeypatch.setattr(summarizer, "SUMMARIES_PATH", summaries_path)
    monkeypatch.setattr(run_phase3, "DEV_SUMMARIES_JSONL_DIR", dev_dir)
    # confirm_real_batch's --force bypass writes an audit line to LOGS_DIR —
    # redirect it so this test doesn't pollute the real data/logs/phase3_safety.log.
    monkeypatch.setattr(summarizer, "LOGS_DIR", tmp_path / "logs")

    parser = run_phase3.build_parser()
    args = parser.parse_args([
        "summarize", "--mode", "dev", "--manifest", str(manifest_path),
        "--providers", "openai", "--force",
    ])
    result = run_phase3.cmd_summarize(args)

    assert result == 0

    rows = [
        json.loads(line) for line in summaries_path.read_text(encoding="utf-8").splitlines()
    ]
    assert len(rows) == 2
    assert {row["journal"] for row in rows} == {"jvim", "javma"}

    txt_files = list(dev_dir.glob("*.txt"))
    assert len(txt_files) == 2


def _write_dev_readable_txt(folder: Path, doi: str, *, suffix: str | None = None) -> Path:
    """Write a minimal dev-mode readable .txt whose first line is ``DOI: <doi>``."""
    from file_paths import doi_to_slug

    folder.mkdir(parents=True, exist_ok=True)
    stem = doi_to_slug(doi)
    if suffix:
        stem = f"{stem}__{suffix}"
    path = folder / f"{stem}.txt"
    path.write_text(f"DOI: {doi}\nJournal: jvim\n\nbody\n", encoding="utf-8")
    return path


def test_read_dois_from_dev_folder_parses_headers(tmp_path: Path) -> None:
    import run_phase3

    folder = tmp_path / "dev_summaries_jsonl"
    # Same DOI under two timestamped suffixes must collapse to one DOI (we parse
    # the header, not the filename).
    _write_dev_readable_txt(folder, "10.1/aaa", suffix="run_A")
    _write_dev_readable_txt(folder, "10.1/aaa", suffix="run_B")
    _write_dev_readable_txt(folder, "10.2/bbb")

    assert run_phase3._read_dois_from_dev_folder(folder) == {"10.1/aaa", "10.2/bbb"}
    # Missing folder → empty set, not an error.
    assert run_phase3._read_dois_from_dev_folder(tmp_path / "nope") == set()


def _wire_dev_jsonl(_wired_paths, monkeypatch, run_phase3):
    """Point the dev folders at tmp dirs; return (source_dir, evals_dir)."""
    source_dir = _wired_paths["manifest_dir"] / "dev_summaries_jsonl"
    evals_dir = _wired_paths["manifest_dir"] / "dev_evals_jsonl"
    detail_dir = _wired_paths["manifest_dir"] / "dev_detailEval_reports"
    monkeypatch.setattr(run_phase3, "DEV_SUMMARIES_JSONL_DIR", source_dir)
    monkeypatch.setattr(run_phase3, "DEV_EVALS_JSONL_DIR", evals_dir)
    monkeypatch.setattr(run_phase3, "DEV_DETAIL_EVAL_REPORTS_DIR", detail_dir)
    return source_dir, evals_dir


def test_evaluate_dev_jsonl_reads_dev_folder_and_writes_dev_evals(
    _wired_paths, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`evaluate` in dev-jsonl mode judges exactly the DOIs sitting in
    data/dev_summaries_jsonl/ and mirrors the scores into data/dev_evals_jsonl/."""
    import run_phase3

    source_dir, evals_dir = _wire_dev_jsonl(_wired_paths, monkeypatch, run_phase3)
    dois = ["10.9999/dev.0001", "10.9999/dev.0002"]
    _write_fake_summaries(_wired_paths["summaries_path"], _wired_paths["processed_dir"], dois)
    for doi in dois:
        _write_dev_readable_txt(source_dir, doi, suffix="run_x")

    # --mode test keeps this at $0/mock while --input-mode dev-jsonl forces the
    # new folder-driven branch.
    result = _run_evaluate(["evaluate", "--mode", "test", "--input-mode", "dev-jsonl"])
    assert result == 0

    manifest_path = _find_manifest(_wired_paths["manifest_dir"])
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert data["status"] == "completed"
    assert data["evaluation_config"]["slice_config"]["type"] == "dev_jsonl"
    assert data["selected_instance_ids"] == sorted(dois)

    eval_files = sorted(evals_dir.glob("*.txt"))
    assert len(eval_files) == 2
    for path in eval_files:
        assert path.read_text(encoding="utf-8").startswith("DOI:")


def test_evaluate_dev_jsonl_skips_already_evaluated(
    _wired_paths, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A DOI already present in data/dev_evals_jsonl/ is not re-judged."""
    import run_phase3

    source_dir, evals_dir = _wire_dev_jsonl(_wired_paths, monkeypatch, run_phase3)
    dois = ["10.9999/dev.0001", "10.9999/dev.0002"]
    _write_fake_summaries(_wired_paths["summaries_path"], _wired_paths["processed_dir"], dois)
    for doi in dois:
        _write_dev_readable_txt(source_dir, doi)
    # Pretend dev.0001 was judged in an earlier run.
    _write_dev_readable_txt(evals_dir, "10.9999/dev.0001")

    result = _run_evaluate(["evaluate", "--mode", "test", "--input-mode", "dev-jsonl"])
    assert result == 0

    manifest_path = _find_manifest(_wired_paths["manifest_dir"])
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert data["selected_instance_ids"] == ["10.9999/dev.0002"]

    rows = [
        json.loads(line)
        for line in _wired_paths["evaluations_path"].read_text(encoding="utf-8").splitlines()
    ]
    assert {row["doi"] for row in rows} == {"10.9999/dev.0002"}


def test_evaluate_dev_jsonl_no_resume_reincludes_done(
    _wired_paths, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--no-resume re-includes DOIs already in data/dev_evals_jsonl/."""
    import run_phase3

    source_dir, evals_dir = _wire_dev_jsonl(_wired_paths, monkeypatch, run_phase3)
    dois = ["10.9999/dev.0001", "10.9999/dev.0002"]
    _write_fake_summaries(_wired_paths["summaries_path"], _wired_paths["processed_dir"], dois)
    for doi in dois:
        _write_dev_readable_txt(source_dir, doi)
    _write_dev_readable_txt(evals_dir, "10.9999/dev.0001")

    result = _run_evaluate(
        ["evaluate", "--mode", "test", "--input-mode", "dev-jsonl", "--no-resume"]
    )
    assert result == 0

    manifest_path = _find_manifest(_wired_paths["manifest_dir"])
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert data["selected_instance_ids"] == sorted(dois)


def test_evaluate_dev_jsonl_empty_source_returns_error(
    _wired_paths, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import run_phase3

    _wire_dev_jsonl(_wired_paths, monkeypatch, run_phase3)
    result = _run_evaluate(["evaluate", "--mode", "test", "--input-mode", "dev-jsonl"])
    assert result == 1


def test_evaluate_dev_jsonl_all_done_returns_zero_noop(
    _wired_paths, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import run_phase3

    source_dir, evals_dir = _wire_dev_jsonl(_wired_paths, monkeypatch, run_phase3)
    _write_fake_summaries(
        _wired_paths["summaries_path"], _wired_paths["processed_dir"], ["10.9999/dev.0001"]
    )
    _write_dev_readable_txt(source_dir, "10.9999/dev.0001")
    _write_dev_readable_txt(evals_dir, "10.9999/dev.0001")

    result = _run_evaluate(["evaluate", "--mode", "test", "--input-mode", "dev-jsonl"])
    # Everything already evaluated → clean no-op, no manifest written.
    assert result == 0
    assert not list(_wired_paths["manifest_dir"].glob("run_manifest_*.json"))


def _setup_dev_summarize(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Shared wiring for the dev-mode summarize skip tests. Returns paths."""
    import prepare_texts
    import run_phase3
    import summarizer
    from file_paths import doi_to_slug

    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setattr(summarizer, "DRY_RUN", True)
    monkeypatch.setenv("PHASE3_DEV_LIMIT", "2")
    monkeypatch.setenv("PHASE3_DEV_SAMPLE_SEED", "42")

    manifest_path = tmp_path / "manifest.jsonl"
    processed = tmp_path / "processed"
    processed.mkdir()
    summaries_path = tmp_path / "summaries.jsonl"
    dev_dir = tmp_path / "dev_summaries_jsonl"

    dois_by_journal: dict[str, list[str]] = {}
    with open(manifest_path, "w", encoding="utf-8") as f:
        for journal in ("jvim", "javma"):
            dois = [f"10.9999/{journal}.{i:04d}" for i in range(3)]
            dois_by_journal[journal] = dois
            for doi in dois:
                f.write(json.dumps({"doi": doi, "journal": journal, "title": f"Paper {doi}"}) + "\n")
                slug = doi_to_slug(doi)
                (processed / f"{slug}.jsonl").write_text(
                    json.dumps({"doi": doi, "slug": slug, "text": "Body text. " * 30}) + "\n",
                    encoding="utf-8",
                )

    monkeypatch.setattr(run_phase3, "PROCESSED_DIR", processed)
    monkeypatch.setattr(prepare_texts, "PROCESSED_DIR", processed)
    monkeypatch.setattr(summarizer, "PROCESSED_DIR", processed)
    monkeypatch.setattr(summarizer, "SUMMARIES_PATH", summaries_path)
    monkeypatch.setattr(run_phase3, "DEV_SUMMARIES_JSONL_DIR", dev_dir)
    monkeypatch.setattr(summarizer, "LOGS_DIR", tmp_path / "logs")
    return manifest_path, summaries_path, dev_dir, dois_by_journal


def _run_dev_summarize(manifest_path: Path, extra: list[str] | None = None) -> int:
    import run_phase3

    parser = run_phase3.build_parser()
    args = parser.parse_args(
        ["summarize", "--mode", "dev", "--manifest", str(manifest_path),
         "--providers", "openai", "--force"] + (extra or [])
    )
    return run_phase3.cmd_summarize(args)


def test_cmd_summarize_dev_skips_dois_already_in_dev_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Papers already written to data/dev_summaries_jsonl/ are excluded from the
    dev-mode random pick, so a re-run picks from the remainder."""
    manifest_path, summaries_path, dev_dir, dois_by_journal = _setup_dev_summarize(
        tmp_path, monkeypatch
    )
    # All javma papers already summarised in a prior dev run.
    for doi in dois_by_journal["javma"]:
        _write_dev_readable_txt(dev_dir, doi)

    assert _run_dev_summarize(manifest_path) == 0

    rows = [json.loads(l) for l in summaries_path.read_text(encoding="utf-8").splitlines()]
    # javma is fully excluded; only jvim papers can be picked.
    assert rows, "expected at least one summarised paper"
    assert {row["journal"] for row in rows} == {"jvim"}
    assert all(not row["doi"].startswith("10.9999/javma") for row in rows)


def test_cmd_summarize_dev_no_skip_existing_reconsiders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--no-skip-existing ignores the dev folder and picks one per journal again."""
    manifest_path, summaries_path, dev_dir, dois_by_journal = _setup_dev_summarize(
        tmp_path, monkeypatch
    )
    for doi in dois_by_journal["javma"]:
        _write_dev_readable_txt(dev_dir, doi)

    assert _run_dev_summarize(manifest_path, ["--no-skip-existing"]) == 0

    rows = [json.loads(l) for l in summaries_path.read_text(encoding="utf-8").splitlines()]
    # With skipping disabled, both journals are eligible again.
    assert {row["journal"] for row in rows} == {"jvim", "javma"}


def test_evaluate_marks_manifest_failed_on_exception(_wired_paths, monkeypatch: pytest.MonkeyPatch) -> None:
    dois = ["10.9999/manifest.0003"]
    _write_fake_summaries(_wired_paths["summaries_path"], _wired_paths["processed_dir"], dois)

    import evaluator

    def _boom(**kwargs):
        raise RuntimeError("simulated crash mid-evaluation")

    monkeypatch.setattr(evaluator, "run_evaluation", _boom)

    with pytest.raises(RuntimeError, match="simulated crash"):
        _run_evaluate(["evaluate", "--mode", "test"])

    manifest_path = _find_manifest(_wired_paths["manifest_dir"])
    data = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert data["status"] == "failed"
    assert data["finished_utc"] is not None
