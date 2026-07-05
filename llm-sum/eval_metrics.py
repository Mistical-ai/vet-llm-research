"""Backward-compatible wrapper for automatic metrics.

The implementation now lives in ``src/metrics/automatic.py`` so reports,
evaluators, and tests can share one metric contract. This module remains because
existing Phase 3 code imports ``eval_metrics`` directly from the hyphenated
``llm-sum`` folder.
"""

from metrics.automatic import (  # noqa: F401
    SECTION_PATTERNS,
    TOKEN_RE,
    calculate_automatic_metrics,
    compression_ratio,
    extractive_coverage,
    rouge_l_recall,
    rouge_recall,
    section_coverage,
    tokenize,
)
