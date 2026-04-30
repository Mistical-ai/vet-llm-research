"""
src/utils.py — The Governor
===========================

WHY DOES THIS MODULE EXIST?
----------------------------
Every pipeline that calls external APIs faces two existential risks:
  1. Runaway costs   – a bug loops 1,000 times and blows the budget in minutes.
  2. Silent failures – a crash mid-run leaves no trace of which papers failed,
                       so the researcher can't resume without re-running everything.

This module is the "Governor": a single, importable safety layer that every
other module must use before touching money or writing errors.

ARCHITECTURAL DECISIONS
------------------------
- BudgetGuard (class, not a global variable):
    A class keeps the running total in one place and can be passed around or
    mocked in tests.  A bare global float could be mutated from anywhere, making
    bugs impossible to trace.

- sys.exit() on budget breach (not raise Exception):
    We want the process to die immediately, even if the calling code accidentally
    catches generic exceptions.  SystemExit bypasses most except blocks.

- JSONL error ledger (not a plain .txt log):
    Each error is a self-contained JSON object on its own line.  The file is
    append-safe: even if the pipeline crashes mid-write, every previously written
    line is still valid JSON.  A plain text log cannot be parsed programmatically
    for later analysis.

- Model-specific sleep dictionary (not a single global delay):
    Different LLM providers have different rate limits.  Putting them in a
    dictionary lets future engineers add a new model without touching the logic.

- load_dotenv() at import time:
    All modules import utils, so calling load_dotenv() here guarantees the
    environment is ready before any config value is read, regardless of which
    module is the entry point.
"""

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

# Load .env as soon as this module is imported.
# Why here? Because every other Phase 2 module imports utils.py, so this one
# load_dotenv() call covers the whole pipeline without each file having to
# remember to call it themselves.
load_dotenv()

# ---------------------------------------------------------------------------
# Module-level constants (read from environment with safe fallbacks)
# ---------------------------------------------------------------------------

# BUDGET_HARD_STOP is read as a float from the environment.
# Default 0.00 means: by default the pipeline costs nothing (dry-run mode).
# A researcher sets this to e.g. 50.00 before a real run.
BUDGET_HARD_STOP: float = float(os.getenv("BUDGET_HARD_STOP", "0.00"))

# Per-model sleep durations in seconds, read from environment with defaults.
# These prevent HTTP 429 "Too Many Requests" errors from LLM providers.
# Why a dictionary? Adding a new model is a one-line change; no if/elif chain.
RATE_LIMITS: dict[str, float] = {
    "gemini":   float(os.getenv("RATE_LIMIT_GEMINI",  "1.0")),
    "openai":   float(os.getenv("RATE_LIMIT_OPENAI",  "1.0")),
    "anthropic": float(os.getenv("RATE_LIMIT_ANTHROPIC", "1.0")),
    "default":  float(os.getenv("RATE_LIMIT_DEFAULT", "1.0")),
}

# Path to the error ledger.  Stored in data/ which is gitignored, so sensitive
# failure messages (containing DOIs, API responses, etc.) never reach GitHub.
ERROR_LOG_PATH: Path = Path("data") / "error_log.jsonl"


# ---------------------------------------------------------------------------
# BudgetGuard
# ---------------------------------------------------------------------------

