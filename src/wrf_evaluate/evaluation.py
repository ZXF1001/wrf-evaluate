"""时间配对、连续误差及事件指标；时段量不按时间戳近似配对。"""

import csv
from bisect import bisect_left, bisect_right
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta, timezone
from pathlib import Path

from .metrics import (
    ContinuousMetrics, EventMetrics, continuous_metrics,
    precipitation_event_metrics, temperature_event_metrics,
)
from .series import Series
from .forecasts import ForecastSeries, run_order


@dataclass(frozen=True)
class Comparison:
    forecast: str
    observation: str
    matched_times: int
    metrics: ContinuousMetrics
    unit: str
    interval_seconds: float | None
    events: tuple[tuple[str, EventMetrics], ...] = ()


def _nearest_pairs(forecast: Series, observation: Series, tolerance: timedelta) -> list:
    """最小时间差优先的一对一匹配；等距时优先较早实况、较早预报。"""
    times = tuple(time.astimezone(timezone.utc) for time in forecast.times)
    candidates = []
    for obs_index, time in enumerate(observation.times):
        time = time.astimezone(timezone.utc)
        first = bisect_left(times, time - tolerance)
        last = bisect_right(times, time + tolerance)
        candidates.extend(
            (abs(times[index] - time), obs_index, index)
            for index in range(first, last)
        )
    candidates.sort()
    used_forecasts, used_observations = set(), set()
    pairs = []
    for _, obs_index, index in candidates:
        if obs_index not in used_observations and index not in used_forecasts:
            used_observations.add(obs_index)
            used_forecasts.add(index)
            pairs.append((forecast.values[index], observation.values[obs_index]))
    return pairs


def _period_pair_records(
    forecast: Series, observation: Series, exclude: set[int] | None = None,
) -> list[tuple[int, float | None, float | None, float]]:
    """以实况窗口为准，只合并完整连续的预报区间，不拆分、不重复使用。

    滚动/重叠的预报区间只能精确匹配，不能相加；不同窗口长度分别统计。
    """
    bounds = tuple((a.astimezone(timezone.utc), b.astimezone(timezone.utc))
                   for a, b in forecast.bounds)
    by_start = {start: index for index, (start, _) in enumerate(bounds)}
    exact = {period: index for index, period in enumerate(bounds)}
    can_aggregate = all(a[1] <= b[0] for a, b in zip(bounds, bounds[1:]))
    used = set()
    records = []
    for obs_index, ((start, end), observed) in enumerate(zip(observation.bounds, observation.values)):
        if exclude and obs_index in exclude:
            continue
        start, end = start.astimezone(timezone.utc), end.astimezone(timezone.utc)
        seconds = (end - start).total_seconds()
        if (start, end) in exact:
            indices = [exact[start, end]]
        elif can_aggregate:
            indices, cursor = [], start
            while cursor < end:
                index = by_start.get(cursor)
                if index is None or bounds[index][1] > end:
                    indices = []
                    break
                indices.append(index)
                cursor = bounds[index][1]
        else:
            indices = []
        if not indices or used.intersection(indices):
            continue
        used.update(indices)
        values = [forecast.values[index] for index in indices]
        predicted = None
        if all(value is not None for value in values):
            if forecast.statistic == "sum":
                predicted = sum(values)
            else:
                predicted = sum(
                    value * (bounds[index][1] - bounds[index][0]).total_seconds()
                    for index, value in zip(indices, values)
                ) / seconds
        records.append((obs_index, predicted, observed, seconds))
    return records


def _period_pairs(forecast: Series, observation: Series) -> dict[float, list]:
    groups = {(end - start).total_seconds(): [] for start, end in observation.bounds}
    for _, predicted, observed, seconds in _period_pair_records(forecast, observation):
        groups.setdefault(seconds, []).append((predicted, observed))
    return groups


def _build_comparisons(forecast: Series, observation: Series, groups: dict[float | None, list],
                       field: str, thresholds: tuple[float, ...]) -> tuple[Comparison, ...]:
    results = []
    for seconds, pairs in groups.items():
        valid = [(a, b) for a, b in pairs if a is not None and b is not None]
        predicted = [a for a, _ in valid]
        observed = [b for _, b in valid]
        events = ()
        if field == "temperature_2m":
            events = tuple(zip(("非正温(≤0℃)", "正温(>0℃)"), temperature_event_metrics(predicted, observed)))
        elif field == "precipitation":
            events = tuple((f"降水≥{threshold:g}mm", precipitation_event_metrics(predicted, observed, threshold))
                           for threshold in thresholds)
        results.append(Comparison(forecast.label, observation.label, len(pairs), continuous_metrics(predicted, observed),
                                  forecast.unit, seconds, events))
    return tuple(results)


