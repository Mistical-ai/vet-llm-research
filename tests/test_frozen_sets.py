import pytest

import json
from pathlib import Path

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


def test_frozen_set_sidecar_checksum_is_used(tmp_path):
    path = tmp_path / "frozen.jsonl"
    rows = [{"instance_id": "a", "doi": "1"}]
    write_jsonl(path, rows)
    path.with_suffix(".manifest.json").write_text(
        json.dumps(
            {"name": "test", "path": str(path), "sha256": dataset_hash(rows), "row_count": 1}
        ),
        encoding="utf-8",
    )

    loaded = load_frozen_set(path, require_manifest=True)

    assert loaded == rows


def test_committed_frozen_set_matches_sidecar():
    path = Path("frozen_sets/example_medhelm_tiny.jsonl")

    loaded = load_frozen_set(path, require_manifest=True)

    assert len(loaded) == 2


def test_frozen_set_required_sidecar_missing_fails(tmp_path):
    path = tmp_path / "frozen.jsonl"
    write_jsonl(path, [{"instance_id": "a", "doi": "1"}])

    with pytest.raises(FrozenSetChecksumError, match="manifest sidecar not found"):
        load_frozen_set(path, require_manifest=True)
