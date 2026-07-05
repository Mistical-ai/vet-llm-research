"""Bootstrap confidence intervals for primary metrics."""

from __future__ import annotations

import random
from statistics import mean


def percentile(values: list[float], q: float) -> float:
    """Return a simple percentile from sorted values."""
    if not values:
        return 0.0
    sorted_values = sorted(values)
    index = (len(sorted_values) - 1) * q
    lower = int(index)
    upper = min(lower + 1, len(sorted_values) - 1)
    weight = index - lower
    return sorted_values[lower] * (1 - weight) + sorted_values[upper] * weight


def bootstrap_mean_ci(
    values: list[float],
    *,
    reps: int = 1000,
    seed: int = 42,
    confidence: float = 0.95,
) -> dict[str, float]:
    """Return deterministic bootstrap CI for a mean.

    Resampling should be done on per-instance values before this helper is
    called. That avoids treating multiple judge rows for one paper as
    independent evidence.
    """
    clean = [float(value) for value in values if value is not None]
    if not clean:
        return {"mean": 0.0, "low": 0.0, "high": 0.0}
    rng = random.Random(seed)
    estimates: list[float] = []
    for _ in range(reps):
        sample = [rng.choice(clean) for _ in clean]
        estimates.append(mean(sample))
    alpha = (1 - confidence) / 2
    return {
        "mean": round(mean(clean), 4),
        "low": round(percentile(estimates, alpha), 4),
        "high": round(percentile(estimates, 1 - alpha), 4),
    }
