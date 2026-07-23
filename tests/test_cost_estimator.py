"""
Tests for llm-sum/cost_estimator.py — offline tokenisation + pricing math.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

import cost_estimator
import models_config
from models_config import all_providers, compute_cost, get_model_spec


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


def test_estimate_summarisation_can_use_raw_text_cache(tmp_path: Path,
                                                       monkeypatch) -> None:
    raw_text = tmp_path / "raw_text"
    raw_text.mkdir()
    _write_processed_jsonl(raw_text / "p1.jsonl", "raw extracted body " * 100)

    monkeypatch.setattr(cost_estimator, "RAW_TEXT_DIR", raw_text)

    rows = cost_estimator.estimate_summarisation(
        processed_dir=raw_text,
        batched=False,
        input_source="raw_text",
    )
    assert {r.provider for r in rows} == set(all_providers())
    assert all(r.paper_count == 1 for r in rows)


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


# ---------------------------------------------------------------------------
# Phase B: tier + role resolution (get_model_spec)
# ---------------------------------------------------------------------------
# Table-driven over (tier, role, which override vars are set) — verification
# item 8. MODEL_TIER/_PREMIUM_PRICES are module globals inside models_config
# itself (not re-imported elsewhere), so monkeypatch.setattr(models_config, ...)
# is what actually changes get_model_spec()'s behaviour here.

def test_get_model_spec_no_role_argument_matches_today(monkeypatch: pytest.MonkeyPatch) -> None:
    """get_model_spec(provider) with NO role argument must return exactly
    what it always has — the ~20 existing call sites never pass a role."""
    monkeypatch.setattr(models_config, "MODEL_TIER", "regular")
    monkeypatch.delenv("OPENAI_SUMMARY_MODEL_REGULAR", raising=False)
    monkeypatch.delenv("OPENAI_JUDGE_MODEL_REGULAR", raising=False)
    monkeypatch.delenv("OPENAI_MODEL_REGULAR", raising=False)

    spec = get_model_spec("openai")
    assert spec.model_id == models_config.MODELS["openai"].model_id
    assert spec.price_input_per_mtok == models_config.MODELS["openai"].price_input_per_mtok


def test_get_model_spec_regular_tier_role_specific_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(models_config, "MODEL_TIER", "regular")
    monkeypatch.setenv("OPENAI_JUDGE_MODEL_REGULAR", "gpt-judge-only")
    spec_judge = get_model_spec("openai", role="judge")
    spec_summarize = get_model_spec("openai", role="summarize")
    assert spec_judge.model_id == "gpt-judge-only"
    # Summarise role is untouched by a judge-only override.
    assert spec_summarize.model_id != "gpt-judge-only"


def test_get_model_spec_premium_tier_uses_premium_id_and_price(monkeypatch: pytest.MonkeyPatch) -> None:
    """Anthropic's premium id/prices are live in the registry — this is the
    'filled in correctly' happy path. (.env.template documents
    ANTHROPIC_MODEL_PREMIUM=claude-opus-4-8 as the live value; set explicitly
    here since tests never load .env.template itself.)"""
    monkeypatch.setattr(models_config, "MODEL_TIER", "premium")
    monkeypatch.setenv("ANTHROPIC_MODEL_PREMIUM", "claude-opus-4-8")
    spec = get_model_spec("anthropic")
    assert spec.model_id == "claude-opus-4-8"
    assert spec.price_input_per_mtok == models_config._PREMIUM_PRICES["anthropic"].price_input_per_mtok
    assert spec.price_input_per_mtok != models_config.MODELS["anthropic"].price_input_per_mtok


def test_get_model_spec_premium_tier_unset_id_falls_back_to_regular(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
) -> None:
    """OpenAI/Gemini's premium ids are blank by default — falls back to the
    regular tier with a printed warning, never a 404-causing blank model id."""
    monkeypatch.setattr(models_config, "MODEL_TIER", "premium")
    monkeypatch.delenv("OPENAI_MODEL_PREMIUM", raising=False)
    monkeypatch.delenv("OPENAI_JUDGE_MODEL_PREMIUM", raising=False)
    monkeypatch.delenv("OPENAI_SUMMARY_MODEL_PREMIUM", raising=False)

    spec = get_model_spec("openai")
    assert spec.model_id == models_config.MODELS["openai"].model_id
    assert spec.price_input_per_mtok == models_config.MODELS["openai"].price_input_per_mtok
    captured = capsys.readouterr()
    assert "falling back to the regular tier" in captured.out


def test_get_model_spec_premium_tier_refuses_stale_price_row(monkeypatch: pytest.MonkeyPatch) -> None:
    """A premium model id that resolves non-empty, but whose premium price
    row still matches the regular tier's numbers exactly, must raise rather
    than let BudgetGuard silently misreport spend."""
    monkeypatch.setattr(models_config, "MODEL_TIER", "premium")
    monkeypatch.setenv("OPENAI_MODEL_PREMIUM", "gpt-6-hypothetical")
    # _PREMIUM_PRICES["openai"] is deliberately still identical to the
    # regular row (see models_config.py's comment) until someone fills in
    # real premium pricing alongside a real premium id.
    with pytest.raises(ValueError, match="premium price row"):
        get_model_spec("openai")


def test_get_model_spec_unknown_role_raises() -> None:
    with pytest.raises(ValueError):
        get_model_spec("openai", role="not-a-real-role")


# ---------------------------------------------------------------------------
# Phase A2: compute_cost cache-token pricing (verification #6)
# ---------------------------------------------------------------------------

def test_compute_cost_zero_cache_args_returns_todays_number() -> None:
    """cache_read_tokens=cache_write_tokens=0 (the default) must return
    EXACTLY today's number — every existing caller/test is unaffected."""
    plain = compute_cost("openai", 1000, 200, batched=False)
    with_zero_cache = compute_cost("openai", 1000, 200, batched=False,
                                   cache_read_tokens=0, cache_write_tokens=0)
    assert with_zero_cache == plain


