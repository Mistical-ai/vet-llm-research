from __future__ import annotations

from eval_report import build_report, summarize_rows


def test_summarize_rows_by_summarizer() -> None:
    rows = [
        {
            "summarizer": "openai",
            "jury_score": 4.0,
            "hallucination_count": 0,
            "confidence_score": 5,
            "parse_method": "json",
            "judge_disagreement": 0.0,
            "hallucination_claims": [],
        },
        {
            "summarizer": "openai",
            "jury_score": 2.0,
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
