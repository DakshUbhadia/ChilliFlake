from __future__ import annotations

import math


def flip_rate(statuses: list[str]) -> float:
    n = len(statuses)
    if n < 2:
        return 0.0
    flips = sum(1 for a, b in zip(statuses, statuses[1:]) if a != b)
    return flips / (n - 1)


def wilson_lower_bound(successes: int, n: int, z: float = 1.96) -> float:
    if n == 0:
        return 0.0
    p_hat = successes / n
    z2 = z * z
    centre = p_hat + z2 / (2 * n)
    margin = z * math.sqrt(p_hat * (1 - p_hat) / n + z2 / (4 * n * n))
    return max(0.0, (centre - margin) / (1 + z2 / n))


def duration_coefficient_of_variation(durations_ms: list[int]) -> float | None:
    n = len(durations_ms)
    if n < 2:
        return None
    mean = sum(durations_ms) / n
    if mean == 0:
        return None
    variance = sum((d - mean) ** 2 for d in durations_ms) / (n - 1)
    return math.sqrt(variance) / mean


def classify(
    sample_size: int,
    pass_rate: float,
    wilson_lb: float,
    min_samples: int = 5,
    flaky_threshold: float = 0.15,
    broken_threshold: float = 0.05,
) -> str:
    if sample_size < min_samples:
        return 'insufficient_data'
    if pass_rate < broken_threshold:
        return 'broken'
    if wilson_lb > flaky_threshold:
        return 'flaky'
    return 'stable'