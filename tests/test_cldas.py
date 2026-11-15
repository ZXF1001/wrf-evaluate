"""用小型 NetCDF 验证有效时间、坐标方向、缺测值和命令入口。"""

from contextlib import redirect_stdout
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import numpy as np
from netCDF4 import Dataset

from wrf_evaluate.cldas import CldasReadError, read_cldas_field, read_cldas_header, read_cldas_point, read_cldas_points
from wrf_evaluate.cli import main


class CldasReaderTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.path = self.root / "Z_NAFP_C_BABJ_20241226000900_P_CLDAS_NRT_ASI_0P0625_HOR-TMP-2024122400.nc"
        self.make_file()

    def make_file(self, file_format="NETCDF4"):
        with Dataset(self.path, "w", format=file_format) as dataset:
            dataset.createDimension("LAT", 2)
            dataset.createDimension("LON", 3)
            dataset.createVariable("LAT", "f4", ("LAT",))[:] = [44, 43]
            dataset.createVariable("LON", "f4", ("LON",))[:] = [86, 87, 88]
            field = dataset.createVariable("TAIR", "f4", ("LAT", "LON"), fill_value=-999)
            field.units = "K"
            field[:] = [[270, -999, np.nan], [271, np.inf, 273]]

    def test_valid_time_ignores_production_date_and_uses_source_timezone(self):
        for file_format in ("NETCDF4", "NETCDF3_CLASSIC"):
            with self.subTest(file_format=file_format):
                self.make_file(file_format)
                header = read_cldas_header(self.path)
                self.assertEqual(header.time, datetime(2024, 12, 24, tzinfo=timezone.utc))
                self.assertEqual(header.product, "TMP")
                self.assertEqual(header.variables, {"TAIR"})
                self.assertEqual(header.shape, (2, 3))
                np.testing.assert_array_equal(header.latitudes, [44, 43])
                np.testing.assert_array_equal(header.longitudes, [86, 87, 88])
        local = read_cldas_header(self.path, "Asia/Shanghai")
        self.assertEqual(local.time.astimezone(timezone.utc), datetime(2024, 12, 23, 16, tzinfo=timezone.utc))

    def test_field_preserves_units_missing_values_and_axis_order(self):
        field = read_cldas_field(self.path, "TAIR")
        self.assertEqual(field.units, "K")
        self.assertEqual(field.values.dtype, np.dtype("float32"))
        np.testing.assert_array_equal(field.values.mask, [[False, True, True], [False, True, False]])
        self.assertEqual(field.values[0, 0], 270)
        self.assertEqual(field.values[1, 2], 273)
        # 数据已载入且文件已关闭，可以删除源文件后继续使用返回数组。
        self.path.unlink()
        self.assertEqual(field.values[1, 0], 271)

    def test_point_reads_nearest_value_without_changing_units_or_time(self):
        point = read_cldas_point(self.path, "TAIR", 87.9, 43.1, "Asia/Shanghai")
        self.assertEqual((point.grid_point.row, point.grid_point.column), (1, 2))
        self.assertEqual(point.value, 273)
        self.assertEqual(point.units, "K")
        self.assertEqual(point.path, self.path)
        self.assertEqual(point.time.utcoffset().total_seconds(), 8 * 3600)
        self.assertGreater(point.grid_point.distance_km, 0)
        self.path.unlink()  # 返回时文件已关闭。

    def test_point_preserves_missing_and_rejects_outside_or_wrong_variable(self):
        for lon, lat in ((87, 44), (88, 44), (87, 43)):
            self.assertIsNone(read_cldas_point(self.path, "TAIR", lon, lat).value)
        for lon, lat in ((85.99, 43), (86, 44.01)):
            with self.assertRaisesRegex(CldasReadError, "范围外"):
                read_cldas_point(self.path, "TAIR", lon, lat)
        for name in ("missing", "LAT"):
            with self.assertRaises(CldasReadError):
                read_cldas_point(self.path, name, 86, 43)

    def test_invalid_filename_date_timezone_or_field_are_rejected(self):
        for name in ("2024122400.nc", "HOR-TMP-2024132400.nc", "HOR-TMP-2024122424.nc", "HOR-TMP-202412240000.nc"):
            with self.subTest(name=name), self.assertRaises(CldasReadError):
                read_cldas_header(self.root / name)
        with self.assertRaisesRegex(CldasReadError, "时区"):
            read_cldas_header(self.path, "Not/A_Timezone")
        for name in ("missing", "LAT"):
            with self.subTest(name=name), self.assertRaises(CldasReadError):
                read_cldas_field(self.path, name)

    def test_missing_duplicate_unordered_and_nonfinite_coordinates_are_rejected(self):
        for coordinates in ([86, 86, 88], [86, 88, 87], [86, np.nan, 88]):
            with self.subTest(coordinates=coordinates):
                with Dataset(self.path, "a") as dataset:
                    dataset.variables["LON"][:] = coordinates
                with self.assertRaisesRegex(CldasReadError, "LON"):
                    read_cldas_header(self.path)
        self.make_file()
        with Dataset(self.path, "a") as dataset:
            dataset.renameVariable("LAT", "latitude")
        with self.assertRaisesRegex(CldasReadError, "LAT"):
            read_cldas_header(self.path)

    def test_transposed_field_is_rejected_instead_of_silently_mislocated(self):
        with Dataset(self.path, "a") as dataset:
            dataset.renameVariable("TAIR", "original")
            dataset.createVariable("TAIR", "f4", ("LON", "LAT"))[:] = 270
        self.assertNotIn("TAIR", read_cldas_header(self.path).variables)
        with self.assertRaisesRegex(CldasReadError, "LAT/LON"):
            read_cldas_field(self.path, "TAIR")

    def test_cldas_only_inspect_works_without_creating_output(self):
        config = self.root / "config.toml"
        config.write_text('''[output]
directory = "../result"
timezone = "UTC"
[sources.cldas]
kind = "cldas"
root = "."
patterns = ["*.nc"]
timezone = "UTC"
''', encoding="utf-8")
        before = set(self.root.iterdir())
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(["--config", str(config), "inspect"]), 0)
        self.assertIn("TMP (TAIR)", output.getvalue())
        self.assertIn("2024-12-24T00:00:00+00:00", output.getvalue())
        self.assertIn("(2, 3)", output.getvalue())
        self.assertEqual(before, set(self.root.iterdir()))

    def test_string_fields_are_rejected_with_file_context(self):
        with Dataset(self.path, "a") as dataset:
            dataset.createVariable("label", str, ("LAT", "LON"))
        self.assertNotIn("label", read_cldas_header(self.path).variables)
        with self.assertRaises(CldasReadError) as error:
            read_cldas_field(self.path, "label")
        self.assertIn(str(self.path), str(error.exception))


