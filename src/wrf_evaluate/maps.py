"""按配置时间绘制 WRF/CLDAS 空间场；仅加载当前比较所需的场。"""

from collections.abc import Callable
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import monotonic
from zoneinfo import ZoneInfo

import numpy as np
from matplotlib import rc_context
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure
from matplotlib.colors import Normalize

from .cldas import CldasFieldReader, read_cldas_field, read_cldas_header
from .config import Config, MapOptions, SourceConfig
from .forecasts import run_order, valid_from
from .matching import iter_cldas_input_files
from .plotting import plot_style
from .spatial import sample_rectilinear_grid
from .variables import UNITS, VariableConfig, convert_value, normalized_statistic, relative_humidity
from .wrf import WrfFieldReader, read_wrf_fields, resolve_wrf_variable


GridField = tuple[np.ndarray, np.ndarray, np.ma.MaskedArray]
FrameIndex = dict[datetime, tuple[Path, int]]
RunIndex = dict[datetime | None, FrameIndex]


def _converted(field: str, values: np.ma.MaskedArray, units: str, spec: VariableConfig) -> np.ma.MaskedArray:
    # 所有已支持单位均为线性换算，复用点位转换规则，避免逐格 Python 循环。
    zero = convert_value(field, 0.0, spec.units or units, spec.time_kind, spec.interval)
    one = convert_value(field, 1.0, spec.units or units, spec.time_kind, spec.interval)
    result = np.ma.masked_invalid(np.ma.asarray(values, dtype=np.float64), copy=False) * (one - zero) + zero
    if field == "relative_humidity_2m":
        result = np.ma.clip(result, 0.0, 100.0)
    if field == "precipitation" and np.ma.any(result < 0):
        raise ValueError("有效降水量不能为负，请核对缺测码与累计规则")
    return result


def _window(time: datetime, spec: VariableConfig) -> tuple[datetime, datetime]:
    if spec.time_kind == "instant":
        return time, time
    if spec.time_kind == "cumulative":
        raise ValueError("空间图的 CLDAS 映射须为 instant、mean 或 sum，不能用累计读数定义比较窗口")
    start = time if spec.timestamp == "start" else time - spec.interval
    return start, start + spec.interval


def required_wrf_windows(
    config: Config, wrf: SourceConfig, sources: list[SourceConfig], index: dict,
) -> tuple[tuple[datetime, datetime], ...]:
    """只索引实际有 CLDAS 输入的出图窗口；累计量保留整个窗口内的原生时次。"""
    windows = set()
    for source in sources:
        for field in config.fields_for("map"):
            spec = source.variable(field)
            if spec is None or wrf.variable(field) is None:
                continue
            intervals = {options.interval for _, _, options in config.plot_jobs("map", field)}
            for _, time, files in iter_cldas_input_files(source.name, spec, index):
                if (not any((time - config.period.start) % interval == timedelta(0) for interval in intervals)
                        or any(info is None for info in files.values())):
                    continue
                start, end = _window(time, spec)
                if config.period.start <= start <= end <= config.period.end:
                    windows.add((start, end))
    return tuple(sorted(windows))


def _forecast_field(
    frames: FrameIndex, field: str, spec: VariableConfig, start: datetime, end: datetime,
    progress: Callable[[str], None] | None = None,
    reader: WrfFieldReader | None = None,
) -> GridField | None:
    """完整连续覆盖目标窗口；累计量逐时次检查差分，拒绝清零和移动网格。"""
    if start == end:
        if spec.time_kind != "instant" or start not in frames:
            return None
        selected = [(start, start, start)]
    elif spec.time_kind == "cumulative":
        times = sorted(time for time in frames if start <= time <= end)
        if not times or times[0] != start or times[-1] != end:
            return None
        selected = [(time, time, time) for time in times]
    elif spec.time_kind in {"sum", "mean"}:
        selected = [(time, *_window(time, spec)) for time in sorted(frames)]
        exact = [(t, a, b) for t, a, b in selected if (a, b) == (start, end)]
        selected = exact or [(t, a, b) for t, a, b in selected if start <= a < b <= end]
        if (not selected or selected[0][1] != start or selected[-1][2] != end
                or any(a[2] != b[1] for a, b in zip(selected, selected[1:]))):
            return None
    else:
        return None
    grid = previous = total = None
    energy = field.startswith("surface_") and spec.time_kind in {"sum", "cumulative"}
    last_report = monotonic()
    for frame_index, (time, a, b) in enumerate(selected, start=1):
        now = monotonic()
        if progress is not None and now - last_report >= 5:
            progress(f"读取空间场 {field}：{frame_index}/{len(selected)} 个时次")
            last_report = now
        path, index = frames[time]
        read = reader.read if reader is not None else read_wrf_fields
        fields = read(path, (*spec.native_variables, "XLONG", "XLAT"), index)
        coordinates = fields["XLONG"].values, fields["XLAT"].values
        if any(np.ma.getmaskarray(axis).any() for axis in coordinates):
            raise ValueError(f"{path}: 空间图坐标含缺测")
        if grid is not None and not all(np.array_equal(x, y) for x, y in zip(grid, coordinates)):
            raise ValueError("目标时段内 WRF 网格移动，不能直接逐格累计或差分")
        grid = coordinates
        location = f"{path} Time[{index}] {time.isoformat()}"
        report = (lambda message: progress(f"警告：{location}: {message}")) if progress else None
        try:
            native, units = resolve_wrf_variable(
                spec, {name: item.values for name, item in fields.items()},
                {name: item.units for name, item in fields.items()}, report,
            )
        except ValueError as exc:
            raise ValueError(f"{location}: {exc}") from exc
        value = _converted(field, native, units, spec)
        if spec.time_kind == "cumulative":
            before, previous = previous, value
            if before is None:
                continue
            value = value - before
            if np.ma.any(value < 0):
                raise ValueError(f"{path}: 出现负累计差分，未自动修正清零或重启")
        elif spec.time_kind == "mean":
            value = value * (b - a).total_seconds()
        total = value if total is None else total + value
    if total is not None and (energy or spec.time_kind == "mean"):
        total = total / (end - start).total_seconds()
    return (*grid, total)


