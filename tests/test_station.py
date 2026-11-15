from contextlib import closing
from datetime import datetime
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from wrf_evaluate.config import DEFAULT_COLUMN_MAPPING, PeriodConfig
from wrf_evaluate.station import StationReadError, iter_regional_observations


class StationParsingTests(unittest.TestCase):
    def setUp(self):
        # 故意打乱顺序，且将 2 分钟风速放在 10 分钟风速前面。
        self.header = ["相对湿度(%RH)", "2min平均风速(m/s)", "区站号", "气温(°)", "观测时间", "10min平均风速(m/s)", "小时降水量(mm)"]
        self.row = [64, 9.9, "Y5012", -12.1, "20240102030405", 2.1, 0.4]
        self.path = Path("station.xlsx")
        self.book = MagicMock()
        self.book.__enter__.return_value = self.book
        self.sheet = self.book.get_sheet_by_name.return_value
        self.sheet.start = (0, 0)
        factory = patch("wrf_evaluate.station.CalamineWorkbook", autospec=True)
        self.factory = factory.start()
        self.addCleanup(factory.stop)
        self.factory.from_path.return_value = self.book

    def records(self, rows, header=None, timezone="UTC", column_mapping=None):
        self.sheet.iter_rows.return_value = iter([self.header if header is None else header, *rows])
        return iter_regional_observations(self.path, timezone, column_mapping)

    def test_renamed_headers_read_same_values(self):
        mapping = {"temperature_2m": " 温度℃ ", "time": "时间"}
        header = ["相对湿度(%RH)", "2min平均风速(m/s)", "区站号", "温度℃", "时间", "10min平均风速(m/s)", "小时降水量(mm)"]
        observation, = self.records([self.row], header=header, column_mapping=mapping)
        self.assertEqual(observation.values['temperature_2m'], -12.1)
        self.assertEqual(observation.time.second, 5)
        self.assertEqual(observation.values['wind_speed_10m'], 2.1)
        self.assertEqual(observation.values['precipitation'], 0.4)

    def test_filtering_skips_unselected_values_and_preserves_row_numbers(self):
        outside = self.row.copy()
        outside[3], outside[4] = 'unused', '20230102030405'
        other = self.row.copy()
        other[2], other[3] = 'other', 'unused'
        self.sheet.iter_rows.return_value = iter([self.header, outside, other, self.row])
        period = PeriodConfig(datetime(2024, 1, 1, tzinfo=ZoneInfo('UTC')),
                              datetime(2024, 1, 3, tzinfo=ZoneInfo('UTC')))
        row, = iter_regional_observations(self.path, 'UTC', station_ids={'Y5012'}, period=period)
        self.assertEqual(row.row, 4)
        self.assertEqual(row.values['temperature_2m'], -12.1)

    def test_invalid_mapping_fails_before_opening_workbook(self):
        for mapping in [{"unknown": "气温"}, {"temperature_2m": "区站号"}, {"time": 1}]:
            with self.subTest(mapping=mapping), self.assertRaises(StationReadError):
                list(self.records([self.row], column_mapping=mapping))
        self.factory.from_path.assert_not_called()

    def test_columns_units_seconds_and_configured_zone(self):
        observation, = self.records([self.row], timezone="Asia/Shanghai")
        self.assertEqual(observation.time, datetime(2024, 1, 2, 3, 4, 5, tzinfo=ZoneInfo("Asia/Shanghai")))
        self.assertEqual(observation.values['temperature_2m'], -12.1)
        self.assertEqual(observation.values['relative_humidity_2m'], 64)
        self.assertEqual(observation.values['wind_speed_10m'], 2.1)
        self.assertEqual((observation.path, observation.row, observation.station_id), (self.path, 2, "Y5012"))
        self.book.__exit__.assert_called_once()

    def test_blank_cells_remain_missing_and_empty_rows_keep_original_row_numbers(self):
        row = [None, 9.9, "Y5012", " ", "20240102030405", "", ""]
        observation, = self.records([[""] * 7, row])
        self.assertEqual(observation.row, 3)
        self.assertIsNone(observation.values['temperature_2m'])
        self.assertIsNone(observation.values['relative_humidity_2m'])
        self.assertIsNone(observation.values['wind_speed_10m'])
        self.assertIsNone(observation.values['precipitation'])

    def test_missing_or_duplicate_header_raises(self):
        for header in [self.header[:-1], self.header + ["气温(°)"]]:
            with self.subTest(header=header), self.assertRaisesRegex(StationReadError, "缺失或重复"):
                list(self.records([self.row], header=header))

    def test_bad_number_reports_file_row_and_column(self):
        for value in ["缺测", float("nan"), float("inf"), True]:
            row = self.row.copy()
            row[3] = value
            with self.subTest(value=value), self.assertRaisesRegex(StationReadError, r"station.xlsx.*第 2 行 temperature_2m（气温"):
                list(self.records([row]))
        self.assertEqual(self.book.__exit__.call_count, 4)

    def test_bad_time_or_missing_station_is_not_silently_dropped(self):
        for index, value in [(4, "20240230030405"), (4, "20240102"), (4, 20240102030405), (2, "")]:
            row = self.row.copy()
            row[index] = value
            with self.subTest(value=value), self.assertRaises(StationReadError):
                list(self.records([row]))

    def test_unsorted_and_duplicate_records_are_preserved(self):
        earlier = self.row.copy()
        earlier[4] = "20230102030405"
        records = list(self.records([self.row, earlier, self.row]))
        self.assertEqual([record.time.year for record in records], [2024, 2023, 2024])
        self.assertEqual([record.row for record in records], [2, 3, 4])

    def test_close_releases_workbook_when_stopping_early(self):
        with closing(self.records([self.row, self.row])) as records:
            next(records)
            self.book.__exit__.assert_not_called()
        self.book.__exit__.assert_called_once()

    def test_unknown_timezone_is_rejected_before_opening_file(self):
        with self.assertRaisesRegex(StationReadError, "未知站点时区"):
            list(self.records([self.row], timezone="not/a/timezone"))
        self.factory.from_path.assert_not_called()

    def test_missing_rain_mapping_uses_default_header(self):
        mapping = dict(DEFAULT_COLUMN_MAPPING)
        del mapping['precipitation']
        row, = self.records([self.row], column_mapping=mapping)
        self.assertEqual(row.values['precipitation'], 0.4)

    def test_empty_sheet_or_missing_header_fails_with_file_context(self):
        self.sheet.start = None
        with self.assertRaisesRegex(StationReadError, 'station.xlsx'):
            list(self.records([]))
        self.sheet.start = (0, 0)
        self.sheet.iter_rows.return_value = iter(())
        with self.assertRaisesRegex(StationReadError, 'station.xlsx'):
            list(iter_regional_observations(self.path, 'UTC'))

    def test_rain_mapping_preserves_reset_values_and_missing_values(self):
        mapping = {**DEFAULT_COLUMN_MAPPING, 'precipitation': '雨量'}
        header = self.header[:-1] + ['雨量']
        rows = [self.row[:-1] + [value] for value in (2.4, 0.2, '', 0)]
        records = list(self.records(rows, header=header, column_mapping=mapping))
        self.assertEqual([row.values['precipitation'] for row in records], [2.4, 0.2, None, 0])


if __name__ == "__main__":
    unittest.main()