def compare_series(
    forecast: Series, observation: Series, tolerance: timedelta,
    field: str = "temperature_2m", thresholds: tuple[float, ...] = (),
) -> tuple[Comparison, ...]:
    if not isinstance(tolerance, timedelta) or tolerance < timedelta(0):
        raise ValueError("时间配对容差须为非负 timedelta")
    if forecast.unit != observation.unit:
        raise ValueError("指标计算前须统一单位")
    if "raw" in {forecast.statistic, observation.statistic}:
        raise ValueError("原始降水记录未定义时间窗口，仅绘图，不参与指标")
    if forecast.statistic == observation.statistic == "instant":
        groups = {None: _nearest_pairs(forecast, observation, tolerance)}
    elif forecast.statistic == observation.statistic:
        groups = _period_pairs(forecast, observation)
    elif field == "wind_speed_10m" and {forecast.statistic, observation.statistic} == {"instant", "mean"}:
        # 已确认的口径：模式瞬时风速与站点10分钟平均风速按时刻对比。
        groups = {None: _nearest_pairs(forecast, observation, tolerance)}
    else:
        raise ValueError(
            f"{forecast.label} / {observation.label}: 时间含义不同 "
            f"({forecast.statistic}/{observation.statistic})，不能直接计算指标"
        )

    return _build_comparisons(forecast, observation, groups, field, thresholds)


def compare_forecast(
    forecast: ForecastSeries, observation: Series, tolerance: timedelta,
    field: str, thresholds: tuple[float, ...] = (),
) -> tuple[Comparison, ...]:
    """瞬时序列使用最新合并结果；时段量按完整窗口选择最新可覆盖的批次。"""
    if not isinstance(tolerance, timedelta) or tolerance < timedelta(0):
        raise ValueError("时间配对容差须为非负 timedelta")
    if "raw" in {forecast.series.statistic, observation.statistic}:
        raise ValueError("原始降水记录未定义时间窗口，仅绘图，不参与指标")
    if forecast.series.statistic == observation.statistic == "instant":
        return compare_series(forecast.series, observation, tolerance, field, thresholds)
    if forecast.series.unit != observation.unit:
        raise ValueError("指标计算前须统一单位")
    if forecast.series.statistic != observation.statistic:
        if not (field == "wind_speed_10m" and {forecast.series.statistic, observation.statistic} == {"instant", "mean"}):
            raise ValueError(f"{forecast.series.label} / {observation.label}: 时间含义不同，不能直接计算指标")
        return compare_series(forecast.series, observation, tolerance, field, thresholds)
    selected = {}
    for _, run in sorted(forecast.runs, key=lambda item: run_order(item[0]), reverse=True):
        for obs_index, predicted, observed, seconds in _period_pair_records(run, observation, set(selected)):
            selected.setdefault(obs_index, (predicted, observed, seconds))
    groups = {(end - start).total_seconds(): [] for start, end in observation.bounds}
    for predicted, observed, seconds in selected.values():
        groups.setdefault(seconds, []).append((predicted, observed))
    return _build_comparisons(forecast.series, observation, groups, field, thresholds)


def write_metrics(path: str | Path, comparisons: Sequence[Comparison], *, unit: str | None = None) -> Path:
    """UTF-8 CSV；时段长度影响指标含义，配对容差只保留在配置副本中。"""
    path = Path(path)
    if not comparisons and unit is None:
        raise ValueError("没有可输出的指标")
    unit = comparisons[0].unit if comparisons else unit
    event_fields = (
        ("命中数", "hits"), ("漏报数", "misses"), ("空报数", "false_alarms"),
        ("正确否定数", "correct_negatives"), ("漏报率", "miss_rate"),
        ("空报率", "false_alarm_rate"), ("空报比", "false_alarm_ratio"),
    )
    event_labels = tuple(dict.fromkeys(label for result in comparisons for label, _ in result.events))
    has_intervals = any(result.interval_seconds is not None for result in comparisons)
    fields = (
        "预报来源", "实况来源", "配对时次数", "有效样本数",
        f"RMSE({unit})", f"MAE({unit})", f"Bias({unit})", "R",
    ) + (("时段长度(秒)",) if has_intervals else ())
    fields += tuple(f"{label}_{name}" for label in event_labels for name, _ in event_fields)
    with path.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for result in comparisons:
            row = {
                "预报来源": result.forecast, "实况来源": result.observation,
                "配对时次数": result.matched_times, "有效样本数": result.metrics.sample_count,
                f"RMSE({unit})": result.metrics.rmse, f"MAE({unit})": result.metrics.mae,
                f"Bias({unit})": result.metrics.bias, "R": result.metrics.correlation,
            }
            if has_intervals:
                row["时段长度(秒)"] = result.interval_seconds
            for label, event in result.events:
                row.update({f"{label}_{name}": getattr(event, field) for name, field in event_fields})
            writer.writerow(row)
    return path
