"""验证变量语义、单位换算和累计差分，避免把不同物理量直接比较。"""

from datetime import datetime, timedelta, timezone
import unittest

from wrf_evaluate.config import PeriodConfig
from wrf_evaluate.series import Series, normalize_series
from wrf_evaluate.variables import DEFAULT_VARIABLES, VariableConfig, convert_value, parse_variables


class VariableTests(unittest.TestCase):
    def test_wrf_wind_uses_two_components_and_rejects_single_component(self):
        spec = DEFAULT_VARIABLES['wrf']['wind_speed_10m']
        self.assertEqual(spec.native_variables, ('U10', 'V10'))
        self.assertEqual(spec.time_kind, 'instant')
        for value in ('U10', 'V10', 'U', 'V'):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, '风分量'):
                parse_variables({'wind_speed_10m': {'variable': value}}, 'wrf')
        custom = dict(parse_variables({
            'wind_speed_10m': {'components': ['U10_EARTH', 'V10_EARTH']},
        }, 'wrf'))['wind_speed_10m']
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
            'wind_speed_10m': {'variable': '风速', 'time_kind': 'instant'},
        }, 'regional'))['wind_speed_10m']
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
        for bounds in (((time(0), time(60)), (time(0), time(90))),
                       ((time(0), time(90)), (time(30), time(60)))):
            with self.subTest(bounds=bounds), self.assertRaises(ValueError):
                Series('test', (time(60), time(90)), (1, 2), bounds, 'sum', 'mm')


if __name__ == '__main__':
    unittest.main()
