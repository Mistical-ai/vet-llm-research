"""Shared retry helper for future provider adapter migration."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")


def retry_with_backoff(
    func: Callable[[], T],
    *,
    attempts: int = 3,
    initial_sleep: float = 1.0,
    multiplier: float = 2.0,
) -> T:
    """Run ``func`` with simple exponential backoff.

    This helper avoids introducing a new abstraction around ``tenacity`` while
    still giving adapters one shared retry policy.
    """
    last_error: Exception | None = None
    sleep_seconds = initial_sleep
    for attempt in range(1, attempts + 1):
        try:
            return func()
        except Exception as exc:
            last_error = exc
            if attempt == attempts:
                break
            time.sleep(sleep_seconds)
            sleep_seconds *= multiplier
    raise RuntimeError(f"Provider call failed after {attempts} attempts") from last_error
