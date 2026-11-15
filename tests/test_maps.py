"""空间图的时段、单位、缺测和配置回归；使用内存场，不创建临时文件。"""

from datetime import datetime, timedelta, timezone
from dataclasses import replace
from io import BytesIO
from pathlib import Path
import unittest
from unittest.mock import patch

import numpy as np
from matplotlib.figure import Figure

from wrf_evaluate.config import ConfigError, load_config
from wrf_evaluate.cldas import CldasFileInfo, CldasField, CldasHeader
from wrf_evaluate.config import Config, PeriodConfig, SourceConfig
from wrf_evaluate.maps import _converted, _draw, _forecast_field, _latest_field, _window, plot_maps
from wrf_evaluate.variables import VariableConfig
from wrf_evaluate.wrf import WrfField


class MapTests(unittest.TestCase):
    def setUp(self):
        self.start = datetime(2024, 12, 24, tzinfo=timezone.utc)
        self.times = [self.start + timedelta(minutes=30 * i) for i in range(3)]
        self.frames = {time: (Path('wrf'), i) for i, time in enumerate(self.times)}
        self.lon, self.lat = np.meshgrid([86., 87.], [43., 44.])

    def read(self, values, units='mm', moving=False):
        def fields(path, names, index):
            return {
                names[0]: WrfField(np.ma.asarray(values[index]), units),
                'XLONG': WrfField(np.ma.asarray(self.lon + (index if moving else 0)), 'degrees'),
                'XLAT': WrfField(np.ma.asarray(self.lat), 'degrees'),
            }
        return patch('wrf_evaluate.maps.read_wrf_fields', side_effect=fields)

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
        self.assertIsNone(_forecast_field(self.frames, 'surface_shortwave_down', spec,
                                         self.start - timedelta(hours=1), self.times[-1]))

    def test_interval_sums_and_means_cover_exact_window(self):
        for field, spec, units, expected in (
            ('precipitation', VariableConfig('RAIN', 'mm', 'sum', timedelta(minutes=30), 'end'), 'mm', 5),
            ('temperature_2m', VariableConfig('T2', 'degC', 'mean', timedelta(minutes=30), 'end'), 'degC', 2.5),
        ):
            with self.read([np.full((2, 2), i) for i in (9, 2, 3)], units):
                result = _forecast_field(self.frames, field, spec, self.times[0], self.times[-1])
            np.testing.assert_allclose(result[2], expected)
            with self.read([np.full((2, 2), i) for i in (9, 2, 3)], units):
                self.assertIsNone(_forecast_field({self.times[-1]: self.frames[self.times[-1]]}, field, spec,
                                                 self.times[0], self.times[-1]))

    def test_unit_conversion_and_timestamp_window(self):
        values = np.ma.array([273.15, 275.15], mask=[False, True])
        converted = _converted('temperature_2m', values, 'K', VariableConfig('T2'))
        self.assertAlmostEqual(converted[0], 0)
        self.assertTrue(converted.mask[1])
        rain = VariableConfig('PRCP', 'mm/h', 'sum', timedelta(hours=2), 'end')
        np.testing.assert_allclose(_converted('precipitation', np.ma.array([2]), 'mm/h', rain), [4])
        self.assertEqual(_window(self.start, rain), (self.start - timedelta(hours=2), self.start))

    def test_map_selects_latest_run_covering_entire_eligible_window(self):
        old, new = self.start - timedelta(hours=6), self.start
        source = SourceConfig('wrf', 'wrf', Path('wrf'), ('*',), 'UTC', True, min_lead_hours=1)
        spec = VariableConfig('RAINNC', time_kind='cumulative')
        runs = {old: self.frames, new: {self.times[-1]: self.frames[self.times[-1]]}}
        with self.read([np.full((2, 2), value) for value in (100, 102, 105)]):
            init, result = _latest_field(runs, source, 'precipitation', spec,
                                         self.start, self.times[-1], lambda message: None)
        self.assertEqual(init, old)
        np.testing.assert_allclose(result[2], 5)
        with self.read([np.full((2, 2), value) for value in (100, 102, 105)]):
            init, _ = _latest_field(runs, replace(source, min_lead_hours=0), 'precipitation', spec,
                                    self.start, self.times[-1], lambda message: None)
        self.assertEqual(init, old)  # 新批次缺少区间前值，也不能拼接旧批次。

    def test_panels_share_color_scale_and_render_png_in_memory(self):
        forecast = self.lon, self.lat, np.ma.array([[0, 1], [2, 3]])
        observation = self.lon[0], self.lat[:, 0], np.ma.array([[4, 5], [6, 7]])
        output = BytesIO()
        original = Figure.savefig

        def save(figure, *args, **kwargs):
            self.assertEqual([ax.get_title() for ax in figure.axes[:2]], ['WRF', 'cldas'])
            self.assertEqual([ax.collections[0].get_clim() for ax in figure.axes[:2]], [(0, 7), (0, 7)])
            return original(figure, *args, **kwargs)

        with patch.object(Figure, 'savefig', save):
            _draw(output, forecast, observation, (86, 87, 43, 44), '2m气温空间对比', 'WRF', '2m气温', '℃')
        self.assertEqual(output.getvalue()[:8], b'\x89PNG\r\n\x1a\n')

    def test_map_interval_defaults_and_validation(self):
        text = b'[sources.wrf]\nkind="wrf"\nroot="raw"\npatterns=["*"]\ntimezone="UTC"\n'
        with patch.object(Path, 'is_dir', return_value=True), patch.object(Path, 'exists', return_value=False):
            for setting, hours in ((b'', 6), (b'\n[maps]\ninterval="2h"\n', 2)):
                with patch.object(Path, 'read_bytes', return_value=text + setting):
                    self.assertEqual(load_config('config.toml').map_interval, timedelta(hours=hours))
            with patch.object(Path, 'read_bytes', return_value=text + b'\n[maps]\ninterval="0h"'):
                with self.assertRaises(ConfigError):
                    load_config('config.toml')

    def test_schedule_reuses_frame_index_and_skips_missing_cldas(self):
        wrf = SourceConfig('wrf', 'wrf', Path('wrf'), ('*',), 'UTC', True)
        cldas = SourceConfig('cldas', 'cldas', Path('cldas'), ('*',), 'UTC', True)
        times = tuple(self.start + timedelta(hours=h) for h in (0, 6, 12))
        config = Config((wrf, cldas), Path('output'), 'UTC', timedelta(minutes=1),
                        period=PeriodConfig(times[0], times[-1]))
        files = tuple(CldasFileInfo(Path(f'{i}.nc'), 'TMP', time, time, 'family')
                      for i, time in enumerate((times[0], times[-1])))
        index = {('cldas', 'family', 'TMP'): ((times[0], times[-1]), files)}
        frames = {'wrf': {None: {time: (Path('wrf'), i) for i, time in enumerate(times)}}}
        header = CldasHeader(Path('cldas'), 'TMP', times[0], self.lat[:, 0], self.lon[0], frozenset({'TAIR'}))
        messages = []
        with patch('wrf_evaluate.maps.read_cldas_header', return_value=header), \
             patch('wrf_evaluate.maps.read_cldas_field', return_value=CldasField(np.ma.ones((2, 2)) * 273.15, 'K')) as read, \
             patch('wrf_evaluate.maps._forecast_field', return_value=(self.lon, self.lat, np.ma.zeros((2, 2)))) as forecast, \
             patch('wrf_evaluate.maps._draw') as draw:
            plot_maps(config, [wrf, cldas], index, frames, Path('output'), messages.append)
        self.assertEqual(draw.call_count, 2)
        self.assertIs(forecast.call_args.args[0], frames['wrf'][None])
        self.assertEqual(read.call_args.kwargs['selection'], (slice(0, 2), slice(0, 2)))
        self.assertTrue(any('生成 2 张，跳过 1 组' in message for message in messages))


if __name__ == '__main__':
    unittest.main()
