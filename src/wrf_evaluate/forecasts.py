"""WRF 起报时间与批次选择；目录约定由配置来源时区解释。"""

from bisect import bisect_left
from dataclasses import dataclass
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
from zoneinfo import ZoneInfo
from time import monotonic

from .catalog import iter_source_files
from .config import PeriodConfig, SourceConfig
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
            f"sources.{source.name}: lead_hours 下限大于0时需要起报时间；"
            "未找到起报目录或 SIMULATION_START_DATE/START_DATE 属性，请在配置中填写 init_time"
        )
    return start.astimezone(timezone.utc) if start is not None else None


def eligible_series(
    curve: Series | None, cutoff: datetime | None, latest: datetime | None = None,
) -> Series | None:
    """时段量的整个区间都必须处于有效预报时效内。"""
    if curve is None or (cutoff is None and latest is None):
        return curve
    indices = [i for i, time in enumerate(curve.times)
               if (cutoff is None or (curve.bounds[i][0] if curve.bounds else time) >= cutoff)
               and (latest is None or (curve.bounds[i][1] if curve.bounds else time) <= latest)]
    if not indices:
        return None
    return Series(curve.label, tuple(curve.times[i] for i in indices),
                  tuple(curve.values[i] for i in indices),
                  tuple(curve.bounds[i] for i in indices) if curve.bounds else (),
                  curve.statistic, curve.unit)


def valid_from(source: SourceConfig, start: datetime | None) -> datetime | None:
    return start + timedelta(hours=source.min_lead_hours) if start is not None else None


def wrf_filename_time(path: Path, zone: ZoneInfo) -> datetime | None:
    """识别标准 WRF 文件名，兼容 Linux 冒号及 Windows 下划线秒级时间。"""
    match = re.fullmatch(r"wrfout_d[0-9]+_([0-9]{4}-[0-9]{2}-[0-9]{2})_([0-9]{2})[:_]([0-9]{2})[:_]([0-9]{2})(?:\.nc)?", path.name)
    if match is None:
        return None
    try:
        date, hour, minute, second = match.groups()
        return datetime.fromisoformat(f"{date}T{hour}:{minute}:{second}").replace(tzinfo=zone)
    except ValueError:
        return None


def iter_wrf_files(
    source: SourceConfig, period: PeriodConfig, progress: Callable[[str], None] | None = None,
    *, windows: tuple[tuple[datetime, datetime], ...] | None = None,
) -> Iterator[Path]:
    """打开 NetCDF 前按起报目录和文件名时间段排除无关资料。

    WRF 文件名是该文件时间段的起点；同一批次下一个文件名是当前文件的
    结束边界，因此即使单个文件含多个时次，也能安全筛选。
    """
    zone = ZoneInfo(source.timezone)
    # 空间图可只请求离散时刻/窗口；合并后按二分查找，避免逐文件遍历全部出图时刻。
    merged = []
    for a, b in sorted(windows or ()):
        if merged and a <= merged[-1][1]:
            merged[-1] = merged[-1][0], max(b, merged[-1][1])
        else:
            merged.append((a, b))
    window_ends = [b for _, b in merged]
    if windows is not None and not merged:
        return
    scanned = skipped = directories_skipped = candidates = 0
    last_report = monotonic()

    def report(force: bool = False) -> None:
        nonlocal last_report
        now = monotonic()
        if progress is not None and (force or now - last_report >= 5):
            progress(f"{source.name} 路径筛选：扫描 {scanned:,} 个文件名，跳过 {skipped:,} 个文件、"
                     f"{directories_skipped:,} 个起报目录，候选 {candidates:,} 个文件")
            last_report = now

    def include_directory(path: Path) -> bool:
        nonlocal directories_skipped
        # 虚拟子路径仅用于复用起报目录解析，不访问文件。
        start = forecast_init_time(source, path / "_", require=False)
        cutoff = valid_from(source, start)
        latest = start + timedelta(hours=source.max_lead_hours) if start is not None else None
        include = (cutoff is None or cutoff <= period.end) and (latest is None or latest >= period.start)
        if not include:
            directories_skipped += 1
        report()
        return include

    records = {}
    groups = {}
    for path in iter_source_files(source, include_directory=include_directory):
        scanned += 1
        start = forecast_init_time(source, path, require=False)
        filename_time = wrf_filename_time(path, zone)
        if filename_time is not None:
            # 不同目录或不同 domain 的文件不能互为时间段边界。
            group = (start, path.parent, path.name.split("_", 2)[1])
            groups.setdefault(group, []).append((filename_time, path))
        records[path] = start, filename_time
        report()
    next_times: dict[Path, datetime] = {}
    for items in groups.values():
        items.sort(key=lambda item: (item[0], item[1].as_posix()))
        following = previous = None
        for time, path in reversed(items):
            if previous is not None and time < previous:
                following = previous
            if following is not None:
                next_times[path] = following
            previous = time
    for path, (start, time) in records.items():
        cutoff = valid_from(source, start)
        next_time = next_times.get(path)
        lower = max(period.start, cutoff) if cutoff is not None else period.start
        # 文件名不早于起报；未知起报时，文件名加最长时效仍是保守上界。
        latest = (start or time) + timedelta(hours=source.max_lead_hours) if start or time else None
        upper = min(period.end, latest) if latest is not None else period.end
        overlaps_period = (time is None or (time <= upper
                           and (next_time is None or next_time > lower)))
        overlaps_windows = True
        if windows is not None and time is not None:
            i = bisect_left(window_ends, max(lower, time))
            overlaps_windows = (i < len(merged) and merged[i][0] <= upper
                                and (next_time is None or merged[i][0] < next_time))
        if ((cutoff is not None and cutoff > period.end)
                or (latest is not None and latest < period.start)
                or not overlaps_period or not overlaps_windows):
            skipped += 1
        else:
            candidates += 1
            yield path
        report()
    report(force=True)


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
