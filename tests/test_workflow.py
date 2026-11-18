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
from wrf_evaluate.config import Config, ConfigError, MetricsOptions, PeriodConfig, PlotTask, PointConfig, SeriesOptions, SourceConfig, MapOptions
from wrf_evaluate.station import StationObservation
from wrf_evaluate.workflow import _cldas_series, _wrf_series, plot_comparisons
from wrf_evaluate.forecasts import eligible_series, forecast_init_time, iter_wrf_files, select_latest
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
        self.station = SourceConfig('station', 'station', Path('stations'), (), 'UTC', True, files=(self.excel,))
        self.point = PointConfig('test', 87.5, 43.5, 'Y5012', excel=self.station)
        self.config = Config(
            (self.wrf, self.cldas), Path('unused-output'), 'Asia/Shanghai',
            points=(self.point,), period=PeriodConfig(self.time, self.time + timedelta(hours=1)),
            output_name='test-run', config_bytes=b'[output]\nname = "test-run"\n',
            plots=(PlotTask('temperature_2m', 'series', SeriesOptions()),),
            metrics=(('temperature_2m', MetricsOptions()),),
        )
        self.files = {'wrf': [self.wrf_path], 'cldas': [self.cldas_path]}
        self.header = WrfHeader(self.wrf_path, (self.time, self.config.period.end), (), frozenset({'T2'}), None)
        self.frames = [WrfPointFrame(self.wrf_path, i, time, 0, 0, 1, {'T2': value}, {'T2': 'K'}) for i, (time, value) in enumerate(zip(self.header.times, (273.15, 275.15)))]
        self.rows = [self.row(minutes, value) for minutes, value in ((10, 1), (0, 0), (5, None), (-5, 99), (65, 99))]
        self.rows.append(replace(self.row(0, 99), station_id='other'))

        def files(source, **kwargs):
            if source.kind == 'station':
                return iter(source.files)
            return iter(self.files[source.name])

        def sample(path, *args):
            info = parse_cldas_filename(path)
            return {name: tuple((CldasPointSample(path, info.time, None, 274.15, 'K') for _ in args[1])) for name in args[0]}
        self.start_patch('wrf_evaluate.workflow.iter_source_files', side_effect=files)
        self.start_patch('wrf_evaluate.forecasts.iter_source_files', side_effect=files)
        self.start_patch('wrf_evaluate.matching.iter_source_files', side_effect=files)
        self.read_frames = self.start_patch('wrf_evaluate.workflow.iter_wrf_points_frames', side_effect=lambda *args, **kwargs: self.batches(self.frames, args[3], kwargs))
        self.read_station = self.start_patch('wrf_evaluate.workflow.iter_station_observations', side_effect=lambda *args, **kwargs: (row for row in self.rows if (kwargs['station_ids'] is None or row.station_id in kwargs['station_ids']) and kwargs['period'].start <= row.time <= kwargs['period'].end))
        self.read_cldas = self.start_patch('wrf_evaluate.workflow.read_cldas_points', side_effect=sample)
        self.draw = self.start_patch('wrf_evaluate.workflow.plot_series')
        self.maps = self.start_patch('wrf_evaluate.workflow.plot_maps')
        self.write_metrics = self.start_patch('wrf_evaluate.workflow.write_metrics', side_effect=lambda path, *args, **kwargs: path)
        self.run_directory = self.start_patch('wrf_evaluate.workflow.create_run_directory', return_value=Path('unused-output/test-run_20261115_120000_000001'))

    def start_patch(self, target, **kwargs):
        patcher = patch(target, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def test_multiple_series_tasks_share_reading_and_do_not_overwrite(self):
        plots = (PlotTask('temperature_2m', 'series', SeriesOptions(dpi=100)), PlotTask('temperature_2m', 'series', SeriesOptions(y_limits=(-20, 20))))
        config = replace(self.config, plots=plots, metrics=())
        result, = plot_comparisons(config)
        self.assertEqual(len(result.paths), 2)
        self.assertEqual(len(set(result.paths)), 2)
        self.assertEqual(self.draw.call_count, 2)
        self.assertIs(self.draw.call_args_list[0].args[2], self.draw.call_args_list[1].args[2])
        self.assertEqual(self.read_frames.call_count, 1)
        self.assertEqual(self.read_station.call_count, 1)
        self.assertEqual(self.read_cldas.call_count, 1)
        self.write_metrics.assert_not_called()

    def test_station_excel_schemas_are_isolated_even_with_same_station_id(self):
        first_excel = replace(self.station, files=(Path('first.xls'),), variable_mapping=(('temperature_2m', VariableConfig('temp_A', 'K')),))
        second_excel = replace(self.station, files=(Path('second.xlsx'),), timezone='Asia/Shanghai', sheet='observations', variable_mapping=(('temperature_2m', VariableConfig('temp_B', 'degC')),))
        points = (replace(self.point, excel=first_excel), replace(self.point, name='second', excel=second_excel))
        self.read_station.side_effect = lambda path, *args, **kwargs: (row for row in [replace(self.row(0, 274.15 if path == first_excel.files[0] else 7), path=path)])
        results = list(plot_comparisons(replace(self.config, sources=(self.wrf, self.cldas), points=points)))
        self.assertAlmostEqual(results[0].series[-1].values[0], 1)
        self.assertEqual(results[1].series[-1].values, (7,))
        calls = self.read_station.call_args_list
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0].args[2]['temperature_2m'], 'temp_A')
        self.assertEqual(calls[1].args[2]['temperature_2m'], 'temp_B')
        self.assertEqual(calls[1].args[1], 'Asia/Shanghai')
        self.assertEqual(calls[1].kwargs['sheet_name'], 'observations')

    def test_station_tolerance_overrides_metrics_and_shared_workbook_is_read_once(self):
        points = (replace(self.point, time_tolerance=timedelta(0), excel=self.station), replace(self.point, name='second', time_tolerance=timedelta(seconds=60), excel=self.station))
        self.rows = [replace(self.row(0, 1), time=self.time + timedelta(seconds=30))]
        config = replace(self.config, sources=(self.wrf,), points=points, plots=(), metrics=(('temperature_2m', MetricsOptions()),))
        results = list(plot_comparisons(config))
        self.assertEqual([r.comparisons[0].metrics.sample_count for r in results], [0, 1])
        self.read_station.assert_called_once()
        self.read_frames.assert_called_once()

    def test_single_station_excel_detects_id_and_shared_file_is_read_once(self):
        self.rows = [self.row(0, 1)]
        points = (replace(self.point, station_id=None, excel=self.station), replace(self.point, name='second', excel=self.station))
        results = list(plot_comparisons(replace(self.config, sources=(self.wrf,), points=points)))
        self.read_station.assert_called_once()
        self.assertTrue(self.read_station.call_args.kwargs['single_station'])
        self.assertIsNone(self.read_station.call_args.kwargs['station_ids'])
        self.assertEqual([r.series[-1].values for r in results], [(1,), (1,)])
        self.assertTrue(all(('Y5012' in r.series[-1].label for r in results)))

    def test_automatic_station_id_cannot_mix_stations_across_files(self):
        station = replace(self.station, files=(Path('one.xls'), Path('two.xls')))
        point = replace(self.point, station_id=None, excel=station)
        self.read_station.side_effect = lambda path, *args, **kwargs: (replace(row, station_id='Y5012' if path.name == 'one.xls' else 'Y5056') for row in [self.row(0, 1)])
        with self.assertRaisesRegex(ValueError, '站号不一致'):
            list(plot_comparisons(replace(self.config, sources=(self.wrf,), points=(point,))))

    def row(self, minutes, value):
        return StationObservation(self.excel, minutes + 100, 'Y5012', self.time + timedelta(minutes=minutes), {'temperature_2m': value})

    def batches(self, frames, points, options):
        for frame in frames:
            period = options['period']
            start = options['init_time'] or frame.model_start_time
            if not period.start <= frame.time <= period.end:
                continue
            if start is not None and (not start + timedelta(hours=options['min_lead_hours']) <= frame.time <= start + timedelta(hours=options['max_lead_hours'])):
                continue
            names = options['frame_variables'](frame.time, frame.time_index, frame.model_start_time)
            if names:
                selected = replace(frame, values={name: frame.values[name] for name in names}, units={name: frame.units[name] for name in names})
                yield tuple((selected for _ in points))

    def test_native_times_units_missing_values_and_shared_excel_read(self):
        second = replace(self.point, name='second', station_id='Y5013')
        self.rows.append(replace(self.row(0, -1), station_id='Y5013'))
        messages = []
        results = list(plot_comparisons(replace(self.config, points=(self.point, second)), progress=messages.append))
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
        self.assertEqual(station.times, tuple((self.time + timedelta(minutes=m) for m in (0, 5, 10))))
        self.assertEqual(results[1].series[-1].values, (-1,))
        self.assertNotEqual(results[0].path, results[1].path)
        self.assertEqual(self.read_frames.call_args.kwargs['period'], self.config.period)
        self.assertEqual(self.draw.call_args.kwargs['period'], self.config.period)
        self.assertEqual(len(results[0].comparisons), 2)
        self.assertEqual(self.write_metrics.call_count, 2)
        self.assertEqual(results[0].metrics_path.suffix, '.csv')
        self.run_directory.assert_called_once_with(replace(self.config, points=(self.point, second)))
        self.assertTrue(all((result.path.parent == self.run_directory.return_value for result in results)))
        self.assertEqual(results[0].series[1].label, 'cldas')
        self.assertTrue(any(('wrf：首末有效时次 2024-12-24 08:00 至 2024-12-24 09:00，有效点 2/2' in message for message in messages)))

    def test_progress_reports_uncovered_start_of_configured_period(self):
        self.frames = [replace(frame, time=frame.time + timedelta(hours=1)) for frame in self.frames]
        config = replace(self.config, period=PeriodConfig(self.time, self.time + timedelta(hours=3)))
        messages = []
        list(plot_comparisons(config, progress=messages.append))
        self.assertTrue(any(('开头未覆盖 1 小时，配置起点 2024-12-24 08:00' in message for message in messages)))

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
        config = replace(self.config, sources=(wrf, cldas), points=(replace(self.point, excel=station),), display_names=(('temperature_2m', '自定义温度'),))
        self.frames = [replace(frame, values={'CUSTOM_T2': frame.values['T2']}, units={'CUSTOM_T2': 'K'}) for frame in self.frames]
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
        cldas = replace(self.cldas, variable_mapping=(('precipitation', VariableConfig('PRCP', 'mm', 'sum', timedelta(hours=1), 'end', 'PRE')),))
        rain_path = Path(str(self.cldas_path).replace('TMP-2024122400', 'PRE-2024122401'))
        self.files['cldas'].append(rain_path)
        self.frames = [replace(frame, values=frame.values | {'RAINC': 10 + i}, units=frame.units | {'RAINC': 'mm'}) for i, frame in enumerate(self.frames)]

        def sample(path, *args):
            info = parse_cldas_filename(path)
            return {args[0][0]: tuple((CldasPointSample(path, info.time, None, 0.5 if info.product == 'PRE' else 274.15, 'mm/h' if info.product == 'PRE' else 'K') for _ in args[1]))}
        self.read_cldas.side_effect = sample
        self.rows = [replace(row, values=row.values | {'relative_humidity_2m': 70}) for row in self.rows]
        self.rows = [replace(row, values=row.values | {'precipitation': 0.3}) for row in self.rows]
        config = replace(self.config, sources=(wrf, cldas, self.station), plots=(PlotTask('temperature_2m', 'series', SeriesOptions()), PlotTask('precipitation', 'series', SeriesOptions())), metrics=(('temperature_2m', MetricsOptions()), ('precipitation', MetricsOptions((1,)))))
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
        self.read_station.assert_called_once()
        self.assertEqual(rain_result.series[-1].statistic, 'sum')
        self.assertEqual(len(rain_result.comparisons), 3)
        self.assertEqual(len(temperature.comparisons), 2)

    def test_station_rain_preserves_hour_to_date_curve_and_compares_hour_ends(self):
        wrf = replace(self.wrf, variable_mapping=(('precipitation', VariableConfig('RAINNC', time_kind='cumulative')),))
        config = replace(self.config, sources=(wrf, self.station), plots=(PlotTask('precipitation', 'series', SeriesOptions()),), metrics=(('precipitation', MetricsOptions()),))
        self.frames = [replace(frame, values={'RAINNC': 10 + i}, units={'RAINNC': 'mm'}) for i, frame in enumerate(self.frames)]
        self.rows = [replace(self.row(m, 0), values={'precipitation': value}) for m, value in ((5, 0.2), (30, 0.5), (60, 0.8))]
        result, = plot_comparisons(config)
        self.assertEqual(result.series[0].values, (1,))
        self.assertEqual(result.series[1].values, (0.2, 0.5, 0.8))
        self.assertEqual(result.series[1].statistic, 'sum')
        comparison, = result.comparisons
        self.assertEqual(comparison.metrics.sample_count, 1)
        self.assertAlmostEqual(comparison.metrics.mae, 0.2)
        self.draw.assert_called_once()

    def test_minute_rain_uses_same_hourly_values_for_plot_and_metrics(self):
        wrf = replace(self.wrf, variable_mapping=(('precipitation', VariableConfig('RAINNC', time_kind='cumulative')),))
        station = replace(self.station, variable_mapping=(('precipitation', VariableConfig('雨量', 'mm', 'sum', timedelta(hours=1), 'end')),))
        self.frames = [replace(self.frames[0], time=self.time + timedelta(minutes=minute), values={'RAINNC': value}, units={'RAINNC': 'mm'}) for minute, value in ((0, 100), (20, 101), (40, 103), (60, 106))]
        self.rows = [replace(self.row(60, 0), values={'precipitation': 4})]
        config = replace(self.config, sources=(wrf,), points=(replace(self.point, excel=station),), plots=(PlotTask('precipitation', 'series', SeriesOptions()),), metrics=(('precipitation', MetricsOptions((5,))),))
        result, = plot_comparisons(config)
        self.assertEqual(result.series[0].values, (6,))
        comparison, = result.comparisons
        self.assertEqual(comparison.metrics.sample_count, 1)
        self.assertEqual(comparison.metrics.mae, 2)
        self.assertEqual(comparison.interval_seconds, 3600)
        self.assertEqual(comparison.events[0][1].false_alarms, 1)
        station = replace(station, variable_mapping=(('precipitation', VariableConfig('雨量', 'mm', 'sum', timedelta(minutes=20), 'end')),))
        result, = plot_comparisons(replace(config, sources=(wrf,), points=(replace(self.point, excel=station),)))
        self.assertEqual(result.comparisons[0].metrics.sample_count, 0)

    def test_requested_station_fields_are_read_together(self):
        wrf = replace(self.wrf, variable_mapping=(('relative_humidity_2m', VariableConfig('RH', '%')),))
        self.frames = [replace(frame, values=frame.values | {'RH': 80}, units=frame.units | {'RH': '%'}) for frame in self.frames]
        self.rows = [replace(row, values=row.values | {'relative_humidity_2m': 70}) for row in self.rows]
        config = replace(self.config, sources=(wrf, self.station), plots=(PlotTask('temperature_2m', 'series', SeriesOptions()), PlotTask('relative_humidity_2m', 'series', SeriesOptions())), metrics=(('temperature_2m', MetricsOptions()), ('relative_humidity_2m', MetricsOptions())))
        results = list(plot_comparisons(config))
        self.assertEqual(len(results), 2)
        self.read_station.assert_called_once()
        self.assertEqual(self.read_station.call_args.kwargs['fields'], config.elements)
        self.read_frames.assert_called_once()
        self.assertEqual(results[1].comparisons[0].metrics.bias, 10)

    def humidity_samples(self, *, missing_pressure=False, moved_grid=False):
        pressure, vapor = (90000.0, 305.6)
        ratio = 0.622 * vapor / (pressure - vapor)
        for product in ('SHU', 'PRS'):
            for hour in (0, 1):
                if missing_pressure and product == 'PRS' and (hour == 1):
                    continue
                self.files['cldas'].append(Path(str(self.cldas_path).replace('TMP-2024122400', f'{product}-202412240{hour}')))
        self.frames = [replace(frame, values={'T2': 273.15, 'Q2': ratio, 'PSFC': pressure}, units={'T2': 'K', 'Q2': 'kg/kg', 'PSFC': 'Pa'}) for frame in self.frames]
        self.files['cldas'].append(Path(str(self.cldas_path).replace('TMP-2024122400', 'TMP-2024122401')))

        def sample(path, names, points, timezone):
            info = parse_cldas_filename(path)
            values = {'TAIR': (273.15, 'K'), 'QAIR': (ratio / (1 + ratio), 'kg/kg'), 'PAIR': (pressure, 'Pa')}
            position = (88.0 if moved_grid and info.product == 'PRS' else 87.5, 43.5)
            return {name: tuple((CldasPointSample(path, info.time, None, *values[name], position) for point in points)) for name in names}
        self.read_cldas.side_effect = sample
        self.rows = [replace(row, values=row.values | {'relative_humidity_2m': 70}) for row in self.rows]
        return replace(self.config, sources=(self.wrf, self.cldas), plots=(PlotTask('temperature_2m', 'series', SeriesOptions()), PlotTask('relative_humidity_2m', 'series', SeriesOptions())), metrics=(('temperature_2m', MetricsOptions()), ('relative_humidity_2m', MetricsOptions())))

    def test_derived_humidity_reuses_temperature_reads_and_reaches_metrics(self):
        config = self.humidity_samples(missing_pressure=True)
        messages = []
        temperature, humidity = plot_comparisons(config, progress=messages.append)
        self.assertEqual(self.read_cldas.call_count, 4)
        self.assertEqual(len({call.args[0] for call in self.read_cldas.call_args_list}), 4)
        self.assertFalse(any(('SHU-2024122401' in call.args[0].name for call in self.read_cldas.call_args_list)))
        self.assertEqual(set(self.read_frames.call_args.args[1]), {'T2', 'Q2', 'PSFC'})
        self.assertAlmostEqual(humidity.series[0].values[0], 50)
        self.assertAlmostEqual(humidity.series[1].values[0], 50)
        self.assertIsNone(humidity.series[1].values[1])
        self.assertEqual(humidity.comparisons[0].metrics.sample_count, 1)
        self.assertAlmostEqual(humidity.comparisons[0].metrics.rmse, 0)
        self.assertTrue(any(('缺少同刻基础产品' in message for message in messages)))

    def test_cldas_interval_outside_period_is_excluded_before_opening_file(self):
        cldas = replace(self.cldas, variable_mapping=(('precipitation', VariableConfig('PRCP', 'mm', 'sum', timedelta(hours=1), 'end', 'PRE')),))
        config = replace(self.config, sources=(cldas,), plots=(PlotTask('precipitation', 'series', SeriesOptions()),), metrics=(('precipitation', MetricsOptions()),))
        paths = [Path(str(self.cldas_path).replace('TMP-2024122400', f'PRE-202412240{h}')) for h in (0, 1)]
        infos = tuple((parse_cldas_filename(path) for path in paths))
        index = {('cldas', infos[0].family, 'PRE'): (tuple((info.time for info in infos)), infos)}
        self.read_cldas.side_effect = lambda path, names, points, zone: {'PRCP': (CldasPointSample(path, infos[1].time, None, 1.0, 'mm'),)}
        curves = _cldas_series([cldas], index, config)
        self.read_cldas.assert_called_once()
        self.assertEqual(self.read_cldas.call_args.args[0], paths[1])
        self.assertEqual(curves['test', 'precipitation'][0].values, (1.0,))

    def test_derived_humidity_rejects_different_nearest_grid_coordinates(self):
        with self.assertRaisesRegex(ValueError, '格点经纬度不一致'):
            list(plot_comparisons(self.humidity_samples(moved_grid=True)))
        self.draw.assert_not_called()

    def test_invalid_wrf_humidity_warns_with_origin_and_is_excluded_from_metrics(self):
        config = self.humidity_samples()
        self.frames[0] = replace(self.frames[0], values=self.frames[0].values | {'Q2': -1e-08})
        messages = []
        _, humidity = plot_comparisons(config, progress=messages.append)
        self.assertIsNone(humidity.series[0].values[0])
        self.assertAlmostEqual(humidity.series[0].values[1], 50)
        self.assertEqual(humidity.comparisons[0].metrics.sample_count, 1)
        warning = next((message for message in messages if '输入无效' in message))
        for text in (str(self.frames[0].path), self.frames[0].time.isoformat(), '点位 test', 'Q2', '-1e-08'):
            self.assertIn(text, warning)
        self.frames = [replace(frame, values=frame.values | {'Q2': -1e-08}) for frame in self.frames]
        self.draw.reset_mock()
        with self.assertRaisesRegex(ValueError, '全部缺测'):
            list(plot_comparisons(config, progress=messages.append))
        self.draw.assert_not_called()

    def test_series_and_metrics_outputs_are_independent(self):
        for output in ('series', 'metrics'):
            with self.subTest(output=output):
                self.draw.reset_mock()
                self.write_metrics.reset_mock()
                self.maps.reset_mock()
                config = replace(self.config, plots=(PlotTask('temperature_2m', output, SeriesOptions()),) if output == 'series' else (), metrics=(('temperature_2m', MetricsOptions()),) if output == 'metrics' else ())
                with patch('wrf_evaluate.workflow.compare_forecast', wraps=compare_forecast) as compare:
                    result, = plot_comparisons(config)
                self.assertEqual(self.draw.call_count, int(output == 'series'))
                self.assertEqual(self.write_metrics.call_count, int(output == 'metrics'))
                self.assertEqual(result.path is not None, output == 'series')
                self.assertEqual(result.metrics_path is not None, output == 'metrics')
                self.assertEqual(bool(result.comparisons), output == 'metrics')
                self.assertEqual(bool(compare.call_count), output == 'metrics')
                self.maps.assert_not_called()

    def test_progress_shows_requested_outputs_before_reading_data(self):
        config = replace(self.humidity_samples(), plots=(PlotTask('temperature_2m', 'series', SeriesOptions()), PlotTask('relative_humidity_2m', 'map', MapOptions())), metrics=(('temperature_2m', MetricsOptions()),))
        unused = Path(str(self.cldas_path).replace('TMP-', 'PRE-'))
        self.files['cldas'] += [unused, Path('duplicate') / unused.name]
        messages = []
        list(plot_comparisons(config, progress=messages.append))
        self.assertIn('2m气温（temperature_2m）：时序图、指标 CSV', messages[1])
        self.assertIn('2m相对湿度（relative_humidity_2m）：空间图', messages[2])
        self.assertLess(messages.index('2m相对湿度（relative_humidity_2m）：空间图'), next((i for i, message in enumerate(messages) if '读取 WRF 来源' in message)))

    def test_progress_reports_unmatched_metrics_before_writing_csv(self):
        self.rows = [self.row(10, 1)]
        config = replace(self.config, sources=(self.wrf, self.station), plots=(), metrics=(('temperature_2m', MetricsOptions()),))
        messages = []
        list(plot_comparisons(config, progress=messages.append))
        message = next((message for message in messages if '时间配对 0，有效样本 0' in message))
        self.assertIn('核对时间覆盖', message)
        self.assertLess(messages.index(message), messages.index('写入 temperature_2m 指标 CSV…'))

    def test_maps_only_do_not_read_station_or_point_data(self):
        config = replace(self.config, points=(), plots=(PlotTask('temperature_2m', 'map', MapOptions()),), metrics=())
        with patch('wrf_evaluate.workflow.read_wrf_header', return_value=self.header) as header:
            self.assertEqual(list(plot_comparisons(config)), [])
        header.assert_called_once_with(self.wrf_path, 'UTC')
        self.read_frames.assert_not_called()
        self.read_station.assert_not_called()
        self.read_cldas.assert_not_called()
        self.draw.assert_not_called()
        self.write_metrics.assert_not_called()
        self.run_directory.assert_called_once()
        self.maps.assert_called_once()
        indices = self.maps.call_args.args[3]
        self.assertEqual(indices['wrf'][None], {time: (self.wrf_path, i) for i, time in enumerate(self.header.times)})

    def test_map_only_element_does_not_expand_point_reads(self):
        config = self.humidity_samples()
        config = replace(config, plots=(PlotTask('temperature_2m', 'series', SeriesOptions()), PlotTask('relative_humidity_2m', 'map', MapOptions())), metrics=())
        result, = plot_comparisons(config)
        self.assertEqual(result.element, 'temperature_2m')
        self.assertEqual(self.read_frames.call_args.args[1], ('T2',))
        self.assertEqual({name for call in self.read_cldas.call_args_list for name in call.args[1]}, {'TAIR'})
        self.write_metrics.assert_not_called()
        self.maps.assert_called_once()

    def test_explicit_maps_require_cldas_and_duplicate_frames_are_rejected(self):
        config = replace(self.config, points=(), plots=(PlotTask('temperature_2m', 'map', MapOptions()),), metrics=())
        with self.assertRaisesRegex(ConfigError, '空间图.*cldas'):
            list(plot_comparisons(replace(config, sources=(self.wrf,))))
        duplicate = replace(self.header, times=(self.time, self.time))
        with patch('wrf_evaluate.workflow.read_wrf_header', return_value=duplicate):
            with self.assertRaisesRegex(ValueError, '重复时次'):
                list(plot_comparisons(config))
        self.run_directory.assert_not_called()

    def test_map_only_index_preserves_runs_and_applies_lead_time_bounds(self):
        wrf = replace(self.wrf, min_lead_hours=1, max_lead_hours=2)
        config = replace(self.config, sources=(wrf, self.cldas), points=(), plots=(PlotTask('temperature_2m', 'map', MapOptions()),), metrics=())
        old, new = (Path('wrf/2024122323/sample'), Path('wrf/2024122400/sample'))
        self.files['wrf'] = [old, new]

        def header(path, zone):
            return replace(self.header, path=path, times=(self.time - timedelta(hours=1), self.time, self.time + timedelta(hours=1), self.time + timedelta(hours=2)))
        with patch('wrf_evaluate.workflow.read_wrf_header', side_effect=header):
            list(plot_comparisons(config))
        runs = self.maps.call_args.args[3]['wrf']
        self.assertEqual(runs[self.time - timedelta(hours=1)], {self.time: (old, 1), self.time + timedelta(hours=1): (old, 2)})
        self.assertEqual(runs[self.time], {self.time + timedelta(hours=1): (new, 2)})

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

    def test_observation_failure_does_not_write_partial_figures(self):
        config = replace(self.config, points=(self.point, replace(self.point, name='second')))
        self.read_cldas.side_effect = [ValueError('CLDAS 文件读取失败')]
        output, errors = (StringIO(), StringIO())
        with patch('wrf_evaluate.cli.load_config', return_value=config), redirect_stdout(output), redirect_stderr(errors):
            self.assertEqual(main(['plot']), 2)
        self.draw.assert_not_called()
        self.assertIn('CLDAS 文件读取失败', errors.getvalue())

    def test_cli_prints_output_sample_counts(self):
        output = StringIO()
        with patch('wrf_evaluate.cli.load_config', return_value=self.config), redirect_stdout(output):
            self.assertEqual(main(['plot']), 0)
        self.assertIn('有效点 2/3', output.getvalue())
        self.assertEqual(output.getvalue().count('时间配对 1，有效样本 1'), 2)
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
            frames = [replace(frame, path=path, model_start_time=self.time - timedelta(days=1), values={'T2': 283.15 if path == new else 273.15}) for frame in self.frames]
            return self.batches(frames, args[2], kwargs)
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
            frames = [WrfPointFrame(path, i, self.time + timedelta(hours=i), 0, 0, 0, {'RAINNC': value}, {'RAINNC': 'mm'}) for i, value in enumerate(values)]
            return self.batches(frames, args[2], kwargs)
        self.read_frames.side_effect = read
        source = replace(self.wrf, min_lead_hours=2, variable_mapping=(('precipitation', VariableConfig('RAINNC', time_kind='cumulative')),))
        config = replace(self.config, sources=(source,), period=PeriodConfig(self.time, self.time + timedelta(hours=2)), plots=(PlotTask('precipitation', 'series', SeriesOptions()),), metrics=(('precipitation', MetricsOptions()),))
        result = _wrf_series(source, config)['test', 'precipitation']
        self.assertEqual(result.series.values, (2, 4))
        observed = Series('obs', (self.time + timedelta(hours=2),), (4,), ((self.time, self.time + timedelta(hours=2)),), 'sum', 'mm')
        comparison, = compare_forecast(result, observed, timedelta(0), 'precipitation')
        self.assertEqual(comparison.metrics.mae, 0)

    def test_cumulative_point_values_cannot_difference_across_moving_grid(self):
        source = replace(self.wrf, variable_mapping=(('precipitation', VariableConfig('RAINNC', time_kind='cumulative')),))
        self.frames = [replace(frame, values={'RAINNC': i}, units={'RAINNC': 'mm'}, grid_coordinates=(87.5 + i * 0.01, 43.5)) for i, frame in enumerate(self.frames)]
        config = replace(self.config, sources=(source,), plots=(PlotTask('precipitation', 'series', SeriesOptions()),), metrics=(('precipitation', MetricsOptions()),))
        with self.assertRaisesRegex(ValueError, '网格发生移动'):
            _wrf_series(source, config)

