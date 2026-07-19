from __future__ import annotations

import json
from pathlib import Path

import eval_instances
import summarizer
import eval_report
from conftest import make_eval_row
from eval_report import (
    build_report,
    main,
    summarize_rows,
    _dedupe_by_doi_summarizer,
    _markdown_stratum_table,
    _render_markdown,
    _render_markdown_detail,
)


def _isolate_detail_lookups(monkeypatch, tmp_path: Path) -> None:
    """Point every path the detail renderer's best-effort joins touch at
    empty tmp_path locations, so tests never read the real repo's
    data/summaries.jsonl, data/*summaries_txt/, or data/manifest.jsonl."""
    monkeypatch.setattr(eval_report, "SUMMARIES_TXT_DIR", tmp_path / "no_summaries_txt")
    monkeypatch.setattr(eval_report, "DEV_TESTS_SUMMARIES_TXT_DIR", tmp_path / "no_dev_summaries_txt")
    monkeypatch.setattr(summarizer, "SUMMARIES_PATH", tmp_path / "no_summaries.jsonl")
    monkeypatch.setattr(eval_instances, "MANIFEST_PATH", tmp_path / "no_manifest.jsonl")
    monkeypatch.setattr(eval_instances, "MANUAL_MANIFEST_PATH", tmp_path / "no_manual_manifest.jsonl")


def test_summarize_rows_by_summarizer() -> None:
    # Two distinct papers, one judge each. The doi matters: aggregation is
    # item-weighted, so a row with no (doi, summarizer) identity cannot be
    # attributed to an item — see test_summarize_rows_surfaces_unkeyed_rows.
    rows = [
        {
            "doi": "10.1/a",
            "judge": "openai",
            "summarizer": "openai",
            "jury_score": 4.0,
            "jury_score_weighted": 4.2,
            "jury_score_unweighted": 4.0,
            "hallucination_count": 0,
            "confidence_score": 5,
            "parse_method": "json",
            "judge_disagreement": 0.0,
            "hallucination_claims": [],
        },
        {
            "doi": "10.1/b",
            "judge": "openai",
            "summarizer": "openai",
            "jury_score": 2.0,
            "jury_score_weighted": 1.8,
            "jury_score_unweighted": 2.0,
            "hallucination_count": 1,
            "confidence_score": 2,
            "parse_method": "json",
            "judge_disagreement": 1.0,
            "hallucination_claims": [{"severity": "major"}],
        },
    ]
    summary = summarize_rows(rows, group_field="summarizer")
    assert summary[0]["group"] == "openai"
    assert summary[0]["mean_score"] == 3.0
    assert summary[0]["mean_score_weighted"] == 3.0
    assert summary[0]["mean_score_unweighted"] == 3.0
    assert summary[0]["hallucination_rate"] == 0.5
    assert summary[0]["major_hallucination_rate"] == 0.5
    assert summary[0]["low_confidence_rate"] == 0.5


def test_build_report_groups_by_species_from_strata() -> None:
    rows = [
        {
            "benchmark_name": "vet_lit_summary_medhelm",
            "summarizer": "gemini",
            "jury_score": 5.0,
            "hallucination_count": 0,
            "confidence_score": 5,
            "parse_method": "json",
            "strata": {
                "species": ["Feline"],
                "study_design": "Retrospective",
                "clinical_topic": "Oncology",
                "journal": "JVIM",
                "input_source": "processed",
            },
            "hallucination_claims": [],
        }
    ]
    report = build_report(rows)
    assert report["by_species"][0]["group"] == "Feline"
    assert report["by_clinical_topic"][0]["group"] == "Oncology"


def test_build_report_includes_taxonomy_header() -> None:
    report = build_report([])
    taxonomy = report["taxonomy"]
    assert taxonomy["taxonomy_id"] == "vet_taxonomy_v1"
    assert taxonomy["task_key"] == "veterinary_summary_quality"


def test_build_report_reliability_unavailable_for_single_judge() -> None:
    """Single-judge runs still get a reliability block, marked unavailable."""
    rows = [
        {"doi": "d1", "summarizer": "openai", "input_source": "processed",
         "judge": "openai", "jury_score": 4.0},
    ]
    report = build_report(rows)
    assert report["reliability"]["available"] is False
    assert report["reliability"]["n_judges"] == 1


