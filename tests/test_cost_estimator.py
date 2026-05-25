"""
Tests for llm-sum/cost_estimator.py — offline tokenisation + pricing math.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

import cost_estimator
from models_config import all_providers, get_model_spec


def _write_processed_jsonl(path: Path, text: str) -> None:
    path.write_text(
        json.dumps({"slug": path.stem, "text": text}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def test_count_tokens_returns_positive_int() -> None:
    n = cost_estimator.count_tokens("The dog has a heart murmur.")
    assert isinstance(n, int) and n > 0


def test_count_tokens_fallback_when_tiktoken_unavailable() -> None:
    """When tiktoken can't be imported, the words * 1.33 fallback runs."""
    with patch.dict("sys.modules", {"tiktoken": None}):
        # Removing tiktoken from sys.modules forces the ImportError branch.
        import importlib
        importlib.reload(cost_estimator)
        n = cost_estimator.count_tokens("one two three four five six seven eight")
        # 8 words * 1.33 = 10.64 -> int(10) is also accepted; check >= 8.
        assert n >= 8
    # Reload again so subsequent tests see the original module.
    import importlib as _il
    _il.reload(cost_estimator)


def test_estimate_summarisation_returns_one_row_per_provider(tmp_path: Path,
                                                              monkeypatch) -> None:
    processed = tmp_path / "processed"
    processed.mkdir()
    _write_processed_jsonl(processed / "p1.jsonl", "a b c d e " * 100)
    _write_processed_jsonl(processed / "p2.jsonl", "x y z " * 50)

    monkeypatch.setattr(cost_estimator, "PROCESSED_DIR", processed)

    rows = cost_estimator.estimate_summarisation(processed_dir=processed, batched=True)
    providers = {r.provider for r in rows}
    assert providers == set(all_providers())
    for r in rows:
        assert r.paper_count == 2
        assert r.total_cost > 0
        assert r.min_input_tokens <= r.avg_input_tokens <= r.max_input_tokens


def test_estimate_evaluation_scales_with_summariser_count(tmp_path: Path,
                                                           monkeypatch) -> None:
    processed = tmp_path / "processed"
    processed.mkdir()
    _write_processed_jsonl(processed / "p1.jsonl", "body " * 200)

    monkeypatch.setattr(cost_estimator, "PROCESSED_DIR", processed)

    cost_1 = cost_estimator.estimate_evaluation(
        processed_dir=processed,
        judge_providers=["openai"],
        summariser_providers=["openai"],
        batched=False,
    )
    cost_3 = cost_estimator.estimate_evaluation(
        processed_dir=processed,
        judge_providers=["openai"],
        summariser_providers=["openai", "anthropic", "gemini"],
        batched=False,
    )
    # 3 summarisers should cost ~3x as much as 1.
    assert abs(cost_3 / cost_1 - 3.0) < 1e-6
