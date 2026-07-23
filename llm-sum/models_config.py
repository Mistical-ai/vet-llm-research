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
    - the model ID to send in the API call (per tier and per role — see
      "MODEL TIERS AND ROLES" below),
    - the per-million-token price (real and batched, per tier),
    - the cached-token price multipliers (see "PROMPT CACHING" below),
    - whether the provider supports a 50%-off batch API,
    - the rate-limit key used in utils.sleep_for_model().

Update HERE when a provider releases a new version. Nothing else changes.

MODEL TIERS AND ROLES
---------------------
``MODEL_TIER`` (.env, section 16) picks between ``regular`` (the study's
current lineup — the default) and ``premium`` (a stronger, pricier model per
provider, for a deliberate sensitivity run). ``get_model_spec(provider, role=)``
resolves the actual model id through a first-non-empty-wins chain:

    {PROVIDER}_{ROLE}_MODEL_{TIER} -> {PROVIDER}_MODEL_{TIER} -> {PROVIDER}_MODEL

``role`` is ``"summarize"`` or ``"judge"`` and defaults to ``"summarize"`` so
every pre-existing ``get_model_spec(provider)`` call site (~20 across
summarizer.py, evaluator.py, cost_estimator.py, run_phase3.py, and the test
suite) keeps compiling and returning exactly what it always has, unchanged.

Per-tier pricing is mandatory, not optional: switching ``MODEL_TIER=premium``
for a provider whose premium price row was never filled in (still identical
to the regular row) raises rather than silently under/over-reporting spend —
see the guard in ``get_model_spec``. A provider whose premium model id is left
blank (the default for OpenAI/Gemini until their premium IDs are confirmed —
see .env.template section 16) falls back to the regular tier for that
provider with a printed warning, never a 404.

