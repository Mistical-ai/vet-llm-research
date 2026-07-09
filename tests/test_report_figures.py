"""
tests/test_report_figures.py — Publication figures + leaderboard (Chunk 8)
============================================================================

Headless smoke tests: figures render to real PNG/SVG bytes via matplotlib's
Agg backend (no display server needed), the leaderboard has the documented
shape with graceful degradation when reliability/human-validation data is
absent, and the CLI writes both to data/results/. Everything here is
offline — no mocks needed because report_figures never touches a provider
API.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import eval_instances
import report_figures as rf
from report_figures import (
    aggregate_criterion_means,
    build_criterion_heatmap_figure,
    build_cost_quality_figure,
    build_leaderboard,
    build_provider_comparison_figure,
    build_reliability_figure,
    main,
    render_leaderboard_markdown,
    save_figures,
    save_leaderboard,
)
import report_tables as rt
import stats_engine


@pytest.fixture(autouse=True)
def _isolate_stats_engine_lookups(monkeypatch, tmp_path):
    """Every test here exercises build_leaderboard, which falls back to
    report_tables.build_publication_report when no `report=` is passed — and
    that now also reads manifest abstracts + data/human_reviews.jsonl for the
    information-density/covariate sections. Auto-applied (not per-test) so no
    test in this file accidentally reads this repo's real (multi-MB)
    data/manifest.jsonl or data/summaries.jsonl. Mirrors
    test_eval_report.py's `_isolate_detail_lookups` for the same reason.
    """
    monkeypatch.setattr(eval_instances, "MANIFEST_PATH", tmp_path / "no_manifest.jsonl")
    monkeypatch.setattr(eval_instances, "MANUAL_MANIFEST_PATH", tmp_path / "no_manual_manifest.jsonl")
    monkeypatch.setattr(stats_engine, "HUMAN_REVIEWS_PATH", tmp_path / "no_human_reviews.jsonl")
    monkeypatch.setattr(stats_engine, "SUMMARIES_PATH", tmp_path / "no_summaries.jsonl")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _eval_row(
    doi: str, summarizer: str, judge: str, unweighted: float, weighted: float,
    *, faithfulness: int = 4, completeness: int = 4, clinical_usefulness: int = 4,
    clarity: int = 4, safety: int = 4, input_source: str = "processed",
) -> dict:
    """One evaluations.jsonl row with both jury modes, strata, and criteria_scores."""
    return {
        "benchmark_name": "vet_lit_summary_medhelm",
        "doi": doi,
        "summarizer": summarizer,
        "judge": judge,
        "input_source": input_source,
        "jury_score": unweighted,
        "jury_score_unweighted": unweighted,
        "jury_score_weighted": weighted,
        "judge_disagreement": 0.0,
        "criteria_scores": {
            "faithfulness": {"score": faithfulness, "reasoning": "ok"},
            "completeness": {"score": completeness, "reasoning": "ok"},
            "clinical_usefulness": {"score": clinical_usefulness, "reasoning": "ok"},
            "clarity": {"score": clarity, "reasoning": "ok"},
            "safety": {"score": safety, "reasoning": "ok"},
        },
        "strata": {
            "species": ["Canine"], "study_design": "RCT", "clinical_topic": "Oncology",
            "journal": "JVIM", "input_source": input_source,
        },
    }


def _separated_corpus() -> list[dict]:
    """6 papers, openai > anthropic > gemini on every paper (single judge)."""
    rows: list[dict] = []
    for i in range(1, 7):
        doi = f"10.1/{i}"
        rows.append(_eval_row(doi, "openai", "openai", 5.0, 5.0, faithfulness=5, safety=5))
        rows.append(_eval_row(doi, "anthropic", "openai", 4.0, 4.0, faithfulness=4, safety=4))
        rows.append(_eval_row(doi, "gemini", "openai", 3.0, 3.0, faithfulness=3, safety=3))
    return rows


def _jury_corpus() -> list[dict]:
    """One paper, one provider, scored by two judges (enables reliability)."""
    return [
        _eval_row("10.1/x", "openai", "openai", 4.0, 4.0, faithfulness=4),
        _eval_row("10.1/x", "openai", "anthropic", 3.0, 3.0, faithfulness=2),
    ]


def _human_review_row(doi: str, summarizer: str, item_id: str, *, human: float, jury: float) -> dict:
    """One normalized data/human_reviews.jsonl record (post-ingest schema)."""
    return {
        "item_id": item_id,
        "reviewer_id": "reviewer_1",
        "doi": doi,
        "summarizer": summarizer,
        "judge": "openai",
        "input_source": "processed",
        "rubric_version": "vet_medhelm_score_v1.0",
        "strata": {"journal": "JVIM"},
        "criteria_scores": {"faithfulness": human, "completeness": human, "clinical_usefulness": human,
                             "clarity": human, "safety": human},
        "human_score_unweighted": human,
        "human_score_weighted": human,
        "hallucination_present": False,
        "hallucination_notes": "",
        "comment": "",
        "llm_jury_score": jury,
        "llm_jury_score_weighted": jury,
        "llm_jury_score_unweighted": jury,
        "llm_criteria_scores": {"faithfulness": {"score": jury}, "completeness": {"score": jury},
                                 "clinical_usefulness": {"score": jury}, "clarity": {"score": jury},
                                 "safety": {"score": jury}},
    }


# ---------------------------------------------------------------------------
# aggregate_criterion_means
# ---------------------------------------------------------------------------

def test_aggregate_criterion_means_pools_across_judges() -> None:
    rows = [
        _eval_row("10.1/x", "openai", "openai", 4.0, 4.0, faithfulness=4),
        _eval_row("10.1/x", "openai", "anthropic", 4.0, 4.0, faithfulness=2),
    ]
    means = aggregate_criterion_means(rows)
    assert means["openai"]["faithfulness"] == 3.0  # (4 + 2) / 2
    assert means["openai"]["completeness"] == 4.0


def test_aggregate_criterion_means_skips_rows_without_provider_or_scores() -> None:
    rows = [{"doi": "x", "summarizer": "", "criteria_scores": {"faithfulness": {"score": 5}}},
            {"doi": "x", "summarizer": "openai", "criteria_scores": None}]
    assert aggregate_criterion_means(rows) == {}


# ---------------------------------------------------------------------------
# Figures — headless smoke tests
# ---------------------------------------------------------------------------

def _png_bytes_are_valid(path: Path) -> bool:
    return path.exists() and path.stat().st_size > 0 and path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_provider_comparison_figure_renders(tmp_path: Path) -> None:
    report = rt.build_publication_report(_separated_corpus(), cost_index={}, n_resamples=50)
    fig = build_provider_comparison_figure(report["provider_comparison"])
    assert fig is not None
    out = tmp_path / "provider_comparison.png"
    fig.savefig(out)
    assert _png_bytes_are_valid(out)


def test_provider_comparison_figure_none_when_empty() -> None:
    assert build_provider_comparison_figure([]) is None


def test_cost_quality_figure_renders_only_priced_providers(tmp_path: Path) -> None:
    report = rt.build_publication_report(
        _separated_corpus(),
        cost_index={("10.1/%d" % i, "openai", "processed"): 0.05 for i in range(1, 7)},
        n_resamples=50,
    )
    fig = build_cost_quality_figure(report["provider_comparison"])
    assert fig is not None
    out = tmp_path / "cost_quality.png"
    fig.savefig(out)
    assert _png_bytes_are_valid(out)


def test_cost_quality_figure_none_without_cost() -> None:
    report = rt.build_publication_report(_separated_corpus(), cost_index={}, n_resamples=50)
    assert build_cost_quality_figure(report["provider_comparison"]) is None


def test_criterion_heatmap_figure_renders(tmp_path: Path) -> None:
    means = aggregate_criterion_means(_separated_corpus())
    fig = build_criterion_heatmap_figure(means, ["openai", "anthropic", "gemini"])
    assert fig is not None
    out = tmp_path / "heatmap.png"
    fig.savefig(out)
    assert _png_bytes_are_valid(out)


def test_criterion_heatmap_figure_none_without_data() -> None:
    assert build_criterion_heatmap_figure({}, ["openai"]) is None


def test_reliability_figure_none_for_single_judge() -> None:
    report = rt.build_publication_report(_separated_corpus(), cost_index={}, n_resamples=50)
    assert report["reliability"]["available"] is False
    assert build_reliability_figure(report["reliability"]) is None


def test_reliability_figure_renders_for_jury(tmp_path: Path) -> None:
    report = rt.build_publication_report(_jury_corpus(), cost_index={}, n_resamples=50)
    assert report["reliability"]["available"] is True
    fig = build_reliability_figure(report["reliability"])
    assert fig is not None
    out = tmp_path / "reliability.png"
    fig.savefig(out)
    assert _png_bytes_are_valid(out)


def test_save_figures_writes_every_format(tmp_path: Path) -> None:
    report = rt.build_publication_report(_separated_corpus(), cost_index={}, n_resamples=50)
    figures = {
        "provider_comparison": build_provider_comparison_figure(report["provider_comparison"]),
        "cost_quality": build_cost_quality_figure(report["provider_comparison"]),  # None (no cost)
    }
    saved = save_figures(figures, tmp_path, formats=("png", "svg"))
    assert "provider_comparison" in saved
    assert "cost_quality" not in saved  # None figures are skipped, not written
    names = {p.name for p in saved["provider_comparison"]}
    assert names == {"provider_comparison.png", "provider_comparison.svg"}
    for path in saved["provider_comparison"]:
        assert path.exists() and path.stat().st_size > 0


def test_save_figures_writes_nothing_when_all_none(tmp_path: Path) -> None:
    saved = save_figures({"a": None, "b": None}, tmp_path)
    assert saved == {}
    assert not tmp_path.exists() or list(tmp_path.iterdir()) == []


# ---------------------------------------------------------------------------
# Leaderboard
# ---------------------------------------------------------------------------

def test_build_leaderboard_shape_and_ranking() -> None:
    rows = _separated_corpus()
    leaderboard = build_leaderboard(rows, cost_index={}, n_resamples=50)
    assert leaderboard["leaderboard_name"] == "VetHELM-style Veterinary Summarization Leaderboard"
    assert leaderboard["taxonomy"]["taxonomy_id"] == "vet_taxonomy_v1"
    assert [e["provider"] for e in leaderboard["entries"]] == ["openai", "anthropic", "gemini"]

    top = leaderboard["entries"][0]
    assert top["quality_unweighted"]["mean"] == 5.0
    # Single judge -> per-provider reliability unavailable, but shape is intact.
    assert top["reliability"]["available"] is False
    assert top["human_validation"]["available"] is False


def test_build_leaderboard_reliability_available_for_jury() -> None:
    leaderboard = build_leaderboard(_jury_corpus(), cost_index={}, n_resamples=50)
    entry = leaderboard["entries"][0]
    assert entry["provider"] == "openai"
    assert entry["reliability"]["available"] is True
    assert entry["reliability"]["krippendorff_alpha"] is not None
    assert leaderboard["overall_reliability"]["available"] is True


def test_build_leaderboard_human_validation_per_provider() -> None:
    rows = _separated_corpus()
    human_rows = [
        _human_review_row("10.1/1", "openai", "item_001", human=5.0, jury=5.0),
        _human_review_row("10.1/2", "anthropic", "item_002", human=3.5, jury=4.0),
    ]
    leaderboard = build_leaderboard(rows, cost_index={}, human_rows=human_rows, n_resamples=50)
    by_provider = {e["provider"]: e for e in leaderboard["entries"]}
    assert by_provider["openai"]["human_validation"]["available"] is True
    assert by_provider["openai"]["human_validation"]["n_items"] == 1
    # gemini has no human-review rows at all.
    assert by_provider["gemini"]["human_validation"]["available"] is False


def test_build_leaderboard_handles_empty_corpus() -> None:
    leaderboard = build_leaderboard([], cost_index={}, n_resamples=50)
    assert leaderboard["entries"] == []


def test_render_leaderboard_markdown_has_expected_sections() -> None:
    leaderboard = build_leaderboard(_jury_corpus(), cost_index={}, n_resamples=50)
    md = render_leaderboard_markdown(leaderboard)
    assert "# VetHELM-style Veterinary Summarization Leaderboard" in md
    assert "| Rank | Provider |" in md
    assert "## Overall inter-judge reliability" in md
    assert "## Notes" in md


def test_render_leaderboard_markdown_empty_corpus() -> None:
    leaderboard = build_leaderboard([], cost_index={}, n_resamples=50)
    md = render_leaderboard_markdown(leaderboard)
    assert "No scored evaluation rows found" in md


def test_save_leaderboard_writes_json_and_markdown(tmp_path: Path) -> None:
    leaderboard = build_leaderboard(_separated_corpus(), cost_index={}, n_resamples=50)
    leaderboard["generated_at"] = "20260101T000000Z"
    md = render_leaderboard_markdown(leaderboard)
    saved = save_leaderboard(leaderboard, md, "20260101T000000Z", tmp_path)
    assert saved["json"].exists()
    assert saved["markdown"].exists()
    reloaded = json.loads(saved["json"].read_text(encoding="utf-8"))
    assert reloaded["entries"][0]["provider"] == "openai"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _write_corpus(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def test_main_writes_leaderboard_and_figures(tmp_path: Path) -> None:
    evaluations = tmp_path / "evaluations.jsonl"
    _write_corpus(evaluations, _separated_corpus())
    results_dir = tmp_path / "results"

    result = main([
        "--evaluations", str(evaluations),
        "--summaries", str(tmp_path / "no_summaries.jsonl"),
        "--human-reviews", str(tmp_path / "no_human_reviews.jsonl"),
        "--results-dir", str(results_dir),
        "--bootstrap-resamples", "50",
    ])

    assert result == 0
    json_files = sorted(results_dir.glob("leaderboard_*.json"))
    md_files = sorted(results_dir.glob("leaderboard_*.md"))
    fig_dirs = sorted(results_dir.glob("figures_*"))
    assert len(json_files) == 1 and len(md_files) == 1 and len(fig_dirs) == 1
    fig_files = sorted(fig_dirs[0].iterdir())
    assert any(f.name == "provider_comparison.png" for f in fig_files)
    assert any(f.name == "provider_comparison.svg" for f in fig_files)
    # No cost data supplied -> the cost/quality figure is skipped, not an error.
    assert not any(f.name.startswith("cost_quality") for f in fig_files)


def test_main_no_figures_flag_skips_png_svg(tmp_path: Path) -> None:
    evaluations = tmp_path / "evaluations.jsonl"
    _write_corpus(evaluations, _separated_corpus())
    results_dir = tmp_path / "results"

    result = main([
        "--evaluations", str(evaluations),
        "--summaries", str(tmp_path / "no_summaries.jsonl"),
        "--results-dir", str(results_dir),
        "--bootstrap-resamples", "50",
        "--no-figures",
    ])

    assert result == 0
    assert list(results_dir.glob("leaderboard_*.json"))
    assert not list(results_dir.glob("figures_*"))


def test_main_single_png_format(tmp_path: Path) -> None:
    evaluations = tmp_path / "evaluations.jsonl"
    _write_corpus(evaluations, _separated_corpus())
    results_dir = tmp_path / "results"

    result = main([
        "--evaluations", str(evaluations),
        "--summaries", str(tmp_path / "no_summaries.jsonl"),
        "--results-dir", str(results_dir),
        "--bootstrap-resamples", "50",
        "--formats", "png",
    ])

    assert result == 0
    fig_dir = next(results_dir.glob("figures_*"))
    assert all(f.suffix == ".png" for f in fig_dir.iterdir())


def test_main_empty_evaluations_writes_nothing(tmp_path: Path) -> None:
    evaluations = tmp_path / "evaluations.jsonl"
    evaluations.write_text("", encoding="utf-8")
    results_dir = tmp_path / "results"

    result = main([
        "--evaluations", str(evaluations),
        "--summaries", str(tmp_path / "no_summaries.jsonl"),
        "--results-dir", str(results_dir),
    ])

    assert result == 0
    assert not results_dir.exists()


def test_import_report_figures_has_no_provider_side_effects() -> None:
    """Importing the module must not require API keys, the network, or matplotlib."""
    import importlib
    importlib.reload(rf)
