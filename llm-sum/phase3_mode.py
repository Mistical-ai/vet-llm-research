"""
llm-sum/phase3_mode.py — single source of truth for Phase 3 run modes
========================================================================

Phase 3 had two confusing toggles: ``USE_BATCH_API`` in ``.env`` and a
hardcoded ``DEVELOPMENT_MODE = True`` in ``summarizer.py``/``evaluator.py``.
A new user couldn't tell from one place which knob did what, and "run
this on exactly one paper to test my prompt" required editing Python.

This module replaces both with one ``PHASE3_MODE`` env var that resolves
into a frozen ``ModeProfile`` describing exactly what the run will do.

Modes
-----
+--------+----------+-------------+-----------+-------------------+
| name   | dry_run  | paper_limit | use_batch | requires_confirm  |
+========+==========+=============+===========+===================+
| test   | True     | None        | False     | False             |
| single | False    | 1           | False     | True              |
| dev    | False    | DEV_LIMIT*  | False     | True              |
| batch  | False    | None        | True      | True              |
+--------+----------+-------------+-----------+-------------------+

  ``*`` ``PHASE3_DEV_LIMIT`` env var, default ``5``.

Rules
-----
* ``test`` is the default. A misconfigured ``.env`` (no PHASE3_MODE set)
  always lands on test mode → mocks only → no spend.
* ``test`` forces ``dry_run=True`` regardless of the ``DRY_RUN`` env var.
  This is the last guardrail before pytest accidentally hits a live API.
* CLI ``--mode`` overrides ``.env``; CLI ``--limit`` overrides
  ``paper_limit``. Either can be ``None`` (= use whichever the mode
  specifies).
* ``requires_confirm`` is True for every mode that makes paid API
  calls. The summarizer / evaluator gate ``confirm_real_*`` on this
  flag; ``--force`` is the only programmatic bypass.

Why no DEVELOPMENT_MODE constant in Python any more?
    A hardcoded ``True`` made the code feel safer than it was: any
    refactor that flipped it (or a bad merge) silently unlocked a
    250-paper run. The new default mode lives in env (which Claude Code
    can see but won't modify) AND the safest mode is the default — so
    "no config" still equals "no spend."
"""

from __future__ import annotations

import os
from dataclasses import dataclass


VALID_MODES = ("test", "single", "dev", "batch")
DEFAULT_MODE = "test"


@dataclass(frozen=True)
class ModeProfile:
    """Resolved Phase 3 run profile. Frozen so it can't be mutated mid-run."""
    name: str                # "test" | "single" | "dev" | "batch"
    dry_run: bool            # forces _is_dry_run() True regardless of DRY_RUN env
    paper_limit: int | None  # None = full corpus
    use_batch: bool
    requires_confirm: bool   # interactive y/N gate before live API calls

    def banner(self) -> str:
        """One-line summary printed by every script on startup."""
        bits = [f"mode={self.name}"]
        if self.paper_limit is not None:
            bits.append(f"limit={self.paper_limit}")
        bits.append("batch-API" if self.use_batch else "real-time")
        if self.dry_run:
            bits.append("DRY_RUN")
        if self.requires_confirm:
            bits.append("confirm-required")
        return f"[phase3] {' | '.join(bits)}"


def _dev_limit(source) -> int:
    raw = source.get("PHASE3_DEV_LIMIT", "5")
    try:
        n = int(raw)
    except (TypeError, ValueError):
        n = 5
    return max(1, n)


def _build_profile(mode: str, source) -> ModeProfile:
    """Map a mode name to its profile. Caller has already validated ``mode``."""
    if mode == "test":
        return ModeProfile(
            name="test", dry_run=True, paper_limit=None,
            use_batch=False, requires_confirm=False,
        )
    if mode == "single":
        return ModeProfile(
            name="single", dry_run=False, paper_limit=1,
            use_batch=False, requires_confirm=True,
        )
    if mode == "dev":
        return ModeProfile(
            name="dev", dry_run=False, paper_limit=_dev_limit(source),
            use_batch=False, requires_confirm=True,
        )
    if mode == "batch":
        return ModeProfile(
            name="batch", dry_run=False, paper_limit=None,
            use_batch=True, requires_confirm=True,
        )
    raise ValueError(f"Unknown Phase 3 mode: {mode!r}. Valid: {VALID_MODES}")


def resolve_mode(cli_override: str | None = None,
                 *, env: dict[str, str] | None = None) -> ModeProfile:
    """
    Return the active ``ModeProfile`` based on (CLI override > env > default).

    Parameters
    ----------
    cli_override:
        Value from ``--mode`` argparse arg, or None.
    env:
        Optional override of ``os.environ`` (for tests). When provided it
        is used for BOTH the ``PHASE3_MODE`` lookup and ``PHASE3_DEV_LIMIT``
        so a unit test can fully control the resolved profile. Production
        callers leave this as None.
    """
    source = env if env is not None else os.environ
    raw = (cli_override or source.get("PHASE3_MODE") or DEFAULT_MODE).strip().lower()
    if raw not in VALID_MODES:
        # Conservative: an unknown mode falls back to test, so a typo in
        # .env never accidentally triggers a paid run.
        print(f"[phase3_mode] WARNING: unknown PHASE3_MODE={raw!r}; "
              f"falling back to {DEFAULT_MODE}. Valid: {VALID_MODES}")
        raw = DEFAULT_MODE
    return _build_profile(raw, source)