def test_build_report_reliability_available_for_jury() -> None:
    """Two judges scoring the same summary → an available reliability block."""

    def _row(judge: str, jury: float) -> dict:
        return {
            "doi": "d1", "summarizer": "openai", "input_source": "processed",
            "judge": judge, "jury_score": jury,
            "criteria_scores": {
                "faithfulness": {"score": 4, "reasoning": ""},
                "completeness": {"score": 4, "reasoning": ""},
                "clinical_usefulness": {"score": 4, "reasoning": ""},
                "clarity": {"score": 4, "reasoning": ""},
                "safety": {"score": 4, "reasoning": ""},
            },
        }

    report = build_report([_row("openai", 4.0), _row("anthropic", 4.0)])
    reliability = report["reliability"]
    assert reliability["available"] is True
    assert reliability["n_judges"] == 2
    assert reliability["n_comparable_items"] == 1
    # Identical scores → perfect agreement.
    assert reliability["jury_score"]["krippendorff_alpha"] == 1.0


def test_main_saves_report_to_results_dir(tmp_path: Path) -> None:
    evaluations_path = tmp_path / "evaluations.jsonl"
    evaluations_path.write_text(
        json.dumps({"doi": "d1", "summarizer": "openai", "jury_score": 4.0}) + "\n",
        encoding="utf-8",
    )
    results_dir = tmp_path / "results"

    result = main(["--evaluations", str(evaluations_path), "--results-dir", str(results_dir)])

    assert result == 0
    saved = sorted(results_dir.glob("eval_report_*.json"))
    assert len(saved) == 1
    saved_report = json.loads(saved[0].read_text(encoding="utf-8"))
    assert saved_report["by_summarizer"][0]["group"] == "openai"


def test_main_no_save_skips_results_file(tmp_path: Path) -> None:
    evaluations_path = tmp_path / "evaluations.jsonl"
    evaluations_path.write_text(
        json.dumps({"doi": "d1", "summarizer": "openai", "jury_score": 4.0}) + "\n",
        encoding="utf-8",
    )
    results_dir = tmp_path / "results"

    result = main([
        "--evaluations", str(evaluations_path),
        "--results-dir", str(results_dir),
        "--no-save",
    ])

    assert result == 0
    assert not results_dir.exists()


def test_main_empty_evaluations_does_not_create_results_file(tmp_path: Path) -> None:
    evaluations_path = tmp_path / "evaluations.jsonl"
    results_dir = tmp_path / "results"

    result = main(["--evaluations", str(evaluations_path), "--results-dir", str(results_dir)])

    assert result == 0
    assert not results_dir.exists()


def test_render_markdown_headline_and_glossary() -> None:
    rows = [
        {
            "benchmark_name": "vet_lit_summary_medhelm",
            "summarizer": "openai",
            "jury_score": 4.0,
            "jury_score_weighted": 4.2,
            "jury_score_unweighted": 4.0,
            "hallucination_count": 0,
            "confidence_score": 5,
            "parse_method": "json",
            "hallucination_claims": [],
        },
        {
            "benchmark_name": "vet_lit_summary_medhelm",
            "summarizer": "anthropic",
            "jury_score": 3.0,
            "jury_score_weighted": 3.1,
            "jury_score_unweighted": 3.0,
            "hallucination_count": 1,
            "confidence_score": 4,
            "parse_method": "json",
            "hallucination_claims": [{"severity": "minor"}],
        },
    ]
    report = build_report(rows)

    markdown = _render_markdown(report, rows)

    assert "## Headline" in markdown
    assert "Unweighted score (primary)" in markdown
    assert "Weighted score (secondary)" in markdown
    # mean_score is a redundant third field dropped from the readable view.
    assert "mean_score" not in markdown
    # All parse_failure_rate values are 0 in this fixture -- no column, no footnote.
    assert "ParseFail" not in markdown


