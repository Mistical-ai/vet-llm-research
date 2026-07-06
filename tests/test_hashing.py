from pathlib import Path

from core.hashing import dataset_hash, sha256_file, sha256_text


def test_text_and_file_hash_match_for_same_content(tmp_path: Path):
    path = tmp_path / "sample.txt"
    path.write_text("veterinary reproducibility", encoding="utf-8")

    assert sha256_file(path) == sha256_text("veterinary reproducibility")


def test_dataset_hash_is_stable_across_row_order():
    left = [{"instance_id": "b", "doi": "2"}, {"instance_id": "a", "doi": "1"}]
    right = list(reversed(left))

    assert dataset_hash(left) == dataset_hash(right)


def test_dataset_hash_uses_doi_and_title_fallbacks_for_ties():
    left = [
        {"instance_id": "", "doi": "10.1/b", "title": "B"},
        {"instance_id": "", "doi": "10.1/a", "title": "A"},
        {"instance_id": "same", "doi": "10.1/d", "title": "D"},
        {"instance_id": "same", "doi": "10.1/c", "title": "C"},
    ]
    right = list(reversed(left))

    assert dataset_hash(left) == dataset_hash(right)
