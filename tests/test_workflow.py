"""验证配置到曲线的连接；文件读取器与画布各有独立的真实文件测试。"""

from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
import unittest
from unittest.mock import patch

from wrf_evaluate.cli import main
from wrf_evaluate.cldas import CldasPointSample, parse_cldas_filename
from wrf_evaluate.config import Config, ConfigError, PeriodConfig, PointConfig, SourceConfig, load_config
from wrf_evaluate.station import StationObservation
from wrf_evaluate.workflow import _wrf_series, plot_comparisons
from wrf_evaluate.forecasts import eligible_series, forecast_init_time, select_latest
from wrf_evaluate.evaluation import compare_forecast
from wrf_evaluate.series import Series
from wrf_evaluate.variables import VariableConfig
from wrf_evaluate.wrf import WrfHeader, WrfPointFrame


class ComparisonWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.time = datetime(2024, 12, 24, tzinfo=timezone.utc)
        self.wrf_path = Path('wrf/sample')
        self.excel = Path('stations/selected.xls')
        self.cldas_path = Path('cldas/Z_NAFP_C_BABJ_20241226000900_P_CLDAS_NRT_ASI_0P0625_HOR-TMP-2024122400.nc')
        self.wrf = SourceConfig('wrf', 'wrf', Path('wrf'), ('*',), 'UTC', True)
        self.cldas = SourceConfig('cldas', 'cldas', Path('cldas'), ('*.nc',), 'UTC', True)
        self.station = SourceConfig('station', 'regional', Path('stations'), (), 'UTC', True, files=(self.excel,))
        self.point = PointConfig('test', 87.5, 43.5, 'Y5012')
        self.config = Config(
            (self.wrf, self.cldas, self.station), Path('unused-output'), 'Asia/Shanghai',
            timedelta(seconds=60), points=(self.point,),
            period=PeriodConfig(self.time, self.time + timedelta(hours=1)),
            output_name='test-run', config_bytes=b'[output]\nname = "test-run"\n',
        )
        self.files = {'wrf': [self.wrf_path], 'cldas': [self.cldas_path]}
        self.header = WrfHeader(self.wrf_path, (self.time, self.config.period.end), (), frozenset({'T2'}), None)
        self.frames = [
            WrfPointFrame(self.wrf_path, i, time, 0, 0, 1, {'T2': value}, {'T2': 'K'})
            for i, (time, value) in enumerate(zip(self.header.times, (273.15, 275.15)))
        ]
        self.rows = [self.row(minutes, value) for minutes, value in ((10, 1), (0, 0), (5, None), (-5, 99), (65, 99))]
        self.rows.append(replace(self.row(0, 99), station_id='other'))

        def files(source):
            return iter(source.files if source.kind == 'regional' else self.files[source.name])

        def sample(path, *args):
            info = parse_cldas_filename(path)
            return {name: tuple(CldasPointSample(path, info.time, None, 274.15, 'K') for _ in args[1]) for name in args[0]}

        self.start_patch('wrf_evaluate.workflow.iter_source_files', side_effect=files)
        self.start_patch('wrf_evaluate.matching.iter_source_files', side_effect=files)
        self.read_frames = self.start_patch('wrf_evaluate.workflow.iter_wrf_points_frames', side_effect=lambda *args, **kwargs: (tuple(frame for _ in args[3]) for frame in self.frames))
        self.read_station = self.start_patch('wrf_evaluate.workflow.iter_regional_observations', side_effect=lambda *args, **kwargs: (row for row in self.rows if row.station_id in kwargs['station_ids'] and kwargs['period'].start <= row.time <= kwargs['period'].end))
        self.read_cldas = self.start_patch('wrf_evaluate.workflow.read_cldas_points', side_effect=sample)
        self.draw = self.start_patch('wrf_evaluate.workflow.plot_series')
        self.maps = self.start_patch('wrf_evaluate.workflow.plot_maps')
        self.write_metrics = self.start_patch('wrf_evaluate.workflow.write_metrics', side_effect=lambda path, *args: path)
        self.run_directory = self.start_patch(
            'wrf_evaluate.workflow.create_run_directory', return_value=Path('unused-output/test-run_20261115_120000_000001'),
        )

    def start_patch(self, target, **kwargs):
        patcher = patch(target, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def row(self, minutes, value):
        return StationObservation(self.excel, minutes + 100, 'Y5012', self.time + timedelta(minutes=minutes), {'temperature_2m': value})

    def test_native_times_units_missing_values_and_shared_excel_read(self):
        second = replace(self.point, name='second', station_id='Y5013')
        self.rows.append(replace(self.row(0, -1), station_id='Y5013'))
        results = list(plot_comparisons(replace(self.config, points=(self.point, second))))
        self.assertEqual(self.read_station.call_args.kwargs['fields'], ('temperature_2m',))
        self.read_frames.assert_called_once()
        self.assertEqual(self.read_frames.call_args.args[3], ((87.5, 43.5), (87.5, 43.5)))
        self.assertEqual(len(results), 2)
        self.read_cldas.assert_called_once()
        self.assertEqual(len(self.read_cldas.call_args.args[2]), 2)
        wrf, cldas, station = results[0].series
        self.assertEqual(wrf.values, (0, 2))
        self.assertEqual(cldas.values, (1,))
        self.assertEqual(station.values, (0, None, 1))
        self.assertEqual(station.times, tuple(self.time + timedelta(minutes=m) for m in (0, 5, 10)))
        self.assertEqual(results[1].series[-1].values, (-1,))
        self.assertNotEqual(results[0].path, results[1].path)
        self.assertEqual(self.read_frames.call_args.kwargs['period'], self.config.period)
        self.assertEqual(self.draw.call_args.kwargs['period'], self.config.period)
        self.assertEqual(len(results[0].comparisons), 2)
        self.assertEqual(self.write_metrics.call_count, 2)
        self.assertEqual(results[0].metrics_path.suffix, '.csv')
        self.run_directory.assert_called_once_with(replace(self.config, points=(self.point, second)))
        self.assertTrue(all(result.path.parent == self.run_directory.return_value for result in results))
        self.assertEqual(results[0].series[1].label, 'cldas')

    def test_cldas_only_opens_latest_version_in_selected_period(self):
        older = Path(str(self.cldas_path).replace('20241226000900', '20241225000900'))
        outside = Path(str(self.cldas_path).replace('TMP-2024122400', 'TMP-2024122402'))
        self.files['cldas'] += [older, outside]
        list(plot_comparisons(self.config))
        self.read_cldas.assert_called_once_with(self.cldas_path, ('TAIR',), ((87.5, 43.5),), 'UTC')

    def test_source_variables_and_display_name_reach_readers_and_plot(self):
        wrf = replace(self.wrf, variable_mapping=(('temperature_2m', VariableConfig('CUSTOM_T2')),))
        cldas = replace(self.cldas, variable_mapping=(('temperature_2m', VariableConfig('CUSTOM_TAIR', product='TMP')),))
        station = replace(self.station, variable_mapping=(('temperature_2m', VariableConfig('温度℃', 'degC')),))
        config = replace(self.config, sources=(wrf, cldas, station),
                         display_names=(('temperature_2m', '自定义温度'),))
        self.frames = [replace(frame, values={'CUSTOM_T2': frame.values['T2']},
                               units={'CUSTOM_T2': 'K'}) for frame in self.frames]
        result, = plot_comparisons(config)
        self.assertEqual(self.read_frames.call_args.args[1], ('CUSTOM_T2',))
        self.read_cldas.assert_called_once_with(self.cldas_path, ('CUSTOM_TAIR',), ((87.5, 43.5),), 'UTC')
        self.assertEqual(self.read_station.call_args.kwargs['fields'], ('temperature_2m',))
        self.assertEqual(self.draw.call_args.kwargs['element_name'], '自定义温度')
        self.assertEqual(result.series[0].label, 'wrf')
        self.assertEqual(result.series[0].values, (0, 2))

    def test_multiple_elements_share_wrf_read_and_select_cldas_product(self):
        rain = VariableConfig('RAINC', time_kind='cumulative')
        wrf = replace(self.wrf, variable_mapping=(('precipitation', rain),))
        cldas = replace(self.cldas, variable_mapping=(('precipitation', VariableConfig(
            'PRCP', 'mm', 'sum', timedelta(hours=1), 'end', 'PRE',
        )),))
        rain_path = Path(str(self.cldas_path).replace('TMP-2024122400', 'PRE-2024122401'))
        self.files['cldas'].append(rain_path)
        self.frames = [replace(frame, values=frame.values | {'RAINC': 10 + i},
                               units=frame.units | {'RAINC': 'mm'}) for i, frame in enumerate(self.frames)]

        def sample(path, *args):
            info = parse_cldas_filename(path)
            return {args[0][0]: tuple(CldasPointSample(path, info.time, None,
                                    0.5 if info.product == 'PRE' else 274.15,
                                    'mm/h' if info.product == 'PRE' else 'K') for _ in args[1])}

        self.read_cldas.side_effect = sample
        config = replace(self.config, sources=(wrf, cldas),
                         elements=('temperature_2m', 'precipitation'), precipitation_thresholds_mm=(1,))
        temperature, rain_result = plot_comparisons(config)
        self.read_frames.assert_called_once()
        self.assertEqual(self.read_frames.call_args.args[1], ('T2', 'RAINC'))
        self.assertCountEqual([call.args[1] for call in self.read_cldas.call_args_list], [('TAIR',), ('PRCP',)])
        self.assertEqual(rain_result.series[0].values, (1,))
        self.assertEqual(rain_result.series[1].values, (0.5,))
        self.assertEqual(rain_result.series[1].label, 'cldas')
        self.assertEqual(rain_result.comparisons[0].metrics.mae, 0.5)
        self.assertEqual(rain_result.comparisons[0].interval_seconds, 3600)
        self.assertEqual(temperature.element, 'temperature_2m')
        self.run_directory.assert_called_once()
        self.read_station.assert_not_called()

    def test_unknown_station_rain_semantics_fail_before_any_data_read(self):
        wrf = replace(self.wrf, variable_mapping=(
            ('precipitation', VariableConfig('RAINNC', time_kind='cumulative')),
        ))
        config = replace(self.config, sources=(wrf, self.station), elements=('precipitation',))
        with self.assertRaisesRegex(ConfigError, 'station.variables.precipitation.*time_kind'):
            list(plot_comparisons(config))
        self.read_station.assert_not_called()

    def test_requested_station_fields_are_read_together(self):
        wrf = replace(self.wrf, variable_mapping=(
            ('relative_humidity_2m', VariableConfig('RH', '%')),
        ))
        self.frames = [replace(frame, values=frame.values | {'RH': 80}, units=frame.units | {'RH': '%'})
                       for frame in self.frames]
        self.rows = [replace(row, values=row.values | {'relative_humidity_2m': 70}) for row in self.rows]
        config = replace(self.config, sources=(wrf, self.station),
                         elements=('temperature_2m', 'relative_humidity_2m'))
        results = list(plot_comparisons(config))
        self.assertEqual(len(results), 2)
        self.read_station.assert_called_once()
        self.assertEqual(self.read_station.call_args.kwargs['fields'], config.elements)
        self.read_frames.assert_called_once()
        self.assertEqual(results[1].comparisons[0].metrics.bias, 10)

    def test_missing_station_or_wrong_period_is_not_silently_omitted(self):
        for rows in ([], [self.row(-5, 1)], [replace(self.row(0, 1), station_id='wrong')]):
            self.rows = rows
            with self.subTest(rows=rows), self.assertRaisesRegex(ValueError, 'station_id'):
                list(plot_comparisons(self.config))
        self.draw.assert_not_called()

    def test_duplicate_station_times_report_origin_before_plot(self):
        self.rows.append(self.row(0, 2))
        with self.assertRaisesRegex(ValueError, 'selected.xls.*第 100 行'):
            list(plot_comparisons(self.config))
        self.draw.assert_not_called()

    def test_unknown_temperature_units_fail_before_plot(self):
        self.frames[0] = replace(self.frames[0], units={'T2': 'unknown'})
        with self.assertRaisesRegex(ValueError, '温度单位'):
            list(plot_comparisons(self.config))
        self.draw.assert_not_called()

    def test_all_missing_temperature_source_fails_before_plot(self):
        self.rows = [self.row(0, None)]
        with self.assertRaisesRegex(ValueError, '全部缺测'):
            list(plot_comparisons(self.config))
        self.draw.assert_not_called()

    def test_all_missing_wrf_or_cldas_is_not_hidden_by_valid_station(self):
        config = replace(self.config, sources=(self.wrf, self.station))
        self.frames = [replace(frame, values={'T2': None}) for frame in self.frames]
        with self.assertRaisesRegex(ValueError, '点位 test: wrf.*全部缺测'):
            list(plot_comparisons(config))
        self.frames = [replace(frame, values={'T2': 273.15}) for frame in self.frames]
        self.read_cldas.side_effect = None
        self.read_cldas.return_value = {'TAIR': (CldasPointSample(self.cldas_path, self.time, None, None, 'K'),)}
        with self.assertRaisesRegex(ValueError, 'cldas.*全部缺测'):
            list(plot_comparisons(self.config))
        self.draw.assert_not_called()

    def test_missing_station_id_is_checked_before_reading_any_files(self):
        config = replace(self.config, sources=(self.wrf, self.station),
                         points=(self.point, replace(self.point, name='second', station_id=None)))
        with self.assertRaisesRegex(ConfigError, 'second.*station_id'):
            list(plot_comparisons(config))
        self.read_station.assert_not_called()
        self.draw.assert_not_called()

    def test_observation_failure_does_not_write_partial_figures(self):
        config = replace(self.config, points=(self.point, replace(self.point, name='second')))
        self.read_cldas.side_effect = [
            ValueError('CLDAS 文件读取失败'),
        ]
        output, errors = StringIO(), StringIO()
        with patch('wrf_evaluate.cli.load_config', return_value=config), redirect_stdout(output), redirect_stderr(errors):
            self.assertEqual(main(['plot']), 2)
        self.draw.assert_not_called()
        self.assertIn('CLDAS 文件读取失败', errors.getvalue())

    def test_missing_configuration_or_unsupported_source_fails_before_reading(self):
        national = replace(self.station, kind='national')
        for config in (
            replace(self.config, period=None), replace(self.config, points=()),
            replace(self.config, sources=(self.wrf,)), replace(self.config, sources=(self.cldas,)),
            replace(self.config, sources=(self.wrf, national)),
        ):
            with self.subTest(config=config), self.assertRaises(ConfigError):
                list(plot_comparisons(config))
        self.read_station.assert_not_called()
        self.read_frames.assert_not_called()

    def test_cli_prints_native_sample_counts(self):
        output = StringIO()
        with patch('wrf_evaluate.cli.load_config', return_value=self.config), redirect_stdout(output):
            self.assertEqual(main(['plot']), 0)
        self.assertIn('3 个原生时次，2 个有效值', output.getvalue())
        self.assertIn('temperature_2m_001_test_', output.getvalue())
        self.assertIn('指标:', output.getvalue())
        text = output.getvalue()
        self.assertIn('加载检验与绘图模块', text)
        self.assertIn('读取 WRF 来源', text)
        self.assertLess(text.index('读取 WRF 来源'), text.index('已生成 test:'))

    def test_latest_eligible_run_and_directory_precedence(self):
        old = Path('wrf/2024122323/sample')
        new = Path('wrf/2024122400/sample')
        self.files['wrf'] = [new, old]
        def read(path, *args, **kwargs):
            return iter((tuple(replace(frame, path=path, model_start_time=self.time - timedelta(days=1),
                                       values={'T2': 283.15 if path == new else 273.15})
                               for _ in args[2]) for frame in self.frames))
        self.read_frames.side_effect = read
        source = replace(self.wrf, min_lead_hours=1)
        result = _wrf_series(source, replace(self.config, sources=(source,)))['test', 'temperature_2m']
        self.assertEqual(result.series.values, (0, 10))
        self.assertEqual(len(result.runs), 2)
        self.assertEqual(self.read_frames.call_args.kwargs['min_lead_hours'], 1)

    def test_cumulative_difference_stays_inside_run_and_lead_window(self):
        old = Path('wrf/2024122322/sample')
        new = Path('wrf/2024122323/sample')
        self.files['wrf'] = [old, new]
        def read(path, *args, **kwargs):
            values = (100, 102, 104) if path == old else (1, 4, 8)
            return iter((WrfPointFrame(path, i, self.time + timedelta(hours=i), 0, 0, 0,
                                       {'RAINNC': value}, {'RAINNC': 'mm'}),)
                        for i, value in enumerate(values))
        self.read_frames.side_effect = read
        source = replace(self.wrf, min_lead_hours=2, variable_mapping=(
            ('precipitation', VariableConfig('RAINNC', time_kind='cumulative')),))
        config = replace(self.config, sources=(source,), elements=('precipitation',),
                         period=PeriodConfig(self.time, self.time + timedelta(hours=2)))
        result = _wrf_series(source, config)['test', 'precipitation']
        self.assertEqual(result.series.values, (2, 4))
        observed = Series('obs', (self.time + timedelta(hours=2),), (4,),
                          ((self.time, self.time + timedelta(hours=2)),), 'sum', 'mm')
        comparison, = compare_forecast(result, observed, timedelta(0), 'precipitation')
        self.assertEqual(comparison.metrics.mae, 0)  # 新批次不能完整覆盖窗口，使用旧批次的 2+2。


class ForecastSelectionTests(unittest.TestCase):
    def setUp(self):
        self.time = datetime(2024, 12, 24, tzinfo=timezone.utc)
        self.source = SourceConfig('wrf', 'wrf', Path('wrf'), ('*',), 'UTC', True)

    def test_lead_configuration_defaults_and_invalid_values(self):
        text = b'[sources.wrf]\nkind="wrf"\nroot="raw"\npatterns=["*"]\ntimezone="UTC"\n'
        with patch.object(Path, 'is_dir', return_value=True), patch.object(Path, 'exists', return_value=False):
            for value, expected in ((b'', 0), (b'min_lead_hours=1.5\n', 1.5)):
                with patch.object(Path, 'read_bytes', return_value=text + value):
                    self.assertEqual(load_config('config.toml').sources[0].min_lead_hours, expected)
            for value in (b'-1', b'nan', b'inf', b'true', b'"6"'):
                with self.subTest(value=value), patch.object(Path, 'read_bytes', return_value=text + b'min_lead_hours=' + value):
                    with self.assertRaises(ConfigError):
                        load_config('config.toml')

    def test_directory_timezone_root_and_file_attribute_fallback(self):
        source = replace(self.source, timezone='Asia/Shanghai')
        self.assertEqual(forecast_init_time(source, Path('wrf/2024122408/nested/file')), self.time)
        self.assertEqual(forecast_init_time(replace(source, root=Path('wrf/2024122408')),
                                            Path('wrf/2024122408/file')), self.time)
        self.assertEqual(forecast_init_time(source, Path('wrf/file'), self.time), self.time)
        self.assertEqual(forecast_init_time(replace(source, init_time=self.time), Path('wrf/file')), self.time)
        self.assertIsNone(forecast_init_time(source, Path('wrf/file')))
        with self.assertRaisesRegex(ValueError, 'min_lead_hours'):
            forecast_init_time(replace(source, min_lead_hours=1), Path('wrf/file'))
        with self.assertRaisesRegex(ValueError, '多个'):
            forecast_init_time(source, Path('wrf/2024122408/2024122409/file'))

    def test_latest_missing_value_does_not_fall_back_to_old_run(self):
        times = (self.time, self.time + timedelta(hours=1))
        old = Series('wrf', times, (1, 2))
        new = Series('wrf', times, (None, 3))
        result = select_latest([(self.time, new), (self.time - timedelta(hours=1), old)])
        self.assertEqual(result.series.values, (None, 3))

    def test_lead_cutoff_requires_entire_interval_and_includes_boundary(self):
        end = self.time + timedelta(hours=1)
        instant = Series('wrf', (self.time, end), (1, 2))
        self.assertEqual(eligible_series(instant, self.time).values, (1, 2))
        rain = Series('wrf', (self.time, end), (1, 2),
                      ((self.time - timedelta(hours=1), self.time), (self.time, end)), 'sum', 'mm')
        self.assertEqual(eligible_series(rain, self.time).values, (2,))

    def test_latest_complete_interval_with_missing_does_not_fall_back(self):
        end = self.time + timedelta(hours=1)
        bounds = ((self.time, end),)
        old = Series('wrf', (end,), (1,), bounds, 'sum', 'mm')
        new = Series('wrf', (end,), (None,), bounds, 'sum', 'mm')
        # 另一个有效时次使绘图序列非全缺测。
        new = Series('wrf', (end, end + timedelta(hours=1)), (None, 1),
                     bounds + ((end, end + timedelta(hours=1)),), 'sum', 'mm')
        result = select_latest([(self.time - timedelta(hours=1), old), (self.time, new)])
        comparison, = compare_forecast(result, old, timedelta(0), 'precipitation')
        self.assertEqual(comparison.matched_times, 1)
        self.assertEqual(comparison.metrics.sample_count, 0)
