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

    assert data["status"] == "completed"
    assert data["finished_utc"] is not None
    assert sorted(data["selected_instance_ids"]) == sorted(dois)
    assert data["judges"] == ["openai"]
    assert "openai" in data["model_ids"]
    # DRY_RUN mock mode returns "<model_id>-DRYRUN" as the judge's observed
    # version — this proves resolved_model_versions reads the real judge
    # response, not just echoing the configured model_ids.
    assert data["resolved_model_versions"].get("openai", "").endswith("-DRYRUN")
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
    # Default: jsonl, regardless of mode.
    assert run_phase3._resolve_eval_input_mode("dev", None) == "jsonl"

    # .env value applies when there's no CLI override.
    monkeypatch.setenv("EVAL_INPUT_MODE", "dev")
    assert run_phase3._resolve_eval_input_mode("single", None) == "dev"

    # CLI override always wins.
    assert run_phase3._resolve_eval_input_mode("single", "regular") == "regular"

    # auto expands based on the active PHASE3_MODE.
    assert run_phase3._resolve_eval_input_mode("dev", "auto") == "dev"
    assert run_phase3._resolve_eval_input_mode("single", "auto") == "regular"
    assert run_phase3._resolve_eval_input_mode("batch", "auto") == "regular"

    # Unknown value falls back to jsonl.
    monkeypatch.setenv("EVAL_INPUT_MODE", "nonsense")
    assert run_phase3._resolve_eval_input_mode("dev", None) == "jsonl"


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

    rows = [
        json.loads(line)
        for line in _wired_paths["evaluations_path"].read_text(encoding="utf-8").splitlines()
    ]
    assert len(rows) == 1
    assert rows[0]["doi"] == "10.9999/txt.0001"
    assert rows[0]["summarizer"] == "openai"


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