def test_markdown_stratum_table_bolds_all_ties() -> None:
    rows = [
        {"group": "openai", "n_items": 5, "n_rows": 15, "mean_score_unweighted": 4.0,
         "mean_score_weighted": 4.1, "hallucination_rate": 0.2, "major_hallucination_rate": 0.0},
        {"group": "anthropic", "n_items": 5, "n_rows": 15, "mean_score_unweighted": 4.0,
         "mean_score_weighted": 3.9, "hallucination_rate": 0.4, "major_hallucination_rate": 0.2},
        {"group": "gemini", "n_items": 5, "n_rows": 15, "mean_score_unweighted": 3.5,
         "mean_score_weighted": 3.4, "hallucination_rate": 0.6, "major_hallucination_rate": 0.4},
    ]

    table_text = "\n".join(_markdown_stratum_table(rows))

    assert table_text.count("**4.00**") == 2
    assert "**3.50**" not in table_text


def test_dedupe_by_doi_summarizer_keeps_latest_timestamp() -> None:
    rows = [
        {"doi": "d1", "summarizer": "openai", "timestamp": "2026-01-01T00:00:00+00:00",
         "jury_score_unweighted": 2.0},
        {"doi": "d1", "summarizer": "openai", "timestamp": "2026-06-01T00:00:00+00:00",
         "jury_score_unweighted": 4.5},
    ]

    deduped = _dedupe_by_doi_summarizer(rows)

    assert len(deduped) == 1
    assert deduped[0]["jury_score_unweighted"] == 4.5


def test_render_markdown_detail_resolves_title_and_falls_back(monkeypatch, tmp_path: Path) -> None:
    _isolate_detail_lookups(monkeypatch, tmp_path)
    rows = [
        {
            "doi": "10.1111/known",
            "summarizer": "openai",
            "timestamp": "2026-07-01T00:00:00+00:00",
            "jury_score_unweighted": 4.0,
            "jury_score_weighted": 4.1,
            "confidence_score": 4,
            "criteria_scores": {"faithfulness": {"score": 4, "reasoning": "solid"}},
            "hallucination_claims": [
                {"claim": "bad claim", "source_quote": "true quote", "severity": "minor"},
            ],
            "strata": {"species": ["Feline"], "study_design": "RCT", "clinical_topic": "Cardiology"},
        },
        {
            "doi": "10.1111/unknown",
            "summarizer": "gemini",
            "timestamp": "2026-07-01T00:00:00+00:00",
            "jury_score_unweighted": 3.0,
            "jury_score_weighted": 3.0,
            "confidence_score": 3,
            "criteria_scores": {},
            "hallucination_claims": [],
            "strata": {},
        },
    ]
    manifest_index = {"10.1111/known": {"title": "A Known Article", "journal": "JVIM"}}

    markdown = _render_markdown_detail(rows, manifest_index)

    assert "## A Known Article" in markdown
    assert "[10.1111/known](https://doi.org/10.1111/known)" in markdown
    assert "**Journal:** JVIM" in markdown
    assert "true quote" in markdown
    assert "## Untitled -- 10.1111/unknown" in markdown


def test_main_markdown_saves_json_and_paired_markdown_files(monkeypatch, tmp_path: Path) -> None:
    _isolate_detail_lookups(monkeypatch, tmp_path)
    evaluations_path = tmp_path / "evaluations.jsonl"
    evaluations_path.write_text(
        json.dumps({
            "doi": "d1", "summarizer": "openai", "jury_score": 4.0,
            "jury_score_weighted": 4.0, "jury_score_unweighted": 4.0,
            "timestamp": "2026-07-01T00:00:00+00:00",
        }) + "\n",
        encoding="utf-8",
    )
    results_dir = tmp_path / "results"

    result = main([
        "--evaluations", str(evaluations_path),
        "--results-dir", str(results_dir),
        "--markdown",
    ])

    assert result == 0
    json_files = sorted(results_dir.glob("eval_report_*.json"))
    assert len(json_files) == 1
    ts = json_files[0].stem[len("eval_report_"):]
    assert (results_dir / f"eval_report_{ts}.md").exists()
    assert (results_dir / f"eval_report_{ts}_detail.md").exists()


