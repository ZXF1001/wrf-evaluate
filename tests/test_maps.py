"""空间图的时段、单位、缺测和配置回归；使用内存场，不创建临时文件。"""
from datetime import datetime, timedelta, timezone
from dataclasses import replace
from io import BytesIO
from pathlib import Path
import unittest
from unittest.mock import patch
import numpy as np
from wrf_evaluate.cldas import CldasFileInfo, CldasField, CldasHeader
from wrf_evaluate.config import Config, MapOptions, PeriodConfig, PlotTask, SourceConfig
from wrf_evaluate.maps import _MapFigure, _converted, _crop_axis, _forecast_field, _latest_field, _observation_field, _window, plot_maps, required_wrf_windows
from wrf_evaluate.plotting import plot_style
from matplotlib import rc_context
from wrf_evaluate.variables import DEFAULT_VARIABLES, VariableConfig
from wrf_evaluate.wrf import WrfField

class MapTests(unittest.TestCase):

    def setUp(self):
        self.start = datetime(2024, 12, 24, tzinfo=timezone.utc)
        self.times = [self.start + timedelta(minutes=30 * i) for i in range(3)]
        self.frames = {time: (Path('wrf'), i) for i, time in enumerate(self.times)}
        self.lon, self.lat = np.meshgrid([86.0, 87.0], [43.0, 44.0])

    def read(self, values, units='mm', moving=False):

        def fields(path, names, index):
            return {names[0]: WrfField(np.ma.asarray(values[index]), units), 'XLONG': WrfField(np.ma.asarray(self.lon + (index if moving else 0)), 'degrees'), 'XLAT': WrfField(np.ma.asarray(self.lat), 'degrees')}
        return patch('wrf_evaluate.maps.read_wrf_fields', side_effect=fields)

    def test_timeline_renders_three_columns_with_shared_absolute_and_symmetric_difference_colors(self):
        options = MapOptions(figsize=(9, 2.5), dpi=50)
        rendered = BytesIO()
        with rc_context(plot_style()):
            figure = _MapFigure(tuple(self.times[:2]), '2m气温', '℃', options)
            try:
                for time, value in zip(self.times, (1, 5)):
                    forecast = self.lon, self.lat, np.ma.ones((2, 2)) * value
                    observation = self.lon[0], self.lat[:, 0], np.ma.ones((2, 2)) * 2
                    figure.add(time, forecast, observation, (86, 87, 43, 44), str(time), 'WRF', 'cldas')
                self.assertTrue(figure.save(rendered))
                self.assertEqual(len(figure.figure.axes), 8) # 2×3面板，另加两条色标。
                self.assertEqual((figure.absolute_norm.vmin, figure.absolute_norm.vmax), (1, 5))
                self.assertEqual((figure.difference_norm.vmin, figure.difference_norm.vmax), (-3, 3))
                for row, error in zip(figure.axes, (-1, 3)):
                    for ax in row[:2]:
                        self.assertIs(ax.collections[0].norm, figure.absolute_norm)
                    self.assertIs(row[2].collections[0].norm, figure.difference_norm)
                    np.testing.assert_allclose(row[2].collections[0].get_array(), error)
                self.assertEqual(rendered.getvalue()[:8], b'\x89PNG\r\n\x1a\n')
            finally:
                figure.close()

    def test_map_tasks_share_reads_and_separate_figures_close_after_each_time(self):
        wrf = SourceConfig('wrf', 'wrf', Path('wrf'), ('*',), 'UTC', True)
        cldas = SourceConfig('cldas', 'cldas', Path('cldas'), ('*',), 'UTC', True)
        times = (self.start, self.start + timedelta(hours=6))
        info = tuple(CldasFileInfo(Path(str(i)), 'TMP', time, time, 'family') for i, time in enumerate(times))
        index = {('cldas', 'family', 'TMP'): (times, info)}
        config = Config((wrf, cldas), Path('output'), 'UTC', period=PeriodConfig(*times), plots=(
            PlotTask('temperature_2m', 'map', MapOptions()),
            PlotTask('temperature_2m', 'map', MapOptions(layout='separate')),
        ))
        events = []
        header = CldasHeader(Path('0'), 'TMP', self.start, self.lat[:, 0], self.lon[0], frozenset())
        def selected(*args):
            events.append('read')
            return None, (self.lon, self.lat, np.ma.zeros((2, 2)))
        with patch('wrf_evaluate.maps._latest_field', side_effect=selected) as forecast, \
                patch('wrf_evaluate.maps.CldasFieldReader') as reader, \
                patch('wrf_evaluate.maps._MapFigure') as figure:
            reader.return_value.header.return_value = header
            reader.return_value.read.return_value = CldasField(np.ma.ones((2, 2)) * 273.15, 'K')
            figure.return_value.save.side_effect = lambda path: events.append('save') or True
            figure.return_value.close.side_effect = lambda: events.append('close')
            plot_maps(config, [wrf, cldas], index, {'wrf': {}}, Path('output'), lambda message: None)
        self.assertEqual(forecast.call_count, 2)
        self.assertEqual(reader.return_value.read.call_count, 2)
        self.assertEqual(figure.call_count, 3) # 一张汇总图，两张单时次图。
        self.assertEqual(events, ['read', 'save', 'close', 'read', 'save', 'close', 'save', 'close'])

    def test_crop_retains_neighbors_for_nearest_sampling_in_both_axis_directions(self):
        for axis in (np.arange(5), np.arange(4, -1, -1)):
            np.testing.assert_array_equal(np.sort(axis[_crop_axis(axis, 1.2, 2.8)]), [1, 2, 3])
        with self.assertRaisesRegex(ValueError, '没有交集'):
            _crop_axis(np.arange(5), 6, 7)

    def test_wrf_humidity_map_uses_same_derived_field_and_mask(self):
        ratio = 0.622 * 305.6 / (90000 - 305.6)
        fields = {'T2': WrfField(np.ma.ones((2, 2)) * 273.15, 'K'), 'PSFC': WrfField(np.ma.ones((2, 2)) * 90000, 'Pa'), 'Q2': WrfField(np.ma.array([[ratio, 0.01], [ratio, 0]], mask=[[0, 0], [1, 0]]), 'kg/kg'), 'XLONG': WrfField(np.ma.asarray(self.lon), 'degrees'), 'XLAT': WrfField(np.ma.asarray(self.lat), 'degrees')}
        with patch('wrf_evaluate.maps.read_wrf_fields', return_value=fields) as read:
            _, _, result = _forecast_field(self.frames, 'relative_humidity_2m', DEFAULT_VARIABLES['wrf']['relative_humidity_2m'], self.start, self.start)
        self.assertEqual(set(read.call_args.args[1]), set(fields))
        self.assertAlmostEqual(result[0, 0], 50)
        self.assertEqual(result[0, 1], 100)
        self.assertTrue(result.mask[1, 0])
        self.assertEqual(result[1, 1], 0)
        fields['Q2'].values[1, 1] = -1e-08
        messages = []
        with patch('wrf_evaluate.maps.read_wrf_fields', return_value=fields):
            _, _, result = _forecast_field(self.frames, 'relative_humidity_2m', DEFAULT_VARIABLES['wrf']['relative_humidity_2m'], self.start, self.start, messages.append)
        self.assertAlmostEqual(result[0, 0], 50)
        self.assertTrue(result.mask[1, 1])
        self.assertIn('wrf Time[0]', messages[0])
        self.assertIn('Q2', messages[0])

    def test_cldas_humidity_crop_and_grid_validation(self):
        spec = DEFAULT_VARIABLES['cldas']['relative_humidity_2m']
        files = {role: CldasFileInfo(Path(item.product), item.product, self.start, self.start, 'family') for role, item in spec.inputs}
        header = CldasHeader(Path('SHU'), 'SHU', self.start, self.lat[:, 0], self.lon[0], frozenset())
        ratio = 0.622 * 305.6 / (90000 - 305.6)
        fields = {'QAIR': CldasField(np.ma.ones((2, 2)) * ratio / (1 + ratio), 'kg/kg'), 'TAIR': CldasField(np.ma.ones((2, 2)) * 273.15, 'K'), 'PAIR': CldasField(np.ma.ones((2, 2)) * 90000, 'Pa')}
        selection = (slice(0, 2), slice(0, 2))
        with patch('wrf_evaluate.maps.read_cldas_header', return_value=header), patch('wrf_evaluate.maps.read_cldas_field', side_effect=lambda path, name, **kw: fields[name]) as read:
            result = _observation_field(files, spec, 'relative_humidity_2m', header, selection, 'UTC')
        np.testing.assert_allclose(result, 50)
        self.assertEqual(read.call_count, 3)
        self.assertTrue(all((call.kwargs['selection'] == selection for call in read.call_args_list)))
        fields['QAIR'].values[0, 0] = 1
        messages = []
        with patch('wrf_evaluate.maps.read_cldas_header', return_value=header), patch('wrf_evaluate.maps.read_cldas_field', side_effect=lambda path, name, **kw: fields[name]):
            result = _observation_field(files, spec, 'relative_humidity_2m', header, selection, 'UTC', messages.append)
        self.assertTrue(result.mask[0, 0])
        self.assertAlmostEqual(result[1, 1], 50)
        for text in ('CLDAS', self.start.isoformat(), 'SHU', 'QAIR', 'kg/kg'):
            self.assertIn(text, messages[0])
        with patch('wrf_evaluate.maps.read_cldas_header', return_value=replace(header, longitudes=self.lon[0] + 0.01)), patch('wrf_evaluate.maps.read_cldas_field', return_value=fields['QAIR']):
            with self.assertRaisesRegex(ValueError, '网格不一致'):
                _observation_field(files, spec, 'relative_humidity_2m', header, selection, 'UTC')

    def test_map_file_windows_follow_rain_period_and_ignore_unscheduled_times(self):
        wrf = SourceConfig('wrf', 'wrf', Path('wrf'), ('*',), 'UTC', True, variable_mapping=(('precipitation', VariableConfig('RAINNC', time_kind='cumulative')),))
        cldas = SourceConfig('cldas', 'cldas', Path('cldas'), ('*',), 'UTC', True, variable_mapping=(('precipitation', VariableConfig('PRCP', 'mm', 'sum', timedelta(hours=1), 'end', 'PRE')),))
        bjc = timezone(timedelta(hours=8))
        config = Config((wrf, cldas), Path('output'), 'Asia/Shanghai', period=PeriodConfig(self.start.astimezone(bjc), (self.start + timedelta(hours=12)).astimezone(bjc)), plots=tuple((PlotTask(e, 'map', MapOptions()) for e in ('precipitation',))))
        times = tuple((self.start + timedelta(hours=h) for h in (0, 1, 6, 12)))
        files = tuple((CldasFileInfo(Path(str(i)), 'PRE', t, t, 'family') for i, t in enumerate(times)))
        index = {('cldas', 'family', 'PRE'): (times, files)}
        expected = tuple(((t - timedelta(hours=1), t) for t in times[2:]))
        self.assertEqual(required_wrf_windows(config, wrf, [cldas], index), expected)
        self.assertEqual(required_wrf_windows(config, wrf, [cldas], {}), ())

    def test_cumulative_rain_sums_native_increments_and_keeps_missing(self):
        spec = VariableConfig('RAINNC', time_kind='cumulative')
        values = [np.ma.array([[i, i], [i, i]], mask=[[False, i == 1], [False, False]]) for i in (10, 11, 13)]
        values[1].mask[0, 1] = True
        with self.read(values) as read:
            lon, lat, value = _forecast_field(self.frames, 'precipitation', spec, self.times[0], self.times[-1])
        self.assertEqual(read.call_count, 3)
        self.assertEqual(value[0, 0], 3)
        self.assertTrue(value.mask[0, 1])

    def test_reset_and_moving_grid_fail_instead_of_hiding_bad_difference(self):
        spec = VariableConfig('RAINNC', time_kind='cumulative')
        for values, moving, message in (([10, 1, 11], False, '负累计'), ([10, 11, 12], True, '网格移动')):
            with self.read([np.full((2, 2), i) for i in values], moving=moving):
                with self.assertRaisesRegex(ValueError, message):
                    _forecast_field(self.frames, 'precipitation', spec, self.times[0], self.times[-1])

    def test_incomplete_interval_is_not_drawn_and_radiation_is_mean_flux(self):
        spec = VariableConfig('ENERGY', time_kind='cumulative')
        with self.read([np.full((2, 2), i) for i in (0, 3600, 7200)], 'J/m2'):
            result = _forecast_field(self.frames, 'surface_shortwave_down', spec, self.times[0], self.times[-1])
        np.testing.assert_allclose(result[2], 2)
        self.assertIsNone(_forecast_field(self.frames, 'surface_shortwave_down', spec, self.start - timedelta(hours=1), self.times[-1]))

    def test_interval_sums_and_means_cover_exact_window(self):
        for field, spec, units, expected in (('precipitation', VariableConfig('RAIN', 'mm', 'sum', timedelta(minutes=30), 'end'), 'mm', 5), ('temperature_2m', VariableConfig('T2', 'degC', 'mean', timedelta(minutes=30), 'end'), 'degC', 2.5)):
            with self.read([np.full((2, 2), i) for i in (9, 2, 3)], units):
                result = _forecast_field(self.frames, field, spec, self.times[0], self.times[-1])
            np.testing.assert_allclose(result[2], expected)
            with self.read([np.full((2, 2), i) for i in (9, 2, 3)], units):
                self.assertIsNone(_forecast_field({self.times[-1]: self.frames[self.times[-1]]}, field, spec, self.times[0], self.times[-1]))

    def test_unit_conversion_and_timestamp_window(self):
        values = np.ma.array([273.15, 275.15], mask=[False, True])
        converted = _converted('temperature_2m', values, 'K', VariableConfig('T2'))
        self.assertAlmostEqual(converted[0], 0)
        self.assertTrue(converted.mask[1])
        rain = VariableConfig('PRCP', 'mm/h', 'sum', timedelta(hours=2), 'end')
        np.testing.assert_allclose(_converted('precipitation', np.ma.array([2]), 'mm/h', rain), [4])
        self.assertEqual(_window(self.start, rain), (self.start - timedelta(hours=2), self.start))

    def test_direct_humidity_map_values_are_bounded(self):
        values = _converted('relative_humidity_2m', np.ma.array([[-5.0, 50.0, 120.0]]), '%', VariableConfig('RH', '%'))
        np.testing.assert_array_equal(values, [[0.0, 50.0, 100.0]])

    def test_map_selects_latest_run_covering_entire_eligible_window(self):
        old, new = (self.start - timedelta(hours=6), self.start)
        source = SourceConfig('wrf', 'wrf', Path('wrf'), ('*',), 'UTC', True, min_lead_hours=1)
        spec = VariableConfig('RAINNC', time_kind='cumulative')
        runs = {old: self.frames, new: {self.times[-1]: self.frames[self.times[-1]]}}
        with self.read([np.full((2, 2), value) for value in (100, 102, 105)]):
            init, result = _latest_field(runs, source, 'precipitation', spec, self.start, self.times[-1], lambda message: None)
        self.assertEqual(init, old)
        np.testing.assert_allclose(result[2], 5)
        with self.read([np.full((2, 2), value) for value in (100, 102, 105)]):
            init, _ = _latest_field(runs, replace(source, min_lead_hours=0), 'precipitation', spec, self.start, self.times[-1], lambda message: None)
        self.assertEqual(init, old)
if __name__ == '__main__':
    unittest.main()
