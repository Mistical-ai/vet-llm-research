from metrics.contracts import MetricInput, MetricResult
from metrics.medhelm import calculate_jury_score, flagged_for_review_rate, hallucination_rate


def test_metric_contract_models_are_json_serializable():
    metric_input = MetricInput(reference_text="source", candidate_summary="summary")
    result = MetricResult(name="example", value=1.0, deterministic=True)

    assert metric_input.model_dump()["reference_text"] == "source"
    assert result.model_dump()["name"] == "example"


def test_medhelm_primary_metrics():
    score = calculate_jury_score(
        {
            "faithfulness": {"score": 5},
            "completeness": {"score": 4},
            "clinical_usefulness": {"score": 4},
            "clarity": {"score": 5},
            "safety": {"score": 5},
        }
    )
    rows = [{"hallucination_count": 1, "requires_human_review": True}, {"hallucination_count": 0}]

    assert score == 4.62
    assert hallucination_rate(rows) == 0.5
    assert flagged_for_review_rate(rows) == 0.5