def test_main_markdown_no_detail_skips_detail_file(monkeypatch, tmp_path: Path) -> None:
    _isolate_detail_lookups(monkeypatch, tmp_path)
    evaluations_path = tmp_path / "evaluations.jsonl"
    evaluations_path.write_text(
        json.dumps({
            "doi": "d1", "summarizer": "openai", "jury_score": 4.0,
            "jury_score_weighted": 4.0, "jury_score_unweighted": 4.0,
            "timestamp": "2026-07-01T00:00:00+00:00",
        }) + "\n",
        encoding="utf-8",
    )
    results_dir = tmp_path / "results"

    result = main([
        "--evaluations", str(evaluations_path),
        "--results-dir", str(results_dir),
        "--markdown",
        "--no-detail",
    ])

    assert result == 0
    md_files = sorted(results_dir.glob("*.md"))
    assert len(md_files) == 1
    assert not md_files[0].stem.endswith("_detail")


def _human_review_row(item_id: str, doi: str, human: float, jury: float) -> dict:
    """One normalized human-review row (as ingest_human_reviews would write)."""
    return {
        "item_id": item_id,
        "reviewer_id": "reviewer_1",
        "doi": doi,
        "summarizer": "openai",
        "judge": "anthropic",
        "input_source": "processed",
        "strata": {},
        "criteria_scores": {c: human for c in
                            ("faithfulness", "completeness", "clinical_usefulness", "clarity", "safety")},
        "human_score_unweighted": human,
        "hallucination_present": False,
        "hallucination_notes": "",
        "comment": "",
        "llm_jury_score": jury,
        "llm_jury_score_weighted": jury,
        "llm_jury_score_unweighted": jury,
        "llm_criteria_scores": {c: {"score": jury} for c in
                                ("faithfulness", "completeness", "clinical_usefulness", "clarity", "safety")},
    }


def test_build_report_human_validation_unavailable_by_default() -> None:
    """Without ingested human reviews the block self-reports as unavailable."""
    report = build_report([{"doi": "d1", "summarizer": "openai", "jury_score": 4.0}])
    hv = report["human_validation"]
    assert hv["available"] is False
    assert "human_reviews.jsonl" in hv["reason"]


def test_build_report_human_validation_available_with_reviews() -> None:
    """Passing normalized human rows populates the human-validation block."""
    human_rows = [
        _human_review_row("item_001", "d1", human=4.0, jury=4.0),
        _human_review_row("item_002", "d2", human=3.0, jury=3.0),
        _human_review_row("item_003", "d3", human=5.0, jury=5.0),
    ]
    eval_rows = [{
        "benchmark_name": "vet_lit_summary_medhelm", "summarizer": "openai",
        "jury_score": 4.0, "jury_score_weighted": 4.0, "jury_score_unweighted": 4.0,
        "hallucination_count": 0, "confidence_score": 5, "parse_method": "json",
        "hallucination_claims": [],
    }]
    report = build_report(eval_rows, human_rows)
    hv = report["human_validation"]
    assert hv["available"] is True
    assert hv["n_items"] == 3
    # Default per_reviewer mode: the single reviewer's overall correlation.
    # Human ranks 4,3,5 match the jury's 4,3,5 -> perfect rank correlation.
    assert hv["by_reviewer"]["reviewer_1"]["overall"]["spearman"] == 1.0
    # And it renders into the Markdown report.
    markdown = _render_markdown(report, eval_rows)
    assert "## Human Validation" in markdown


def test_build_report_human_validation_mode_switch() -> None:
    """The pooled/both mode is threaded from build_report through analysis."""
    human_rows = [_human_review_row("item_001", "d1", human=4.0, jury=4.0)]
    eval_rows = [{"benchmark_name": "b", "summarizer": "openai", "jury_score": 4.0,
                  "jury_score_weighted": 4.0, "jury_score_unweighted": 4.0,
                  "hallucination_count": 0, "confidence_score": 5, "parse_method": "json",
                  "hallucination_claims": []}]
    pooled = build_report(eval_rows, human_rows, human_validation_mode="pooled")["human_validation"]
    assert pooled["human_vs_jury"] is not None
    assert pooled["by_reviewer"] == {}


