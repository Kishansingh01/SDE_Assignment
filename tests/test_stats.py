"""Unit tests for statistical evaluations, z-tests, and guardrail validations."""
import pytest
from controller.stats import (
    normal_cdf,
    passes_guardrail,
    required_sample_size,
    two_proportion_z_test,
)


def test_normal_cdf():
    assert abs(normal_cdf(0.0) - 0.5) < 1e-6
    assert abs(normal_cdf(1.96) - 0.975002) < 1e-4
    assert abs(normal_cdf(-1.96) - 0.024998) < 1e-4


def test_two_proportion_z_test_significant():
    # 50% vs 60% on 500 trials each
    z, p_val, lift = two_proportion_z_test(250, 500, 300, 500)
    assert lift == pytest.approx(0.20, rel=1e-2)
    assert z > 3.0
    assert p_val < 0.01


def test_two_proportion_z_test_no_difference():
    z, p_val, lift = two_proportion_z_test(250, 500, 250, 500)
    assert lift == 0.0
    assert z == 0.0
    assert p_val == 1.0


def test_passes_guardrail():
    # Primary improved, guardrail dropped by 0.01 (within 0.02 threshold)
    assert passes_guardrail(0.50, 0.55, 0.30, 0.29, max_drop=0.02) is True
    # Guardrail dropped by 0.03 (exceeds 0.02 threshold)
    assert passes_guardrail(0.50, 0.55, 0.30, 0.27, max_drop=0.02) is False


def test_required_sample_size():
    n = required_sample_size(0.50, mde=0.10)
    assert n > 200

