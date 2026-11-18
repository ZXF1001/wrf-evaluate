"""验证变量语义、单位换算和累计差分，避免把不同物理量直接比较。"""

from datetime import datetime, timedelta, timezone
import unittest

import numpy as np

from wrf_evaluate.config import PeriodConfig
from wrf_evaluate.series import Series, hourly_precipitation, normalize_series
from wrf_evaluate.variables import DEFAULT_VARIABLES, VariableConfig, convert_value, parse_variables, relative_humidity


class VariableTests(unittest.TestCase):
    def test_humidity_inputs_are_explicit_and_direct_mapping_overrides_defaults(self):
        spec = dict(parse_variables({'relative_humidity_2m': {'inputs': {
            'pressure': {'variable': 'P', 'product': 'PRS', 'units': 'hPa'},
            'specific_humidity': {'variable': 'Q', 'product': 'SHU', 'units': 'g/kg'},
            'temperature': {'variable': 'T', 'product': 'TMP', 'units': 'degC'},
        }}}, 'cldas'))['relative_humidity_2m']
        self.assertEqual(spec.source_inputs[0][0], 'specific_humidity')
        self.assertEqual(set(spec.native_variables), {'P', 'Q', 'T'})
        direct = dict(parse_variables({'relative_humidity_2m': {
            'variable': 'RH', 'product': 'RHU', 'units': '%',
        }}, 'cldas'))['relative_humidity_2m']
        self.assertFalse(direct.inputs)
        self.assertEqual(direct.source_inputs[0][1].variable, 'RH')
        for changes in (
            {'inputs': {'temperature': {'variable': 'T2'}}},
            {'time_kind': 'mean', 'interval': '1h', 'timestamp': 'end'},
            {'units': 'kg/kg'},
            {'inputs': {'temperature': {'variable': 'T', 'units': 'bad'},
                        'pressure': {'variable': 'P'}, 'mixing_ratio': {'variable': 'Q'}}},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                parse_variables({'relative_humidity_2m': changes}, 'wrf')

    def test_humidity_specific_and_mixing_ratio_agree_for_known_vapor_pressure(self):
        # At 0 C, saturation vapor pressure is 611.2 Pa; use half of it.
        pressure, vapor = 90000.0, 305.6
        ratio = 0.622 * vapor / (pressure - vapor)
        specific = ratio / (1 + ratio)
        wrf = DEFAULT_VARIABLES['wrf']['relative_humidity_2m']
        cldas = DEFAULT_VARIABLES['cldas']['relative_humidity_2m']
        a = relative_humidity({'temperature': 273.15, 'pressure': pressure, 'mixing_ratio': ratio},
                              {'temperature': 'K', 'pressure': 'Pa', 'mixing_ratio': 'kg kg-1'}, wrf)
        b = relative_humidity({'temperature': 0, 'pressure': 900, 'specific_humidity': specific * 1000},
                              {'temperature': 'degC', 'pressure': 'hPa', 'specific_humidity': 'g/kg'}, cldas)
        self.assertAlmostEqual(a, 50)
        self.assertAlmostEqual(b, 50)

    def test_humidity_preserves_masks_and_limits_result_to_zero_to_hundred(self):
        spec = DEFAULT_VARIABLES['wrf']['relative_humidity_2m']
        units = {'temperature': 'K', 'pressure': 'Pa', 'mixing_ratio': 'kg/kg'}
        values = {'temperature': 273.15, 'pressure': 90000, 'mixing_ratio': 0.01}
        self.assertEqual(relative_humidity(values, units, spec), 100)
        self.assertEqual(relative_humidity(values | {'mixing_ratio': 0}, units, spec), 0)
        self.assertIsNone(relative_humidity(values | {'mixing_ratio': None}, units, spec))
        self.assertIsNone(relative_humidity(values | {'pressure': float('nan')}, units, spec))
        result = relative_humidity(values | {
            'temperature': np.ma.array([273.15, 273.15, np.nan], mask=[0, 1, 0]),
        }, units, spec)
        self.assertEqual(result.mask.tolist(), [False, True, True])
        for changes in ({'pressure': 0}, {'mixing_ratio': -0.001}, {'temperature': 0}):
            with self.subTest(changes=changes), self.assertWarnsRegex(RuntimeWarning, '已保留为缺测'):
                self.assertIsNone(relative_humidity(values | changes, units, spec))
        with self.assertWarns(RuntimeWarning):
            result = relative_humidity(values | {'pressure': np.zeros((2, 2))}, units, spec)
        self.assertEqual(result.count(), 0)
        self.assertEqual(convert_value('relative_humidity_2m', 120, '%', 'instant'), 100)
        self.assertEqual(convert_value('relative_humidity_2m', -5, '%', 'instant'), 0)

    def test_humidity_combines_input_masks_before_checking_invalid_values(self):
        spec = DEFAULT_VARIABLES['wrf']['relative_humidity_2m']
        units = {'temperature': 'K', 'pressure': 'Pa', 'mixing_ratio': 'kg/kg'}
        ratio = 0.622 * 305.6 / (90000 - 305.6)
        values = {'temperature': np.ma.array([273.15] * 4, mask=[0, 1, 0, 0]),
                  'pressure': np.ma.array([90000, 0, 90000, 0], mask=[0, 0, 1, 0]),
                  'mixing_ratio': np.ma.array([ratio, -999, -1, ratio], mask=[0, 0, 0, 1])}
        originals = {key: value.copy() for key, value in values.items()}
        messages = []
        result = relative_humidity(values, units, spec, messages.append)
        self.assertAlmostEqual(result[0], 50)
        np.testing.assert_array_equal(result.mask, [False, True, True, True])
        self.assertEqual(messages, [])
        for key, value in values.items():
            np.testing.assert_array_equal(value.data, originals[key].data)
            np.testing.assert_array_equal(value.mask, originals[key].mask)

    def test_humidity_invalid_cells_are_missing_with_converted_value_diagnostics(self):
        spec = DEFAULT_VARIABLES['cldas']['relative_humidity_2m']
        units = {'temperature': 'degC', 'pressure': 'hPa', 'specific_humidity': 'g/kg'}
        values = {'temperature': np.array([0, 0, 0, 0, 0, 0, np.nan]),
                  'pressure': np.array([900, 900, 900, 900, 0, 900, 900]),
                  'specific_humidity': np.array([1, -0.001, 1000, 1001, 1, 0, 1])}
        messages = []
        result = relative_humidity(values, units, spec, messages.append)
        np.testing.assert_array_equal(result.mask, [False, True, True, True, True, False, True])
        self.assertGreater(result[0], 0)
        self.assertEqual(result[5], 0)
        self.assertEqual(len(messages), 1)
        for text in ('4 个位置', 'PAIR', 'QAIR', '-1e-06', 'kg/kg', 'Pa'):
            self.assertIn(text, messages[0])
        with self.assertRaisesRegex(ValueError, '气压单位'):
            relative_humidity(values, units | {'pressure': 'bad'}, spec)

    def test_wrf_wind_uses_two_components_and_rejects_single_component(self):
        spec = DEFAULT_VARIABLES['wrf']['wind_speed']
        self.assertEqual(spec.native_variables, ('U10', 'V10'))
        self.assertEqual(spec.time_kind, 'instant')
        for value in ('U10', 'V10', 'U', 'V'):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, '风分量'):
                parse_variables({'wind_speed': {'variable': value}}, 'wrf')
        custom = dict(parse_variables({
            'wind_speed': {'components': ['U10_EARTH', 'V10_EARTH']},
        }, 'wrf'))['wind_speed']
        self.assertEqual(custom.native_variables, ('U10_EARTH', 'V10_EARTH'))

    def test_mapping_is_one_native_variable_without_implicit_components(self):
        spec = dict(parse_variables({
            'precipitation': {'variable': 'RAINC', 'time_kind': 'cumulative'},
        }, 'wrf'))['precipitation']
        self.assertEqual(spec.variable, 'RAINC')
        for options in (
            {'variable': ['RAINC', 'RAINNC']},
            {'variable': 'RAINC', 'combine': 'sum'},
        ):
            with self.subTest(options=options), self.assertRaises(ValueError):
                parse_variables({'precipitation': options}, 'wrf')

    def test_mean_override_discards_inherited_interval_when_changed_to_instant(self):
        spec = dict(parse_variables({
            'wind_speed': {'variable': '风速', 'time_kind': 'instant'},
        }, 'station'))['wind_speed']
        self.assertIsNone(spec.interval)
        self.assertIsNone(spec.timestamp)

    def test_period_metadata_is_explicit_and_duration_is_strict(self):
        good = {'variable': 'PRCP', 'time_kind': 'sum', 'interval': '1h', 'timestamp': 'end', 'product': 'PRE'}
        spec = dict(parse_variables({'precipitation': good}, 'cldas'))['precipitation']
        self.assertEqual(spec.interval, timedelta(hours=1))
        self.assertEqual(spec.product, 'PRE')
        cases = [good | {'interval': value} for value in ('0h', '-1h', '1.5h', 'hour', 60)]
        cases += [good | {'time_kind': 'interval_sum'}, good | {'timestamp': 'middle'},
                  good | {'time_kind': 'cumulative'}, {'variable': 'PRCP', 'time_kind': 'sum'}]
        for options in cases:
            with self.subTest(options=options), self.assertRaises(ValueError):
                parse_variables({'precipitation': options}, 'cldas')

    def test_safe_units_do_not_infer_specific_humidity_or_rain_rate(self):
        self.assertAlmostEqual(convert_value('relative_humidity_2m', 0.5, '1', 'instant'), 50)
        self.assertAlmostEqual(convert_value('precipitation', 0.001, 'm', 'sum'), 1)
        for field, units, kind in (
            ('relative_humidity_2m', 'kg/kg', 'instant'),
            ('precipitation', 'mm/h', 'sum'),
            ('surface_shortwave_down', 'W/m2', 'sum'),
            ('surface_longwave_down', 'J/m2', 'instant'),
        ):
            with self.subTest(field=field, units=units), self.assertRaises(ValueError):
                convert_value(field, 1, units, kind)


