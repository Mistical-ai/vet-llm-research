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

for path in (_REPO_ROOT / "src", _REPO_ROOT / "llm-sum"):
    sp = str(path)
    if sp not in sys.path:
        sys.path.insert(0, sp)
