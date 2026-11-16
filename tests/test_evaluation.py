"""检查气温时间配对、缺测处理和指标 CSV。"""

from datetime import datetime, timedelta, timezone
import csv
from io import StringIO
from pathlib import Path
import unittest
from unittest.mock import mock_open, patch

from wrf_evaluate.evaluation import compare_series, write_metrics
from wrf_evaluate.series import Series


class SeriesEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.start = datetime(2024, 1, 1, tzinfo=timezone.utc)

    def compare(self, *args):
        result, = compare_series(*args)
        return result

    def series(self, label, minutes, values):
        return Series(label, tuple(self.start + timedelta(minutes=m) for m in minutes), tuple(values))

    def test_pairs_nearest_times_once_with_inclusive_tolerance(self):
        forecast = self.series('WRF', [0, 2, 4], [0, 2, 4])
        observation = self.series('观测', [1, 5], [1, 3])
        result = self.compare(forecast, observation, timedelta(minutes=1))
        self.assertEqual(result.matched_times, 2)
        self.assertEqual(result.metrics.sample_count, 2)
        self.assertEqual(result.metrics.rmse, 1)
        self.assertEqual(result.metrics.mae, 1)
        self.assertEqual(result.metrics.bias, 0)
        self.assertAlmostEqual(result.metrics.correlation, 1)

    def test_observation_uses_closest_forecast_instead_of_first_in_window(self):
        forecast = self.series('WRF', [0, 2], [0, 2])
        observation = self.series('观测', [1.2], [2])
        result = self.compare(forecast, observation, timedelta(minutes=1))
        self.assertEqual(result.matched_times, 1)
        self.assertEqual(result.metrics.bias, 0)

    def test_missing_pairs_reduce_valid_sample_count_without_zero_fill(self):
        forecast = self.series('WRF', [0, 1], [None, 2])
        observation = self.series('观测', [0, 1], [1, None])
        result = self.compare(forecast, observation, timedelta(0))
        self.assertEqual(result.matched_times, 2)
        self.assertEqual(result.metrics.sample_count, 0)
        self.assertIsNone(result.metrics.rmse)

    def test_csv_is_utf8_excel_friendly_and_contains_blank_metrics_without_samples(self):
        forecast = self.series('WRF', [0], [None])
        observation = self.series('站点实测', [0], [1])
        comparison = self.compare(forecast, observation, timedelta(seconds=60))
        output = mock_open()
        with patch.object(Path, 'open', output):
            write_metrics(Path('metrics.csv'), [comparison])
        output.assert_called_once_with('w', newline='', encoding='utf-8-sig')
        content = ''.join(call.args[0] for call in output().write.call_args_list)
        row = next(csv.DictReader(StringIO(content)))
        self.assertEqual(row['实况来源'], '站点实测')
        self.assertEqual(row['配对时次数'], '1')
        self.assertEqual(row['有效样本数'], '0')
        self.assertEqual(row['RMSE(℃)'], '')
        self.assertEqual(row['R'], '')
        self.assertEqual(row['非正温(≤0℃)_命中数'], '0')
        self.assertEqual(row['正温(>0℃)_漏报率'], '')
        self.assertEqual(row['非正温(≤0℃)_空报比'], '')

    def test_events_use_same_unique_pairs_and_export_both_categories(self):
        forecast = self.series('WRF', [0, 0.5, 2, 3, 4], [0, 9, 1, None, -1])
        observation = self.series('实测', [0, 2, 3, 4], [0, -1, 1, 2])
        result = self.compare(forecast, observation, timedelta(minutes=1))
        self.assertEqual(result.matched_times, 4)
        self.assertEqual(result.metrics.sample_count, 3)
        self.assertEqual(dict(result.events)['非正温(≤0℃)'].sample_count, 3)
        self.assertEqual(dict(result.events)['正温(>0℃)'].sample_count, 3)
        output = mock_open()
        with patch.object(Path, 'open', output):
            write_metrics('metrics.csv', [result])
        content = ''.join(call.args[0] for call in output().write.call_args_list)
        row = next(csv.DictReader(StringIO(content)))
        self.assertEqual(row['非正温(≤0℃)_命中数'], '1')
        self.assertEqual(row['非正温(≤0℃)_漏报数'], '1')
        self.assertEqual(row['非正温(≤0℃)_空报数'], '1')
        self.assertEqual(float(row['非正温(≤0℃)_漏报率']), 0.5)
        self.assertEqual(float(row['正温(>0℃)_空报比']), 1)

    def test_csv_exports_correlation_and_leaves_undefined_correlation_blank(self):
        forecast = self.series('WRF', [0, 1, 2], [3, 2, 1])
        observations = [self.series('变化', [0, 1, 2], [1, 2, 3]),
                        self.series('常量', [0, 1, 2], [1, 1, 1])]
        comparisons = [self.compare(forecast, obs, timedelta(0)) for obs in observations]
        output = mock_open()
        with patch.object(Path, 'open', output):
            write_metrics('metrics.csv', comparisons)
        content = ''.join(call.args[0] for call in output().write.call_args_list)
        rows = list(csv.DictReader(StringIO(content)))
        self.assertAlmostEqual(float(rows[0]['R']), -1)
        self.assertNotIn('配对容差(秒)', rows[0])
        self.assertEqual(rows[1]['R'], '')
        self.assertNotEqual(rows[1]['RMSE(℃)'], '')

    def test_forecast_time_is_not_reused_for_multiple_observations(self):
        forecast = self.series('WRF', [0], [1])
        observation = self.series('观测', [0, 0.5], [1, 99])
        result = self.compare(forecast, observation, timedelta(minutes=1))
        self.assertEqual(result.matched_times, 1)
        self.assertEqual(result.metrics.mae, 0)

    def test_conflicting_nearest_observations_use_next_closest_available_forecast(self):
        forecast = self.series('WRF', [0, 10], [0, 10])
        observation = self.series('观测', [4, 5], [0, 10])
        result = self.compare(forecast, observation, timedelta(minutes=5))
        self.assertEqual(result.matched_times, 2)
        self.assertEqual(result.metrics.mae, 0)

    def test_timezone_offsets_exact_matching_and_unmatched_samples(self):
        forecast = self.series('WRF', [0, 1], [2, 99])
        observation = Series('观测', (self.start.astimezone(timezone(timedelta(hours=8))),), (1,))
        result = self.compare(forecast, observation, timedelta(0))
        self.assertEqual(result.matched_times, 1)
        self.assertEqual(result.metrics.bias, 1)
        later = self.series('观测', [2.01], [1])
        result = self.compare(forecast, later, timedelta(minutes=1))
        self.assertEqual(result.matched_times, 0)
        self.assertIsNone(result.metrics.mae)

    def test_invalid_tolerance_is_rejected(self):
        series = self.series('WRF', [0], [1])
        for tolerance in (-1, timedelta(seconds=-1)):
            with self.subTest(tolerance=tolerance), self.assertRaises(ValueError):
                self.compare(series, series, tolerance)

    def test_no_verified_rain_comparisons_write_header_without_fake_scores(self):
        output = mock_open()
        with patch.object(Path, 'open', output):
            write_metrics('rain.csv', [], unit='mm')
        content = ''.join(call.args[0] for call in output().write.call_args_list)
        reader = csv.DictReader(StringIO(content))
        self.assertIn('RMSE(mm)', reader.fieldnames)
        self.assertEqual(list(reader), [])


class PeriodEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.start = datetime(2024, 1, 1, tzinfo=timezone.utc)

    def series(self, bounds, values, statistic='sum'):
        bounds = tuple((self.start + timedelta(minutes=a), self.start + timedelta(minutes=b)) for a, b in bounds)
        return Series('test', tuple(b for _, b in bounds), tuple(values), bounds, statistic, 'mm')

    def compare(self, forecast, observation):
        return compare_series(forecast, observation, timedelta(minutes=1), 'precipitation', (0.1, 1))

    def test_rain_combines_complete_fine_periods_and_computes_events(self):
        forecast = self.series([(0, 20), (20, 40), (40, 60), (60, 120)], [0.2, 0.3, 0.5, 0])
        observation = self.series([(0, 60), (60, 120)], [0, 2])
        result, = self.compare(forecast, observation)
        self.assertEqual(result.matched_times, 2)
        self.assertEqual(result.interval_seconds, 3600)
        self.assertEqual(result.metrics.mae, 1.5)
        event = dict(result.events)['降水≥1mm']
        self.assertEqual((event.hits, event.misses, event.false_alarms), (0, 1, 1))

    def test_mean_aggregation_weights_by_duration(self):
        forecast = self.series([(0, 10), (10, 60)], [0, 12], 'mean')
        observation = self.series([(0, 60)], [10], 'mean')
        result, = compare_series(forecast, observation, timedelta(0), 'surface_shortwave_down')
        self.assertEqual(result.metrics.mae, 0)

    def test_gaps_partial_windows_missing_values_and_no_splitting(self):
        observation = self.series([(0, 60)], [1])
        for bounds, values, matched in (
            ([(0, 20), (30, 60)], [0.5, 0.5], 0),
            ([(0, 90)], [1], 0),
            ([(0, 30), (30, 60)], [None, 1], 1),
            ([(0, 59)], [1], 0),  # 时段量不使用一分钟容差近似配对。
        ):
            with self.subTest(bounds=bounds):
                result, = self.compare(self.series(bounds, values), observation)
                self.assertEqual(result.matched_times, matched)
                self.assertEqual(result.metrics.sample_count, 0)

    def test_overlapping_forecasts_only_allow_exact_periods(self):
        forecast = self.series([(-10, 10), (0, 20), (20, 40), (40, 60)], [1, 1, 1, 1])
        result, = self.compare(forecast, self.series([(0, 60)], [3]))
        self.assertEqual(result.matched_times, 0)
        result, = self.compare(forecast, self.series([(0, 20)], [1]))
        self.assertEqual(result.metrics.sample_count, 1)

    def test_forecast_periods_not_reused_for_rolling_observations(self):
        forecast = self.series([(0, 30), (30, 60), (60, 90)], [1, 1, 1])
        observation = self.series([(0, 60), (30, 90)], [2, 2])
        result, = self.compare(forecast, observation)
        self.assertEqual(result.metrics.sample_count, 1)

    def test_different_window_lengths_have_separate_csv_rows(self):
        forecast = self.series([(0, 30), (30, 60), (60, 90)], [1, 1, 1])
        observation = self.series([(0, 60), (60, 90)], [2, 1])
        results = self.compare(forecast, observation)
        self.assertEqual([result.interval_seconds for result in results], [3600, 1800])
        output = mock_open()
        with patch.object(Path, 'open', output):
            write_metrics('metrics.csv', results)
        rows = list(csv.DictReader(StringIO(''.join(call.args[0] for call in output().write.call_args_list))))
        self.assertEqual([float(row['时段长度(秒)']) for row in rows], [3600, 1800])
        self.assertIn('RMSE(mm)', rows[0])
        self.assertIn('降水≥0.1mm_漏报率', rows[0])

    def test_only_confirmed_wind_case_allows_instant_against_mean(self):
        instant = Series('instant', (self.start + timedelta(minutes=10),), (2,), unit='m/s')
        mean = Series('mean', instant.times, (1,), ((self.start, instant.times[0]),), 'mean', 'm/s')
        result, = compare_series(instant, mean, timedelta(0), 'wind_speed_10m')
        self.assertEqual(result.metrics.mae, 1)
        with self.assertRaisesRegex(ValueError, '时间含义不同'):
            compare_series(instant, mean, timedelta(0), 'temperature_2m')


if __name__ == '__main__':
    unittest.main()