def test_import_eval_report_has_no_provider_side_effects() -> None:
    """Importing eval_report alone must not require API keys or network --
    the summarizer/summarize_all_ingest imports it needs for the detail
    report's best-effort text join are lazy, loaded only inside
    _render_markdown_detail(), not at module import time."""
    import importlib

    importlib.reload(eval_report)


# ---------------------------------------------------------------------------
# Tier 1b.2 / 1b.3 — aggregation must be item-weighted and single-scale
# ---------------------------------------------------------------------------

def _ragged_panel() -> list[dict]:
    """One paper judged by 3 judges, another by 1 — what per-judge resume produces.

    Row-weighted aggregation reads this as (5+5+5+2)/4 = 4.25, letting the
    3-judge paper count triple. Item-weighted reads it as (5.0+2.0)/2 = 3.50.
    """
    rows = [make_eval_row("10.1/x", "openai", judge=j, unweighted=5.0, weighted=5.0)
            for j in ("openai", "anthropic", "gemini")]
    rows.append(make_eval_row("10.1/y", "openai", judge="openai", unweighted=2.0, weighted=2.0))
    return rows


def test_summarize_rows_is_item_weighted() -> None:
    """A paper judged three times is still one paper."""
    summary = summarize_rows(_ragged_panel(), group_field="summarizer")
    assert summary[0]["mean_score_unweighted"] == 3.5
    assert summary[0]["mean_score_weighted"] == 3.5


def test_summarize_rows_reports_items_not_judge_rows() -> None:
    """'N' must name items; the judge-row count stays available but separate."""
    summary = summarize_rows(_ragged_panel(), group_field="summarizer")
    assert summary[0]["n_items"] == 2
    assert summary[0]["n_rows"] == 4


def test_summarize_rows_never_mixes_score_scales() -> None:
    """quality_score is 1-10; jury_score is 1-5. Averaging them is meaningless."""
    rows = [
        make_eval_row("10.1/a", "openai", unweighted=4.0, weighted=4.0),
        make_eval_row("10.1/b", "openai", unweighted=None, weighted=None,
                      jury_score=None, quality_score=8),
    ]
    summary = summarize_rows(rows, group_field="summarizer")
    # 6.0 is the old bug: mean(4.0, 8) — a value on neither scale.
    assert summary[0]["mean_score"] != 6.0
    assert summary[0]["mean_score"] == 4.0


def test_eval_report_and_report_tables_agree_on_provider_means() -> None:
    """The cross-module invariant: one corpus, one answer.

    This is the acceptance criterion for routing summarize_rows through
    report_tables.collect_item_scores. Keep it forever — it is the test that
    catches a future third reimplementation of aggregation.
    """
    import report_tables as rt

    rows = _ragged_panel() + [
        make_eval_row("10.1/x", "anthropic", judge="openai", unweighted=3.0, weighted=3.0),
        make_eval_row("10.1/y", "anthropic", judge="openai", unweighted=4.0, weighted=4.0),
    ]
    from_eval_report = {
        s["group"]: s["mean_score_unweighted"]
        for s in summarize_rows(rows, group_field="summarizer")
    }

    item_scores = rt.collect_item_scores(rows)
    from_report_tables: dict[str, float] = {}
    for provider in ("openai", "anthropic"):
        vals = [agg[provider]["unweighted"] for agg in item_scores.values()
                if provider in agg and agg[provider]["unweighted"] is not None]
        from_report_tables[provider] = round(sum(vals) / len(vals), 3)

    assert from_eval_report == from_report_tables


def test_summarize_rows_surfaces_unkeyed_rows() -> None:
    """Item-weighting needs an item identity. A row without (doi, summarizer)
    cannot be attributed to one, so it is excluded from every mean — but the
    shortfall must be visible, not silent."""
    rows = [
        make_eval_row("10.1/a", "openai", unweighted=4.0, weighted=4.0),
        {"summarizer": "openai", "judge": "openai", "jury_score_unweighted": 1.0},  # no doi
    ]
    summary = summarize_rows(rows, group_field="summarizer")
    assert summary[0]["n_items"] == 1
    assert summary[0]["n_rows"] == 2
    assert summary[0]["n_unkeyed_rows"] == 1
    assert summary[0]["mean_score_unweighted"] == 4.0  # the unkeyed 1.0 never lands


