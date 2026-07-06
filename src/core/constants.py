"""
src/core/constants.py — shared deterministic defaults
======================================================

Centralizing reproducibility constants keeps sampling, bootstrap statistics,
and run manifests from drifting when old environment variable names are still
supported for backward compatibility.
"""

from __future__ import annotations

import os

DEFAULT_RANDOM_SEED = 42


def get_random_seed() -> int:
    """Return the configured deterministic seed with legacy env fallbacks.

    ``RANDOM_SEED`` is the canonical setting. ``EVAL_SAMPLE_SEED`` and ``SEED``
    remain supported because existing Phase 3 scripts and `.env.template` have
    used those names for sampling and provider calls.
    """
    for name in ("RANDOM_SEED", "EVAL_SAMPLE_SEED", "SEED"):
        raw_value = os.getenv(name)
        if raw_value is None or raw_value == "":
            continue
        return int(raw_value)
    return DEFAULT_RANDOM_SEED


RANDOM_SEED = get_random_seed()