class BudgetGuard:
    """
    Stateful budget tracker.

    HOW TO USE:
        guard = BudgetGuard()
        guard.add_cost(0.05)   # charged $0.05 for one LLM call
        guard.add_cost(0.10)   # now $0.15 total

    WHY STATEFUL?
        The guard accumulates costs across many calls in a single pipeline run.
        A stateless function would need the caller to maintain the running total,
        which is error-prone.

    WHY sys.exit()?
        We want a hard stop.  If we raised a custom exception, a careless
        except block in user code could silently swallow it and keep spending.
        SystemExit is only caught by code that explicitly catches SystemExit or
        BaseException — a deliberate choice.
    """

    def __init__(self, hard_stop: float | None = None) -> None:
        # Allow tests to inject a custom limit without touching the environment.
        self.hard_stop: float = hard_stop if hard_stop is not None else BUDGET_HARD_STOP
        self.total_spent: float = 0.0

    def add_cost(self, amount: float) -> None:
        """
        Add `amount` (in USD) to the running total and enforce the hard stop.

        Parameters
        ----------
        amount : float
            Cost of a single operation, in USD.  Must be >= 0.
        """
        if amount < 0:
            raise ValueError(f"Cost amount cannot be negative; got {amount}")

        self.total_spent += amount

        if self.total_spent > self.hard_stop:
            # Print a clear message before dying so the researcher knows why.
            print(
                f"\n[BudgetGuard] HARD STOP TRIGGERED.\n"
                f"  Total spent : ${self.total_spent:.4f}\n"
                f"  Hard limit  : ${self.hard_stop:.4f}\n"
                f"  Action      : Exiting now to protect budget.\n"
            )
            sys.exit(1)

    @property
    def remaining(self) -> float:
        """How many dollars are left before the hard stop fires."""
        return max(0.0, self.hard_stop - self.total_spent)

    def __repr__(self) -> str:
        return (
            f"BudgetGuard(spent=${self.total_spent:.4f}, "
            f"limit=${self.hard_stop:.4f}, "
            f"remaining=${self.remaining:.4f})"
        )


# ---------------------------------------------------------------------------
# sleep_for_model
# ---------------------------------------------------------------------------

def sleep_for_model(model_name: str) -> None:
    """
    Pause execution for the model-specific rate-limit delay.

    WHY IS THIS A SEPARATE FUNCTION AND NOT INLINE?
        If we scattered time.sleep() calls throughout the code, future engineers
        would have to hunt down every LLM call to tune rate limits.  Centralising
        the logic here means a single change adjusts all callers.

    WHY NOT ASYNCIO SLEEP?
        The pipeline is a synchronous script.  Async would add complexity and
        dependency overhead for no benefit at this scale.

    Parameters
    ----------
    model_name : str
        One of "gemini", "openai", "anthropic", or any key in RATE_LIMITS.
        Unknown names fall back to the "default" delay.
    """
    key = model_name.lower()
    delay = RATE_LIMITS.get(key, RATE_LIMITS["default"])

    dry_run = os.getenv("DRY_RUN", "true").lower() == "true"
    if dry_run:
        # In dry-run mode we skip the actual sleep so tests run instantly.
        # We still print a message so the researcher can verify the delay
        # that *would* have fired.
        print(f"[sleep_for_model] DRY_RUN — would sleep {delay}s for '{model_name}'")
        return

    print(f"[sleep_for_model] Sleeping {delay}s for '{model_name}' rate limit...")
    time.sleep(delay)


# ---------------------------------------------------------------------------
# log_error
# ---------------------------------------------------------------------------

def log_error(doi: str, stage: str, message: str) -> None:
    """
    Append one structured error entry to the JSONL error ledger.

    WHY A SEPARATE ERROR LEDGER (not just print to stderr)?
        1. Survives crashes  — the file exists even if the process dies.
        2. Programmatically queryable — each line is valid JSON, so a
           follow-up analysis script can count failures by stage, grep for
           specific DOIs, etc.
        3. Does not pollute stdout — the pipeline's normal output stays clean.

    WHY APPEND MODE?
        Opening with "a" means each call adds one line without overwriting
        earlier entries.  This is the defining property of JSONL: crash-safe
        incremental writes.

    Entry schema
    ------------
    {
        "timestamp": "2026-04-29T23:00:00+00:00",  # ISO-8601 UTC
        "doi":       "10.1111/example.12345",
        "stage":     "download",
        "message":   "No OA version available"
    }

    Parameters
    ----------
    doi     : str  — The paper's DOI (or "N/A" if not yet resolved).
    stage   : str  — Pipeline stage name, e.g. "collect", "download", "extract".
    message : str  — Human-readable description of the failure.
    """
    # Ensure the data/ directory exists before trying to write.
    # Why parents=True? The whole data/ tree may not exist on first run.
    ERROR_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "doi":       doi,
        "stage":     stage,
        "message":   message,
    }

    # "a" = append mode; creates the file if it does not yet exist.
    with open(ERROR_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")

    # Echo to stdout so the researcher watching the terminal sees failures live.
    print(f"[log_error] {stage} | {doi} | {message}")
