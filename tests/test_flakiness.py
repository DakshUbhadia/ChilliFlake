from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.analyzer.flakiness import classify, duration_coefficient_of_variation, flip_rate, wilson_lower_bound


class TestFlipRate:
    def test_all_same_returns_zero(self):
        assert flip_rate(['passed', 'passed', 'passed']) == 0.0

    def test_strictly_alternating_returns_one(self):
        assert flip_rate(['passed', 'failed', 'passed', 'failed']) == 1.0

    def test_single_element_returns_zero(self):
        assert flip_rate(['passed']) == 0.0

    def test_empty_returns_zero(self):
        assert flip_rate([]) == 0.0

    def test_one_flip_in_three(self):
        assert abs(flip_rate(['passed', 'failed', 'failed']) - 0.5) < 1e-9

    def test_two_element_flip(self):
        assert flip_rate(['passed', 'failed']) == 1.0

    def test_two_element_no_flip(self):
        assert flip_rate(['failed', 'failed']) == 0.0


class TestWilsonLowerBound:
    def test_zero_trials_returns_zero(self):
        assert wilson_lower_bound(0, 0) == 0.0

    def test_no_successes_near_zero(self):
        assert 0.0 <= wilson_lower_bound(0, 10) < 0.01

    def test_same_p_hat_larger_n_gives_higher_lower_bound(self):
        small_n = wilson_lower_bound(successes=1, n=2)
        large_n = wilson_lower_bound(successes=10, n=20)
        assert large_n > small_n, f'{large_n:.4f} <= {small_n:.4f}'

    def test_all_successes_gives_high_lower_bound(self):
        assert wilson_lower_bound(40, 40) > 0.85

    def test_result_between_zero_and_one(self):
        for s, n in [(0, 5), (3, 10), (10, 10), (1, 100)]:
            assert 0.0 <= wilson_lower_bound(s, n) <= 1.0

    def test_monotone_in_successes_fixed_n(self):
        bounds = [wilson_lower_bound(s, 20) for s in range(21)]
        assert bounds == sorted(bounds)


class TestDurationCV:
    def test_fewer_than_two_samples_returns_none(self):
        assert duration_coefficient_of_variation([]) is None
        assert duration_coefficient_of_variation([500]) is None

    def test_constant_durations_returns_near_zero(self):
        result = duration_coefficient_of_variation([100, 100, 100, 100])
        assert result is not None and abs(result) < 1e-9

    def test_high_variance_notably_higher_than_constant(self):
        variable_cv = duration_coefficient_of_variation([10, 1000, 50, 900])
        assert variable_cv is not None and variable_cv > 0.5

    def test_all_zero_durations_returns_none(self):
        assert duration_coefficient_of_variation([0, 0, 0]) is None

    def test_two_samples_different_durations(self):
        result = duration_coefficient_of_variation([100, 200])
        assert result is not None and result > 0.0


class TestClassify:
    def test_insufficient_data_just_under_min_samples(self):
        assert classify(sample_size=4, pass_rate=0.5, wilson_lb=0.5) == 'insufficient_data'

    def test_exactly_at_min_samples_is_not_insufficient(self):
        assert classify(sample_size=5, pass_rate=1.0, wilson_lb=0.0) != 'insufficient_data'

    def test_broken_just_under_broken_threshold(self):
        assert classify(sample_size=20, pass_rate=0.04, wilson_lb=0.0) == 'broken'

    def test_flaky_just_over_flaky_threshold(self):
        assert classify(sample_size=20, pass_rate=0.6, wilson_lb=0.16) == 'flaky'

    def test_stable_all_below_thresholds(self):
        assert classify(sample_size=50, pass_rate=0.98, wilson_lb=0.02) == 'stable'

    def test_broken_takes_priority_over_flaky(self):
        assert classify(sample_size=20, pass_rate=0.0, wilson_lb=0.99) == 'broken'

    def test_custom_thresholds_respected(self):
        assert classify(50, 0.5, 0.5, flaky_threshold=0.9) == 'stable'
        assert classify(50, 0.5, 0.02, flaky_threshold=0.01) == 'flaky'