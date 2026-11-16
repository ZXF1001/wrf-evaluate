"""Exercise WRF NetCDF reading with small temporary files."""

from contextlib import closing, redirect_stdout
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
from netCDF4 import Dataset, stringtochar

from wrf_evaluate.cli import main
from wrf_evaluate.spatial import nearest_grid_point
from wrf_evaluate.config import Config, PeriodConfig, PointConfig, SourceConfig
from wrf_evaluate.workflow import _wrf_series
from wrf_evaluate.wrf import (
    WrfField, WrfFieldReader, WrfPointLocator, WrfReadError, iter_wrf_point_frames, iter_wrf_points_frames, read_wrf_field,
    read_wrf_fields, read_wrf_header, resolve_wrf_variable, sample_wrf_station,
)
from wrf_evaluate.variables import VariableConfig


class WrfReaderTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        # Keep the filename unrelated to the embedded timestamps.
        self.path = self.root / "wrfout_test"
        self.make_file()

    def make_file(self, file_format="NETCDF4"):
        with Dataset(self.path, "w", format=file_format) as dataset:
            for name, size in [("Time", 2), ("DateStrLen", 19), ("south_north", 2), ("west_east", 3)]:
                dataset.createDimension(name, size)
            dataset.SIMULATION_START_DATE = "2024-12-23_12:00:00"
            dataset.START_DATE = "2024-12-23_21:00:00"
            times = dataset.createVariable("Times", "S1", ("Time", "DateStrLen"))
            times[:] = stringtochar(np.array(["2024-12-23_22:00:00", "2024-12-23_22:05:00"], dtype="S19"))
            times._Encoding = "ascii"
            temperature = dataset.createVariable("T2", "f4", ("Time", "south_north", "west_east"), fill_value=-9999)
            temperature.units = "K"
            temperature[0] = 273.15
            temperature[1] = [[274, -9999, float("nan")], [275, 276, 277]]

    def test_header_preserves_minutes_timezone_and_simulation_start(self):
        for file_format in ["NETCDF4", "NETCDF3_CLASSIC"]:
            with self.subTest(file_format=file_format):
                self.make_file(file_format)
                header = read_wrf_header(self.path, "Asia/Shanghai")
                self.assertEqual(header.time_start.utcoffset(), timedelta(hours=8))
                self.assertEqual(header.time_end - header.time_start, timedelta(minutes=5))
                self.assertEqual(header.model_start_time.hour, 12)
                self.assertEqual(dict(header.dimensions)["west_east"], 3)
                self.assertIn("T2", header.variables)
                self.assertEqual(header.time_start.astimezone(timezone.utc), datetime(2024, 12, 23, 14, tzinfo=timezone.utc))

    def test_field_reads_selected_time_and_preserves_units_and_missing_values(self):
        field = read_wrf_field(self.path, "T2", time_index=1)
        self.assertEqual(field.values.shape, (2, 3))
        self.assertEqual(field.units, "K")
        np.testing.assert_array_equal(field.values.mask, [[False, True, True], [False, False, False]])
        self.assertEqual(field.values[1, 2], 277)
        self.assertAlmostEqual(float(read_wrf_field(self.path, "T2").values[0, 0]), 273.15, places=4)

    def test_ten_meter_wind_components_are_combined_and_propagate_missing_values(self):
        spec = VariableConfig(None, components=('U10', 'V10'))
        values, units = resolve_wrf_variable(
            spec, {'U10': 3.0, 'V10': 4.0}, {'U10': 'm s-1', 'V10': 'm s-1'},
        )
        self.assertEqual(values, 5.0)
        self.assertEqual(units, 'm/s')
        missing, _ = resolve_wrf_variable(
            spec, {'U10': 3.0, 'V10': None}, {'U10': 'm s-1', 'V10': 'm s-1'},
        )
        self.assertIsNone(missing)
        arrays, _ = resolve_wrf_variable(
            spec, {'U10': np.ma.array([3., 3.], mask=[False, True]),
                   'V10': np.ma.array([4., 4.])},
            {'U10': 'm/s', 'V10': 'm/s'},
        )
        self.assertEqual(arrays[0], 5.0)
        self.assertTrue(np.ma.is_masked(arrays[1]))

    def test_staggered_three_dimensional_fields_are_rejected_with_guidance(self):
        with Dataset(self.path, 'a') as dataset:
            dataset.createDimension('west_east_stag', 4)
            dataset.createDimension('south_north_stag', 3)
            dataset.createDimension('bottom_top', 1)
            dataset.createVariable('U', 'f4', ('Time', 'bottom_top', 'south_north', 'west_east_stag'))
            dataset.createVariable('V', 'f4', ('Time', 'bottom_top', 'south_north_stag', 'west_east'))
        for name in ('U', 'V'):
            with self.subTest(name=name), self.assertRaisesRegex(WrfReadError, '交错网格'):
                read_wrf_field(self.path, name)

    def test_multiple_fields_share_one_open_and_keep_independent_units(self):
        with Dataset(self.path, "a") as dataset:
            latitude = dataset.createVariable("XLAT", "f4", ("Time", "south_north", "west_east"))
            latitude.units = "degrees_north"
            latitude[:] = 43.0
        with patch("wrf_evaluate.wrf.Dataset", wraps=Dataset) as open_dataset:
            fields = read_wrf_fields(self.path, ("T2", "XLAT"), time_index=1)
        open_dataset.assert_called_once_with(self.path, mode="r")
        self.assertEqual(set(fields), {"T2", "XLAT"})
        self.assertEqual(fields["XLAT"].units, "degrees_north")
        self.assertEqual(fields["T2"].units, "K")
        np.testing.assert_array_equal(fields["T2"].values.mask, [[False, True, True], [False, False, False]])
        np.testing.assert_array_equal(fields["XLAT"].values, 43.0)

    def test_multiple_field_request_is_validated_before_reading(self):
        for names in ((), ("T2", "missing"), ("T2", "T2"), "T2", None, ("T2", 1)):
            with self.subTest(names=names), self.assertRaises(WrfReadError):
                read_wrf_fields(self.path, names)

    def test_map_reader_reuses_fields_and_releases_previous_file_and_frame(self):
        self.add_coordinates()
        second = self.root / 'second'
        second.write_bytes(self.path.read_bytes())
        with patch('wrf_evaluate.wrf.Dataset', wraps=Dataset) as opened:
            with closing(WrfFieldReader()) as reader:
                first = reader.read(self.path, ('T2', 'XLONG'), 0)
                same = reader.read(self.path, ('T2', 'XLONG', 'XLAT'), 0)
                self.assertIs(first['T2'], same['T2'])
                self.assertIs(first['XLONG'], same['XLONG'])
                self.assertEqual(opened.call_count, 1)
                next_frame = reader.read(self.path, ('T2',), 1)
                self.assertEqual(next_frame['T2'].values[0, 0], 274)
                self.assertTrue(next_frame['T2'].values.mask[0, 1])
                self.assertEqual(set(reader.fields), {'T2'})
                self.assertEqual(opened.call_count, 1)
                first_dataset = reader.dataset
                other = reader.read(second, ('T2',), 0)
                self.assertFalse(first_dataset.isopen())
                self.assertIsNot(other['T2'], first['T2'])
                self.assertEqual(opened.call_count, 2)
                last_dataset = reader.dataset
            self.assertFalse(last_dataset.isopen())
            self.assertEqual(reader.fields, {})

    def test_map_reader_closes_file_on_invalid_request_and_drawing_failure(self):
        reader = WrfFieldReader()
        with self.assertRaisesRegex(WrfReadError, '缺少变量'):
            reader.read(self.path, ('T2', 'missing'))
        self.assertIsNone(reader.dataset)
        with self.assertRaisesRegex(RuntimeError, 'draw failed'):
            with closing(reader):
                reader.read(self.path, ('T2',))
                dataset = reader.dataset
                raise RuntimeError('draw failed')
        self.assertFalse(dataset.isopen())

    def test_nearest_grid_station_sample_returns_values_and_keeps_missing(self):
        with Dataset(self.path, "a") as dataset:
            latitude = dataset.createVariable("XLAT", "f4", ("Time", "south_north", "west_east"))
            longitude = dataset.createVariable("XLONG", "f4", ("Time", "south_north", "west_east"))
            latitude[:] = [[43, 43, 43], [44, 44, 44]]
            longitude[:] = [[87, 88, 89], [87, 88, 89]]
        fields = read_wrf_fields(self.path, ("T2", "XLAT", "XLONG"), time_index=1)
        sample = sample_wrf_station(fields, 88.01, 43.02)
        self.assertEqual((sample.row, sample.column), (0, 1))
        self.assertEqual(sample.values["T2"], None)  # 样例该网格点是缺测值。
        self.assertGreater(sample.distance_km, 0)

    def test_station_sampling_requires_coordinates_and_consistent_fields(self):
        field = read_wrf_field(self.path, "T2")
        with self.assertRaisesRegex(WrfReadError, "XLONG"):
            sample_wrf_station({"T2": field}, 88, 43)
        with Dataset(self.path, "a") as dataset:
            dataset.createVariable("XLAT", "f4", ("Time", "south_north", "west_east"))[:] = [[42, 42, 42], [43, 43, 43]]
            dataset.createVariable("XLONG", "f4", ("Time", "south_north", "west_east"))[:] = [[87, 88, 89], [87, 88, 89]]
        coordinates = read_wrf_fields(self.path, ("XLAT", "XLONG"))
        with self.assertRaisesRegex(WrfReadError, "形状"):
            sample_wrf_station({**coordinates, "T2": WrfField(field.values[:, :1], field.units)}, 88, 43)
        with self.assertRaisesRegex(WrfReadError, "网格范围外"):
            sample_wrf_station({**coordinates, "T2": field}, 90, 43)

    def add_coordinates(self):
        with Dataset(self.path, "a") as dataset:
            dimensions = ("Time", "south_north", "west_east")
            dataset.createVariable("XLAT", "f4", dimensions)[:] = [[43, 43, 43], [44, 44, 44]]
            dataset.createVariable("XLONG", "f4", dimensions)[:] = [[87, 88, 89], [87, 88, 89]]

    def test_multi_point_frames_share_one_read_and_preserve_independent_values(self):
        self.add_coordinates()
        with patch("wrf_evaluate.wrf.Dataset", wraps=Dataset) as open_dataset, \
             patch("wrf_evaluate.wrf.nearest_grid_point", wraps=nearest_grid_point) as locate:
            frames = list(iter_wrf_points_frames(
                self.path, ["T2"], "UTC", ((87.01, 43.01), (88.01, 43.01), (87.02, 43.02)),
            ))
        open_dataset.assert_called_once_with(self.path, mode="r")
        self.assertEqual(len(frames), 2)
        self.assertEqual(len(frames[0]), 3)
        self.assertEqual(locate.call_count, 3)
        self.assertEqual([frame.column for frame in frames[0]], [0, 1, 0])
        self.assertEqual([frame.values["T2"] for frame in frames[1]], [274, None, 274])
        self.assertEqual(frames[0][0].model_start_time, datetime(2024, 12, 23, 12, tzinfo=timezone.utc))

    def test_point_frames_keep_native_times_units_and_reuse_static_grid(self):
        self.add_coordinates()
        with patch("wrf_evaluate.wrf.Dataset", wraps=Dataset) as open_dataset, \
             patch("wrf_evaluate.wrf.nearest_grid_point", wraps=nearest_grid_point) as locate:
            frames = list(iter_wrf_point_frames(self.path, ["T2"], "Asia/Shanghai", 87.01, 43.01))
        open_dataset.assert_called_once_with(self.path, mode="r")
        self.assertEqual(locate.call_count, 1)
        self.assertEqual([frame.time_index for frame in frames], [0, 1])
        self.assertEqual(frames[1].time - frames[0].time, timedelta(minutes=5))
        self.assertEqual(frames[0].time.utcoffset(), timedelta(hours=8))
        self.assertEqual(frames[0].path, self.path)
        self.assertEqual((frames[0].row, frames[0].column), (0, 0))
        self.assertAlmostEqual(frames[0].values["T2"], 273.15, places=4)
        self.assertEqual(frames[1].values["T2"], 274)
        self.assertEqual(frames[1].units["T2"], "K")

    def test_point_frames_read_only_times_inside_configured_period(self):
        self.add_coordinates()
        period = PeriodConfig(
            datetime(2024, 12, 23, 22, 4, tzinfo=timezone.utc),
            datetime(2024, 12, 23, 22, 5, tzinfo=timezone.utc),
        )
        with patch("wrf_evaluate.wrf.nearest_grid_point", wraps=nearest_grid_point) as locate:
            frames = list(iter_wrf_point_frames(
                self.path, ["T2"], "UTC", 87.01, 43.01, period=period,
            ))
        self.assertEqual([frame.time_index for frame in frames], [1])
        self.assertEqual(frames[0].time, period.end)
        locate.assert_called_once()

    def test_locator_reuses_across_files_and_relocates_changed_grid(self):
        self.add_coordinates()
        second = self.root / 'second'
        second.write_bytes(self.path.read_bytes())
        points = ((87.01, 43.01), (87.01, 43.01))
        locator = WrfPointLocator(points)
        with patch('wrf_evaluate.wrf.nearest_grid_point', wraps=nearest_grid_point) as locate:
            for path in (self.path, second):
                frames = list(iter_wrf_points_frames(path, ['T2'], 'UTC', points, locator=locator))
                self.assertEqual([frame.column for frame in frames[0]], [0, 0])
            self.assertEqual(locate.call_count, 1)
            with Dataset(second, 'a') as dataset:
                dataset.variables['XLONG'][:] = [[86, 87, 88], [86, 87, 88]]
            frames = list(iter_wrf_points_frames(second, ['T2'], 'UTC', points, locator=locator))
            self.assertEqual([frame.column for frame in frames[0]], [1, 1])
            self.assertEqual(locate.call_count, 2)
        with self.assertRaisesRegex(WrfReadError, '点位列表'):
            list(iter_wrf_points_frames(second, ['T2'], 'UTC', ((88., 43.),), locator=locator))

    def test_point_frames_exclude_times_after_maximum_forecast_lead(self):
        self.add_coordinates()
        init_time = datetime(2024, 12, 23, 22, tzinfo=timezone.utc)
        frames = list(iter_wrf_point_frames(
            self.path, ["T2"], "UTC", 87.01, 43.01,
            init_time=init_time, max_lead_hours=0.01,
        ))
        self.assertEqual([frame.time_index for frame in frames], [0])

    def test_hourly_rain_across_files_keeps_period_and_lead_boundaries(self):
        self.add_coordinates()
        start = datetime(2024, 12, 24, tzinfo=timezone.utc)
        for hour in (-1, 0, 1, 2):
            time = start + timedelta(hours=hour)
            path = self.root / f'wrfout_d03_{time:%Y-%m-%d_%H_%M_%S}.nc'
            path.write_bytes(self.path.read_bytes())
            with Dataset(path, 'a') as dataset:
                dataset.SIMULATION_START_DATE = '2024-12-24_00:00:00'
                times = dataset.variables['Times']
                times.set_auto_chartostring(False)
                times[:] = stringtochar(np.array([
                    (time + timedelta(minutes=m)).strftime('%Y-%m-%d_%H:%M:%S')
                    for m in (0, 30)
                ], dtype='S19'))
                rain = dataset.createVariable('RAINNC', 'f4', ('Time', 'south_north', 'west_east'))
                rain.units = 'mm'
                rain[0], rain[1] = 100 + hour * 2, 101 + hour * 2
        source = SourceConfig('wrf', 'wrf', self.root, ('*.nc',), 'UTC', True,
                              variable_mapping=(('precipitation', VariableConfig('RAINNC', time_kind='cumulative')),))
        config = Config((source,), self.root / 'unused', 'UTC', timedelta(0),
                        points=(PointConfig('test', 87.01, 43.01),), elements=('precipitation',),
                        period=PeriodConfig(start, start + timedelta(hours=2)))
        with patch('wrf_evaluate.wrf.Dataset', wraps=Dataset) as opened:
            forecast = _wrf_series(source, config)['test', 'precipitation']
        self.assertEqual(opened.call_count, 3)  # Filename filtering excludes the previous hour.
        self.assertEqual(forecast.series.values, (2, 2))
        self.assertEqual(forecast.series.bounds[0], (start, start + timedelta(hours=1)))
        forecast = _wrf_series(replace(source, min_lead_hours=1), config)['test', 'precipitation']
        self.assertEqual(forecast.series.values, (2,))
        self.assertEqual(forecast.series.bounds[0][0], start + timedelta(hours=1))

    def test_previous_run_covers_start_until_new_run_reaches_minimum_lead(self):
        self.add_coordinates()
        period = PeriodConfig(datetime.fromisoformat('2024-12-01T12:00:00+08:00'),
                              datetime.fromisoformat('2024-12-01T15:00:00+08:00'))
        # Both batches cover 04:00–07:30 UTC, with two frames per file.
        # The new 00 UTC run is usable only from 06 UTC (14:00 Beijing).
        for layout in ('directories', 'linked_directory', 'linked_files'):
            with self.subTest(layout=layout):
                root = self.root / layout / 'wrf'
                root.mkdir(parents=True)
                archive = root.parent / 'archive'
                archive.mkdir()
                for directory, start, temperature in (
                    ('2024113012', '2024-11-30_12:00:00', 273.15),
                    ('2024120100', '2024-12-01_00:00:00', 283.15),
                ):
                    run = root / directory
                    old = directory == '2024113012'
                    if old and layout == 'linked_directory':
                        run.symlink_to(archive, target_is_directory=True)
                        (archive / 'back').symlink_to(root, target_is_directory=True)
                    else:
                        run.mkdir()
                    for hour in range(3, 8):
                        name = f'wrfout_d03_2024-12-01_{hour:02}_00_00'
                        path = archive / name if old and layout == 'linked_files' else run / name
                        path.write_bytes(self.path.read_bytes())
                        with Dataset(path, 'a') as dataset:
                            dataset.SIMULATION_START_DATE = start
                            times = dataset.variables['Times']
                            times.set_auto_chartostring(False)
                            times[:] = stringtochar(np.array([
                                f'2024-12-01_{hour:02}:{minute:02}:00' for minute in (0, 30)
                            ], dtype='S19'))
                            dataset.variables['T2'][:] = temperature
                        if old and layout == 'linked_files':
                            (run / name).symlink_to(path)
                source = SourceConfig('wrf', 'wrf', root, ('wrfout_d03*',), 'UTC', True,
                                      min_lead_hours=6)
                config = Config((source,), self.root / 'unused', 'Asia/Shanghai', timedelta(seconds=60),
                                points=(PointConfig('test', 87.01, 43.01),), period=period)
                # Keep the real scan/read/merge chain, including directory time precedence.
                with patch('wrf_evaluate.wrf.Dataset', wraps=Dataset) as opened:
                    forecast = _wrf_series(source, config)['test', 'temperature_2m']
                self.assertEqual(forecast.series.times,
                                 tuple(period.start + timedelta(minutes=30 * i) for i in range(7)))
                np.testing.assert_allclose(forecast.series.values, [0, 0, 0, 0, 10, 10, 10], atol=1e-4)
                self.assertEqual(len(forecast.runs), 2)
                self.assertEqual(opened.call_count, 6)  # Four old files, two eligible new files.

    def test_point_frames_keep_missing_values_and_relocate_moving_grid(self):
        self.add_coordinates()
        for longitude in (88.01, 88.99):
            with self.subTest(longitude=longitude):
                frames = list(iter_wrf_point_frames(self.path, ["T2"], "UTC", longitude, 43.01))
                self.assertIsNone(frames[1].values["T2"])
        with Dataset(self.path, "a") as dataset:
            dataset.variables["XLONG"][1] = [[86, 87, 88], [86, 87, 88]]
            dataset.variables["T2"][1, 0, 1] = 281
        with patch("wrf_evaluate.wrf.nearest_grid_point", wraps=nearest_grid_point) as locate:
            frames = list(iter_wrf_point_frames(self.path, ["T2"], "UTC", 87.01, 43.01))
        self.assertEqual(locate.call_count, 2)
        self.assertEqual([frame.column for frame in frames], [0, 1])
        self.assertEqual(frames[1].values["T2"], 281)

    def test_point_frames_close_file_when_stopped_early(self):
        self.add_coordinates()
        dataset = Dataset(self.path, mode="r")
        with patch("wrf_evaluate.wrf.Dataset", return_value=dataset):
            with closing(iter_wrf_point_frames(self.path, ["T2"], "UTC", 87.01, 43.01)) as frames:
                next(frames)
                self.assertTrue(dataset.isopen())
        self.assertFalse(dataset.isopen())

    def test_point_frames_recheck_domain_after_grid_moves_and_close_on_error(self):
        self.add_coordinates()
        with Dataset(self.path, "a") as dataset:
            dataset.variables["XLONG"][1] = [[90, 91, 92], [90, 91, 92]]
        dataset = Dataset(self.path, mode="r")
        with patch("wrf_evaluate.wrf.Dataset", return_value=dataset):
            frames = iter_wrf_point_frames(self.path, ["T2"], "UTC", 87.01, 43.01)
            next(frames)
            with self.assertRaisesRegex(WrfReadError, r"wrfout_test.*网格范围外"):
                next(frames)
        self.assertFalse(dataset.isopen())

    def test_point_frames_reject_invalid_variables_before_returning_data(self):
        self.add_coordinates()
        for names in ([], "T2", ["T2", "T2"], ["T2", "missing"], ["XLAT"], ["Times"], [1]):
            with self.subTest(names=names), self.assertRaises(WrfReadError):
                next(iter_wrf_point_frames(self.path, names, "UTC", 87.01, 43.01))

    def test_invalid_fields_and_indices_fail_clearly(self):
        for name, index in [("missing", 0), ("Times", 0), ("T2", -1), ("T2", 2), ("T2", True)]:
            with self.subTest(name=name, index=index), self.assertRaises(WrfReadError):
                read_wrf_field(self.path, name, index)

    def test_missing_malformed_or_unordered_times_are_rejected(self):
        for value in ["bad________________", "2024-12-23_21:00:00", "2024-12-23_22:00:00"]:
            with self.subTest(value=value):
                with Dataset(self.path, "a") as dataset:
                    times = dataset.variables["Times"]
                    times.set_auto_chartostring(False)
                    times[1] = np.array(list(value), dtype="S1")
                with self.assertRaises(WrfReadError):
                    read_wrf_header(self.path)
        with Dataset(self.path, "a") as dataset:
            dataset.renameVariable("Times", "OtherTimes")
        with self.assertRaisesRegex(WrfReadError, "Times"):
            read_wrf_header(self.path)

    def test_missing_model_start_time_is_not_invented(self):
        with Dataset(self.path, "a") as dataset:
            dataset.delncattr("SIMULATION_START_DATE")
        self.assertEqual(read_wrf_header(self.path).model_start_time.hour, 21)
        with Dataset(self.path, "a") as dataset:
            dataset.delncattr("START_DATE")
        self.assertIsNone(read_wrf_header(self.path).model_start_time)

    def test_nonnumeric_ground_field_and_noncharacter_times_report_file(self):
        with Dataset(self.path, "a") as dataset:
            dataset.createVariable("label", "S1", ("Time", "south_north", "west_east"))[:] = b"x"
            dataset.renameVariable("Times", "old_times")
            dataset.createVariable("Times", "i4", ("Time", "DateStrLen"))[:] = 1
        with self.assertRaisesRegex(WrfReadError, r"wrfout_test.*label"):
            read_wrf_field(self.path, "label")
        with self.assertRaisesRegex(WrfReadError, r"wrfout_test.*Times"):
            read_wrf_header(self.path)

    def test_inspect_uses_config_without_creating_output(self):
        config = self.root / "config.toml"
        config.write_text('''[output]
directory = "../result"
timezone = "UTC"
[sources.wrf]
kind = "wrf"
root = "."
patterns = ["wrfout_*"]
timezone = "UTC"
''', encoding="utf-8")
        before = set(self.root.iterdir())
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(["--config", str(config), "inspect"]), 0)
        self.assertIn("22:05:00+00:00", output.getvalue())
        self.assertIn("(2, 3)", output.getvalue())
        self.assertEqual(before, set(self.root.iterdir()))


if __name__ == "__main__":
    unittest.main()
