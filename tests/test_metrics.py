"""验证配对误差和降水列联指标的分母、缺测和边界。"""

import unittest

import numpy as np

from wrf_evaluate.metrics import (
    MetricError,
    continuous_metrics,
    precipitation_event_metrics,
    temperature_event_metrics,
)


class MetricTests(unittest.TestCase):
    def test_zero_belongs_to_non_positive_without_rounding(self):
        non_positive, positive = temperature_event_metrics(
            [-1, 0, 0.001, 1, 0], [-2, 0.001, 0, 2, -1],
        )
        self.assertEqual(non_positive.sample_count, 5)
        self.assertEqual((non_positive.hits, non_positive.misses, non_positive.false_alarms,
                          non_positive.correct_negatives), (2, 1, 1, 1))
        self.assertEqual((positive.hits, positive.misses, positive.false_alarms,
                          positive.correct_negatives), (1, 1, 1, 2))
        self.assertAlmostEqual(non_positive.miss_rate, 1 / 3)
        self.assertAlmostEqual(non_positive.false_alarm_ratio, 1 / 3)
        self.assertAlmostEqual(non_positive.false_alarm_rate, 0.5)
        self.assertEqual((positive.miss_rate, positive.false_alarm_ratio), (0.5, 0.5))
        self.assertEqual(positive.false_alarm_rate, 1 / 3)

    def test_temperature_events_exclude_missing_and_keep_input_unchanged(self):
        forecast = np.ma.array([0, 1, -999, np.nan, -1, np.inf], mask=[0, 0, 1, 0, 0, 0])
        observation = np.array([0, -1, 1, 1, np.nan, 1])
        before = forecast.copy()
        non_positive, positive = temperature_event_metrics(forecast, observation)
        self.assertEqual(non_positive.sample_count, 2)
        self.assertEqual(positive.sample_count, 2)
        self.assertEqual((non_positive.hits, non_positive.misses), (1, 1))
        self.assertEqual((positive.hits, positive.false_alarms), (0, 1))
        self.assertIsNone(positive.miss_rate)
        self.assertEqual(positive.false_alarm_ratio, 1)
        np.testing.assert_array_equal(forecast.data, before.data)
        np.testing.assert_array_equal(forecast.mask, before.mask)

    def test_temperature_events_handle_empty_and_absent_events(self):
        for forecast, observation in [([], []), ([np.nan], [0])]:
            for event in temperature_event_metrics(forecast, observation):
                self.assertEqual(event.sample_count, 0)
                self.assertEqual((event.hits, event.misses, event.false_alarms, event.correct_negatives), (0, 0, 0, 0))
                self.assertIsNone(event.miss_rate)
                self.assertIsNone(event.false_alarm_ratio)
        non_positive, positive = temperature_event_metrics([0, -1], [0, -1])
        self.assertEqual((non_positive.miss_rate, non_positive.false_alarm_ratio), (0, 0))
        self.assertEqual(positive.correct_negatives, 2)
        self.assertIsNone(positive.miss_rate)
        self.assertIsNone(positive.false_alarm_ratio)

    def test_continuous_metrics_use_common_samples_and_forecast_minus_observation_bias(self):
        forecast = np.ma.array([2, 5, 9, np.nan], mask=[0, 0, 1, 0])
        observation = np.array([1, 3, 7, 8])
        result = continuous_metrics(forecast, observation)
        self.assertEqual(result.sample_count, 2)
        self.assertEqual(result.rmse, np.sqrt(2.5))
        self.assertEqual(result.mae, 1.5)
        self.assertEqual(result.bias, 1.5)
        self.assertAlmostEqual(result.correlation, 1)

    def test_continuous_metrics_with_no_common_values_are_unavailable(self):
        result = continuous_metrics(np.ma.masked_all(2), [1, 2])
        self.assertEqual(result.sample_count, 0)
        self.assertIsNone(result.rmse)
        self.assertIsNone(result.mae)
        self.assertIsNone(result.bias)
        self.assertIsNone(result.correlation)

    def test_pearson_correlation_preserves_sign_and_is_not_squared(self):
        for forecast, expected in [([1, 2, 4], np.sqrt(27 / 28)), ([4, 2, 1], -np.sqrt(27 / 28)), ([1, 0, 1], 0)]:
            with self.subTest(forecast=forecast):
                result = continuous_metrics(forecast, [1, 2, 3])
                self.assertAlmostEqual(result.correlation, expected)

    def test_correlation_is_unavailable_for_single_sample_or_constant_series(self):
        for forecast, observation in [([1], [2]), ([1, 1], [1, 2]), ([1, 2], [0, 0]), ([0.1] * 7, list(range(7)))]:
            with self.subTest(forecast=forecast, observation=observation):
                result = continuous_metrics(forecast, observation)
                self.assertIsNone(result.correlation)
                self.assertIsNotNone(result.rmse)

    def test_unsigned_error_is_signed_and_overflow_fails(self):
        result = continuous_metrics(np.array([0], dtype='uint16'), np.array([2], dtype='uint16'))
        self.assertEqual((result.rmse, result.mae, result.bias), (2, 2, -2))
        with self.assertRaises(MetricError):
            continuous_metrics([1e308], [-1e308])

    def test_common_mask_excludes_missing_from_event_counts_without_mutation(self):
        forecast = np.ma.array([1., 2., -999., 1., np.inf], mask=[0, 0, 1, 0, 0])
        observation = np.ma.array([1., 0., 0., 1., 1.], mask=[0, 0, 0, 1, 0])
        result = precipitation_event_metrics(forecast, observation, 0.1)
        self.assertEqual(result.sample_count, 2)
        self.assertEqual((result.hits, result.false_alarms, result.misses), (1, 1, 0))
        self.assertEqual((result.miss_rate, result.false_alarm_ratio), (0, 0.5))
        np.testing.assert_array_equal(forecast.mask, [0, 0, 1, 0, 0])
        self.assertEqual(forecast.data[2], -999)

    def test_precipitation_event_rates_and_threshold_boundary(self):
        result = precipitation_event_metrics(
            [0.99, 1, 2, 0], [1, 1, 0, 0], threshold_mm=1,
        )
        self.assertEqual((result.hits, result.misses, result.false_alarms, result.correct_negatives), (1, 1, 1, 1))
        self.assertEqual(result.miss_rate, 0.5)
        self.assertEqual(result.false_alarm_ratio, 0.5)

    def test_precipitation_rates_are_unavailable_without_denominators(self):
        result = precipitation_event_metrics([0, 0], [0, 0], threshold_mm=0.1)
        self.assertIsNone(result.miss_rate)
        self.assertIsNone(result.false_alarm_ratio)
        predicted_only = precipitation_event_metrics([1], [0], threshold_mm=0.1)
        self.assertIsNone(predicted_only.miss_rate)
        self.assertEqual(predicted_only.false_alarm_ratio, 1)

    def test_shapes_types_and_threshold_are_validated(self):
        for forecast, observation in [([1], [1, 2]), (["1"], [1]), (np.array([True]), [1])]:
            with self.subTest(forecast=forecast), self.assertRaises(MetricError):
                continuous_metrics(forecast, observation)
            with self.subTest(forecast=forecast), self.assertRaises(MetricError):
                temperature_event_metrics(forecast, observation)
        for threshold in [0, -1, float("nan"), float("inf"), True, "1"]:
            with self.subTest(threshold=threshold), self.assertRaises(MetricError):
                precipitation_event_metrics([0], [0], threshold)
        with self.assertRaises(MetricError):
            precipitation_event_metrics([-0.1], [0], 0.1)


if __name__ == "__main__":
    unittest.main()