def _crop_axis(axis: np.ndarray, lower: float, upper: float) -> slice:
    # 留出边缘相邻格点，否则裁剪会将原本在 CLDAS 范围内的 WRF 边缘误判为域外。
    if upper < axis.min() or lower > axis.max():
        raise ValueError("WRF 范围与 CLDAS 没有交集，无法绘制空间对比")
    ascending = axis[0] < axis[-1]
    ordered = axis if ascending else axis[::-1]
    first = max(0, int(np.searchsorted(ordered, lower)) - 1)
    last = min(len(axis), int(np.searchsorted(ordered, upper, side="right")) + 1)
    if last - first < 2:
        raise ValueError("CLDAS 交集不足两个格点，无法绘制空间对比")
    return slice(first, last) if ascending else slice(len(axis) - last, len(axis) - first)


def _observation_field(files, spec, field, header, selection, timezone, progress=None, *, cache=None, reader=None):
    """派生湿度在同网格的原始场上计算，只加载 WRF 范围内的 CLDAS 子区域。"""
    values, units = {}, {}
    headers, native_fields = ({}, {}) if cache is None else cache
    headers[header.path] = header
    for role, item in spec.source_inputs:
        path = files[role].path
        if path not in headers:
            headers[path] = (reader.header if reader else read_cldas_header)(path, timezone)
        other = headers[path]
        if (not np.array_equal(header.longitudes, other.longitudes)
                or not np.array_equal(header.latitudes, other.latitudes)):
            raise ValueError(f"{path}: CLDAS 湿度基础变量网格不一致，不能逐格组合")
        key = path, item.variable, tuple((axis.start, axis.stop, axis.step) for axis in selection)
        if key not in native_fields:
            native_fields[key] = (reader.read if reader else read_cldas_field)(path, item.variable, selection=selection)
        native = native_fields[key]
        values[role], units[role] = native.values, native.units
    if spec.inputs:
        location = f"CLDAS {header.time.isoformat()} ({', '.join(str(path) for path in headers)})"
        report = (lambda message: progress(f"警告：{location}: {message}")) if progress else None
        try:
            return relative_humidity(values, units, spec, report)
        except ValueError as exc:
            raise ValueError(f"{location}: {exc}") from exc
    return _converted(field, values["value"], units["value"], spec)


def _latest_field(
    runs: RunIndex, source: SourceConfig, field: str, spec: VariableConfig,
    start: datetime, end: datetime, progress: Callable[[str], None],
    reader: WrfFieldReader | None = None,
) -> tuple[datetime | None, GridField] | None:
    for init_time, frames in sorted(runs.items(), key=lambda item: run_order(item[0]), reverse=True):
        cutoff = valid_from(source, init_time)
        if cutoff is not None and start < cutoff:
            continue
        if init_time is not None and end > init_time + timedelta(hours=source.max_lead_hours):
            continue
        result = _forecast_field(frames, field, spec, start, end, progress, reader)
        if result is not None:
            return init_time, result
    return None


