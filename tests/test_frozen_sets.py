import pytest

from core.hashing import dataset_hash
from core.jsonl import write_jsonl
from validation.frozen_sets import FrozenSetChecksumError, load_frozen_set, stable_instance_sort


def test_frozen_set_checksum_enforced(tmp_path):
    path = tmp_path / "frozen.jsonl"
    rows = [{"instance_id": "b", "doi": "2"}, {"instance_id": "a", "doi": "1"}]
    write_jsonl(path, rows)
    expected = dataset_hash(stable_instance_sort(rows))

    loaded = load_frozen_set(path, expected_sha256=expected)

    assert [row["instance_id"] for row in loaded] == ["a", "b"]


def test_frozen_set_checksum_mismatch_fails(tmp_path):
    path = tmp_path / "frozen.jsonl"
    write_jsonl(path, [{"instance_id": "a", "doi": "1"}])

    with pytest.raises(FrozenSetChecksumError):
        load_frozen_set(path, expected_sha256="not-the-real-hash")
