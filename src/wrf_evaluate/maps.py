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

from .cldas import read_cldas_field, read_cldas_header
from .config import Config, SourceConfig
from .forecasts import run_order, valid_from
from .matching import iter_cldas_input_files
from .plotting import plot_style
from .variables import UNITS, VariableConfig, convert_value, relative_humidity
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


def _statistic(field: str, spec: VariableConfig) -> str:
    if field.startswith("surface_") and spec.time_kind in {"sum", "cumulative"}:
        return "mean"
    return "sum" if spec.time_kind == "cumulative" else spec.time_kind


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
        native, units = resolve_wrf_variable(
            spec, {name: item.values for name, item in fields.items()},
            {name: item.units for name, item in fields.items()},
        )
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
    indices = np.flatnonzero((axis >= lower) & (axis <= upper))
    if len(indices) < 2:
        raise ValueError("WRF 范围与 CLDAS 交集不足两个格点，无法绘制空间对比")
    return slice(int(indices[0]), int(indices[-1]) + 1)


def _observation_field(files, spec, field, header, selection, timezone):
    """派生湿度在同网格的原始场上计算，只加载 WRF 范围内的 CLDAS 子区域。"""
    values, units = {}, {}
    headers = {header.path: header}
    native_fields = {}
    for role, item in spec.source_inputs:
        path = files[role].path
        if path not in headers:
            headers[path] = read_cldas_header(path, timezone)
        other = headers[path]
        if (not np.array_equal(header.longitudes, other.longitudes)
                or not np.array_equal(header.latitudes, other.latitudes)):
            raise ValueError(f"{path}: CLDAS 湿度基础变量网格不一致，不能逐格组合")
        key = path, item.variable
        if key not in native_fields:
            native_fields[key] = read_cldas_field(path, item.variable, selection=selection)
        native = native_fields[key]
        values[role], units[role] = native.values, native.units
    if spec.inputs:
        return relative_humidity(values, units, spec)
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


def _draw(path, forecast, observation, extent, title, source_name, element, unit):
    values = [forecast[2], observation[2]]
    if any(value.count() == 0 for value in values):
        raise ValueError("空间图的比较区域内数值全部缺测")
    low, high = min(float(v.min()) for v in values), max(float(v.max()) for v in values)
    if low == high:
        low, high = (0, max(1, high)) if unit == "mm" else (low - 0.5, high + 0.5)
    with rc_context(plot_style()):
        figure = Figure(figsize=(12, 5), layout="constrained")
        FigureCanvasAgg(figure)
        axes = figure.subplots(1, 2)
        try:
            for ax, (lon, lat, value), label in zip(axes, (forecast, observation), (source_name, "cldas")):
                mesh = ax.pcolormesh(lon, lat, value, shading="auto", vmin=low, vmax=high, cmap="viridis")
                ax.set(xlim=extent[:2], ylim=extent[2:], title=label, xlabel="经度（°E）", ylabel="纬度（°N）")
                ax.set_aspect(1 / np.cos(np.deg2rad((extent[2] + extent[3]) / 2)))
            figure.suptitle(title)
            figure.colorbar(mesh, ax=axes, label=f"{element}（{unit}）", shrink=0.85)
            figure.savefig(path, dpi=160)
        finally:
            figure.clear()


def plot_maps(
    config: Config, sources: list[SourceConfig], cldas_index: dict,
    indices: dict[str, RunIndex], output_dir: Path, progress: Callable[[str], None],
) -> None:
    """与点位结果共用输出目录；严格同一时刻/时段，不重网格、不算空间误差。"""
    zone = ZoneInfo(config.display_timezone)
    wrf_sources = [source for source in sources if source.kind == "wrf"]
    cldas_sources = {source.name: source for source in sources if source.kind == "cldas"}
    if not cldas_sources:
        progress("未启用 CLDAS，跳过空间对比图")
        return
    plans = {}
    for source in cldas_sources.values():
        for field in config.elements:
            spec = source.variable(field)
            if spec is None:
                continue
            for _, time, files in iter_cldas_input_files(source.name, spec, cldas_index):
                if (config.period.start <= time <= config.period.end
                        and (time - config.period.start) % config.map_interval == timedelta(0)):
                    plans.setdefault((time, field), []).append((source, files))
    count = skipped = 0
    time = config.period.start
    with closing(WrfFieldReader()) as reader:
        while time <= config.period.end:
            for field in config.elements:
                found = False
                for source, files_by_role in plans.get((time, field), ()):
                    source_name = source.name
                    spec = source.variable(field)
                    found = True
                    if any(info is None for info in files_by_role.values()):
                        progress(f"跳过空间图 {field} {time.isoformat()}：缺少同一系列、同刻湿度基础产品")
                        skipped += 1
                        continue
                    start, end = _window(time, spec)
                    if start < config.period.start or end > config.period.end:
                        progress(f"跳过空间图 {field} {time.isoformat()}：完整时段超出 period")
                        skipped += 1
                        continue
                    info = files_by_role[spec.source_inputs[0][0]]
                    header = read_cldas_header(info.path, source.timezone)
                    for wrf in wrf_sources:
                        wrf_spec = wrf.variable(field)
                        if wrf_spec is None:
                            continue
                        if _statistic(field, wrf_spec) != _statistic(field, spec):
                            raise ValueError(f"空间图 {field}: WRF/CLDAS 时间含义不同，不能直接并排比较")
                        progress(f"绘制空间图 {field} {time.isoformat()}：{wrf.name} / {source_name}")
                        selected = _latest_field(indices[wrf.name], wrf, field, wrf_spec, start, end, progress, reader)
                        if selected is None:
                            progress("跳过：WRF 缺少严格对应的时次或完整时段")
                            skipped += 1
                            continue
                        init_time, forecast = selected
                        lon, lat, value = forecast
                        extent = (float(lon.min()), float(lon.max()), float(lat.min()), float(lat.max()))
                        columns = _crop_axis(header.longitudes, *extent[:2])
                        rows = _crop_axis(header.latitudes, *extent[2:])
                        observed = _observation_field(files_by_role, spec, field, header,
                                                      (rows, columns), source.timezone)
                        if field.startswith("surface_") and spec.time_kind == "sum":
                            observed = observed / (end - start).total_seconds()
                        observation = header.longitudes[columns], header.latitudes[rows], observed
                        label = f"{time.astimezone(zone):%Y-%m-%d %H:%M:%S}（{config.display_timezone}）"
                        if start != end:
                            label = f"{start.astimezone(zone):%Y-%m-%d %H:%M:%S} 至 {end.astimezone(zone):%Y-%m-%d %H:%M:%S}（{config.display_timezone}）"
                        title = f"{config.display_name(field)}空间对比\n{label}"
                        count += 1
                        path = Path(output_dir) / f"map_{field}_{time.astimezone(timezone.utc):%Y%m%dT%H%M%SZ}_{count:04d}.png"
                        source_label = wrf.name
                        if init_time is not None:
                            source_label += f"（起报 {init_time.astimezone(zone):%Y-%m-%d %H:%M}）"
                        _draw(path, forecast, observation, extent, title, source_label, config.display_name(field), UNITS[field])
                        progress(f"已生成空间图：{path.name}")
                if not found:
                    skipped += 1
                    progress(f"跳过空间图 {field} {time.isoformat()}：没有对应 CLDAS 产品/时次")
            time += config.map_interval
    progress(f"空间图完成：生成 {count} 张，跳过 {skipped} 组")
