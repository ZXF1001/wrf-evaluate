"""WRF 起报时间与批次选择；目录约定由配置来源时区解释。"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
from zoneinfo import ZoneInfo

from .config import SourceConfig
from .series import Series


@dataclass(frozen=True)
class ForecastSeries:
    series: Series  # 绘图和瞬时指标使用的最新预报拼接序列。
    runs: tuple[tuple[datetime | None, Series], ...]  # 时段指标须在单个批次内完整匹配。


def run_order(init_time: datetime | None) -> datetime:
    return init_time or datetime.min.replace(tzinfo=timezone.utc)


def forecast_init_time(
    source: SourceConfig, path: Path, model_start_time: datetime | None = None,
    *, require: bool = True,
) -> datetime | None:
    """起报时间优先取目录名，其次取明确配置，最后使用 WRF 文件属性。"""
    relative = path.relative_to(source.root)
    directories = (source.root.name, *relative.parts[:-1])
    starts = set()
    for name in directories:
        if re.fullmatch(r"[0-9]{10}", name):
            try:
                start = datetime.strptime(name, "%Y%m%d%H").replace(tzinfo=ZoneInfo(source.timezone))
            except ValueError as exc:
                raise ValueError(f"{path}: 起报目录日期无效: {name}") from exc
            starts.add(start.astimezone(timezone.utc))
    if len(starts) > 1:
        raise ValueError(f"{path}: 路径中有多个不同的起报目录，无法确定批次")
    start = next(iter(starts), source.init_time or model_start_time)
    if start is None and require and source.min_lead_hours > 0:
        raise ValueError(
            f"sources.{source.name}: min_lead_hours 需要起报时间；"
            "未找到起报目录或 SIMULATION_START_DATE/START_DATE 属性，请在配置中填写 init_time"
        )
    return start.astimezone(timezone.utc) if start is not None else None


def eligible_series(curve: Series | None, cutoff: datetime | None) -> Series | None:
    """时段量的整个区间都必须达到最低预报时效。"""
    if curve is None or cutoff is None:
        return curve
    indices = [i for i, time in enumerate(curve.times)
               if (curve.bounds[i][0] if curve.bounds else time) >= cutoff]
    if not indices:
        return None
    return Series(curve.label, tuple(curve.times[i] for i in indices),
                  tuple(curve.values[i] for i in indices),
                  tuple(curve.bounds[i] for i in indices) if curve.bounds else (),
                  curve.statistic, curve.unit)


def valid_from(source: SourceConfig, start: datetime | None) -> datetime | None:
    return start + timedelta(hours=source.min_lead_hours) if start is not None else None


def select_latest(runs: list[tuple[datetime | None, Series]]) -> ForecastSeries:
    """同一原生时间取最新起报；缺测不回退旧预报，不跨批次计算累计差分。"""
    ordered = sorted(runs, key=lambda item: run_order(item[0]))
    samples = {}
    for _, curve in ordered:
        for index, time in enumerate(curve.times):
            samples[time] = (curve.values[index], curve.bounds[index] if curve.bounds else None)
    times = tuple(sorted(samples))
    values = tuple(samples[time][0] for time in times)
    if all(value is None for value in values):
        raise ValueError(f"{ordered[0][1].label}: 配置时段内最新有效预报数值全部缺测")
    template = ordered[0][1]
    if any(curve.unit != template.unit or curve.statistic != template.statistic for _, curve in ordered):
        raise ValueError(f"{template.label}: 不同批次的单位或时间含义不一致")
    bounds = tuple(samples[time][1] for time in times) if template.bounds else ()
    try:
        curve = Series(template.label, times, values, bounds, template.statistic, template.unit)
    except ValueError as exc:
        raise ValueError(f"{template.label}: 最新批次拼接后的时间区间不兼容: {exc}") from exc
    return ForecastSeries(curve, tuple(ordered))
