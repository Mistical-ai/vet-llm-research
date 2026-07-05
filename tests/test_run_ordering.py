from validation.frozen_sets import stable_instance_sort


def test_stable_instance_sort_uses_instance_id_then_doi():
    rows = [
        {"instance_id": "b", "doi": "10.2"},
        {"instance_id": "a", "doi": "10.1"},
        {"doi": "10.0"},
    ]

    sorted_rows = stable_instance_sort(rows)

    assert [row.get("doi") for row in sorted_rows] == ["10.0", "10.1", "10.2"]