def test_compute_cost_prices_anthropic_cache_reads_and_writes() -> None:
    """Synthetic Anthropic usage {input:100, cache_creation:900,
    cache_read:8000} (input_tokens=9000 total, per the parser's summing
    rule) prices the uncached remainder at the normal rate, reads at 0.1x,
    and writes at the TTL-appropriate multiplier."""
    spec = get_model_spec("anthropic")
    output_tokens = 50
    cost_5m = compute_cost(
        "anthropic", 9000, output_tokens, batched=False,
        cache_read_tokens=8000, cache_write_tokens=900, cache_ttl="5m",
    )
    expected_5m = (
        (100 / 1_000_000) * spec.price_input_per_mtok
        + (8000 / 1_000_000) * spec.price_input_per_mtok * spec.cache_read_multiplier
        + (900 / 1_000_000) * spec.price_input_per_mtok * spec.cache_write_multiplier_5m
        + (output_tokens / 1_000_000) * spec.price_output_per_mtok
    )
    assert abs(cost_5m - expected_5m) < 1e-9

    cost_1h = compute_cost(
        "anthropic", 9000, output_tokens, batched=False,
        cache_read_tokens=8000, cache_write_tokens=900, cache_ttl="1h",
    )
    # A 1h-TTL write costs more than a 5m one (2.0x vs 1.25x) — everything
    # else about the request is identical.
    assert cost_1h > cost_5m


def test_compute_cost_cache_hit_cheaper_than_no_caching_at_all() -> None:
    """A cache hit must cost less than the same total tokens with no
    caching involved — sanity check on the multiplier direction."""
    uncached = compute_cost("anthropic", 9000, 50, batched=False)
    cached = compute_cost(
        "anthropic", 9000, 50, batched=False,
        cache_read_tokens=8000, cache_write_tokens=0,
    )
    assert cached < uncached
