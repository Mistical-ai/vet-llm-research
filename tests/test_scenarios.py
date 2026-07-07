from __future__ import annotations

import json
from pathlib import Path

import pytest

from file_paths import legacy_doi_filename
from scenarios import PrimaryResearchCorpusScenario, ScenarioPaths, VeterinarySummaryQualityScenario
from scenarios.corpus_status import load_manifest_records


def _write_jsonl(path: Path, rows: list[dict | str]) -> None:
    """Write compact JSONL fixtures, preserving raw malformed rows for tests."""
    lines = [row if isinstance(row, str) else json.dumps(row) for row in rows]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _scenario(tmp_path: Path) -> PrimaryResearchCorpusScenario:
    """Build a corpus scenario that cannot touch the real data directory."""
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    paths = ScenarioPaths(
        manifest_path=tmp_path / "manifest.jsonl",
        manual_manifest_path=tmp_path / "manual_manifest.jsonl",
        raw_dir=raw_dir,
        processed_dir=tmp_path / "processed",
        summaries_path=tmp_path / "summaries.jsonl",
    )
    return PrimaryResearchCorpusScenario(paths)


def test_scenario_imports_and_metadata_are_stable() -> None:
    corpus = PrimaryResearchCorpusScenario()
    summary_quality = VeterinarySummaryQualityScenario()

    assert corpus.metadata()["name"] == "primary_research_corpus"
    assert corpus.metadata()["corpus_target"] == 250
    assert summary_quality.metadata()["uses_live_api"] is False
    assert "clinical_topic" in summary_quality.metadata()["stratification_fields"]


def test_load_manifest_records_deduplicates_and_validates_manual_pdfs(tmp_path: Path) -> None:
    scenario = _scenario(tmp_path)
    manual_doi = "10.1111/manual.1"
    invalid_manual_doi = "10.1111/missing.1"
    errors: list[tuple[str, str, str]] = []

    _write_jsonl(
        scenario.paths.manifest_path,
        [
            {"doi": "10.1111/oa.1", "journal": "JVIM", "title": "OA paper"},
            {"doi": "10.1111/oa.1", "journal": "JVIM", "title": "Duplicate OA paper"},
            "{malformed json",
        ],
    )
    _write_jsonl(
        scenario.paths.manual_manifest_path,
        [
            {"doi": "10.1111/oa.1", "journal": "JVIM", "title": "Manual duplicate"},
            {"doi": manual_doi, "journal": "JAVMA", "title": "Manual with PDF"},
            {"doi": invalid_manual_doi, "journal": "VRU", "title": "Manual without PDF"},
            {"journal": "JFMS", "title": "Manual missing DOI"},
        ],
    )
    (scenario.paths.raw_dir / legacy_doi_filename(manual_doi)).write_bytes(b"%PDF-1.4")

    corpus_seed = load_manifest_records(
        scenario,
        error_logger=lambda doi, stage, message: errors.append((doi, stage, message)),
    )

    assert [record["doi"] for record in corpus_seed["records"]] == ["10.1111/oa.1", manual_doi]
    assert corpus_seed["oa_count"] == 1
    assert corpus_seed["manual_count"] == 1
    assert corpus_seed["invalid_manual"] == [{
        "doi": invalid_manual_doi,
        "title": "Manual without PDF",
        "reason": "PDF not found in data/raw/",
    }]
    assert any("Malformed OA manifest line 3" in message for _doi, _stage, message in errors)
    assert any("Manual manifest line 4: missing doi" in message for _doi, _stage, message in errors)


def test_corpus_status_counts_primary_secondary_and_missing_pdfs(tmp_path: Path) -> None:
    scenario = _scenario(tmp_path)
    primary_doi = "10.1111/primary.1"
    secondary_doi = "10.1111/secondary.1"
    missing_doi = "10.1111/missing.1"

    _write_jsonl(
        scenario.paths.manifest_path,
        [
            {"doi": primary_doi, "journal": "JVIM", "title": "Primary paper"},
            {"doi": secondary_doi, "journal": "JVIM", "title": "Secondary paper"},
            {"doi": missing_doi, "journal": "JAVMA", "title": "Missing paper"},
        ],
    )
    _write_jsonl(scenario.paths.manual_manifest_path, [])
    (scenario.paths.raw_dir / legacy_doi_filename(primary_doi)).write_bytes(b"%PDF-1.4")
    (scenario.paths.raw_dir / f"2_{legacy_doi_filename(secondary_doi)}").write_bytes(b"%PDF-1.4")

    corpus = scenario.load_corpus()
    report = scenario.build_status_report(corpus)

    assert corpus["downloaded_primary"] == [primary_doi]
    assert corpus["downloaded_secondary"] == [secondary_doi]
    assert corpus["missing_pdfs"] == [missing_doi]
    assert scenario.counts_toward_quota({"doi": primary_doi}) is True
    assert scenario.counts_toward_quota({"doi": secondary_doi}) is False
    assert report.n_primary == 1
    assert report.n_secondary == 1
    assert report.n_total == 2
    assert report.journal_counts["JVIM"]["primary"] == 1
    assert report.journal_counts["JVIM"]["secondary"] == 1


def test_pipeline_cli_scenario_flag_keeps_default_behavior(monkeypatch) -> None:
    import pipeline

    loaded_scenarios: list[str] = []

    def fake_load_corpus(scenario):
        loaded_scenarios.append(scenario.name)
        return {"downloaded_primary": ["doi"] * scenario.oa_threshold}

    monkeypatch.setattr(pipeline, "load_corpus", fake_load_corpus)
    monkeypatch.setattr(pipeline, "report_corpus_status", lambda corpus, scenario: None)

    assert pipeline.main([]) == 0
    assert pipeline.main(["--scenario", "primary_research_corpus"]) == 0
    assert loaded_scenarios == ["primary_research_corpus", "primary_research_corpus"]


def test_pipeline_cli_rejects_unknown_scenario_before_loading(monkeypatch) -> None:
    import pipeline

    monkeypatch.setattr(
        pipeline,
        "load_corpus",
        lambda scenario: pytest.fail("unknown scenario should fail before loading"),
    )

    with pytest.raises(SystemExit) as exc_info:
        pipeline.main(["--scenario", "unknown"])

    assert exc_info.value.code == 2
