"""按有效时间查找 WRF—CLDAS 对应文件；不读取气象场、不计算指标。"""

from bisect import bisect_left
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import monotonic

from .catalog import iter_source_files
from .cldas import CldasFileInfo, parse_cldas_filename
from .config import PeriodConfig, SourceConfig
from .forecasts import forecast_init_time, run_order, valid_from
from .wrf import read_wrf_header


class TimeMatchError(ValueError):
    """重复版本或等距时次无法唯一选择。"""


@dataclass(frozen=True)
class WrfFrame:
    source: str
    path: Path
    time_index: int
    time: datetime
    model_start_time: datetime | None


@dataclass(frozen=True)
class TimeMatch:
    forecast: WrfFrame
    cldas_source: str
    family: str
    product: str
    observation: CldasFileInfo | None  # 超出容差时为空，保留未匹配记录。

    @property
    def offset(self) -> timedelta | None:
        """实况时间减预报时间；双方原始时间戳保持不变。"""
        if self.observation is None:
            return None
        return self.observation.time.astimezone(timezone.utc) - self.forecast.time.astimezone(timezone.utc)


def index_cldas_files(
    sources: Iterable[SourceConfig], *, product: str | None = None,
    period: PeriodConfig | None = None, progress: Callable[[str], None] | None = None,
) -> dict:
    """按来源/产品系列/有效时间选择最新制作版本，仅索引路径；可限定产品和时段。"""
    # 只索引来源/产品/时间及路径，不打开 CLDAS 文件或保留网格数组。
    groups: dict[tuple[str, str, str], dict[datetime, tuple[CldasFileInfo, Path | None]]] = {}
    for source in sources:
        found = False
        count = 0
        last_report = monotonic() - 5
        if progress is not None:
            progress(f"扫描 CLDAS 来源 {source.name}…")
        for path in iter_source_files(source):
            found = True
            count += 1
            now = monotonic()
            if progress is not None and (count == 1 or now - last_report >= 5):
                progress(f"索引 CLDAS 文件：已处理 {count:,} 个")
                last_report = now
            info = parse_cldas_filename(path, source.timezone)
            if product is not None and info.product != product:
                continue
            if period is not None and not period.start <= info.time <= period.end:
                continue
            key = (source.name, info.family, info.product)
            versions = groups.setdefault(key, {})
            time = info.time.astimezone(timezone.utc)
            previous, _ = versions.get(time, (None, None))
            if previous is None or info.production_time > previous.production_time:
                versions[time] = info, None
            elif info.production_time == previous.production_time:
                versions[time] = previous, info.path
        if not found:
            raise TimeMatchError(f"sources.{source.name}: 未找到 CLDAS 文件，请检查 root 和 patterns")
        if progress is not None:
            progress(f"CLDAS 来源 {source.name} 索引完成：检查 {count:,} 个文件")

    index = {}
    for key, versions in groups.items():
        times = tuple(sorted(versions))
        ordered = []
        for time in times:
            info, conflict = versions[time]
            if conflict is not None:
                raise TimeMatchError(f"CLDAS 最新制作时间仍有重复文件，无法唯一选择: {info.path} 与 {conflict}")
            ordered.append(info)
        index[key] = times, tuple(ordered)
    return index


def _nearest(times: tuple[datetime, ...], time: datetime, tolerance: timedelta) -> int | None:
    position = bisect_left(times, time)
    candidates = [i for i in (position - 1, position) if 0 <= i < len(times)]
    if not candidates:
        return None
    distances = [(abs(times[i] - time), i) for i in candidates]
    distance, index = min(distances)
    if distance > tolerance:
        return None
    if sum(delta == distance for delta, _ in distances) > 1:
        raise TimeMatchError(f"{time.isoformat()} 在容差内有两个等距 CLDAS 时次，无法唯一选择")
    return index


def iter_time_matches(sources: Iterable[SourceConfig], tolerance: timedelta) -> Iterator[TimeMatch]:
    """按有效时刻选满足最低时效的最新批次，再查找最近 CLDAS 时间候选。

    此处不认定统计样本，也不判定降水窗口；指标另做一对一或完整区间配对。
    """
    if not isinstance(tolerance, timedelta) or tolerance < timedelta(0):
        raise ValueError("时间配对容差须为非负 timedelta")
    enabled = [source for source in sources if source.enabled]
    wrf_sources = [source for source in enabled if source.kind == "wrf"]
    cldas_sources = [source for source in enabled if source.kind == "cldas"]
    if not wrf_sources or not cldas_sources:
        raise ValueError("时间配对需要同时启用 WRF 和 CLDAS 数据源")
    series = sorted(index_cldas_files(cldas_sources).items())
    for source in wrf_sources:
        found = False
        selected = {}
        seen = {}
        for path in iter_source_files(source):
            found = True
            header = read_wrf_header(path, source.timezone)
            init_time = forecast_init_time(source, path, header.model_start_time)
            cutoff = valid_from(source, init_time)
            latest = init_time + timedelta(hours=source.max_lead_hours) if init_time else None
            for time_index, time in enumerate(header.times):
                if cutoff is not None and time < cutoff:
                    continue
                if latest is not None and time > latest:
                    continue
                frame = WrfFrame(source.name, path, time_index, time, header.model_start_time)
                utc_time = time.astimezone(timezone.utc)
                key = init_time, utc_time
                if key in seen:
                    raise TimeMatchError(f"同一起报有重复时次 {time.isoformat()}: {seen[key]} 与 {path}")
                seen[key] = path
                previous = selected.get(utc_time)
                if previous is None or run_order(init_time) > run_order(previous[0]):
                    selected[utc_time] = init_time, frame
        if not found:
            raise TimeMatchError(f"sources.{source.name}: 未找到 WRF 文件，请检查 root 和 patterns")
        for utc_time, (_, frame) in sorted(selected.items()):
            for (name, family, product), (times, files) in series:
                nearest = _nearest(times, utc_time, tolerance)
                observation = files[nearest] if nearest is not None else None
                yield TimeMatch(frame, name, family, product, observation)
