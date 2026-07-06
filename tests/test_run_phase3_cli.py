from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.hashing import dataset_hash
from validation.frozen_sets import stable_instance_sort


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def _write_frozen_set(path: Path, rows: list[dict]) -> None:
    _write_jsonl(path, rows)
    digest = dataset_hash(stable_instance_sort(rows))
    path.with_suffix(".manifest.json").write_text(
        json.dumps({"name": "test", "path": str(path), "sha256": digest, "row_count": len(rows)}),
        encoding="utf-8",
    )


def test_run_phase3_summarize_delegates_legacy_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    import run_phase3
    import summarizer

    captured: dict[str, list[str]] = {}

    def fake_summarize_main(argv: list[str]) -> int:
        captured["argv"] = argv
        return 0

    monkeypatch.setattr(summarizer, "main", fake_summarize_main)

    ret = run_phase3.main(
        [
            "summarize",
            "--mode",
            "test",
            "--limit",
            "2",
            "--resume",
            "--providers",
            "openai",
            "--input-source",
            "processed",
            "--guide-summary",
            "guide.txt",
        ]
    )

    assert ret == 0
    assert captured["argv"] == [
        "--mode",
        "test",
        "--limit",
        "2",
        "--resume",
        "--providers",
        "openai",
        "--input-source",
        "processed",
        "--guide-summary",
        "guide.txt",
    ]


def test_run_phase3_evaluate_frozen_set_keeps_legacy_output_by_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import evaluator
    import run_phase3

    frozen_path = tmp_path / "frozen.jsonl"
    _write_frozen_set(
        frozen_path,
        [
            {"instance_id": "tiny-001", "doi": "10.1/a"},
            {"instance_id": "tiny-002", "doi": "10.1/b"},
        ],
    )
    captured: dict[str, object] = {}

    def fake_run_evaluation(**kwargs: object) -> dict[str, int]:
        captured.update(kwargs)
        return {"evaluated": 2, "skipped": 0, "failed": 0, "no_text": 0}

    monkeypatch.setattr(evaluator, "run_evaluation", fake_run_evaluation)

    ret = run_phase3.main(["evaluate", "--mode", "test", "--frozen-set", str(frozen_path)])

    assert ret == 0
    assert captured["doi_filter"] == {"10.1/a", "10.1/b"}
    assert captured["output_path"] is None


def test_run_phase3_evaluate_run_dir_uses_run_artifact_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import evaluator
    import run_phase3

    frozen_path = tmp_path / "frozen.jsonl"
    run_dir = tmp_path / "run-artifacts"
    _write_frozen_set(frozen_path, [{"instance_id": "tiny-001", "doi": "10.1/a"}])
    captured: dict[str, object] = {}

    def fake_run_evaluation(**kwargs: object) -> dict[str, int]:
        captured.update(kwargs)
        output_path = kwargs["output_path"]
        assert isinstance(output_path, Path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(
                {
                    "doi": "10.1/a",
                    "summarizer": "openai",
                    "judge": "openai",
                    "judge_model_version": "gpt-test-2026",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        return {"evaluated": 1, "skipped": 0, "failed": 0, "no_text": 0}

    monkeypatch.setattr(evaluator, "run_evaluation", fake_run_evaluation)

    ret = run_phase3.main(
        ["evaluate", "--mode", "test", "--frozen-set", str(frozen_path), "--run-dir", str(run_dir)]
    )

    manifest = json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8"))
    assert ret == 0
    assert captured["output_path"] == run_dir / "evaluations.jsonl"
    assert manifest["artifact_paths"]["evaluations"].endswith("evaluations.jsonl")
    assert manifest["selected_instance_ids"] == ["tiny-001"]
    assert manifest["resolved_model_versions"]["openai"] == "gpt-test-2026"


def test_run_phase3_eval_report_exports_with_seed(tmp_path: Path) -> None:
    import run_phase3

    evaluations = tmp_path / "evaluations.jsonl"
    output_dir = tmp_path / "reports"
    _write_jsonl(
        evaluations,
        [
            {
                "benchmark_name": "vet_lit_summary_medhelm",
                "doi": "10.1/a",
                "summarizer": "openai",
                "judge": "openai",
                "jury_score": 4.0,
                "hallucination_count": 0,
                "requires_human_review": False,
                "strata": {"journal": "JVIM", "input_source": "processed"},
            }
        ],
    )

    ret = run_phase3.main(
        [
            "eval-report",
            "--evaluations",
            str(evaluations),
            "--output-dir",
            str(output_dir),
            "--bootstrap-reps",
            "10",
            "--seed",
            "42",
        ]
    )

    assert ret == 0
    assert (output_dir / "summary.json").exists()
    assert (output_dir / "strata_summary.csv").exists()
    assert (output_dir / "paired_model_comparison.csv").exists()
