from __future__ import annotations

from eval_metrics import (
    calculate_automatic_metrics,
    compression_ratio,
    extractive_coverage,
    rouge_l_recall,
    rouge_recall,
    section_coverage,
)


def test_rouge_recall_counts_overlap_deterministically() -> None:
    reference = "Dogs with heart disease improved after treatment."
    candidate = "Dogs improved after treatment."
    assert rouge_recall(reference, candidate, n=1) > 0
    assert rouge_recall(reference, candidate, n=2) > 0
    assert rouge_l_recall(reference, candidate) > 0


def test_compression_and_extractive_coverage_are_local_metrics() -> None:
    reference = "Dogs with heart disease improved after treatment."
    candidate = "Dogs improved after treatment."
    assert 0 < compression_ratio(reference, candidate) < 1
    assert extractive_coverage(reference, candidate) == 1.0


def test_section_coverage_detects_veterinary_summary_elements() -> None:
    summary = (
        "Objective: test treatment in 20 dogs. Methods: randomized trial. "
        "Results were significant. Clinical recommendation is cautious because "
        "limitations include small sample size."
    )
    coverage = section_coverage(summary)
    assert coverage["objective"] is True
    assert coverage["methods"] is True
    assert coverage["species_sample"] is True
    assert coverage["coverage_ratio"] > 0.5


def test_calculate_automatic_metrics_returns_expected_keys() -> None:
    metrics = calculate_automatic_metrics(
        "Dogs with heart disease improved after treatment.",
        "Dogs improved after treatment.",
    )
    assert set(metrics) == {
        "compression_ratio",
        "extractive_coverage",
        "section_coverage",
        "rouge_1",
        "rouge_2",
        "rouge_l",
    }