class CldasPointIOTests(unittest.TestCase):
    """验证点读取次数，不依赖临时 NetCDF 文件。"""

    def setUp(self):
        self.path = Path("Z_NAFP_C_BABJ_20241226000900_P_CLDAS_NRT_ASI_0P0625_HOR-TMP-2024122400.nc")

    def test_point_reads_only_one_weather_value(self):
        dataset = MagicMock()
        dataset.__enter__.return_value = dataset
        variable = MagicMock(dimensions=("LAT", "LON"), dtype=np.dtype("f4"), units="K")
        dataset.variables = {"TAIR": variable}
        variable.__getitem__.return_value = np.float32(271)
        with patch("wrf_evaluate.cldas.Dataset", return_value=dataset), patch(
            "wrf_evaluate.cldas._read_axis", side_effect=[np.array([86, 87, 88]), np.array([44, 43])],
        ):
            read_cldas_point(self.path, "TAIR", 86, 43)
        variable.__getitem__.assert_called_once_with((1, 0))
        dataset.__exit__.assert_called_once()

    def test_multiple_point_values_and_variables_share_open_and_duplicate_grid_reads(self):
        dataset = MagicMock()
        dataset.__enter__.return_value = dataset
        fields = {
            name: MagicMock(dimensions=("LAT", "LON"), dtype=np.dtype("f4"), units=unit)
            for name, unit in (("TAIR", "K"), ("RH", "%"))
        }
        dataset.variables = fields
        fields["TAIR"].__getitem__.side_effect = lambda index: np.float32(270 + index[0])
        fields["RH"].__getitem__.side_effect = lambda index: np.float32(70 + index[0])
        points = ((86, 44), (86.01, 43.99), (87, 43))
        with patch("wrf_evaluate.cldas.Dataset", return_value=dataset), patch(
            "wrf_evaluate.cldas._read_axis", side_effect=[np.array([86, 87, 88]), np.array([44, 43])],
        ):
            samples = read_cldas_points(self.path, ("TAIR", "RH"), points)
        dataset.__exit__.assert_called_once()
        self.assertEqual(fields["TAIR"].__getitem__.call_count, 2)
        self.assertEqual(fields["RH"].__getitem__.call_count, 2)
        self.assertEqual([sample.value for sample in samples["TAIR"]], [270, 270, 271])
        self.assertEqual([sample.units for sample in samples["RH"]], ["%"] * 3)


if __name__ == "__main__":
    unittest.main()