# ---------------------------------------------------------------------------
# Tier 1b.4 — missing hallucination data is not a clean bill of health
# ---------------------------------------------------------------------------

def test_hallucination_rate_excludes_missing() -> None:
    """Unparsed rows must leave the denominator, not count as clean.

    The old rule biased the rate toward the flattering answer: the worse the
    parse rate, the fewer hallucinations the study appeared to find.
    """
    rows = []
    for i in range(2):                      # 2 papers with a hallucination
        rows.append(make_eval_row(f"10.1/h{i}", "openai", hallucination_count=1))
    for i in range(3):                      # 3 papers with no usable data
        rows.append(make_eval_row(f"10.1/m{i}", "openai", hallucination_count=None))
    for i in range(5):                      # 5 clean papers
        rows.append(make_eval_row(f"10.1/c{i}", "openai", hallucination_count=0))

    summary = summarize_rows(rows, group_field="summarizer")[0]
    assert summary["hallucination_rate"] == round(2 / 7, 3)   # not 2/10 = 0.2
    assert summary["hallucination_n_missing"] == 3


def test_hallucination_rate_none_when_no_usable_data() -> None:
    """No usable rows at all -> no rate, rather than a confident 0%."""
    rows = [make_eval_row("10.1/a", "openai", hallucination_count=None)]
    summary = summarize_rows(rows, group_field="summarizer")[0]
    assert summary["hallucination_rate"] is None
    assert summary["hallucination_n_missing"] == 1


# ---------------------------------------------------------------------------
# Dry-run filtering at the shared reader
# ---------------------------------------------------------------------------
#
# iter_evaluation_rows is the single reader behind every report, table,
# figure, significance test and human-review export, so filtering here keeps
# mock rows out of all of them at once. See tests/test_evaluator.py for the
# background on the 2026-07-09 contamination.


def _write_ledger(path, rows) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def _mock_eval_row(doi: str = "10.1111/jvim.16872") -> dict:
    return {"doi": doi, "summarizer": "anthropic", "judge": "openai",
            "judge_model_version": "gpt-5.4-DRYRUN",
            "reasoning": "[MOCK] dry-run evaluation", "jury_score": 3.2}


def _real_eval_row(doi: str = "10.2460/javma.23.06.0320") -> dict:
    return {"doi": doi, "summarizer": "anthropic", "judge": "openai",
            "judge_model_version": "gpt-5.4-2026-03-05",
            "reasoning": "Well supported.", "jury_score": 4.4}


def test_iter_evaluation_rows_excludes_dry_run_by_default(tmp_path) -> None:
    ledger = tmp_path / "evaluations.jsonl"
    _write_ledger(ledger, [_mock_eval_row(), _real_eval_row()])

    rows = list(eval_report.iter_evaluation_rows(ledger))
    assert len(rows) == 1
    assert rows[0]["doi"] == "10.2460/javma.23.06.0320"


def test_iter_evaluation_rows_include_dry_run_yields_everything(tmp_path) -> None:
    """Forensic escape hatch: the raw ledger is still inspectable."""
    ledger = tmp_path / "evaluations.jsonl"
    _write_ledger(ledger, [_mock_eval_row(), _real_eval_row()])

    rows = list(eval_report.iter_evaluation_rows(ledger, include_dry_run=True))
    assert len(rows) == 2


def test_count_dry_run_rows(tmp_path) -> None:
    """The excluded count is reported, not silently swallowed.

    An unexplained N is what let the original contamination survive five days
    of reports.
    """
    ledger = tmp_path / "evaluations.jsonl"
    _write_ledger(ledger, [_mock_eval_row(), _mock_eval_row(), _real_eval_row()])

    assert eval_report.count_dry_run_rows(ledger) == 2