class _MapFigure:
    """逐行加入面板，只保留画布数据；共用 Normalize 保证整张图色标一致。"""

    def __init__(self, times, element, unit, options, timezone="UTC"):
        self.options, self.unit, self.element = options, unit, element
        self.timezone = timezone
        self.times = times
        self.figure = Figure(figsize=(options.figsize[0], options.figsize[1] * len(times)), layout="constrained")
        FigureCanvasAgg(self.figure)
        self.axes = self.figure.subplots(len(times), 3, squeeze=False)
        self.time_indices = {time: index for index, time in enumerate(times)}
        self.absolute_norm, self.difference_norm = Normalize(), Normalize()
        self.absolute_mesh = self.difference_mesh = None
        self.low, self.high, self.error = float("inf"), -float("inf"), 0.0
        self.count = 0
        for row in self.axes:
            for ax in row:
                ax.set_axis_off()

    def add(self, time, forecast, observation, extent, label, source_name, observation_name):
        row = self.axes[self.time_indices[time]]
        lon, lat, predicted = forecast
        observed = sample_rectilinear_grid(*observation, lon, lat)
        difference = predicted - observed
        fields = (forecast, observation, (lon, lat, difference))
        labels = (source_name, observation_name, "预报−实况")
        for column, (ax, (x, y, value), title) in enumerate(zip(row, fields, labels)):
            ax.set_axis_on()
            norm = self.difference_norm if column == 2 else self.absolute_norm
            cmap = self.options.difference_cmap if column == 2 else self.options.cmap
            mesh = ax.pcolormesh(x, y, value, shading="auto", norm=norm, cmap=cmap)
            if value.count():
                if column == 2:
                    self.error = max(self.error, float(np.ma.abs(value).max()))
                else:
                    self.low = min(self.low, float(value.min()))
                    self.high = max(self.high, float(value.max()))
            if column == 2:
                self.difference_mesh = mesh
            else:
                self.absolute_mesh = mesh
            ax.set(xlim=extent[:2], ylim=extent[2:], title=title,
                   xlabel="经度（°E）", ylabel="纬度（°N）")
            ax.set_aspect(1 / np.cos(np.deg2rad((extent[2] + extent[3]) / 2)))
        row[0].set_ylabel(f"{label}\n纬度（°N）")
        self.count += 1

    def save(self, path):
        if not self.count:
            return False
        if not np.isfinite(self.low):
            raise ValueError("空间图比较区域全部缺测")
        low, high = self.low, self.high
        if low == high:
            low, high = (0, max(1, high)) if self.unit == "mm" else (low - 0.5, high + 0.5)
        low, high = self.options.color_limits or (low, high)
        self.absolute_norm.vmin, self.absolute_norm.vmax = low, high
        error = self.options.difference_limit or self.error or 1.0
        self.difference_norm.vmin, self.difference_norm.vmax = -error, error
        for time, row in zip(self.times, self.axes):
            if not row[0].axison:
                for ax in row:
                    ax.text(0.5, 0.5, f"{time:%Y-%m-%d %H:%M}\n无完整对应数据", ha="center", va="center", transform=ax.transAxes)
        self.figure.suptitle(f"{self.element}空间对比（{self.timezone}）")
        self.figure.colorbar(self.absolute_mesh, ax=list(self.axes[:, :2].flat),
                             label=f"{self.element}（{self.unit}）", orientation="horizontal", fraction=0.035, pad=0.03)
        self.figure.colorbar(self.difference_mesh, ax=list(self.axes[:, 2]),
                             label=f"预报−实况（{self.unit}）", orientation="horizontal", fraction=0.035, pad=0.03)
        self.figure.savefig(path, dpi=self.options.dpi)
        return True

    def close(self):
        self.figure.clear()


