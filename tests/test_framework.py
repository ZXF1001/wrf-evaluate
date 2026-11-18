"""验证配置和只读扫描边界；不依赖真实气象数据或第三方库。"""
from contextlib import redirect_stderr
from datetime import timedelta
from io import StringIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import os
from wrf_evaluate.catalog import scan_source
from wrf_evaluate.cli import main
from wrf_evaluate.config import ConfigError, load_config
from wrf_evaluate.output import create_run_directory


class ConfigurationTests(unittest.TestCase):
    """配置语义使用内存文件内容；路径扫描和输出仍由下面的集成测试覆盖。"""

    run_text = '''source_config = "sources.toml"
[[station]]
name = "测试站"
longitude = 87
latitude = 43
excel.files = ["station.xls"]
excel.time_format = "%Y-%m-%d %H:%M:%S"
excel.missing_values = [-9999, "M"]
[[plots]]
element = "temperature_2m"
type = "map"
layout = "timeline"
difference_limit = 5
difference_cmap = "RdBu_r"
'''
    stable = '''[sources.wrf]
kind = "wrf"
root = "raw"
patterns = ["wrfout*"]
timezone = "UTC"
lead_hours = [6, 168]
'''

    def load(self, run=None, stable=None):
        contents = {'run.toml': (self.run_text if run is None else run).encode('utf-8'),
                    'sources.toml': (self.stable if stable is None else stable).encode('utf-8')}
        with patch.object(Path, 'read_bytes', lambda path: contents[path.name]), \
                patch.object(Path, 'is_file', lambda path: path.name != 'absent.xls'), \
                patch.object(Path, 'is_dir', return_value=True):
            return load_config('run.toml')

    def test_station_parsing_options_map_layout_and_default_output(self):
        config = self.load()
        station, = config.points
        self.assertEqual(station.excel.time_format, '%Y-%m-%d %H:%M:%S')
        self.assertEqual(station.excel.missing_values, (-9999, 'M'))
        self.assertEqual(station.time_tolerance, timedelta(seconds=60))
        self.assertEqual(config.output_dir.name, 'output')
        self.assertEqual((config.sources[0].min_lead_hours, config.sources[0].max_lead_hours), (6, 168))
        self.assertEqual(config.plots[0].options.layout, 'timeline')
        self.assertEqual(config.plots[0].options.difference_limit, 5)
        self.assertEqual(self.load(self.run_text.replace('layout = "timeline"', 'layout = "separate"')).plots[0].options.layout, 'separate')

    def test_invalid_station_and_map_options_are_rejected(self):
        invalid = (
            self.run_text.replace('[-9999, "M"]', '[true]'),
            self.run_text.replace('[-9999, "M"]', '[inf]'),
            self.run_text.replace('[-9999, "M"]', '"M"'),
            self.run_text.replace('%Y-%m-%d %H:%M:%S', '%H:%M'),
            self.run_text.replace('difference_limit = 5', 'difference_limit = 0'),
            self.run_text.replace('difference_cmap = "RdBu_r"', 'difference_cmap = "unknown"'),
            self.run_text.replace('layout = "timeline"', 'layout = "unknown"'),
            self.run_text.replace('excel.files = ["station.xls"]', ''),
            self.run_text.replace('source_config = "sources.toml"', ''),
        )
        for text in invalid:
            with self.subTest(text=text), self.assertRaises(ConfigError):
                self.load(text)
        for source in (
            self.stable.replace('kind = "wrf"', 'kind = "unknown"'),
            self.stable.replace('lead_hours = [6, 168]', 'lead_hours = [168, 6]'),
        ):
            with self.subTest(source=source), self.assertRaises(ConfigError):
                self.load(stable=source)


