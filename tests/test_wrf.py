"""Exercise WRF NetCDF reading with small temporary files."""

from contextlib import closing, redirect_stdout
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
from wrf_evaluate.config import PeriodConfig
from wrf_evaluate.wrf import (
    WrfField, WrfReadError, iter_wrf_point_frames, read_wrf_field,
    read_wrf_fields, read_wrf_header, sample_wrf_station,
)


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
