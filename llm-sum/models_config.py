"""
llm-sum/models_config.py — Single source of truth for model IDs and pricing
============================================================================

WHY THIS FILE EXISTS
--------------------
LLM providers release new versions silently and the IDs change. Pricing also
shifts. Without a central registry, summarizer.py, evaluator.py, the cost
estimator, and the batch builder would each carry their own copies of these
strings — guaranteeing they drift out of sync.

This module is the ONE place that knows:
    - the model ID to send in the API call,
    - the per-million-token price (real and batched),
    - whether the provider supports a 50%-off batch API,
    - the rate-limit key used in utils.sleep_for_model().

Update HERE when a provider releases a new version. Nothing else changes.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

# Provider keys used everywhere in Phase 3. Keep these stable — they are the
# join keys for data/summaries.jsonl, data/evaluations.jsonl, custom_id
# parsing, and CLI flags.
ProviderKey = Literal["openai", "anthropic", "gemini"]


@dataclass(frozen=True)
class ModelSpec:
    """A single provider's configuration. Frozen so callers can't mutate it."""
    provider: ProviderKey
    model_id: str              # The literal string sent to the provider API.
    # Per-million-token USD prices. Batched prices are the 50%-off rates the
    # batch APIs charge once the job completes (or refunds back if it doesn't).
    price_input_per_mtok: float
    price_output_per_mtok: float
    price_input_per_mtok_batched: float
    price_output_per_mtok_batched: float
    supports_batch: bool
    # Rate-limit key for utils.sleep_for_model() — must match a key in
    # src/utils.py RATE_LIMITS.
    rate_limit_key: str


# ---------------------------------------------------------------------------
# Model registry
# ---------------------------------------------------------------------------
# Prices below come from the Phase 3 design doc (May 2026 pricing). When
# providers update their pricing pages, edit these numbers and nothing else.
# Model IDs default to the env var (so the user can pin a specific version
# string like "gpt-5.4-0325-preview"), with a sensible default if unset.

MODELS: dict[ProviderKey, ModelSpec] = {
    "openai": ModelSpec(
        provider="openai",
        model_id=os.getenv("OPENAI_MODEL", "gpt-5.4"),
        price_input_per_mtok=5.00,
        price_output_per_mtok=30.00,
        price_input_per_mtok_batched=2.50,
        price_output_per_mtok_batched=15.00,
        supports_batch=os.getenv("OPENAI_BATCH_ENABLED", "true").lower() == "true",
        rate_limit_key="openai",
    ),
    "anthropic": ModelSpec(
        provider="anthropic",
        model_id=os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6"),
        price_input_per_mtok=5.00,
        price_output_per_mtok=25.00,
        price_input_per_mtok_batched=2.50,
        price_output_per_mtok_batched=12.50,
        supports_batch=os.getenv("ANTHROPIC_BATCH_ENABLED", "true").lower() == "true",
        rate_limit_key="anthropic",
    ),
    "gemini": ModelSpec(
        provider="gemini",
        model_id=os.getenv("GEMINI_MODEL", "gemini-3.5-flash"),
        price_input_per_mtok=4.00,
        price_output_per_mtok=18.00,
        # Gemini batch API not in scope for Phase 3 — same as real-time price.
        price_input_per_mtok_batched=4.00,
        price_output_per_mtok_batched=18.00,
        supports_batch=os.getenv("GEMINI_BATCH_ENABLED", "false").lower() == "true",
        rate_limit_key="gemini",
    ),
}


def get_model_spec(provider: str) -> ModelSpec:
    """Lookup with a clear error if the provider key is unknown."""
    key = provider.lower()
    if key not in MODELS:
        raise KeyError(
            f"Unknown provider '{provider}'. Valid keys: {sorted(MODELS.keys())}"
        )
    return MODELS[key]  # type: ignore[index]


def compute_cost(
    provider: str,
    input_tokens: int,
    output_tokens: int,
    *,
    batched: bool,
) -> float:
    """
    Compute the USD cost of one API call.

    Pulling tokens directly from the API response (not estimating) is the
    only way to keep BudgetGuard.total_spent matching the provider's bill.
    Estimates accumulate ±5% error per call; over 1000 calls that is a
    budget mismatch large enough to break a small research budget.
    """
    spec = get_model_spec(provider)
    if batched:
        in_price = spec.price_input_per_mtok_batched
        out_price = spec.price_output_per_mtok_batched
    else:
        in_price = spec.price_input_per_mtok
        out_price = spec.price_output_per_mtok

    return (input_tokens / 1_000_000.0) * in_price + (output_tokens / 1_000_000.0) * out_price


def all_providers() -> list[ProviderKey]:
    """Return providers in a stable order for predictable iteration."""
    return ["openai", "anthropic", "gemini"]
