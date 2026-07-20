"""
tests/conftest.py — Shared test setup
======================================

Adds `src/` and the hyphenated `llm-sum/` Phase 3 folder to sys.path so all
tests can import flat-module style:

    from utils import log_error           # src/utils.py
    from file_paths import doi_to_slug    # src/file_paths.py
    from summarizer import generate_summary  # llm-sum/summarizer.py
    from evaluator import parse_judge_response  # llm-sum/evaluator.py

Forces DRY_RUN=true and a permissive BUDGET_HARD_STOP so accidental real
API calls in test code would still abort safely, and so BudgetGuard does
not exit during deterministic-cost mock tests.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any


_REPO_ROOT = Path(__file__).resolve().parent.parent

# Force these for tests, overriding anything in the shell environment OR the
# project .env. Without `os.environ[...] = ...`, an explicit DRY_RUN=false in
# the user's local .env would leak into test runs and trigger real API code
# paths.
os.environ["DRY_RUN"] = "true"
os.environ["BUDGET_HARD_STOP"] = "1000.00"
# PHASE3_MODE=test forces dry_run regardless of the DRY_RUN env var — a
# defence-in-depth guardrail so a stray DRY_RUN=false in a child shell
# can't promote a unit test into a paid API call.
os.environ.setdefault("PHASE3_MODE", "test")
# Force the original data/summaries.jsonl pipeline for tests that don't pass
# --input-mode explicitly. Without this, a local .env with EVAL_INPUT_MODE=
# dev/regular (routing `evaluate` at data/dev_tests/summaries_txt or
# data/summaries_txt instead) leaks into run_phase3 wiring tests and makes
# them fail on developer machines while passing in a clean CI environment.
os.environ["EVAL_INPUT_MODE"] = "jsonl"
# Pin the batch-support matrix to the documented defaults (OpenAI/Anthropic
# batch-enabled, Gemini real-time) regardless of the developer's local .env.
# models_config.py reads these once at import time into a frozen MODELS dict,
# so whatever's ambient here becomes permanent for the whole test session —
# without this, a developer who set GEMINI_BATCH_ENABLED=true locally to
# smoke-test the feature (per docs/phase3/run_phase3.md) breaks every test
# that assumes the documented default. Tests that want the opposite state
# still override it explicitly with monkeypatch.setitem(models_config.MODELS,
# "gemini", dataclasses.replace(...)) — see test_batch_mode_submits_gemini_as_
# batch_when_flag_enabled in test_summarizer.py.
os.environ["GEMINI_BATCH_ENABLED"] = "false"
os.environ["OPENAI_BATCH_ENABLED"] = "true"
os.environ["ANTHROPIC_BATCH_ENABLED"] = "true"

for path in (_REPO_ROOT / "src", _REPO_ROOT / "llm-sum"):
    sp = str(path)
    if sp not in sys.path:
        sys.path.insert(0, sp)


# ---------------------------------------------------------------------------
# Shared evaluation-row factory
# ---------------------------------------------------------------------------
# One builder for data/evaluations.jsonl-shaped rows, used by every statistics
# test (report_tables, report_figures, stats_engine, eval_report). Previously
# three files each carried their own `_eval_row` with divergent signatures, so
# a test could pass against a row shape the neighbouring module never sees.
# Import it as `from conftest import make_eval_row`.
#
# Rows are INTERNALLY CONSISTENT by construction: unless a test says otherwise,
# every criterion is set to the requested `unweighted` score, so the stored
# composite and the criteria agree. That matters because the reporting layer
# recomputes composites from criteria_scores (report_tables._apply_recomputed_
# composites); a fixture whose criteria contradict its jury_score describes a row
# the evaluator could never emit, and tests built on one assert against fiction.
#
# Passing None for any criterion OMITS that key entirely rather than scoring it
# — that is how the missing-criterion regression tests express "the judge never
# returned this field", which is distinct from "the judge scored it 1". Use the
# explicit `criteria_scores=` override for shapes the kwargs cannot express.
# `hallucination_count` defaults to None (key omitted) for the same reason: an
# absent field means "no usable hallucination data", not "zero hallucinations".
# Tests asserting a hallucination rate must set it explicitly.

_CRITERIA = ("faithfulness", "completeness", "clinical_usefulness", "clarity", "safety")

# Distinguishes "caller said nothing" (derive from the score) from an explicit
# None (omit this criterion, i.e. the judge never returned it).
_UNSET = object()


def make_eval_row(
    doi: str,
    summarizer: str,
    judge: str = "openai",
    unweighted: float = 4.0,
    weighted: float = 4.0,
    *,
    faithfulness: Any = _UNSET,
    completeness: Any = _UNSET,
    clinical_usefulness: Any = _UNSET,
    clarity: Any = _UNSET,
    safety: Any = _UNSET,
    species: str = "Canine",
    study_design: str = "RCT",
    clinical_topic: str = "Oncology",
    journal: str = "JVIM",
    input_source: str = "processed",
    hallucination_count: int | None = None,
    criteria_scores: dict | None = None,
    **extra,
) -> dict:
    """One evaluations.jsonl row with both jury modes, strata, and criteria_scores.

    ``criteria_scores`` overrides the per-criterion kwargs wholesale when a test
    needs a shape the kwargs cannot express (e.g. a non-numeric score). Any
    remaining keyword lands on the row verbatim, so a test can add or override a
    field without this signature having to grow.
    """
    if criteria_scores is None:
        scores = {
            "faithfulness": faithfulness,
            "completeness": completeness,
            "clinical_usefulness": clinical_usefulness,
            "clarity": clarity,
            "safety": safety,
        }
        criteria_scores = {
            # Unspecified -> mirror the composite so the row is self-consistent.
            name: {"score": unweighted if value is _UNSET else value, "reasoning": "ok"}
            for name, value in scores.items()
            if value is not None
        }
    row = {
        "benchmark_name": "vet_lit_summary_medhelm",
        "doi": doi,
        "summarizer": summarizer,
        "judge": judge,
        "input_source": input_source,
        "jury_score": unweighted,
        "jury_score_unweighted": unweighted,
        "jury_score_weighted": weighted,
        "judge_disagreement": 0.0,
        "criteria_scores": criteria_scores,
        "strata": {
            "species": [species],
            "study_design": study_design,
            "clinical_topic": clinical_topic,
            "journal": journal,
            "input_source": input_source,
        },
    }
    if hallucination_count is not None:
        row["hallucination_count"] = hallucination_count
    row.update(extra)
    return row
