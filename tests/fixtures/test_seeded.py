"""
Seeded fixture test suite for ChilliFlake.

This file is the ground-truth dataset for the flaky-test detection pipeline.
Known outcomes (for precision/recall measurement):
  - test_stable_pass_1   → always passes
  - test_stable_pass_2   → always passes
  - test_stable_fail     → always fails  (should NOT be flagged as flaky)
  - test_flaky           → fails ~30% of runs  (should be flagged as flaky)
  - test_skip_me         → always skipped

Do NOT modify this file between runs — stability of outcomes is required
for the ground-truth measurement to be meaningful.
"""
import random
import pytest


def test_stable_pass_1():
    """Unconditionally passes. Baseline stable-green test."""
    assert True


def test_stable_pass_2():
    """Another stable passing test with trivial arithmetic."""
    assert 1 + 1 == 2


def test_stable_pass_3():
    """Third stable passing test."""
    result = [x * 2 for x in range(5)]
    assert result == [0, 2, 4, 6, 8]


def test_stable_fail():
    """
    Unconditionally fails — represents a genuinely broken test.
    The analyzer must NOT flag this as flaky (it never passes).
    """
    assert False, "This test is intentionally broken"


def test_flaky():
    """
    Deliberately flaky: fails roughly 30% of runs.

    Uses random.random() with no fixed seed so each CI run is independent.
    Over enough runs, the analyzer should detect this as flaky.

    Ground truth: reported_flaky = 1 when it fails then passes on retry,
    or when the analyzer's statistical model flags it based on pass-rate variance.
    """
    assert random.random() > 0.3, "Flaky failure — expected ~30% of the time"


def test_skip_me():
    """Intentionally skipped. Should appear in test_runs with status='skipped'."""
    pytest.skip("Intentionally skipped — part of seeded ground truth")
