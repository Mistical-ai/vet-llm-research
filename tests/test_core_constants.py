from __future__ import annotations

from core.constants import DEFAULT_RANDOM_SEED, get_random_seed


def test_random_seed_prefers_canonical_env(monkeypatch) -> None:
    monkeypatch.setenv("RANDOM_SEED", "7")
    monkeypatch.setenv("EVAL_SAMPLE_SEED", "8")
    monkeypatch.setenv("SEED", "9")

    assert get_random_seed() == 7


def test_random_seed_preserves_legacy_fallback(monkeypatch) -> None:
    monkeypatch.delenv("RANDOM_SEED", raising=False)
    monkeypatch.setenv("EVAL_SAMPLE_SEED", "8")
    monkeypatch.setenv("SEED", "9")

    assert get_random_seed() == 8


def test_random_seed_defaults_to_42(monkeypatch) -> None:
    monkeypatch.delenv("RANDOM_SEED", raising=False)
    monkeypatch.delenv("EVAL_SAMPLE_SEED", raising=False)
    monkeypatch.delenv("SEED", raising=False)

    assert get_random_seed() == DEFAULT_RANDOM_SEED