class SeriesTests(unittest.TestCase):
    def setUp(self):
        self.start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        self.period = PeriodConfig(self.start, self.start + timedelta(hours=3))

    def normalize(self, field, spec, minutes, values, units):
        records = [(self.start + timedelta(minutes=m), value, units, f'row {i}')
                   for i, (m, value) in enumerate(zip(minutes, values))]
        return normalize_series('test', field, spec, records, self.period)

    def test_temperature_uses_units_override_and_keeps_native_times(self):
        series = self.normalize('temperature_2m', VariableConfig('TEMP', 'K'),
                                [0, 0.5, 1], [273.15, None, 274.15], 'unknown')
        self.assertEqual(series.values, (0, None, 1))
        self.assertEqual(series.times[1] - series.times[0], timedelta(seconds=30))

    def test_cumulative_rain_differences_actual_irregular_intervals(self):
        series = self.normalize('precipitation', VariableConfig('RAINC', time_kind='cumulative'),
                                [0, 5, 20, 60], [10, 11, 13, 14], 'mm')
        self.assertEqual(series.values, (1, 2, 1))
        self.assertEqual(series.statistic, 'sum')
        self.assertEqual([(b - a).total_seconds() for a, b in series.bounds], [300, 900, 2400])

    def test_period_average_rain_rate_uses_configured_duration(self):
        spec = VariableConfig('PRCP', time_kind='sum', interval=timedelta(minutes=30), timestamp='end')
        series = self.normalize('precipitation', spec, [30, 60], [2, 4], 'mm/h')
        self.assertEqual(series.values, (1, 2))
        self.assertEqual(series.unit, 'mm')

    def test_cumulative_missing_does_not_bridge_and_reset_is_not_silently_fixed(self):
        spec = VariableConfig('RAIN', time_kind='cumulative')
        series = self.normalize('precipitation', spec, [0, 10, 20, 30, 40], [0, None, 2, 4, 5], 'mm')
        self.assertEqual(series.values, (None, None, 2, 1))
        with self.assertRaisesRegex(ValueError, 'row 2.*负累计差分'):
            self.normalize('precipitation', spec, [0, 10, 20], [1, 2, 0], 'mm')

    def test_sum_start_end_and_partial_period_boundaries(self):
        for timestamp, times in (('start', [0, 60, 120, 180]), ('end', [0, 60, 120, 180])):
            spec = VariableConfig('RAIN', 'mm', 'sum', timedelta(hours=1), timestamp)
            series = self.normalize('precipitation', spec, times, [1, 2, 3, 4], 'mm')
            self.assertEqual(series.values, (1, 2, 3) if timestamp == 'start' else (2, 3, 4))
            self.assertEqual(series.bounds[0], (self.start, self.start + timedelta(hours=1)))

    def test_radiation_energy_is_converted_to_average_flux_by_actual_seconds(self):
        spec = VariableConfig('ENERGY', time_kind='cumulative')
        series = self.normalize('surface_shortwave_down', spec, [0, 30, 90], [0, 180000, 900000], 'J m-2')
        self.assertEqual(series.values, (100, 200))
        self.assertEqual(series.unit, 'W/m²')
        self.assertEqual(series.statistic, 'mean')
        spec = VariableConfig('ENERGY', 'J/m2', 'sum', timedelta(hours=1), 'end')
        series = self.normalize('surface_longwave_down', spec, [60], [360000], 'J/m2')
        self.assertEqual(series.values, (100,))

    def test_duplicate_time_and_unknown_precipitation_semantics_fail(self):
        with self.assertRaisesRegex(ValueError, '重复记录.*row 0.*row 1'):
            self.normalize('temperature_2m', VariableConfig('T'), [0, 0], [1, 2], 'degC')
        with self.assertRaisesRegex(ValueError, 'time_kind'):
            self.normalize('precipitation', VariableConfig('RAIN', 'mm', None), [0], [0], 'mm')

    def test_period_bounds_must_be_ordered_and_unambiguous(self):
        time = lambda minutes: self.start + timedelta(minutes=minutes)
        for bounds in (((time(10), time(60)), (time(0), time(90))),
                       ((time(0), time(90)), (time(30), time(60)))):
            with self.subTest(bounds=bounds), self.assertRaises(ValueError):
                Series('test', (time(60), time(90)), (1, 2), bounds, 'sum', 'mm')

    def test_hourly_rain_aggregates_minutes_but_not_gaps_or_partial_hours(self):
        def curve(minutes, values):
            times = [self.start + timedelta(minutes=m) for m in minutes]
            return Series('wrf', tuple(times[1:]), tuple(values), tuple(zip(times, times[1:])), 'sum', 'mm')
        result = hourly_precipitation(curve([0, 20, 40, 60, 90, 120, 130], [1, 2, 3, None, 5, 99]))
        self.assertEqual(result.values, (6, None))
        self.assertEqual(result.label, 'wrf（逐小时雨量）')
        self.assertTrue(all(b - a == timedelta(hours=1) for a, b in result.bounds))
        self.assertIsNone(hourly_precipitation(curve([5, 30, 60], [1, 2])))
        gap = curve([0, 20, 40, 60], [1, 2, 3])
        gap = Series('wrf', (gap.times[0], gap.times[2]), (1, 3),
                     (gap.bounds[0], gap.bounds[2]), 'sum', 'mm')
        self.assertIsNone(hourly_precipitation(gap))

    def test_hour_to_date_rain_preserves_values_and_compares_complete_hours(self):
        from wrf_evaluate.evaluation import compare_series

        spec = dict(parse_variables({'precipitation': {'variable': '小时雨量', 'units': 'mm'}}, 'station'))['precipitation']
        curve = self.normalize('precipitation', spec, [5, 30, 60, 65, 120], [0.1, 0.8, 1.3, 0.2, 1], 'mm')
        self.assertEqual(curve.values, (0.1, 0.8, 1.3, 0.2, 1))
        self.assertEqual([(b - a).total_seconds() for a, b in curve.bounds], [300, 1800, 3600, 300, 3600])
        forecast = self.normalize('precipitation', VariableConfig('RAIN', 'mm', 'cumulative'),
                                  [0, 60, 120], [0, 1.5, 2.5], 'mm')
        result, = compare_series(forecast, curve, timedelta(0), 'precipitation')
        self.assertEqual(result.interval_seconds, 3600)
        self.assertEqual(result.metrics.sample_count, 2)
        self.assertAlmostEqual(result.metrics.mae, 0.1)

    def test_hour_to_date_window_is_explicit_and_cannot_be_used_as_rain_rate(self):
        for options in (
            {'time_kind': 'raw'},
            {'window': 'unknown'},
            {'window': 'hour_to_date', 'interval': '30min'},
            {'window': 'hour_to_date', 'timestamp': 'start'},
            {'window': 'hour_to_date', 'units': 'mm/h'},
        ):
            with self.subTest(options=options), self.assertRaises(ValueError):
                parse_variables({'precipitation': options}, 'station')
        spec = dict(parse_variables({'precipitation': {
            'time_kind': 'sum', 'interval': '1h', 'timestamp': 'end',
        }}, 'station'))['precipitation']
        self.assertEqual(spec.window, 'rolling')


if __name__ == '__main__':
    unittest.main()