class FrameworkTests(unittest.TestCase):

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / "raw").mkdir()
        self.path = self.root / "config.toml"
        self.source_path = self.root / "sources.toml"
        self.source_text = '''[sources.wrf]
kind = "wrf"
root = "raw"
patterns = ["20????????/wrfout_d03_*"]
timezone = "UTC"
'''
        self.source_path.write_text(self.source_text, encoding="utf-8")
        self.text = '''source_config = "sources.toml"
[[plots]]
element = "temperature_2m"
type = "map"
[output]
directory = "result"
timezone = "UTC"
'''
        self.write_config(self.text)

    def write_config(self, text):
        self.path.write_text(text, encoding='utf-8')

    def test_split_sources_resolve_relative_paths_and_snapshot_loaded_bytes(self):
        folder = self.root / 'definitions'
        folder.mkdir()
        source_path = folder / 'data.toml'
        source_text = '[sources.wrf]\nkind = "wrf"\nroot = "../raw"\npatterns = ["wrfout*"]\ntimezone = "UTC"\n[elements.temperature_2m]\nname = "测试气温"\nsources.wrf = {variable = "温度", units = "degC"}\n'
        source_path.write_text(source_text, encoding='utf-8')
        (self.root / 'station.xls').touch()
        run_text = 'source_config = "definitions/data.toml"\n[[station]]\nname = "测试站点"\nlongitude = 87\nlatitude = 43\ntime_tolerance_seconds = 30\nexcel.files = ["station.xls"]\n[[plots]]\nelement = "temperature_2m"\ntype = "series"\ny_limits = [-10, 10]\n[[plots]]\nelement = "temperature_2m"\ntype = "series"\ndpi = 200\n[[metrics]]\nelement = "temperature_2m"\n'
        self.write_config(run_text)
        expected_run = self.path.read_bytes()
        expected_source = source_path.read_bytes()
        config = load_config(self.path)
        self.assertEqual(config.sources[0].root, self.root / 'raw')
        self.assertEqual(config.sources[0].variable('temperature_2m').variable, '温度')
        self.assertEqual(config.output_dir, self.root / 'output')
        self.assertEqual(config.display_name('temperature_2m'), '测试气温')
        jobs = config.plot_jobs('series')
        self.assertEqual(len(jobs), 2)
        self.assertEqual(jobs[0][2].y_limits, (-10, 10))
        self.assertEqual(jobs[1][2].dpi, 200)
        self.assertEqual(config.fields_for('map'), ())
        self.assertEqual(config.points[0].time_tolerance.total_seconds(), 30)
        source_path.write_text('# changed after loading', encoding='utf-8')
        result = create_run_directory(config)
        self.assertEqual((result / 'config.toml').read_bytes(), expected_run)
        self.assertEqual((result / 'data.toml').read_bytes(), expected_source)

    def test_tasks_reject_disabled_switches_duplicates_and_mixed_config(self):
        source_path = self.root / 'sources.toml'
        source_path.write_text(self.source_text, encoding='utf-8')
        base = 'source_config = "sources.toml"\n'
        for content in ('', '[[plots]]\nelement="temperature_2m"\ntype="series"\nenabled=false', '[[plots]]\nelement="temperature_2m"\ntype=[]', '[[plots]]\nelement="temperature_2m"\nkind="series"', '[[plots]]\nelement="temperature_2m"\ntype="series"\nkind="map"', '[[plots]]\nelement="unknown"\ntype="series"', '[[metrics]]\nelement="temperature_2m"\ntype="series"', '[[metrics]]\nelement="temperature_2m"\ntime_tolerance_seconds=30', '[[metrics]]\nelement="temperature_2m"\nprecipitation_thresholds_mm=[1]', '[[metrics]]\nelement="temperature_2m"\n[[metrics]]\nelement="temperature_2m"', '[sources.extra]\nkind="wrf"', '[elements.temperature_2m]\noutputs=["series"]'):
            self.write_config(base + content)
            with self.subTest(content=content), self.assertRaises(ConfigError):
                load_config(self.path)
        self.write_config(base + '[[metrics]]\nelement="precipitation"\nprecipitation_thresholds_mm=[1,5]')
        config = load_config(self.path)
        self.assertEqual(config.plot_jobs('series'), ())
        self.assertEqual(config.plot_jobs('map'), ())
        self.assertEqual(config.fields_for('metrics'), ('precipitation',))

    def test_station_excel_rules_and_tolerances_are_independent(self):
        source = self.root / 'sources.toml'
        source.write_text(self.source_text, encoding='utf-8')
        (self.root / 'a.xls').write_bytes(b'fixture')
        (self.root / 'b.xlsx').write_bytes(b'fixture')
        base = 'source_config = "sources.toml"\n[[plots]]\nelement = "temperature_2m"\ntype = "series"\n[[station]]\nname = "a"\nlongitude = 87\nlatitude = 43\nstation_id = "001"\ntime_tolerance_seconds = 0\nexcel.files = ["a.xls"]\nexcel.variables.temperature_2m = {variable = "温度A", units = "K"}\n[[station]]\nname = "b"\nlongitude = 88\nlatitude = 44\nstation_id = "001"\nexcel.files = ["b.xlsx"]\nexcel.sheet = "逐时数据"\nexcel.timezone = "Asia/Shanghai"\nexcel.variables.temperature_2m = {variable = "温度B", units = "degC"}\n'
        self.write_config(base)
        a, b = load_config(self.path).points
        self.assertEqual((a.time_tolerance, b.time_tolerance), (timedelta(0), timedelta(seconds=60)))
        self.assertEqual(a.excel.files, (self.root / 'a.xls',))
        self.assertEqual(b.excel.sheet, '逐时数据')
        self.assertEqual(b.excel.timezone, 'Asia/Shanghai')
        self.assertEqual(a.excel.variable('temperature_2m').variable, '温度A')
        self.assertEqual(b.excel.variable('temperature_2m').variable, '温度B')
        for text in (base.replace('time_tolerance_seconds = 0', 'time_tolerance_seconds = -1'), base.replace('time_tolerance_seconds = 0', 'time_tolerance_seconds = true'), base.replace('excel.files = ["a.xls"]', ''), base.replace('["a.xls"]', '[]'), base.replace('["a.xls"]', '["a.xls", "a.xls"]'), base.replace('["a.xls"]', '["absent.xls"]'), base.replace('variable = "温度A"', 'variable = "观测时间"'), base.replace('[[station]]', '[[points]]')):
            with self.subTest(text=text), self.assertRaises(ConfigError):
                self.write_config(text)
                load_config(self.path)
        self.write_config(base.replace('station_id = "001"', ''))
        self.assertTrue(all((point.station_id is None for point in load_config(self.path).points)))

    def test_output_name_is_a_single_safe_directory_component(self):
        self.write_config(self.text.replace('directory = "result"', 'directory = "output"\nname = "trial"'))
        config = load_config(self.path)
        self.assertEqual(config.output_name, 'trial')
        for name in ('', ' ', '.', '..', 'a/b', 'a\\b', 'name:time', 'trailing.'):
            text = self.text.replace('directory = "result"', f'directory = "result"\nname = {name!r}')
            self.write_config(text)
            with self.subTest(name=name), self.assertRaises(ConfigError):
                load_config(self.path)

    def test_run_name_collision_creates_unique_directory_without_overwrite(self):
        config = load_config(self.path)
        with patch('wrf_evaluate.output.datetime') as clock:
            clock.now.return_value.strftime.return_value = '20261115_120000_000001'
            first = create_run_directory(config)
            previous = first / 'result.csv'
            previous.write_text('previous result', encoding='utf-8')
            second = create_run_directory(config)
        self.assertNotEqual(first, second)
        self.assertTrue(second.name.endswith('_001'))
        for directory in (first, second):
            self.assertEqual(directory.parent, config.output_dir)
            self.assertTrue(directory.name.startswith(config.output_name + '_'))
            self.assertEqual((directory / 'config.toml').read_bytes(), self.path.read_bytes())
            self.assertEqual((directory / 'sources.toml').read_bytes(), self.source_path.read_bytes())
        self.assertEqual(previous.read_text(encoding='utf-8'), 'previous result')
        self.assertEqual({p.name for p in second.iterdir()}, {"config.toml", "sources.toml"})

    def test_scan_deduplicates_overlapping_patterns_and_ignores_unmatched_files(self):
        files = {
            '2024120100/wrfout_d03_a': b'123',
            '2024113012/wrfout_d03_b': b'12',
            '2024120100/wrfout_d03_c': b'1234',
            'extra/2024120100/wrfout_d03_d': b'excluded depth',
            '2024120100.zip': b'archive',
            'unrelated/notes.txt': b'other source',
        }
        for name, data in files.items():
            path = self.root / 'raw' / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        self.source_path.write_text(self.source_text.replace(
            '["20????????/wrfout_d03_*"]',
            '["20????????/wrfout_d03_*", "2024120100/wrfout_d03_*"]',
        ), encoding='utf-8')
        result = scan_source(load_config(self.path).sources[0], example_limit=2)
        self.assertEqual((result.file_count, result.total_bytes), (3, 9))
        self.assertEqual(result.examples, ('2024113012/wrfout_d03_b', '2024120100/wrfout_d03_a'))

    def test_filename_pattern_includes_root_and_nested_files(self):
        (self.root / 'raw' / 'one.nc').write_bytes(b'1')
        (self.root / 'raw' / 'sub').mkdir()
        (self.root / 'raw' / 'sub' / 'two.nc').write_bytes(b'22')
        self.source_path.write_text(self.source_text.replace('["20????????/wrfout_d03_*"]', '["*.nc"]'), encoding='utf-8')
        result = scan_source(load_config(self.path).sources[0])
        self.assertEqual((result.file_count, result.total_bytes), (2, 3))

    def test_scan_does_not_enter_directories_excluded_by_patterns(self):
        self.source_path.write_text(self.source_text, encoding="utf-8")
        excluded = self.root / 'raw' / 'unrelated'
        excluded.mkdir()
        allowed = self.root / 'raw' / '2024120100'
        allowed.mkdir(parents=True)
        (allowed / 'wrfout_d03_sample').write_bytes(b'data')
        scandir = os.scandir

        def checked_scandir(path):
            if Path(path) == excluded:
                raise PermissionError('unrelated directory must not be visited')
            return scandir(path)
        with patch('wrf_evaluate.catalog.os.scandir', side_effect=checked_scandir):
            result = scan_source(load_config(self.path).sources[0])
        self.assertEqual(result.file_count, 1)

    def test_cli_errors_are_readable_and_return_nonzero(self):
        output = StringIO()
        with redirect_stderr(output):
            code = main(['--config', str(self.path), 'scan', '--source', 'typo'])
        self.assertEqual(code, 2)
        self.assertIn('typo', output.getvalue())
        self.assertNotIn('Traceback', output.getvalue())
if __name__ == '__main__':
    unittest.main()
