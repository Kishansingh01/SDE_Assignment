"""Statistical evaluation, significance testing, and guardrail validation."""
from __future__ import annotations

import math
from typing import Optional, Tuple


def normal_cdf(x: float) -> float:
    """Standard normal cumulative distribution function."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def two_proportion_z_test(
    successes_a: int,
    trials_a: int,
    successes_b: int,
    trials_b: int,
) -> Tuple[float, float, float]:
    """
    Computes two-sample z-test for difference in proportions (p_b - p_a).
    Returns (z_score, p_value, relative_lift).
    """
    if trials_a <= 0 or trials_b <= 0:
        return 0.0, 1.0, 0.0

    p_a = successes_a / trials_a
    p_b = successes_b / trials_b
    lift = (p_b - p_a) / p_a if p_a > 1e-6 else 0.0

    p_pooled = (successes_a + successes_b) / (trials_a + trials_b)
    if p_pooled <= 0 or p_pooled >= 1:
        return 0.0, 1.0, lift

    se = math.sqrt(p_pooled * (1.0 - p_pooled) * (1.0 / trials_a + 1.0 / trials_b))
    if se <= 1e-9:
        return 0.0, 1.0, lift

    z = (p_b - p_a) / se
    p_val = 2.0 * (1.0 - normal_cdf(abs(z)))
    return z, p_val, lift


def passes_guardrail(
    baseline_primary: float,
    candidate_primary: float,
    baseline_guardrail: float,
    candidate_guardrail: float,
    max_drop: float = 0.02,
) -> bool:
    """
    Checks that the candidate does not drop the guardrail metric by more than max_drop.
    """
    drop = baseline_guardrail - candidate_guardrail
    return drop <= max_drop + 1e-6


def required_sample_size(
    base_rate: float,
    mde: float = 0.05,
    alpha: float = 0.05,
    power: float = 0.80,
) -> int:
    """
    Calculates sample size needed per variant arm to detect a minimum detectable effect (MDE).
    z_alpha/2 = 1.96 for alpha=0.05, z_beta = 0.84 for 80% power.
    """
    z_a = 1.96
    z_b = 0.84
    p1 = base_rate
    p2 = base_rate * (1.0 + mde)
    p_bar = (p1 + p2) / 2.0
    numerator = (z_a * math.sqrt(2 * p_bar * (1 - p_bar)) + z_b * math.sqrt(p1 * (1 - p1) + p2 * (1 - p2))) ** 2
    denominator = (p2 - p1) ** 2 if abs(p2 - p1) > 1e-6 else 1e-4
    return int(math.ceil(numerator / denominator))