def plot_maps(config: Config, sources: list[SourceConfig], cldas_index: dict,
              indices: dict[str, RunIndex], output_dir: Path, progress: Callable[[str], None]) -> None:
    """同一任务、来源系列汇总所有时刻；差值以最近 CLDAS 格点落到 WRF 网格。"""
    zone = ZoneInfo(config.display_timezone)
    wrf_sources = [s for s in sources if s.kind == "wrf"]
    cldas_sources = [s for s in sources if s.kind == "cldas"]
    schedule, tasks = {}, {}
    for suffix, field, options in config.plot_jobs("map"):
        times, time = [], config.period.start
        while time <= config.period.end:
            times.append(time)
            schedule.setdefault(time, set()).add(field)
            time += options.interval
        tasks[suffix] = field, options, tuple(times)
    plans = {}
    for source in cldas_sources:
        for field in config.fields_for("map"):
            spec = source.variable(field)
            if spec is None:
                continue
            for family, time, files in iter_cldas_input_files(source.name, spec, cldas_index):
                if field in schedule.get(time, ()):
                    plans.setdefault((time, field), []).append((source, family, files))
    figures = {}
    count = skipped = 0

    def save_figure(key):
        nonlocal count
        figure = figures.pop(key)
        suffix, _, _, _, stamp = key
        field = tasks[suffix][0]
        path = Path(output_dir) / f"map_{field}{suffix}{stamp}_{count + 1:04d}.png"
        try:
            if figure.save(path):
                count += 1
                progress(f"已生成空间图：{path.name}")
        finally:
            figure.close()

    cldas_reader = CldasFieldReader()
    with rc_context(plot_style()), closing(WrfFieldReader()) as reader:
        try:
            for time, fields in sorted(schedule.items()):
                # 本时刻各变量、来源、任务共享 CLDAS 原始子场；时刻结束立即释放。
                cldas_reader.close()
                cache = ({}, {})
                for field in sorted(fields):
                    candidates = plans.get((time, field), ())
                    if not candidates:
                        progress(f"跳过空间图 {field} {time.isoformat()}：没有对应 CLDAS 产品/时次")
                        skipped += 1
                    for source, family, files in candidates:
                        spec = source.variable(field)
                        if any(info is None for info in files.values()):
                            progress(f"跳过空间图 {field} {time.isoformat()}：缺少同系列、同刻基础产品")
                            skipped += 1
                            continue
                        start, end = _window(time, spec)
                        if start < config.period.start or end > config.period.end:
                            progress(f"跳过空间图 {field} {time.isoformat()}：完整时段超出 period")
                            skipped += 1
                            continue
                        for wrf in wrf_sources:
                            wrf_spec = wrf.variable(field)
                            if wrf_spec is None:
                                continue
                            if normalized_statistic(field, wrf_spec) != normalized_statistic(field, spec):
                                raise ValueError(f"空间图 {field}: WRF/CLDAS 时间含义不同")
                            selected = _latest_field(indices[wrf.name], wrf, field, wrf_spec, start, end, progress, reader)
                            if selected is None:
                                progress(f"跳过空间图 {field} {time.isoformat()}：WRF 无完整对应窗口")
                                skipped += 1
                                continue
                            init_time, forecast = selected
                            lon, lat, _ = forecast
                            extent = float(lon.min()), float(lon.max()), float(lat.min()), float(lat.max())
                            info = files[spec.source_inputs[0][0]]
                            if info.path not in cache[0]:
                                cache[0][info.path] = cldas_reader.header(info.path, source.timezone)
                            header = cache[0][info.path]
                            columns = _crop_axis(header.longitudes, *extent[:2])
                            rows = _crop_axis(header.latitudes, *extent[2:])
                            observed = _observation_field(files, spec, field, header, (rows, columns), source.timezone,
                                                          progress, cache=cache, reader=cldas_reader)
                            if field.startswith("surface_") and spec.time_kind == "sum":
                                observed = observed / (end - start).total_seconds()
                            observation = header.longitudes[columns], header.latitudes[rows], observed
                            label = f"{time.astimezone(zone):%Y-%m-%d %H:%M}"
                            if start != end:
                                label = f"{start.astimezone(zone):%Y-%m-%d %H:%M} 至\n{end.astimezone(zone):%Y-%m-%d %H:%M}"
                            source_label = wrf.name
                            if init_time is not None:
                                source_label += f"\n起报 {init_time.astimezone(zone):%Y-%m-%d %H:%M}"
                            for suffix, (element, options, times) in tasks.items():
                                if element != field or time not in times:
                                    continue
                                stamp = time.astimezone(timezone.utc).strftime("_%Y%m%dT%H%M%SZ") if options.layout == "separate" else ""
                                key = suffix, wrf.name, source.name, family, stamp
                                if key not in figures:
                                    display_times = tuple(t.astimezone(zone) for t in ((time,) if stamp else times))
                                    figures[key] = _MapFigure(display_times, config.display_name(field), UNITS[field], options,
                                                              config.display_timezone)
                                figures[key].add(time.astimezone(zone), forecast, observation, extent, label, source_label,
                                                 "cldas" if len(cldas_sources) == 1 else f"{source.name} / {family}")
                            progress(f"已加入空间面板 {field} {time.isoformat()}：{wrf.name} / {source.name}")
                if cache[1]:
                    cache[1].clear()
                # 单时次模式及时保存释放，不把整段时间的画布留在内存中。
                for key in tuple(figures):
                    if tasks[key[0]][1].layout == "separate":
                        save_figure(key)
            for key in tuple(figures):
                save_figure(key)
        finally:
            cldas_reader.close()
            for figure in figures.values():
                figure.close()
    progress(f"空间图完成：生成 {count} 张，跳过 {skipped} 组")