PROMPT CACHING
--------------
``PROMPT_CACHE_ENABLED`` (.env, section 12; default false) toggles only the
Anthropic ``cache_control`` marker and the OpenAI ``prompt_cache_key`` — see
``anthropic_cache_control`` / ``openai_prompt_cache_key`` below. It never
changes prompt text or block structure (see evaluator.build_judge_prompt_segments
and batch_utils's segmented request builders), so scoring is identical with
the flag on or off; only billing and (maybe) routing differ.

``ModelSpec.cache_read_multiplier`` / ``cache_write_multiplier_5m`` /
``cache_write_multiplier_1h`` feed ``compute_cost``'s optional cache
arguments. Only Anthropic's multipliers are confirmed against current
provider docs (cache reads ~0.1x, writes 1.25x at the 5-minute default TTL or
2.0x at the 1-hour TTL). OpenAI's automatic-caching discount and Gemini's
implicit batch caching are written here from the project brief, not
independently verified — see .env.template section 12 for the caveat this
carries into any pilot's reported hit rate. Gemini's cached-token discount
REPLACES its batch discount rather than stacking with it (Finding E in the
caching design plan) — do not assume the two combine.
"""

from __future__ import annotations

import dataclasses
import os
import time
from dataclasses import dataclass
from typing import Literal

# Provider keys used everywhere in Phase 3. Keep these stable — they are the
# join keys for data/summaries.jsonl, data/evaluations.jsonl, custom_id
# parsing, and CLI flags.
ProviderKey = Literal["openai", "anthropic", "gemini"]

# The two things a model id can be resolved for. "summarize" is the default
# for get_model_spec() so every pre-existing call site (no role argument)
# keeps today's behaviour exactly.
Role = Literal["summarize", "judge"]

_ROLE_ENV_PREFIX: dict[str, str] = {"summarize": "SUMMARY", "judge": "JUDGE"}


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
    # Cached-token price multipliers, applied off price_input_per_mtok by
    # compute_cost() when cache_read_tokens/cache_write_tokens are passed.
    # Defaults are the Anthropic-confirmed figures; each provider's registry
    # entry below overrides these with its own (see module docstring).
    cache_read_multiplier: float = 0.1
    cache_write_multiplier_5m: float = 1.25
    cache_write_multiplier_1h: float = 2.0


@dataclass(frozen=True)
class _PremiumPricing:
    """A provider's premium-tier prices, kept separate from the (possibly
    test-monkeypatched) MODELS registry below — see get_model_spec()."""
    price_input_per_mtok: float
    price_output_per_mtok: float
    price_input_per_mtok_batched: float
    price_output_per_mtok_batched: float


# ---------------------------------------------------------------------------
# Model registry — regular tier (today's rates, unchanged)
# ---------------------------------------------------------------------------
# Prices below come from the Phase 3 design doc (May 2026 pricing). When
# providers update their pricing pages, edit these numbers and nothing else.
# Model IDs default to the env var (so the user can pin a specific version
# string like "gpt-5.4-0325-preview"), with a sensible default if unset.
#
# NOTE ON MUTABILITY: tests monkeypatch entries of this dict directly (e.g.
# `monkeypatch.setitem(models_config.MODELS, "gemini", dataclasses.replace(...,
# supports_batch=True))` — see tests/conftest.py's comment on GEMINI_BATCH_ENABLED
# and tests/test_summarizer.py). get_model_spec() below reads through MODELS
# for every field it doesn't override for tier/role, so such a monkeypatch
# still takes effect on every subsequent get_model_spec() call.

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
        # OpenAI's automatic prompt-caching discount is ~50% off cached input
        # tokens, with no separate cache-write premium (caching is automatic
        # above ~1024 tokens; there is no cache_control marker to pay extra for).
        cache_read_multiplier=0.5,
        cache_write_multiplier_5m=1.0,
        cache_write_multiplier_1h=1.0,
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
        # Confirmed against current Anthropic docs: cache reads ~0.1x price;
        # cache writes 1.25x at the 5-minute default TTL, 2.0x at the 1-hour TTL.
        cache_read_multiplier=0.1,
        cache_write_multiplier_5m=1.25,
        cache_write_multiplier_1h=2.0,
    ),
    "gemini": ModelSpec(
        provider="gemini",
        model_id=os.getenv("GEMINI_MODEL", "gemini-3.5-flash"),
        price_input_per_mtok=4.00,
        price_output_per_mtok=18.00,
        # Google's Batch API is 50% off the real-time price (same discount
        # structure as OpenAI/Anthropic above) — update alongside the
        # real-time prices if Google changes Gemini pricing.
        price_input_per_mtok_batched=2.00,
        price_output_per_mtok_batched=9.00,
        supports_batch=os.getenv("GEMINI_BATCH_ENABLED", "false").lower() == "true",
        rate_limit_key="gemini",
        # Gemini's implicit-caching discount is ~0.1x (90% off) reads, applied
        # INSTEAD OF the batch discount, not stacked with it (Finding E) — see
        # .env.template section 12. No separate write premium is modelled;
        # Google does not bill an explicit "cache creation" step for implicit
        # caching the way Anthropic's cache_control does.
        cache_read_multiplier=0.1,
        cache_write_multiplier_5m=1.0,
        cache_write_multiplier_1h=1.0,
    ),
}

# ---------------------------------------------------------------------------
# Premium tier — prices only (model ids are resolved from .env; see
# _resolve_model_id_and_tier). Kept OUT of the MODELS dict above so a test
# monkeypatching MODELS never has to know about tiers at all.
# ---------------------------------------------------------------------------
# OpenAI/Gemini premium rows are DELIBERATELY set equal to their regular-tier
# numbers as a placeholder: get_model_spec() refuses to serve a premium tier
# whose prices still match the regular tier's (see the guard there), so if
# someone later fills in OPENAI_MODEL_PREMIUM / GEMINI_MODEL_PREMIUM without
# also updating these two rows, the run fails loudly instead of silently
# under-reporting spend. Anthropic's premium id (claude-opus-4-8) is live —
# see .env.template section 16 — so its row below carries real, distinct
# pricing. VERIFY every number here against the provider's current pricing
# page before a paid premium-tier run.
_PREMIUM_PRICES: dict[ProviderKey, _PremiumPricing] = {
    "openai": _PremiumPricing(5.00, 30.00, 2.50, 15.00),      # placeholder — see note above
    "anthropic": _PremiumPricing(15.00, 75.00, 7.50, 37.50),  # claude-opus-4-8 (verify before a paid run)
    "gemini": _PremiumPricing(4.00, 18.00, 2.00, 9.00),       # placeholder — see note above
}

MODEL_TIER = os.getenv("MODEL_TIER", "regular").strip().lower()
if MODEL_TIER not in ("regular", "premium"):
    print(f"[models_config] Unknown MODEL_TIER={MODEL_TIER!r}; falling back to 'regular'.")
    MODEL_TIER = "regular"


def _resolve_model_id_and_tier(
    key: str, role: str, tier: str, *, default_model_id: str,
) -> tuple[str, str]:
    """Return (model_id, effective_tier) for one provider/role/requested-tier.

    First-non-empty-wins chain, role-specific var before the provider-wide
    one: ``{PROVIDER}_{ROLE}_MODEL_{TIER}`` -> ``{PROVIDER}_MODEL_{TIER}``.
    A ``tier="premium"`` request that matches neither falls back to the
    regular chain (and ultimately ``default_model_id`` — today's
    ``{PROVIDER}_MODEL`` value already baked into MODELS[key].model_id) with
    a printed warning, rather than sending a blank/placeholder model id to
    the provider.
    """
    provider_upper = key.upper()
    role_prefix = _ROLE_ENV_PREFIX[role]

    if tier == "premium":
        for var in (f"{provider_upper}_{role_prefix}_MODEL_PREMIUM",
                    f"{provider_upper}_MODEL_PREMIUM"):
            value = os.getenv(var, "").strip()
            if value:
                return value, "premium"
        print(f"[models_config] MODEL_TIER=premium requested for {key}/{role} but neither "
              f"{provider_upper}_{role_prefix}_MODEL_PREMIUM nor {provider_upper}_MODEL_PREMIUM "
              f"is set — falling back to the regular tier for {key}.")
        return _resolve_model_id_and_tier(key, role, "regular", default_model_id=default_model_id)

    # tier == "regular"
    for var in (f"{provider_upper}_{role_prefix}_MODEL_REGULAR",
                f"{provider_upper}_MODEL_REGULAR"):
        value = os.getenv(var, "").strip()
        if value:
            return value, "regular"
    return default_model_id, "regular"


def get_model_spec(provider: str, role: str = "summarize") -> ModelSpec:
    """Lookup with a clear error if the provider key is unknown.

    ``role`` defaults to ``"summarize"`` — today's behaviour — so every
    pre-existing call site with no role argument is unaffected. Pass
    ``role="judge"`` at judge call sites so a ``{PROVIDER}_JUDGE_MODEL_*``
    override (and, in premium tier, a judge-specific premium id) is honoured.
    """
    key = provider.lower()
    if key not in MODELS:
        raise KeyError(
            f"Unknown provider '{provider}'. Valid keys: {sorted(MODELS.keys())}"
        )
    if role not in _ROLE_ENV_PREFIX:
        raise ValueError(
            f"Unknown role '{role}'. Valid roles: {sorted(_ROLE_ENV_PREFIX)}"
        )

    # Read through MODELS[key] (not a cached copy) so a test's
    # monkeypatch.setitem(MODELS, ...) is honoured on every call — see the
    # NOTE ON MUTABILITY comment above the MODELS dict.
    base = MODELS[key]  # type: ignore[index]
    model_id, effective_tier = _resolve_model_id_and_tier(
        key, role, MODEL_TIER, default_model_id=base.model_id,
    )

    if effective_tier == "regular":
        return dataclasses.replace(base, model_id=model_id)

    premium = _PREMIUM_PRICES[key]
    if (premium.price_input_per_mtok == base.price_input_per_mtok
            and premium.price_output_per_mtok == base.price_output_per_mtok
            and premium.price_input_per_mtok_batched == base.price_input_per_mtok_batched
            and premium.price_output_per_mtok_batched == base.price_output_per_mtok_batched):
        raise ValueError(
            f"MODEL_TIER=premium resolved model id {model_id!r} for {key}/{role}, but "
            f"{key}'s premium price row in models_config.py still matches its regular "
            "tier prices exactly. Update price_input_per_mtok / price_output_per_mtok "
            "(and the batched variants) in _PREMIUM_PRICES before running with a "
            "premium model — otherwise BudgetGuard silently under/over-reports spend."
        )
    return dataclasses.replace(
        base,
        model_id=model_id,
        price_input_per_mtok=premium.price_input_per_mtok,
        price_output_per_mtok=premium.price_output_per_mtok,
        price_input_per_mtok_batched=premium.price_input_per_mtok_batched,
        price_output_per_mtok_batched=premium.price_output_per_mtok_batched,
    )


def compute_cost(
    provider: str,
    input_tokens: int,
    output_tokens: int,
    *,
    batched: bool,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    cache_ttl: str = "5m",
) -> float:
    """
    Compute the USD cost of one API call.

    Pulling tokens directly from the API response (not estimating) is the
    only way to keep BudgetGuard.total_spent matching the provider's bill.
    Estimates accumulate ±5% error per call; over 1000 calls that is a
    budget mismatch large enough to break a small research budget.

    ``cache_read_tokens`` / ``cache_write_tokens`` default to 0, in which
    case this returns EXACTLY today's number — every existing caller and
    test is unaffected. When either is non-zero, ``input_tokens`` is assumed
    to be the TOTAL prompt size (uncached + cache-write + cache-read tokens,
    per Phase A2's accounting rule — see the batch/real-time judge parsers),
    and the uncached remainder is priced at the normal rate while the cached
    portions are priced at ``spec.cache_read_multiplier`` /
    ``cache_write_multiplier_{5m,1h}`` off the same base input price.
    """
    spec = get_model_spec(provider)
    if batched:
        in_price = spec.price_input_per_mtok_batched
        out_price = spec.price_output_per_mtok_batched
    else:
        in_price = spec.price_input_per_mtok
        out_price = spec.price_output_per_mtok

    cache_read_tokens = max(0, int(cache_read_tokens))
    cache_write_tokens = max(0, int(cache_write_tokens))

    if not cache_read_tokens and not cache_write_tokens:
        return (input_tokens / 1_000_000.0) * in_price + (output_tokens / 1_000_000.0) * out_price

    write_multiplier = (
        spec.cache_write_multiplier_1h if cache_ttl == "1h" else spec.cache_write_multiplier_5m
    )
    uncached_tokens = max(0, int(input_tokens) - cache_read_tokens - cache_write_tokens)
    input_cost = (
        (uncached_tokens / 1_000_000.0) * in_price
        + (cache_read_tokens / 1_000_000.0) * in_price * spec.cache_read_multiplier
        + (cache_write_tokens / 1_000_000.0) * in_price * write_multiplier
    )
    return input_cost + (output_tokens / 1_000_000.0) * out_price


def all_providers() -> list[ProviderKey]:
    """Return providers in a stable order for predictable iteration."""
    return ["openai", "anthropic", "gemini"]


# ---------------------------------------------------------------------------
# Prompt caching — flag, TTL, cache-key scope (Phase A3)
# ---------------------------------------------------------------------------
# PROMPT_CACHE_ENABLED toggles ONLY the Anthropic cache_control marker and the
# OpenAI prompt_cache_key (see module docstring). Block structure, prompt
# text, and token accounting are identical whether this is on or off.

PROMPT_CACHE_ENABLED = os.getenv("PROMPT_CACHE_ENABLED", "false").strip().lower() == "true"

PROMPT_CACHE_TTL = os.getenv("PROMPT_CACHE_TTL", "5m").strip().lower()
if PROMPT_CACHE_TTL not in ("5m", "1h"):
    print(f"[models_config] Unknown PROMPT_CACHE_TTL={PROMPT_CACHE_TTL!r}; falling back to '5m'.")
    PROMPT_CACHE_TTL = "5m"

PROMPT_CACHE_KEY_SCOPE = os.getenv("PROMPT_CACHE_KEY_SCOPE", "run").strip().lower()
if PROMPT_CACHE_KEY_SCOPE not in ("run", "article"):
    print(f"[models_config] Unknown PROMPT_CACHE_KEY_SCOPE={PROMPT_CACHE_KEY_SCOPE!r}; falling back to 'run'.")
    PROMPT_CACHE_KEY_SCOPE = "run"

# One token per process, so every real-time judge call in a single `evaluate`
# invocation shares one prompt_cache_key under scope="run" (a fresh process —
# e.g. tomorrow's run — gets a different token, which is fine: the key only
# needs to be stable WITHIN a run, per OpenAI's routing hint semantics).
_RUN_CACHE_TOKEN = os.getenv("PHASE3_RUN_ID", "").strip() or str(int(time.time()))


def anthropic_cache_control(ttl: str | None = None) -> dict[str, str]:
    """Build an Anthropic ``cache_control`` marker for the configured/given TTL.

    Only called when PROMPT_CACHE_ENABLED is true. Omits the ``ttl`` key
    entirely for the 5-minute default (Anthropic's own default), and sets
    ``"ttl": "1h"`` only when the 1-hour TTL is requested.
    """
    marker: dict[str, str] = {"type": "ephemeral"}
    if (ttl or PROMPT_CACHE_TTL) == "1h":
        marker["ttl"] = "1h"
    return marker


def openai_prompt_cache_key(article_id: str | None = None) -> str:
    """Build the OpenAI ``prompt_cache_key`` value per PROMPT_CACHE_KEY_SCOPE.

    ``scope="article"`` emits a per-article key (``veteval-judge-{article_id}``),
    which may route one paper's three judgments together more consistently
    than a single global run key — unverified provider behaviour (see
    .env.template section 12); cheap either way, do not bank savings on it.
    Falls back to the run-scoped key when no ``article_id`` is supplied even
    under scope="article" (e.g. a caller that has no article identity handy).
    """
    if PROMPT_CACHE_KEY_SCOPE == "article" and article_id:
        return f"veteval-judge-{article_id}"
    return f"veteval-judge-run-{_RUN_CACHE_TOKEN}"
