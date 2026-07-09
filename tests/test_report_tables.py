"""
tests/test_report_tables.py — Publication reporting (Chunk 7)
=============================================================

Exercises the offline publication tables: the hand-rolled statistics
primitives (Wilcoxon, Friedman, chi-square/normal tails, bootstrap CIs), the
per-item/per-provider aggregation, cost joining, the assembled report shape,
and the Markdown/CSV renderers + CLI. Everything here is pure/offline — no
mocks needed because report_tables never touches a provider API.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import eval_instances
import report_tables as rt
import stats_engine
from report_tables import (
    benjamini_hochberg,
    bootstrap_ci,
    build_publication_report,
    build_significance,
    build_summary_cost_index,
    collect_item_scores,
    friedman,
    main,
    render_csvs,
    render_markdown,
    wilcoxon_signed_rank,
)


@pytest.fixture(autouse=True)
def _isolate_stats_engine_lookups(monkeypatch, tmp_path):
    """build_publication_report now also reads manifest abstracts and
    data/human_reviews.jsonl for its information-density/covariate sections.
    Auto-applied to every test in this file (most call build_publication_report
    directly with no manifest/human-reviews override) so none of them
    accidentally reads this repo's real (multi-MB) data/manifest.jsonl or
    data/summaries.jsonl. Mirrors test_eval_report.py's `_isolate_detail_lookups`.
    """
    monkeypatch.setattr(eval_instances, "MANIFEST_PATH", tmp_path / "no_manifest.jsonl")
    monkeypatch.setattr(eval_instances, "MANUAL_MANIFEST_PATH", tmp_path / "no_manual_manifest.jsonl")
    monkeypatch.setattr(stats_engine, "HUMAN_REVIEWS_PATH", tmp_path / "no_human_reviews.jsonl")
    monkeypatch.setattr(stats_engine, "SUMMARIES_PATH", tmp_path / "no_summaries.jsonl")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _eval_row(doi: str, summarizer: str, judge: str, unweighted: float, weighted: float,
              *, species: str = "Canine", study_design: str = "RCT",
              clinical_topic: str = "Oncology", journal: str = "JVIM",
              input_source: str = "processed") -> dict:
    """One evaluations.jsonl row (single judge) with both jury modes + strata."""
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
        "strata": {
            "species": [species],
            "study_design": study_design,
            "clinical_topic": clinical_topic,
            "journal": journal,
            "input_source": input_source,
        },
    }


def _separated_corpus() -> list[dict]:
    """6 papers, openai > anthropic > gemini on every paper (single judge)."""
    rows: list[dict] = []
    for i in range(1, 7):
        doi = f"10.1/{i}"
        rows.append(_eval_row(doi, "openai", "openai", 5.0, 5.0))
        rows.append(_eval_row(doi, "anthropic", "openai", 4.0, 4.0))
        rows.append(_eval_row(doi, "gemini", "openai", 3.0, 3.0))
    return rows


# ---------------------------------------------------------------------------
# Statistics primitives
# ---------------------------------------------------------------------------

def test_wilcoxon_all_positive_is_significant() -> None:
    result = wilcoxon_signed_rank([1.0] * 10)
    assert result["available"] is True
    assert result["n"] == 10
    assert result["mean_diff"] == 1.0
    assert result["w_minus"] == 0.0
    assert result["p_value"] < 0.05


def test_wilcoxon_all_zero_is_unavailable() -> None:
    result = wilcoxon_signed_rank([0.0, 0.0, 0.0])
    assert result["available"] is False
    assert result["n"] == 0


def test_friedman_full_separation_is_significant() -> None:
    providers = ["openai", "anthropic", "gemini"]
    blocks = [{"openai": 5.0, "anthropic": 4.0, "gemini": 3.0} for _ in range(6)]
    result = friedman(blocks, providers)
    assert result["available"] is True
    assert result["df"] == 2
    assert abs(result["statistic"] - 12.0) < 1e-6
    assert result["p_value"] < 0.05
    assert result["mean_ranks"]["openai"] > result["mean_ranks"]["gemini"]


def test_friedman_needs_three_providers() -> None:
    result = friedman([{"openai": 5.0, "anthropic": 4.0}], ["openai", "anthropic"])
    assert result["available"] is False


def test_bootstrap_ci_is_deterministic_and_brackets_mean() -> None:
    values = [3.0, 4.0, 5.0, 4.0, 3.0, 5.0]
    a = bootstrap_ci(values, seed=42, n_resamples=500)
    b = bootstrap_ci(values, seed=42, n_resamples=500)
    assert a == b  # same seed → identical interval
    assert a["ci_low"] <= a["mean"] <= a["ci_high"]


def test_bootstrap_ci_single_value_has_no_interval() -> None:
    result = bootstrap_ci([4.0], seed=1)
    assert result["mean"] == 4.0
    assert result["ci_low"] is None and result["ci_high"] is None


def test_benjamini_hochberg_matches_known_values() -> None:
    # Classic BH example: three ordered p-values, q_i = min over j>=i of p_j*m/j.
    assert benjamini_hochberg([0.01, 0.04, 0.20]) == [0.03, 0.06, 0.20]


def test_benjamini_hochberg_preserves_input_order() -> None:
    # Unsorted input: the i-th q-value must correspond to the i-th p-value.
    adjusted = benjamini_hochberg([0.20, 0.01, 0.04])
    assert adjusted == [0.20, 0.03, 0.06]


def test_benjamini_hochberg_edge_cases() -> None:
    assert benjamini_hochberg([]) == []
    # A family of one needs no correction — returned unchanged.
    assert benjamini_hochberg([0.03]) == [0.03]


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def test_collect_item_scores_averages_across_judges() -> None:
    rows = [
        _eval_row("10.1/x", "openai", "openai", 4.0, 4.0),
        _eval_row("10.1/x", "openai", "anthropic", 2.0, 2.0),
    ]
    item_scores = collect_item_scores(rows)
    agg = item_scores[("10.1/x", "processed")]["openai"]
    assert agg["unweighted"] == 3.0  # (4 + 2) / 2
    assert agg["n_judges"] == 2


def test_cost_index_prices_success_slots(tmp_path: Path) -> None:
    summaries = tmp_path / "summaries.jsonl"
    summaries.write_text(
        json.dumps({
            "doi": "10.1/x",
            "input_source": "processed",
            "models": {
                "openai": {"status": "success", "input_tokens": 1_000_000, "output_tokens": 0},
                "gemini": {"status": "failed", "input_tokens": 5, "output_tokens": 5},
            },
        }) + "\n",
        encoding="utf-8",
    )
    index = build_summary_cost_index(summaries, batched=False)
    # openai input price is $5.00 / Mtok, so 1M input tokens = $5.00.
    assert abs(index[("10.1/x", "openai", "processed")] - 5.0) < 1e-9
    # A failed slot is never priced.
    assert ("10.1/x", "gemini", "processed") not in index


# ---------------------------------------------------------------------------
# Full report
# ---------------------------------------------------------------------------

def test_build_publication_report_shape_and_significance() -> None:
    rows = _separated_corpus()
    cost_index = {
        ("10.1/%d" % i, provider, "processed"): cost
        for i in range(1, 7)
        for provider, cost in (("openai", 0.10), ("anthropic", 0.05), ("gemini", 0.02))
    }
    report = build_publication_report(rows, cost_index=cost_index, n_resamples=300)

    assert report["providers"] == ["openai", "anthropic", "gemini"]
    assert report["n_items"] == 6

    # Provider comparison is a leaderboard: openai first, with a CI and cost.
    top = report["provider_comparison"][0]
    assert top["provider"] == "openai"
    assert top["quality_unweighted"]["mean"] == 5.0
    assert top["cost"]["available"] is True
    assert top["cost_per_quality_point"] is not None
    # Subscription cost-per-quality is a separate, always-present column
    # (flat $20/mo / 500 papers, not derived from the real cost above).
    assert top["subscription_cost_per_quality_point"] == round(0.04 / 5.0, 6)

    # New stats_engine sections are always present with the documented shape,
    # even with no manifest/summaries/human-review data behind them (isolated
    # by this file's autouse fixture) — they degrade to "unavailable"/empty
    # rather than being absent keys.
    assert report["information_density"]["available"] is False
    assert report["subscription_economics"][0]["provider"] == "openai"
    assert report["covariate_analysis"]["fields"] == list(stats_engine.COVARIATE_FIELDS)

    # Omnibus + pairwise significance on the fully separated corpus.
    sig = report["significance_unweighted"]
    assert sig["friedman"]["available"] is True
    assert sig["friedman"]["p_value"] < 0.05
    pair = next(p for p in sig["pairwise_wilcoxon"]
                if p["provider_a"] == "openai" and p["provider_b"] == "gemini")
    assert pair["n_shared"] == 6
    assert pair["mean_diff"] == 2.0

    # Every testable pair carries a Benjamini-Hochberg-adjusted p >= its raw p
    # (correction only ever moves p-values upward), and the family size + method
    # are recorded for the methods section.
    assert sig["multiple_comparison_method"] == "benjamini-hochberg"
    assert sig["n_comparisons_corrected"] == 3  # 3 pairs among 3 providers
    for p in sig["pairwise_wilcoxon"]:
        assert p["p_adjusted"] is not None
        assert p["p_adjusted"] >= p["wilcoxon"]["p_value"]

    # Taxonomy header names the versioned benchmark.
    assert report["taxonomy"]["taxonomy_id"] == "vet_taxonomy_v1"


def test_build_significance_single_family_p_adjusted_equals_raw() -> None:
    """With only two providers there is one pairwise test, so BH leaves it
    unchanged (a family of one needs no correction)."""
    item_scores = collect_item_scores([
        _eval_row(f"10.1/{i}", "openai", "openai", 5.0, 5.0) for i in range(1, 7)
    ] + [
        _eval_row(f"10.1/{i}", "anthropic", "openai", 4.0, 4.0) for i in range(1, 7)
    ])
    sig = build_significance(item_scores, ["openai", "anthropic"],
                             score_key="unweighted", seed=42, n_resamples=50)
    assert sig["n_comparisons_corrected"] == 1
    only = sig["pairwise_wilcoxon"][0]
    assert only["p_adjusted"] == round(only["wilcoxon"]["p_value"], 4)


def test_report_handles_single_provider_gracefully() -> None:
    rows = [_eval_row("10.1/x", "openai", "openai", 4.0, 4.0)]
    report = build_publication_report(rows, cost_index={}, n_resamples=50)
    assert report["providers"] == ["openai"]
    # No pairs, Friedman unavailable — but the shape is intact.
    assert report["significance_unweighted"]["friedman"]["available"] is False
    assert report["significance_unweighted"]["pairwise_wilcoxon"] == []


def test_input_source_comparison_splits_channels() -> None:
    rows = [
        _eval_row("10.1/x", "openai", "openai", 5.0, 5.0, input_source="processed"),
        _eval_row("10.1/x", "openai", "openai", 3.0, 3.0, input_source="pdf"),
    ]
    report = build_publication_report(rows, cost_index={}, n_resamples=50)
    sources = {e["input_source"] for e in report["input_source_comparison"]}
    assert sources == {"processed", "pdf"}


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def test_render_markdown_has_expected_sections() -> None:
    report = build_publication_report(_separated_corpus(), cost_index={}, n_resamples=50)
    md = render_markdown(report)
    assert "# Veterinary Summarization — Publication Report" in md
    assert "## Provider comparison" in md
    assert "## Significance (unweighted score)" in md
    assert "## Processed text vs. direct PDF" in md
    assert "## Notes on method" in md
    # The pairwise table exposes the BH-adjusted p column.
    assert "p (BH-adj)" in md


def test_render_csvs_emits_all_tables() -> None:
    report = build_publication_report(_separated_corpus(), cost_index={}, n_resamples=50)
    csvs = render_csvs(report)
    assert "provider_comparison.csv" in csvs
    assert "significance_pairwise.csv" in csvs
    assert "significance_friedman.csv" in csvs
    assert "by_species.csv" in csvs
    assert "input_source_comparison.csv" in csvs
    # Header + one row per provider in the comparison CSV.
    assert csvs["provider_comparison.csv"].splitlines()[0].startswith("provider,")
    # The pairwise CSV carries the BH-adjusted p column.
    assert "wilcoxon_p_bh_adjusted" in csvs["significance_pairwise.csv"].splitlines()[0]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _write_corpus(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def test_main_saves_json_markdown_and_csv_dir(tmp_path: Path) -> None:
    evaluations = tmp_path / "evaluations.jsonl"
    _write_corpus(evaluations, _separated_corpus())
    results_dir = tmp_path / "results"

    result = main([
        "--evaluations", str(evaluations),
        "--summaries", str(tmp_path / "no_summaries.jsonl"),
        "--results-dir", str(results_dir),
        "--bootstrap-resamples", "50",
    ])

    assert result == 0
    json_files = sorted(results_dir.glob("publication_report_*.json"))
    md_files = sorted(results_dir.glob("publication_report_*.md"))
    csv_dirs = sorted(results_dir.glob("publication_report_*_tables"))
    assert len(json_files) == 1 and len(md_files) == 1 and len(csv_dirs) == 1
    assert (csv_dirs[0] / "provider_comparison.csv").exists()
    saved = json.loads(json_files[0].read_text(encoding="utf-8"))
    assert saved["provider_comparison"][0]["provider"] == "openai"


def test_main_empty_evaluations_does_not_write(tmp_path: Path) -> None:
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


def test_import_report_tables_has_no_provider_side_effects() -> None:
    """Importing the module must not require API keys or the network."""
    import importlib
    importlib.reload(rt)
