"""
llm-sum/cost_estimator.py — Offline cost forecasting for Phase 3
==================================================================

Computes the projected USD cost of a full summarisation + evaluation run
WITHOUT calling any API. Reads cached `data/processed/*.jsonl` files and
tokenises them with `tiktoken` (the OpenAI tokenizer) for an accurate
estimate of input tokens. Falls back to a `words × 1.33` heuristic if
`tiktoken` is not installed.

Why use the GPT tokenizer for all three providers?
    Anthropic and Gemini publish their own tokenizers, but in practice the
    GPT BPE tokenizer is within 5–10% of all three for English scientific
    text. That's accurate enough for budget forecasting, and it avoids
    pulling in three separate dependencies.

Output:
    Per-provider min / mean / max cost projections,
    plus a total compared against BUDGET_HARD_STOP.
"""

from __future__ import annotations

from _bootstrap import *  # noqa: F401,F403

import os
from dataclasses import dataclass
from pathlib import Path

from models_config import all_providers, compute_cost, get_model_spec  # noqa: E402
from prepare_texts import iter_cached_texts  # noqa: E402
from utils import BUDGET_HARD_STOP  # noqa: E402

# A conservative GPT-family ratio for English scientific text:
# 1 token ≈ 0.75 words, equivalently words × 1.33 ≈ tokens.
WORDS_TO_TOKENS = 1.33

# Match summarizer.py and batch_utils.py when .env is absent. A lower estimator
# default would understate summarisation cost before a paid run.
MAX_OUTPUT_TOKENS = int(os.getenv("MAX_OUTPUT_TOKENS", "1200"))
JUDGE_INPUT_OVERHEAD_TOKENS = 800     # judge prompt boilerplate + 3 summaries.
JUDGE_OUTPUT_TOKENS = 300


@dataclass
class CostBreakdown:
    """One provider's projected cost for the full corpus."""
    provider: str
    paper_count: int
    avg_input_tokens: int
    min_input_tokens: int
    max_input_tokens: int
    avg_cost_per_paper: float
    total_cost: float
    batched: bool


def count_tokens(text: str) -> int:
    """
    Return an integer token count for `text`. Prefers `tiktoken`; falls back
    to a word-based ratio if tiktoken is unavailable so the function never
    crashes a budget forecast.
    """
    try:
        import tiktoken  # type: ignore[import-not-found]
    except ImportError:
        return int(len(text.split()) * WORDS_TO_TOKENS)

    # cl100k_base is the GPT-4/4o/4-turbo tokenizer; close enough for forecasting.
    encoding = tiktoken.get_encoding("cl100k_base")
    return len(encoding.encode(text))


def estimate_summarisation(
    processed_dir: Path = PROCESSED_DIR,
    *,
    batched: bool,
    input_source: str = "processed",
) -> list[CostBreakdown]:
    """
    Estimate per-provider summarisation cost across all cached texts.
    """
    token_counts = [
        count_tokens(text)
        for _slug, text in iter_cached_texts(input_source, processed_dir)
    ]
    if not token_counts:
        return []

    n = len(token_counts)
    avg_in = int(sum(token_counts) / n)
    min_in = min(token_counts)
    max_in = max(token_counts)

    results: list[CostBreakdown] = []
    for provider in all_providers():
        spec = get_model_spec(provider)
        provider_batched = batched and spec.supports_batch
        avg_cost = compute_cost(provider, avg_in, MAX_OUTPUT_TOKENS, batched=provider_batched)
        total = sum(
            compute_cost(provider, t, MAX_OUTPUT_TOKENS, batched=provider_batched)
            for t in token_counts
        )
        results.append(CostBreakdown(
            provider=provider,
            paper_count=n,
            avg_input_tokens=avg_in,
            min_input_tokens=min_in,
            max_input_tokens=max_in,
            avg_cost_per_paper=avg_cost,
            total_cost=total,
            batched=provider_batched,
        ))
    return results


def estimate_evaluation(
    processed_dir: Path = PROCESSED_DIR,
    *,
    judge_providers: list[str],
    summariser_providers: list[str],
    batched: bool,
    input_source: str = "processed",
) -> float:
    """
    Estimate judge cost. Each judge scores each (paper, summariser) pair.
    """
    paper_token_counts = [
        count_tokens(t)
        for _, t in iter_cached_texts(input_source, processed_dir)
    ]
    if not paper_token_counts:
        return 0.0

    total = 0.0
    n_summarisers = len(summariser_providers)
    for paper_tokens in paper_token_counts:
        judge_input = paper_tokens + JUDGE_INPUT_OVERHEAD_TOKENS
        for judge in judge_providers:
            spec = get_model_spec(judge)
            judge_batched = batched and spec.supports_batch
            per_pair = compute_cost(judge, judge_input, JUDGE_OUTPUT_TOKENS, batched=judge_batched)
            total += per_pair * n_summarisers
    return total


def print_report(
    summarisation: list[CostBreakdown],
    evaluation_total: float,
    *,
    batched: bool,
    input_source: str,
) -> None:
    """Pretty-print the forecast in a single readable block."""
    if not summarisation:
        print("[phase3:estimate] No cached texts in data/processed/. "
              "Run `python llm-sum/run_phase3.py extract` first.")
        return

    n_papers = summarisation[0].paper_count
    print(f"\n[phase3:estimate] Cost forecast for {n_papers} paper(s)")
    print(f"  Mode: {'BATCH (50% off where supported)' if batched else 'REAL-TIME'}")
    print(f"  Input source: {input_source}")
    print(f"  Output tokens per summary: {MAX_OUTPUT_TOKENS}\n")

    summ_total = 0.0
    for row in summarisation:
        flag = " (batched)" if row.batched else " (real-time)"
        print(
            f"  {row.provider:>10}{flag:<14}  "
            f"avg_in={row.avg_input_tokens:>5}tok  "
            f"avg=${row.avg_cost_per_paper:.4f}/paper  "
            f"total=${row.total_cost:.2f}"
        )
        summ_total += row.total_cost

    print(f"\n  Summarisation subtotal : ${summ_total:.2f}")
    print(f"  Evaluation (judge)     : ${evaluation_total:.2f}")
    grand = summ_total + evaluation_total
    print(f"  GRAND TOTAL            : ${grand:.2f}")

    if BUDGET_HARD_STOP > 0:
        margin = BUDGET_HARD_STOP - grand
        status = "WITHIN BUDGET" if margin >= 0 else "OVER BUDGET"
        print(f"  Budget hard stop       : ${BUDGET_HARD_STOP:.2f}  [{status}, margin ${margin:.2f}]")
    else:
        print("  Budget hard stop       : $0.00  (set BUDGET_HARD_STOP in .env before running)")


def run(*, batched: bool, judge_providers: list[str],
        input_source: str = "processed") -> None:
    """Top-level convenience used by run_phase3.py."""
    root = RAW_TEXT_DIR if input_source == "raw_text" else PROCESSED_DIR
    summ = estimate_summarisation(
        processed_dir=root,
        batched=batched,
        input_source=input_source,
    )
    eval_total = estimate_evaluation(
        processed_dir=root,
        judge_providers=judge_providers,
        summariser_providers=list(all_providers()),
        batched=batched,
        input_source=input_source,
    )
    print_report(summ, eval_total, batched=batched, input_source=input_source)
