from reporting.paired import all_pairwise_comparisons, paired_deltas, paired_win_rate


def test_paired_deltas_only_include_shared_instances():
    rows = [
        {"doi": "d1", "input_source": "processed", "summarizer": "openai", "jury_score": 4.0},
        {"doi": "d1", "input_source": "processed", "summarizer": "anthropic", "jury_score": 3.0},
        {"doi": "d2", "input_source": "processed", "summarizer": "openai", "jury_score": 5.0},
    ]

    deltas = paired_deltas(rows, model_a="openai", model_b="anthropic")

    assert len(deltas) == 1
    assert deltas[0]["delta_a_minus_b"] == 1.0
    assert paired_win_rate(deltas)["model_a_win_rate"] == 1.0
    assert all_pairwise_comparisons(rows)[0]["n"] == 1


def test_paired_deltas_match_by_instance_id_before_doi():
    rows = [
        {
            "instance_id": "case-001",
            "doi": "doi-a",
            "input_source": "processed",
            "summarizer": "openai",
            "jury_score": 5.0,
        },
        {
            "instance_id": "case-001",
            "doi": "doi-b",
            "input_source": "processed",
            "summarizer": "anthropic",
            "jury_score": 4.0,
        },
    ]

    deltas = paired_deltas(rows, model_a="openai", model_b="anthropic")

    assert len(deltas) == 1
    assert deltas[0]["instance_id"] == "case-001"
    assert deltas[0]["delta_a_minus_b"] == 1.0
