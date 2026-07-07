from __future__ import annotations

import json
from pathlib import Path

from file_paths import descriptive_stem
from evaluation import rubric_scoring


REPO_ROOT = Path(__file__).resolve().parent.parent
RUBRIC_PATH = REPO_ROOT / "docs" / "rubrics" / "rubric_v1.yaml"


def test_rubric_schema_is_valid() -> None:
    rubric = rubric_scoring.load_rubric(RUBRIC_PATH)

    rubric_scoring.validate_rubric(rubric)

    assert rubric["version"] == "rubric_v1"
    assert tuple(rubric["dimensions"].keys()) == rubric_scoring.DIMENSIONS
    for dimension in rubric_scoring.DIMENSIONS:
        assert set(rubric["dimensions"][dimension]["anchors"].keys()) == {1, 2, 3, 4, 5}


def test_score_output_has_stable_keys() -> None:
    result = rubric_scoring.score_output(
        model_output=(
            "Objective: assess treatment outcomes in dogs. Methods included a "
            "retrospective study. Results found improved clinical management, "
            "with limitations from small sample size."
        ),
        source_text=(
            "This retrospective study assessed treatment outcomes in dogs. "
            "Results supported improved clinical management but were limited "
            "by small sample size."
        ),
        rubric_path=RUBRIC_PATH,
    )

    assert set(result.keys()) == {"rubric_version", "scores", "overall_score"}
    assert result["rubric_version"] == "rubric_v1"
    assert set(result["scores"].keys()) == set(rubric_scoring.DIMENSIONS)
    assert 1 <= result["overall_score"] <= 5
    for dimension_score in result["scores"].values():
        assert set(dimension_score.keys()) == {"score", "rationale"}
        assert 1 <= dimension_score["score"] <= 5
        assert dimension_score["rationale"]


def test_reference_text_prefers_processed_cache_over_manifest_abstract(tmp_path: Path) -> None:
    processed_dir = tmp_path / "processed"
    processed_dir.mkdir()
    doi = "10.1111/processed.ref"
    record = {
        "doi": doi,
        "journal": "JVIM",
        "title": "Processed preference paper",
        "abstract": "Short abstract only.",
    }
    processed_text = "Full article body with XylophoneOutcomeMarker in dogs."
    cache_path = processed_dir / f"{descriptive_stem(record)}.jsonl"
    cache_path.write_text(
        json.dumps({"doi": doi, "text": processed_text}) + "\n",
        encoding="utf-8",
    )

    assert rubric_scoring._reference_text_for(record, processed_dir) == processed_text


def test_rubric_scoring_uses_processed_reference_for_grounding(tmp_path: Path) -> None:
    processed_dir = tmp_path / "processed"
    processed_dir.mkdir()
    summaries_path = tmp_path / "summaries.jsonl"
    output_path = tmp_path / "rubric_scores.jsonl"
    doi = "10.1111/processed.ground"
    record = {
        "doi": doi,
        "journal": "JVIM",
        "title": "Grounding preference paper",
        "abstract": "Abstract without the distinctive outcome marker.",
    }
    processed_text = "Dogs showed XylophoneOutcomeMarker after treatment in this full article body."
    (processed_dir / f"{descriptive_stem(record)}.jsonl").write_text(
        json.dumps({"doi": doi, "text": processed_text}) + "\n",
        encoding="utf-8",
    )
    summaries_path.write_text(
        json.dumps({
            "doi": doi,
            "models": {
                "openai": {
                    "status": "success",
                    "summary": "Dogs showed XylophoneOutcomeMarker after treatment.",
                },
            },
        }) + "\n",
        encoding="utf-8",
    )

    written = rubric_scoring.write_rubric_scores(
        summaries_path=summaries_path,
        manifest_records=[record],
        rubric_path=RUBRIC_PATH,
        processed_dir=processed_dir,
        output_path=output_path,
    )
    row = json.loads(output_path.read_text(encoding="utf-8").strip())

    assert written == 1
    assert row["scores"]["factual_accuracy"]["score"] == 5
    assert row["scores"]["hallucination_risk"]["score"] >= 4


def test_write_rubric_scores_uses_existing_summaries_only(tmp_path: Path) -> None:
    summaries_path = tmp_path / "summaries.jsonl"
    output_path = tmp_path / "rubric_scores.jsonl"
    doi = "10.1111/test.rubric"
    summaries_path.write_text(
        json.dumps({
            "doi": doi,
            "models": {
                "openai": {
                    "status": "success",
                    "summary": "Dogs improved after treatment in a retrospective study.",
                },
                "anthropic": {"status": "failed", "summary": ""},
                "gemini": {
                    "status": "success",
                    "summary": "The canine clinical outcome improved, with limitations noted.",
                },
            },
        }) + "\n",
        encoding="utf-8",
    )

    written = rubric_scoring.write_rubric_scores(
        summaries_path=summaries_path,
        manifest_records=[{
            "doi": doi,
            "abstract": "A retrospective study of dogs found improved clinical outcomes after treatment.",
        }],
        rubric_path=RUBRIC_PATH,
        output_path=output_path,
    )

    rows = [json.loads(line) for line in output_path.read_text(encoding="utf-8").splitlines()]
    assert written == 2
    assert [row["summarizer"] for row in rows] == ["openai", "gemini"]
    assert all(row["doi"] == doi for row in rows)
    assert all(row["rubric_version"] == "rubric_v1" for row in rows)
    assert all(set(row["scores"].keys()) == set(rubric_scoring.DIMENSIONS) for row in rows)


def test_missing_summaries_file_writes_nothing(tmp_path: Path) -> None:
    output_path = tmp_path / "rubric_scores.jsonl"

    written = rubric_scoring.write_rubric_scores(
        summaries_path=tmp_path / "missing_summaries.jsonl",
        manifest_records=[{"doi": "10.1111/missing", "abstract": "Dogs improved."}],
        rubric_path=RUBRIC_PATH,
        output_path=output_path,
    )

    assert written == 0
    assert not output_path.exists()


def test_pipeline_use_rubric_flag_is_optional_and_separate(monkeypatch) -> None:
    import pipeline

    calls: list[dict] = []

    def fake_load_corpus(scenario):
        return {
            "records": [{"doi": "10.1111/test", "abstract": "Dogs improved."}],
            "downloaded_primary": ["doi"] * scenario.oa_threshold,
        }

    def fake_write_rubric_scores(**kwargs):
        calls.append(kwargs)
        return 1

    monkeypatch.setattr(pipeline, "load_corpus", fake_load_corpus)
    monkeypatch.setattr(pipeline, "report_corpus_status", lambda corpus, scenario: None)
    monkeypatch.setattr(rubric_scoring, "write_rubric_scores", fake_write_rubric_scores)

    assert pipeline.main([]) == 0
    assert calls == []

    assert pipeline.main(["--use-rubric"]) == 0
    assert len(calls) == 1
    assert calls[0]["output_path"] == Path("data") / "rubric_scores.jsonl"
