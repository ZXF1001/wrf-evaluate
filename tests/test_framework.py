"""验证配置和只读扫描边界；不依赖真实气象数据或第三方库。"""

from contextlib import redirect_stderr, redirect_stdout
from datetime import timedelta
from io import StringIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import os

from wrf_evaluate.catalog import iter_source_files, scan_source
from wrf_evaluate.cli import main
from wrf_evaluate.config import ConfigError, load_config
from wrf_evaluate.output import create_run_directory


class FrameworkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / "raw").mkdir()
        self.path = self.root / "config.toml"
        self.text = '''[output]
directory = "result"
timezone = "UTC"
[sources.regional]
kind = "regional"
root = "raw"
files = []
timezone = "UTC"
'''
        self.grid_text = self.text.replace('kind = "regional"', 'kind = "wrf"').replace(
            'files = []', 'patterns = ["20??/excel/*.xlsx", "20??/excel/*.xls"]',
        )
        self.write_config(self.text)

    def write_config(self, text):
        self.path.write_text(text, encoding="utf-8")

    def test_output_and_display_defaults_allow_omitted_tables(self):
        self.write_config(self.text[self.text.index('[sources.regional]'):])
        config = load_config(self.path)
        self.assertEqual(config.output_dir, self.root / 'output')
        self.assertEqual(config.output_name, 'evaluate')
        self.assertEqual(config.display_timezone, 'Asia/Shanghai')
        self.assertEqual(config.display_name('temperature_2m'), '2m气温')
        self.assertFalse(config.output_dir.exists())
        self.write_config(self.text + '\n[display_names]\ntemperature_2m = "自定义温度"\n')
        self.assertEqual(load_config(self.path).display_name('temperature_2m'), '自定义温度')
        for table in ('[output]\ndirectory = ""', '[output]\ntimezone = ""',
                      '[display_names]\ntemperature_2m = 2', '[display_names]\nunknown = "气温"'):
            self.write_config(table + '\n' + self.text[self.text.index('[sources.regional]'):])
            with self.subTest(table=table), self.assertRaises(ConfigError):
                load_config(self.path)

    def test_grid_variable_mapping_defaults_overrides_and_errors(self):
        for kind, default in (('wrf', 'T2'), ('cldas', 'TAIR')):
            base = self.grid_text.replace('kind = "wrf"', f'kind = "{kind}"')
            self.write_config(base)
            self.assertEqual(load_config(self.path).sources[0].variable('temperature_2m').variable, default)
            self.write_config(base + '\n[sources.regional.variables.temperature_2m]\nvariable = " CUSTOM_TEMP "\n')
            self.assertEqual(load_config(self.path).sources[0].variable('temperature_2m').variable, 'CUSTOM_TEMP')
            for setting in ('temperature_2m = ""', 'temperature_2m = 1', 'typo = "T"'):
                self.write_config(base + '\n[sources.regional.variables]\n' + setting)
                with self.subTest(kind=kind, setting=setting), self.assertRaises(ConfigError):
                    load_config(self.path)

    def test_time_tolerance_default_override_and_invalid_values(self):
        self.assertEqual(load_config(self.path).time_tolerance, timedelta(minutes=1))
        for seconds in (0, 30, 120):
            self.write_config(self.text + f'\n[matching]\ntime_tolerance_seconds = {seconds}\n')
            self.assertEqual(load_config(self.path).time_tolerance, timedelta(seconds=seconds))
        for value in ('-1', 'true', '"60"', '1.5', 'inf', 'nan', str(10**30)):
            with self.subTest(value=value):
                self.write_config(self.text + f'\n[matching]\ntime_tolerance_seconds = {value}\n')
                with self.assertRaises(ConfigError):
                    load_config(self.path)

    def test_elements_and_source_metadata_errors_are_reported_by_check(self):
        for selection in ('[]', '["unknown"]', '["temperature_2m", "temperature_2m"]', '"temperature_2m"'):
            self.write_config(f'elements = {selection}\n' + self.text)
            with self.subTest(selection=selection), self.assertRaisesRegex(ConfigError, 'elements'):
                load_config(self.path)
        cases = (
            ('cldas', 'precipitation = {variable="PRCP", time_kind="sum", interval="1h", timestamp="end"}'),
            ('cldas', 'relative_humidity_2m = {variable="QAIR", units="kg/kg", product="SHU"}'),
            ('wrf', 'surface_shortwave_down = {variable="SWDOWN", units="W/m2", time_kind="cumulative"}'),
        )
        for kind, mapping in cases:
            self.write_config(self.grid_text.replace('kind = "wrf"', f'kind = "{kind}"')
                              + '\n[sources.regional.variables]\n' + mapping)
            with self.subTest(kind=kind, mapping=mapping), self.assertRaises(ConfigError):
                load_config(self.path)

    def test_configured_period_requires_timezone_and_increasing_bounds(self):
        from datetime import datetime, timezone

        self.assertIsNone(load_config(self.path).period)
        self.write_config(self.text + '\n[period]\nstart = "2024-12-24T06:00:00+08:00"\nend = "2024-12-24T14:00:00+08:00"\n')
        period = load_config(self.path).period
        self.assertEqual(period.start, datetime(2024, 12, 24, 6, tzinfo=timezone(timedelta(hours=8))))
        self.assertEqual(period.end - period.start, timedelta(hours=8))
        for start, end in (
            ('"2024-12-24T06:00:00"', '"2024-12-24T14:00:00+08:00"'),
            ('"bad"', '"2024-12-24T14:00:00+08:00"'),
            ('"2024-12-24T14:00:00+08:00"', '"2024-12-24T06:00:00+08:00"'),
            ('"2024-12-24T06:00:00+08:00"', '"2024-12-24T06:00:00+08:00"'),
            ('1', '"2024-12-24T14:00:00+08:00"'),
        ):
            text = self.text + f'\n[period]\nstart = {start}\nend = {end}\n'
            with self.subTest(start=start, end=end), self.assertRaises(ConfigError):
                self.write_config(text)
                load_config(self.path)
        self.write_config(self.text + '\n[period]\nstart = "2024-12-24T06:00:00+08:00"\n')
        with self.assertRaises(ConfigError):
            load_config(self.path)

    def test_precipitation_thresholds_are_optional_and_validated(self):
        self.assertEqual(load_config(self.path).precipitation_thresholds_mm, ())
        self.write_config(self.text + '\n[metrics]\nprecipitation_thresholds_mm = [0.1, 1, 10]\n')
        self.assertEqual(load_config(self.path).precipitation_thresholds_mm, (0.1, 1.0, 10.0))
        for value in ('[0]', '[-1]', '[1, 1]', '[true]', '["1"]', '[nan]'):
            with self.subTest(value=value):
                self.write_config(self.text + f'\n[metrics]\nprecipitation_thresholds_mm = {value}\n')
                with self.assertRaises(ConfigError):
                    load_config(self.path)

    def test_points_are_optional_and_read_directly_from_config(self):
        self.assertEqual(load_config(self.path).points, ())
        self.write_config(self.text + '\n[[points]]\nname = "水西沟"\nlongitude = 87.4897\nlatitude = 43.4733\nstation_id = "Y5012"\n'
                          + '\n[[points]]\nname = "示例点"\nlongitude = 88\nlatitude = 44\n')
        points = load_config(self.path).points
        self.assertEqual([(p.name, p.longitude, p.latitude, p.station_id) for p in points], [
            ("水西沟", 87.4897, 43.4733, "Y5012"), ("示例点", 88.0, 44.0, None),
        ])
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(["--config", str(self.path), "inspect"]), 0)
        self.assertIn("配置点位: 2 个", output.getvalue())
        self.assertIn("水西沟: 经度 87.4897，纬度 43.4733", output.getvalue())
        self.assertFalse((self.root / "result").exists())

    def test_point_coordinates_and_names_are_validated(self):
        point = '\n[[points]]\nname = "站点"\nlongitude = 87\nlatitude = 43\n'
        cases = [point.replace('name = "站点"', 'name = " "'),
                 point.replace('latitude = 43', ''),
                 point + 'typo = 1\n', point + point,
                 point + point.replace('"站点"', '" 站点 "')]
        cases.extend(point + f'station_id = {value}\n' for value in ('" "', '123', 'true'))
        for field, original, bad_values in (
            ("longitude", "87", ("181", "-181", "nan", "inf", "true", '\"87\"')),
            ("latitude", "43", ("91", "-91", "nan", "-inf", "false", '\"43\"')),
        ):
            cases.extend(point.replace(f"{field} = {original}", f"{field} = {value}") for value in bad_values)
        for invalid in cases:
            with self.subTest(point=invalid):
                self.write_config(self.text + invalid)
                with self.assertRaises(ConfigError):
                    load_config(self.path)
        self.write_config(self.text + '\n[points]\nname = "站点"\n')
        with self.assertRaisesRegex(ConfigError, "points"):
            load_config(self.path)
        self.write_config(self.text + '\n[stations]\nlocation_file = "unused.xlsx"\n')
        with self.assertRaisesRegex(ConfigError, "stations"):
            load_config(self.path)

    def test_column_mapping_default_custom_and_invalid_config(self):
        self.assertEqual(dict(load_config(self.path).sources[0].column_mapping)['time'], '观测时间')
        columns = '''
[sources.regional.variables]
station_id = {variable = "站号"}
time = {variable = "时间"}
temperature_2m = {variable = " 温度℃ "}
relative_humidity_2m = {variable = "湿度%"}
wind_speed_10m = {variable = "十分钟平均风速"}
precipitation = {variable = "小时雨量", time_kind = "sum", interval = "1h", timestamp = "end"}
'''
        self.write_config(self.text + columns)
        mapping = dict(load_config(self.path).sources[0].column_mapping)
        self.assertEqual(mapping["temperature_2m"], "温度℃")
        self.assertEqual(mapping["wind_speed_10m"], "十分钟平均风速")
        self.assertEqual(mapping["precipitation"], "小时雨量")
        self.write_config(self.text + '\n[sources.regional.variables.temperature_2m]\nvariable = "温度℃"\n')
        mapping = dict(load_config(self.path).sources[0].column_mapping)
        self.assertEqual(mapping['temperature_2m'], '温度℃')
        self.assertEqual(mapping['time'], '观测时间')
        for invalid in [
            columns.replace('variable = "时间"', 'variable = "站号"'),
            columns.replace('variable = "时间"', 'variable = " "'),
            columns.replace('variable = "时间"', 'variable = 1'),
            columns + 'typo = "气温"\n',
        ]:
            with self.subTest(columns=invalid):
                self.write_config(self.text + invalid)
                with self.assertRaises(ConfigError):
                    load_config(self.path)
        self.write_config(self.text.replace('kind = "regional"', 'kind = "wrf"') + columns)
        with self.assertRaisesRegex(ConfigError, "未知字段"):
            load_config(self.path)

    def test_station_files_only_use_explicit_paths_without_scanning(self):
        selected = self.root / "raw" / "nested" / "任意名称.xlsx"
        selected.parent.mkdir()
        selected.write_bytes(b"selected")
        second = self.root / "raw" / "older.xls"
        second.write_bytes(b"old")
        (self.root / "raw" / "Y5012_unselected.xlsx").write_bytes(b"unselected")
        for kind in ("regional", "national"):
            with self.subTest(kind=kind):
                self.write_config(self.text.replace('kind = "regional"', f'kind = "{kind}"').replace(
                    'files = []', 'files = ["nested/任意名称.xlsx", "older.xls"]',
                ))
                with patch("wrf_evaluate.catalog.os.scandir", side_effect=AssertionError("不得扫描目录")):
                    source = load_config(self.path).sources[0]
                    self.assertEqual(source.files, (selected.resolve(), second.resolve()))
                    self.assertEqual(list(iter_source_files(source)), list(source.files))
                    result = scan_source(source)
                self.assertEqual(source.patterns, ())
                self.assertEqual((result.file_count, result.total_bytes), (2, 11))
                self.assertEqual(result.examples, ("nested/任意名称.xlsx", "older.xls"))
        self.assertEqual(selected.read_bytes(), b"selected")

    def test_empty_station_files_do_not_fall_back_to_scan(self):
        (self.root / "raw" / "station.xlsx").write_bytes(b"not selected")
        output = StringIO()
        with patch("wrf_evaluate.catalog.os.scandir", side_effect=AssertionError("不得扫描目录")):
            self.assertEqual(list(iter_source_files(load_config(self.path).sources[0])), [])
            with redirect_stdout(output):
                self.assertEqual(main(["--config", str(self.path), "check"]), 0)
                self.assertEqual(main(["--config", str(self.path), "scan"]), 0)
        self.assertIn("尚未选入实测", output.getvalue())
        self.assertNotIn("patterns", output.getvalue())

    def test_station_files_reject_missing_invalid_and_duplicate_paths(self):
        (self.root / "raw" / "chosen.xlsx").touch()
        (self.root / "raw" / "folder.xlsx").mkdir()
        for files in (
            '"chosen.xlsx"', '[1]', '[""]', '["missing.xlsx"]', '["folder.xlsx"]',
            '["../outside.xlsx"]', '["/outside.xlsx"]', '["C:/outside.xlsx"]',
            '["*.xlsx"]', '["station?.xlsx"]', '["notes.csv"]', '["sub//chosen.xlsx"]',
            '["./chosen.xlsx"]', "['sub\\chosen.xlsx']", '["chosen.xlsx", "chosen.xlsx"]',
        ):
            with self.subTest(files=files):
                self.write_config(self.text.replace("files = []", f"files = {files}"))
                with self.assertRaisesRegex(ConfigError, "files"):
                    load_config(self.path)
        for text in (
            self.text.replace("files = []", ""),
            self.text.replace("files = []", 'patterns = ["*.xlsx"]'),
            self.text + 'patterns = ["*.xlsx"]\n',
            self.grid_text + 'files = []\n',
        ):
            with self.subTest(config=text):
                self.write_config(text)
                with self.assertRaisesRegex(ConfigError, "files"):
                    load_config(self.path)

    def test_disabled_station_source_may_have_missing_files(self):
        self.write_config(self.grid_text + '''
[sources.unused]
kind = "national"
root = "not_available"
files = ["missing.xlsx"]
timezone = "Asia/Shanghai"
enabled = false
''')
        source = load_config(self.path).sources[1]
        self.assertFalse(source.enabled)
        self.assertEqual(list(iter_source_files(source)), [])
        self.assertEqual(scan_source(source).file_count, 0)

    def test_paths_are_relative_to_config_and_commands_create_nothing(self):
        config = load_config(self.path)
        self.assertEqual(config.sources[0].root, (self.root / "raw").resolve())
        self.assertEqual(config.output_dir, (self.root / "result").resolve())
        with redirect_stdout(StringIO()):
            self.assertEqual(main(["--config", str(self.path), "check"]), 0)
            self.assertEqual(main(["--config", str(self.path), "scan"]), 0)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["config.toml", "raw"])
        self.assertEqual(list((self.root / "raw").iterdir()), [])

    def test_output_name_is_a_single_safe_directory_component(self):
        self.write_config(self.text.replace('directory = "result"', 'directory = "output"\nname = "trial"'))
        config = load_config(self.path)
        self.assertEqual(config.output_name, 'trial')
        for name in ('', ' ', '.', '..', 'a/b', 'a\\b', 'name:time', 'trailing.'):
            text = self.text.replace('directory = "result"', f'directory = "result"\nname = {name!r}')
            self.write_config(text)
            with self.subTest(name=name), self.assertRaises(ConfigError):
                load_config(self.path)

    def test_runs_keep_loaded_config_and_do_not_overwrite_previous_results(self):
        config = load_config(self.path)
        original = self.path.read_bytes()
        self.write_config(self.text + '\n# edited after loading\n')
        first = create_run_directory(config)
        (first / 'result.csv').write_text('previous result', encoding='utf-8')
        second = create_run_directory(config)
        self.assertNotEqual(first, second)
        for directory in (first, second):
            self.assertEqual(directory.parent, config.output_dir)
            self.assertTrue(directory.name.startswith(config.output_name + '_'))
            self.assertEqual((directory / 'config.toml').read_bytes(), original)
        self.assertEqual((first / 'result.csv').read_text(encoding='utf-8'), 'previous result')

    def test_run_name_collision_creates_unique_directory_without_overwrite(self):
        config = load_config(self.path)
        with patch('wrf_evaluate.output.datetime') as clock:
            clock.now.return_value.strftime.return_value = '20261115_120000_000001'
            first = create_run_directory(config)
            second = create_run_directory(config)
        self.assertNotEqual(first, second)
        self.assertTrue(second.name.endswith('_001'))
        self.assertEqual(list(first.iterdir()), [first / 'config.toml'])
        self.assertEqual((first / 'config.toml').read_bytes(), self.path.read_bytes())
        self.assertEqual((second / 'config.toml').read_bytes(), self.path.read_bytes())

    def test_rejects_invalid_configuration(self):
        cases = [
            self.text.replace('timezone = "UTC"', 'timezone = "Not/A_Timezone"'),
            self.text.replace('timezone = "UTC"', 'timezone = "../UTC"'),
            self.text + 'enabled = "false"\n',
            self.text + 'unknown_key = 1\n',
            self.grid_text.replace('patterns = ["20??/excel/*.xlsx", "20??/excel/*.xls"]', 'patterns = []'),
            self.text.replace('directory = "result"', 'directory = "raw/results"'),
            self.text.replace('directory = "result"', 'directory = "."'),
            self.text.replace('root = "raw"', 'root = "missing"'),
            self.grid_text.replace('20??/excel/*.xlsx', '../*.xlsx'),
            self.grid_text.replace('20??/excel/*.xlsx', '**/*.xlsx'),
            self.grid_text.replace('20??/excel/*.xlsx', '20??//*.xlsx'),
            self.grid_text.replace('20??/excel/*.xlsx', './*.xlsx'),
            self.grid_text.replace('20??/excel/*.xlsx', 'C:/data/*.xlsx'),
            self.text + 'enabled = false\n',
        ]
        for text in cases:
            with self.subTest(text=text):
                self.write_config(text)
                with self.assertRaises(ConfigError):
                    load_config(self.path)

    def test_disabled_source_may_have_missing_directory(self):
        self.write_config(self.text + '''
[sources.ec]
kind = "ec"
root = "not_available"
patterns = ["*.grib2"]
timezone = "UTC"
enabled = false
''')
        config = load_config(self.path)
        self.assertFalse(config.sources[1].enabled)
        self.assertEqual(scan_source(config.sources[1]).file_count, 0)

    def test_scan_counts_once_filters_archives_and_excludes_national(self):
        files = {
            "2024/excel/b.xls": b"123",
            "2023/excel/a.xlsx": b"12",
            "2024/excel/c.xlsx": b"1234",
            "extra/2024/excel/too-deep.xlsx": b"should be excluded",
            "2024.zip": b"archive",
            "national/2024/01/a.xlsx": b"other source",
        }
        for name, data in files.items():
            path = self.root / "raw" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        # 重叠 pattern 也只计数一次。
        self.write_config(self.grid_text.replace('"20??/excel/*.xls"]', '"20??/excel/*.xls", "2024/excel/*.xlsx"]'))
        result = scan_source(load_config(self.path).sources[0], example_limit=2)
        self.assertEqual((result.file_count, result.total_bytes), (3, 9))
        self.assertEqual(result.examples, ("2023/excel/a.xlsx", "2024/excel/b.xls"))

    def test_filename_pattern_includes_root_and_nested_files(self):
        (self.root / "raw" / "one.nc").write_bytes(b"1")
        (self.root / "raw" / "sub").mkdir()
        (self.root / "raw" / "sub" / "two.nc").write_bytes(b"22")
        self.write_config(self.grid_text.replace('["20??/excel/*.xlsx", "20??/excel/*.xls"]', '["*.nc"]'))
        result = scan_source(load_config(self.path).sources[0])
        self.assertEqual((result.file_count, result.total_bytes), (2, 3))

    def test_scan_does_not_enter_directories_excluded_by_patterns(self):
        self.write_config(self.grid_text)
        excluded = self.root / "raw" / "national"
        excluded.mkdir()
        allowed = self.root / "raw" / "2024" / "excel"
        allowed.mkdir(parents=True)
        (allowed / "station.xlsx").write_bytes(b"data")
        scandir = os.scandir

        def checked_scandir(path):
            if Path(path) == excluded:
                raise PermissionError("unrelated directory must not be visited")
            return scandir(path)

        with patch("wrf_evaluate.catalog.os.scandir", side_effect=checked_scandir):
            result = scan_source(load_config(self.path).sources[0])
        self.assertEqual(result.file_count, 1)

    def test_cli_errors_are_readable_and_return_nonzero(self):
        output = StringIO()
        with redirect_stderr(output):
            code = main(["--config", str(self.path), "scan", "--source", "typo"])
        self.assertEqual(code, 2)
        self.assertIn("typo", output.getvalue())
        self.assertNotIn("Traceback", output.getvalue())


if __name__ == "__main__":
    unittest.main()