class ForecastSelectionTests(unittest.TestCase):

    def setUp(self):
        self.time = datetime(2024, 12, 24, tzinfo=timezone.utc)
        self.source = SourceConfig('wrf', 'wrf', Path('wrf'), ('*',), 'UTC', True)

    def test_directory_timezone_root_and_file_attribute_fallback(self):
        source = replace(self.source, timezone='Asia/Shanghai')
        self.assertEqual(forecast_init_time(source, Path('wrf/2024122408/nested/file')), self.time)
        self.assertEqual(forecast_init_time(replace(source, root=Path('wrf/2024122408')), Path('wrf/2024122408/file')), self.time)
        self.assertEqual(forecast_init_time(source, Path('wrf/file'), self.time), self.time)
        self.assertEqual(forecast_init_time(replace(source, init_time=self.time), Path('wrf/file')), self.time)
        self.assertIsNone(forecast_init_time(source, Path('wrf/file')))
        with self.assertRaisesRegex(ValueError, 'lead_hours'):
            forecast_init_time(replace(source, min_lead_hours=1), Path('wrf/file'))
        with self.assertRaisesRegex(ValueError, '多个'):
            forecast_init_time(source, Path('wrf/2024122408/2024122409/file'))

    def test_filename_interval_filter_keeps_previous_multi_time_file(self):
        period = PeriodConfig(datetime(2024, 12, 10, 3, 5, tzinfo=timezone.utc), datetime(2024, 12, 10, 3, 15, tzinfo=timezone.utc))
        paths = [Path(f'wrf/2024121000/wrfout_d03_2024-12-10_{time}') for time in ('02_50_00', '03_00_00', '03_10_00', '03_20_00')]
        source = replace(self.source, max_lead_hours=168)

        def scan(_source, *, include_directory):
            directory = Path('wrf/2024121000')
            if include_directory(directory):
                yield from paths
        with patch('wrf_evaluate.forecasts.iter_source_files', side_effect=scan):
            selected = list(iter_wrf_files(source, period))
        self.assertEqual(selected, paths[1:3])

    def test_map_window_filter_keeps_only_files_covering_requested_minutes(self):
        period = PeriodConfig(datetime(2024, 12, 10, 3, tzinfo=timezone.utc), datetime(2024, 12, 10, 3, 20, tzinfo=timezone.utc))
        paths = [Path(f'wrf/2024121000/wrfout_d03_2024-12-10_{time}') for time in ('02_50_00', '03_00_00', '03_10_00', '03_20_00')]
        source = replace(self.source, max_lead_hours=168)

        def scan(_source, *, include_directory):
            directory = Path('wrf/2024121000')
            if include_directory(directory):
                yield from paths
        windows = ((period.start + timedelta(minutes=5), period.start + timedelta(minutes=15)),)
        with patch('wrf_evaluate.forecasts.iter_source_files', side_effect=scan):
            selected = list(iter_wrf_files(source, period, windows=windows))
        self.assertEqual(selected, paths[1:3])

    def test_empty_map_windows_do_not_scan_wrf_tree(self):
        period = PeriodConfig(self.time, self.time + timedelta(hours=1))
        with patch('wrf_evaluate.forecasts.iter_source_files') as scan:
            self.assertEqual(list(iter_wrf_files(self.source, period, windows=())), [])
        scan.assert_not_called()

    def test_map_instant_uses_next_file_at_exact_filename_boundary(self):
        period = PeriodConfig(self.time, self.time + timedelta(hours=12))
        paths = [Path(f'wrf/2024122400/wrfout_d03_2024-12-24_{h:02d}_00_00') for h in range(13)]
        instant = self.time + timedelta(hours=6)
        with patch('wrf_evaluate.forecasts.iter_source_files', return_value=iter(paths)):
            selected = list(iter_wrf_files(self.source, period, windows=((instant, instant),)))
        self.assertEqual(selected, [paths[6]])

    def test_seven_day_bound_prunes_old_runs_before_listing_files(self):
        period = PeriodConfig(datetime(2024, 12, 10, tzinfo=timezone.utc), datetime(2024, 12, 10, 3, tzinfo=timezone.utc))
        old = Path('wrf/2024120100/wrfout_d03_2024-12-01_00_00_00')
        new = Path('wrf/2024121000/wrfout_d03_2024-12-10_00_00_00')
        files = [old, new]
        decisions = []

        def scan(_source, *, include_directory):
            for path in files:
                directory = path.parent
                decision = include_directory(directory)
                decisions.append((directory, decision))
                if decision:
                    yield path
        with patch('wrf_evaluate.forecasts.iter_source_files', side_effect=scan):
            selected = list(iter_wrf_files(self.source, period))
        self.assertEqual(selected, [new])
        self.assertIn((old.parent, False), decisions)

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
        rain = Series('wrf', (self.time, end), (1, 2), ((self.time - timedelta(hours=1), self.time), (self.time, end)), 'sum', 'mm')
        self.assertEqual(eligible_series(rain, self.time).values, (2,))

    def test_latest_complete_interval_with_missing_does_not_fall_back(self):
        end = self.time + timedelta(hours=1)
        bounds = ((self.time, end),)
        old = Series('wrf', (end,), (1,), bounds, 'sum', 'mm')
        new = Series('wrf', (end, end + timedelta(hours=1)), (None, 1), bounds + ((end, end + timedelta(hours=1)),), 'sum', 'mm')
        result = select_latest([(self.time - timedelta(hours=1), old), (self.time, new)])
        comparison, = compare_forecast(result, old, timedelta(0), 'precipitation')
        self.assertEqual(comparison.matched_times, 1)
        self.assertEqual(comparison.metrics.sample_count, 0)

    def test_lead_upper_bound_checks_interval_end_not_just_timestamp(self):
        end = self.time + timedelta(hours=1)
        curve = Series('wrf', (self.time, end), (1, 2), ((self.time, end), (end, end + timedelta(hours=1))), 'sum', 'mm')
        self.assertEqual(eligible_series(curve, self.time, end).values, (1,))
        self.assertIsNone(eligible_series(curve, None, self.time))
