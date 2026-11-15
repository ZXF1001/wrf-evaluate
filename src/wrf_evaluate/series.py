"""点位序列的单位与时间区间标准化；不修改来源数据、不做重采样。"""

from dataclasses import dataclass
from datetime import datetime, timezone
import math

from .variables import UNITS, VariableConfig, convert_value, validate_variable
from .config import PeriodConfig


@dataclass(frozen=True)
class Series:
    label: str
    times: tuple[datetime, ...]
    values: tuple[float | None, ...]
    # 瞬时序列不含区间；时段序列的绘图时刻仍为原始时间戳。
    bounds: tuple[tuple[datetime, datetime], ...] = ()
    statistic: str = "instant"
    unit: str = "℃"

    def __post_init__(self) -> None:
        if not self.label.strip() or not self.times or len(self.times) != len(self.values):
            raise ValueError("序列须有名称、时间，且时间和值的数量一致")
        if any(time.utcoffset() is None for time in self.times):
            raise ValueError(f"{self.label}: 时间须包含时区")
        if any(right <= left for left, right in zip(self.times, self.times[1:])):
            raise ValueError(f"{self.label}: 时间须严格递增，重复记录须先核实来源")
        if any(value is not None and (isinstance(value, bool) or not math.isfinite(value)) for value in self.values):
            raise ValueError(f"{self.label}: 数值须为有限值或 None")
        if self.statistic not in {"instant", "mean", "sum"}:
            raise ValueError("标准化后的统计方式应为 instant / mean / sum")
        if self.statistic == "instant" and self.bounds:
            raise ValueError("瞬时值不应含时段边界")
        if self.statistic != "instant":
            if len(self.bounds) != len(self.times):
                raise ValueError("时段序列须为每个值保留起止边界")
            for start, end in self.bounds:
                if start.utcoffset() is None or end.utcoffset() is None or end <= start:
                    raise ValueError("时段边界须包含时区，且结束晚于开始")
            if any(b[0] <= a[0] or b[1] <= a[1] for a, b in zip(self.bounds, self.bounds[1:])):
                raise ValueError("时段起点和终点须分别严格递增，不可重复或嵌套")


def normalize_series(
    label: str, field: str, spec: VariableConfig,
    records: list[tuple[datetime, float | None, str, str]], period: PeriodConfig,
    *, allow_empty: bool = False,
) -> Series | None:
    """records 为 (时间, 原值, 原单位, 来源位置)。累计量须包含所需区间的前值。"""
    validate_variable(field, spec)
    records.sort(key=lambda row: row[0].astimezone(timezone.utc))
    for left, right in zip(records, records[1:]):
        if left[0] == right[0]:
            raise ValueError(f"{label}: {left[0].isoformat()} 有重复记录: {left[3]} 与 {right[3]}")
    times, values, bounds = [], [], []
    previous = None
    radiation_energy = field.startswith("surface_") and spec.time_kind in {"sum", "cumulative"}
    statistic = "mean" if radiation_energy else "sum" if spec.time_kind == "cumulative" else spec.time_kind
    for time, value, original_units, location in records:
        try:
            value = convert_value(field, value, spec.units or original_units, spec.time_kind, spec.interval)
            start = end = time
            if spec.time_kind == "cumulative":
                before = previous
                previous = (time, value)
                if before is None:
                    continue
                start = before[0]
                value = None if value is None or before[1] is None else value - before[1]
                if value is not None and value < 0:
                    raise ValueError("出现负累计差分，可能存在清零、重启或序列混用；未自动修正")
            elif spec.time_kind in {"mean", "sum"}:
                start = time if spec.timestamp == "start" else time - spec.interval
                end = start + spec.interval
            if radiation_energy and value is not None:
                value /= (end - start).total_seconds()
            # 时段量仅保留完整落入检验范围的区间，不能裁剪后仍使用完整区间值。
            inside = period.start <= time <= period.end if statistic == "instant" else period.start <= start < end <= period.end
            if inside:
                times.append(time)
                values.append(value)
                if statistic != "instant":
                    bounds.append((start, end))
        except ValueError as exc:
            raise ValueError(f"{label} {location}: {exc}") from exc
    if not times:
        if allow_empty:
            return None
        raise ValueError(f"{label}: 配置时段内没有可用记录或完整区间；累计量需要前一个时次")
    if not allow_empty and all(value is None for value in values):
        raise ValueError(f"{label}: 配置时段内数值全部缺测")
    return Series(label, tuple(times), tuple(values), tuple(bounds), statistic, UNITS[field])
