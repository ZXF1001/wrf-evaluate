from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

import numpy as np
from matplotlib.dates import date2num
from matplotlib.axes import Axes
from matplotlib.figure import Figure

from wrf_evaluate.config import PointConfig
from wrf_evaluate.plotting import plot_series
from wrf_evaluate.series import Series


class SeriesPlotTests(unittest.TestCase):
    def setUp(self):
        self.time = datetime(2024, 12, 24, tzinfo=timezone.utc)
        self.point = PointConfig("Y5012", 87.4897, 43.4733)

    def test_native_minutes_and_hours_share_one_axes_with_missing_gap(self):
        wrf = Series("WRF", (self.time, self.time + timedelta(hours=1)), (-12, -10))
        obs = Series("cldas", tuple(self.time + timedelta(minutes=i) for i in (0, 5, 10)), (-13, None, -12))
        calls = []
        labels = {}
        original = Axes.plot
        original_save = Figure.savefig

        def capture(ax, times, values, **kwargs):
            calls.append((ax, times, values))
            return original(ax, times, values, **kwargs)

        def capture_save(figure, *args, **kwargs):
            ax = figure.axes[0]
            result = original_save(figure, *args, **kwargs)
            labels.update(
                title=ax.get_title(), ylabel=ax.get_ylabel(), xlabel=ax.get_xlabel(),
                legend=[text.get_text() for text in ax.get_legend().get_texts()],
                notes=[text.get_text() for text in figure.texts],
                date_offset=ax.xaxis.get_offset_text().get_text(),
            )
            return result

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "temperature.png"
            with patch.object(Axes, "plot", capture), patch.object(Figure, "savefig", capture_save):
                self.assertEqual(plot_series(path, self.point, [wrf, obs], "Asia/Shanghai"), path)
            self.assertEqual(path.read_bytes()[:8], b"\x89PNG\r\n\x1a\n")
        self.assertIs(calls[0][0], calls[1][0])
        self.assertEqual([len(call[1]) for call in calls], [2, 3])
        self.assertEqual(calls[0][1][0].hour, 8)
        self.assertTrue(np.isnan(calls[1][2][1]))
        self.assertEqual(labels['title'].splitlines()[0], "Y5012 | 2m气温对比")
        self.assertEqual(labels['ylabel'], "2m气温（℃）")
        self.assertEqual(labels['xlabel'], "时间（Asia/Shanghai）")
        self.assertEqual(labels['legend'], ["WRF", "cldas"])
        self.assertEqual(labels['notes'], [])
        self.assertEqual(labels['date_offset'], "2024-12-24")
        self.assertEqual(wrf.times[0].hour, 0)
        self.assertEqual(obs.values, (-13, None, -12))

    def test_invalid_times_and_values_fail_before_plotting(self):
        for times, values in (
            ((self.time, self.time), (1, 2)),
            ((self.time.replace(tzinfo=None),), (1,)),
            ((self.time,), (float("nan"),)),
            ((self.time,), (float("inf"),)),
            ((self.time,), (True,)),
            ((self.time,), ()),
        ):
            with self.subTest(times=times, values=values), self.assertRaises(ValueError):
                Series("test", times, values)

    def test_empty_plot_and_duplicate_labels_are_rejected(self):
        empty = Series("empty", (self.time,), (None,))
        valid = Series("valid", (self.time,), (1,))
        with patch.object(Figure, "savefig") as save:
            for series in ([], [empty], [valid, valid]):
                with self.assertRaises(ValueError):
                    plot_series("unused.png", self.point, series, "UTC")
            save.assert_not_called()

    def test_equivalent_times_in_different_zones_are_duplicate(self):
        with self.assertRaisesRegex(ValueError, "重复"):
            Series("test", (self.time, self.time.astimezone(ZoneInfo("Asia/Shanghai"))), (1, 2))

    def test_configured_period_sets_plot_limits_in_display_timezone(self):
        from wrf_evaluate.config import PeriodConfig

        series = Series("WRF", (self.time, self.time + timedelta(hours=1)), (-12, -10))
        period = PeriodConfig(self.time, self.time + timedelta(hours=2))
        limits = []

        def capture(figure, *args, **kwargs):
            limits.append(figure.axes[0].get_xlim())
            self.assertEqual(str(figure.axes[0].xaxis.get_major_locator().tz), "Asia/Shanghai")
            self.assertIn('自定义气温对比', figure.axes[0].get_title())
            self.assertEqual(figure.axes[0].get_ylabel(), '自定义气温（℃）')

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "temperature.png"
            with patch.object(Figure, "savefig", capture):
                plot_series(path, self.point, [series], "Asia/Shanghai", period=period, element_name='自定义气温')
        self.assertEqual(len(limits), 1)
        np.testing.assert_allclose(limits[0], date2num([period.start, period.end]))
