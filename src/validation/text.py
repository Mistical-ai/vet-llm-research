"""
src/validation/text.py — pure text validation helpers
=====================================================

This module starts thinning the larger extraction script by moving deterministic
text helpers into a small, testable module. The public extraction entrypoints
still live in ``src/extract.py`` for backward compatibility.
"""

from __future__ import annotations


def truncate_to_sentence_boundary(text: str, limit: int) -> str:
    """Return text no longer than ``limit``, preferring a sentence boundary.

    The helper is pure: it does not print, read files, or mutate state. That
    makes it easy to test and reuse when prompt builders need the same behavior.
    """
    if len(text) <= limit:
        return text

    window = text[:limit]
    last_period = window.rfind(".")
    if last_period > limit // 2:
        return window[: last_period + 1]
    return window
