"""检验分钟容差、时区、多文件时次及最新 CLDAS 版本选择。"""

from contextlib import redirect_stdout
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
import unittest
from unittest.mock import patch

from wrf_evaluate.cldas import CldasReadError, parse_cldas_filename
from wrf_evaluate.cli import main
from wrf_evaluate.config import Config, PeriodConfig, SourceConfig
from wrf_evaluate.matching import TimeMatchError, index_cldas_files, iter_time_matches
from wrf_evaluate.wrf import WrfHeader


class TimeMatchingTests(unittest.TestCase):
    def setUp(self):
        self.time = datetime(2024, 12, 24, tzinfo=timezone.utc)
        self.wrf = SourceConfig('forecast', 'wrf', Path('wrf'), ('*',), 'UTC', True)
        self.cldas = SourceConfig('analysis', 'cldas', Path('cldas'), ('*.nc',), 'UTC', True)
        self.forecast = Path('wrf/first')
        self.observation = self.cldas_path()
        self.files = {'forecast': [self.forecast], 'analysis': [self.observation]}
        self.headers = {self.forecast: WrfHeader(self.forecast, (self.time,), (), frozenset(), None)}
        files = patch('wrf_evaluate.matching.iter_source_files', side_effect=lambda source: iter(self.files[source.name]))
        headers = patch('wrf_evaluate.matching.read_wrf_header', side_effect=lambda path, zone: self.headers[path])
        files.start()
        headers.start()
        self.addCleanup(files.stop)
        self.addCleanup(headers.stop)

    def cldas_path(self, production='20241226000900', valid='2024122400', product='TMP', family='NRT_ASI_0P0625'):
        return Path(f'cldas/Z_NAFP_C_BABJ_{production}_P_CLDAS_{family}_HOR-{product}-{valid}.nc')

    def matches(self, seconds=60, sources=None):
        return list(iter_time_matches(sources or (self.wrf, self.cldas), timedelta(seconds=seconds)))

    def test_tolerance_is_inclusive_and_keeps_unmatched_frames(self):
        offsets = (-61, -60, 0, 60, 61)
        times = tuple(self.time + timedelta(seconds=s) for s in offsets)
        self.headers[self.forecast] = replace(self.headers[self.forecast], times=times)
        matches = self.matches()
        self.assertEqual([m.observation is not None for m in matches], [False, True, True, True, False])
        self.assertEqual([m.forecast.time_index for m in matches], list(range(5)))
        self.assertEqual(matches[1].offset, timedelta(seconds=60))
        self.assertEqual(matches[3].offset, timedelta(seconds=-60))
        self.assertEqual(sum(m.observation is not None for m in self.matches(0)), 1)

    def test_timezones_represent_same_instant_without_changing_original_time(self):
        self.cldas = replace(self.cldas, timezone='Asia/Shanghai')
        self.files['analysis'] = [self.cldas_path(valid='2024122408')]
        match = self.matches(0)[0]
        self.assertEqual(match.offset, timedelta(0))
        self.assertEqual(match.observation.time.hour, 8)
        self.assertEqual(match.forecast.time.hour, 0)

    def test_latest_version_selection_is_independent_of_input_order(self):
        old = self.cldas_path(production='20241225000900')
        latest = self.cldas_path(production='20241227000900')
        for paths in ([old, self.observation, latest], [latest, self.observation, old]):
            self.files['analysis'] = paths
            self.assertEqual(self.matches()[0].observation.path, latest)
        # 旧版本重复不应阻止唯一的更新版本被选中。
        old_copy = Path('copy') / old.name
        self.files['analysis'] = [old, old_copy, latest]
        self.assertEqual(self.matches()[0].observation.path, latest)
        self.files['analysis'] = [latest, Path('copy') / latest.name]
        with self.assertRaisesRegex(TimeMatchError, '重复'):
            self.matches()

    def test_cldas_index_can_select_only_requested_product_and_period(self):
        other_product = self.cldas_path(product='PRE')
        other_time = self.cldas_path(valid='2024122401')
        self.files['analysis'] = [self.observation, other_product, other_time]
        period = PeriodConfig(self.time, self.time + timedelta(minutes=30))
        index = index_cldas_files((self.cldas,), product='TMP', period=period)
        self.assertEqual(len(index), 1)
        self.assertEqual(index[('analysis', 'NRT_ASI_0P0625', 'TMP')][1][0].path, self.observation)

    def test_different_products_families_and_sources_are_not_overwritten(self):
        self.files['analysis'] += [self.cldas_path(product='PRE'), self.cldas_path(family='NRT_CHN_0P0625')]
        other = replace(self.cldas, name='other')
        self.files['other'] = [self.observation]
        matches = self.matches(sources=(self.wrf, self.cldas, other))
        self.assertEqual(len(matches), 4)
        self.assertEqual(len({(m.cldas_source, m.family, m.product) for m in matches}), 4)

    def test_overlapping_forecasts_choose_latest_file_initialization(self):
        second = Path('wrf/second')
        self.files['forecast'].append(second)
        start = self.time - timedelta(hours=6)
        self.headers[second] = replace(self.headers[self.forecast], path=second, model_start_time=start)
        matches = self.matches()
        self.assertEqual([m.forecast.path for m in matches], [second])
        self.assertEqual(matches[0].forecast.model_start_time, start)

    def test_nearest_time_tie_requires_explicit_resolution(self):
        self.files['analysis'].append(self.cldas_path(valid='2024122401'))
        header = self.headers[self.forecast]
        self.headers[self.forecast] = replace(header, times=(self.time + timedelta(minutes=30),))
        self.assertIsNone(self.matches()[0].observation)
        with self.assertRaisesRegex(TimeMatchError, '等距'):
            self.matches(1800)
        self.headers[self.forecast] = replace(header, times=(self.time + timedelta(minutes=31),))
        self.assertEqual(self.matches(1800)[0].observation.time.hour, 1)

    def test_missing_source_or_cldas_files_fail_clearly(self):
        with self.assertRaises(ValueError):
            self.matches(sources=(self.wrf, replace(self.cldas, enabled=False)))
        self.files['analysis'] = []
        with self.assertRaisesRegex(TimeMatchError, 'CLDAS'):
            self.matches()

    def test_filename_parser_validates_production_date_without_opening_file(self):
        info = parse_cldas_filename(self.observation)
        self.assertEqual(info.production_time, datetime(2024, 12, 26, 0, 9, tzinfo=timezone.utc))
        self.assertEqual(info.time, self.time)
        for production in ('20241326000900', '202412260009', 'bad'):
            with self.assertRaises(CldasReadError):
                parse_cldas_filename(self.cldas_path(production=production))

    def test_empty_source_is_not_hidden_by_other_nonempty_sources(self):
        for source in (self.wrf, self.cldas):
            empty = replace(source, name='empty_trial')
            self.files[empty.name] = []
            with self.subTest(kind=empty.kind), self.assertRaisesRegex(TimeMatchError, 'empty_trial'):
                self.matches(sources=(self.wrf, self.cldas, empty))

    def test_cli_match_reports_counts_without_opening_cldas_or_writing_files(self):
        config = Config((self.wrf, self.cldas), Path('output'), 'UTC', timedelta(seconds=60))
        output = StringIO()
        with patch('wrf_evaluate.cli.load_config', return_value=config), redirect_stdout(output):
            self.assertEqual(main(['match']), 0)
        self.assertIn('配对 1，未配对 0', output.getvalue())
        self.assertIn('最大绝对时差 0 秒', output.getvalue())


if __name__ == '__main__':
    unittest.main()
