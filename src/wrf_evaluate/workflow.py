"""配置到点位曲线和指标的直接流程；逐点处理，工作簿和文件索引复用。"""

from collections.abc import Callable, Iterator
from contextlib import closing
from dataclasses import dataclass
from datetime import timedelta, timezone
from pathlib import Path
import re
from time import monotonic

from .catalog import iter_source_files
from .cldas import read_cldas_points
from .config import Config, ConfigError, PointConfig, SourceConfig
from .evaluation import Comparison, compare_forecast, write_metrics
from .forecasts import eligible_series, forecast_init_time, iter_wrf_files, select_latest, valid_from
from .matching import index_cldas_files
from .maps import plot_maps
from .output import create_run_directory
from .plotting import plot_series
from .series import Series, normalize_series
from .station import iter_regional_observations
from .variables import convert_value, validate_variable
from .wrf import iter_wrf_points_frames, resolve_wrf_variable


@dataclass(frozen=True)
class PlotResult:
    point: PointConfig
    element: str
    path: Path
    series: tuple[Series, ...]
    metrics_path: Path
    comparisons: tuple[Comparison, ...]


def _fields(source: SourceConfig, config: Config) -> tuple[str, ...]:
    return tuple(field for field in config.elements if source.variable(field) is not None)


def _validate_sources(config: Config) -> list[SourceConfig]:
    if config.period is None or not config.points:
        raise ConfigError("plot 需要配置 [period] 的 start/end 和至少一个 [[points]]")
    sources = [source for source in config.sources if source.enabled
               and (source.kind not in {"regional", "national"} or source.files)]
    for source in sources:
        if source.kind in {"ec", "national"}:
            raise ConfigError(f"sources.{source.name}: 尚未接入 {source.kind} 读取，请暂时禁用此来源")
        for field in _fields(source, config):
            spec = source.variable(field)
            try:
                validate_variable(field, spec)
                if spec.units is not None:
                    convert_value(field, None, spec.units, spec.time_kind, spec.interval)
                if source.kind == "regional" and spec.units is None:
                    raise ValueError("Excel 须填写 units")
                if source.kind == "cldas" and spec.product is None:
                    raise ValueError("CLDAS 须填写文件名产品代码 product")
            except ValueError as exc:
                raise ConfigError(f"sources.{source.name}.variables.{field}: {exc}") from exc
    for field in config.elements:
        forecasts = [source for source in sources if source.kind == "wrf" and field in _fields(source, config)]
        observations = [source for source in sources if source.kind != "wrf" and field in _fields(source, config)]
        if not forecasts or not observations:
            raise ConfigError(f"{field}: 须至少有一个 WRF 和一个实况来源的变量映射")
        has_cldas = any(source.kind == "cldas" for source in observations)
        if not has_cldas:
            for point in config.points:
                if point.station_id is None:
                    raise ConfigError(f"点位 {point.name}: {field} 没有实况，请填写 station_id 或启用 CLDAS")
        if any(source.kind == "regional" for source in observations) and not any(p.station_id for p in config.points):
            raise ConfigError("已指定实测 Excel，请在需要实测对比的 points 中填写 station_id")
    return sources


def _notify(progress: Callable[[str], None] | None, message: str) -> None:
    if progress is not None:
        progress(message)


def _regional_series(
    sources: list[SourceConfig], config: Config,
    progress: Callable[[str], None] | None = None,
) -> dict:
    station_ids = {point.station_id for point in config.points if point.station_id}
    result = {}
    if not station_ids:
        return result
    for source in sources:
        fields = _fields(source, config)
        if source.kind != "regional" or not fields:
            continue
        specs = {field: source.variable(field) for field in fields}
        records = {(station_id, field): [] for station_id in station_ids for field in fields}
        count = 0
        last_report = monotonic() - 5
        _notify(progress, f"读取站点来源 {source.name}…")
        for path in iter_source_files(source):
            count += 1
            now = monotonic()
            if count == 1 or now - last_report >= 5:
                _notify(progress, f"读取站点 Excel：第 {count:,} 个，{path.name}")
                last_report = now
            with closing(iter_regional_observations(
                path, source.timezone, dict(source.column_mapping), fields=fields,
                station_ids=station_ids, period=config.period, progress=progress,
            )) as rows:
                for row in rows:
                    for field in fields:
                        spec = specs[field]
                        records[row.station_id, field].append(
                            (row.time, row.values[field], spec.units, f"{path} 第 {row.row} 行")
                        )
        _notify(progress, f"站点观测读取完成：检查 {count:,} 个 Excel")
        for (station_id, field), rows in records.items():
            if not rows:
                raise ValueError(
                    f"sources.{source.name}: 站点 {station_id} 在配置时段内无记录；"
                    "请核对 files、station_id 和 period"
                )
            label = _label(source, field, station_id=station_id)
            result.setdefault((station_id, field), []).append(
                normalize_series(label, field, specs[field], rows, config.period)
            )
    return result


def _label(source: SourceConfig, field: str, *, station_id: str | None = None) -> str:
    if source.kind == "cldas":
        return "cldas"
    spec = source.variable(field)
    name = source.name
    if station_id:
        name += f"（站号 {station_id}）"
    if spec.time_kind == "mean":
        name += f"（{spec.interval.total_seconds() / 60:g}分钟平均）"
    elif spec.time_kind == "sum":
        name += f"（{spec.interval.total_seconds() / 60:g}分钟量）"
    elif spec.time_kind == "cumulative":
        name += "（累计差分）"
    elif field == "wind_speed_10m":
        name += "（瞬时）"
    return name


def _wrf_series(
    source: SourceConfig, config: Config,
    progress: Callable[[str], None] | None = None,
    *, map_frames: dict | None = None,
) -> dict:
    fields = _fields(source, config)
    run_records = {}
    specs = {field: source.variable(field) for field in fields}
    cumulative = any(spec.time_kind == "cumulative" for spec in specs.values())
    run_locations = {}
    names = tuple(dict.fromkeys(name for spec in specs.values() for name in spec.native_variables))
    if not names:
        return {}
    frames_read = 0
    files_read = 0
    coordinates = tuple((point.longitude, point.latitude) for point in config.points)
    last_report = monotonic() - 5
    reported_starts = set()
    _notify(progress, f"读取 WRF 来源 {source.name}：一次提取 {len(coordinates)} 个点位…")
    for path in iter_wrf_files(source, config.period, progress):
        files_read += 1
        now = monotonic()
        if files_read == 1 or now - last_report >= 5:
            _notify(progress, f"{source.name}：打开候选文件 {files_read:,}，已取 {frames_read:,} 个时次；当前 {path.name}")
            last_report = now
        directory_start = forecast_init_time(source, path, require=False)
        with closing(iter_wrf_points_frames(
            path, names, source.timezone, coordinates, period=config.period,
            init_time=directory_start, min_lead_hours=source.min_lead_hours,
            max_lead_hours=source.max_lead_hours,
        )) as frames:
            start = None
            for batch in frames:
                frames_read += 1
                if start is None:
                    start = forecast_init_time(source, path, batch[0].model_start_time)
                eligible_from = valid_from(source, start)
                if eligible_from is not None and batch[0].time < eligible_from:
                    continue
                if start not in reported_starts:
                    _notify(progress, f"{source.name} 起报：{start.isoformat() if start else '未提供'}；"
                            f"最低预报时效 {source.min_lead_hours:g} 小时")
                    reported_starts.add(start)
                if directory_start is not None and batch[0].model_start_time not in (None, directory_start):
                    mismatch = (directory_start, batch[0].model_start_time)
                    if mismatch not in reported_starts:
                        _notify(progress, f"{path}: 采用目录/配置起报 {directory_start.isoformat()}；"
                                f"文件模式起始时间为 {batch[0].model_start_time.isoformat()}")
                        reported_starts.add(mismatch)
                if start not in run_records:
                    run_records[start] = {(point.name, field): [] for point in config.points for field in fields}
                if map_frames is not None:
                    map_frames.setdefault(start, {})[batch[0].time] = (path, batch[0].time_index)
                now = monotonic()
                if now - last_report >= 5:
                    _notify(progress, f"读取 WRF 时次：已处理 {frames_read:,} 个")
                    last_report = now
                for point, frame in zip(config.points, batch, strict=True):
                    if cumulative:
                        location = frame.row, frame.column, frame.grid_coordinates
                        key = start, point.name
                        if run_locations.setdefault(key, location) != location:
                            raise ValueError(f"{path}: 点位 {point.name} 的 WRF 取值网格发生移动，不能对不同格点的累计量差分")
                    for field in fields:
                        value, units = resolve_wrf_variable(specs[field], frame.values, frame.units)
                        run_records[start][point.name, field].append(
                            (frame.time, value, units, f"{path} Time[{frame.time_index}]")
                        )
    _notify(progress, f"{source.name} 取值完成：打开 {files_read:,} 个候选文件，时段内 {frames_read:,} 个时次")
    if not run_records:
        raise ValueError(f"sources.{source.name}: 配置时段内没有满足 min_lead_hours 的 WRF 时次")
    result = {}
    for point in config.points:
        for field in fields:
            runs = []
            for start, records in run_records.items():
                try:
                    curve = normalize_series(
                        _label(source, field), field, source.variable(field), records[point.name, field],
                        config.period, allow_empty=True,
                    )
                    latest = start + timedelta(hours=source.max_lead_hours) if start is not None else None
                    curve = eligible_series(curve, valid_from(source, start), latest)
                except ValueError as exc:
                    raise ValueError(f"点位 {point.name}: {exc}") from exc
                if curve is not None:
                    runs.append((start, curve))
            if not runs:
                raise ValueError(f"点位 {point.name}: {field} 的有效预报时效内没有可用 WRF 记录")
            try:
                result[point.name, field] = select_latest(runs)
            except ValueError as exc:
                raise ValueError(f"点位 {point.name}: {exc}") from exc
    return result


def _cldas_series(
    cldas: list[SourceConfig], index: dict, config: Config,
    progress: Callable[[str], None] | None = None,
) -> dict:
    """每个产品文件只打开一次，提取全部点位及所需变量。"""
    cldas_curves = {}
    point_coordinates = tuple((point.longitude, point.latitude) for point in config.points)
    for source in cldas:
        fields = _fields(source, config)
        groups = {}
        for field in fields:
            spec = source.variable(field)
            for key, (_, files) in index.items():
                if key[0] == source.name and key[2] == spec.product:
                    groups.setdefault(key, {"files": files, "fields": []})["fields"].append(field)
        for (_, family, _), group in sorted(groups.items()):
            fields = tuple(group["fields"])
            names = tuple(dict.fromkeys(source.variable(field).variable for field in fields))
            samples_by_field = {field: [[] for _ in config.points] for field in fields}
            files = group["files"]
            last_report = monotonic() - 5
            _notify(progress, f"读取 CLDAS {source.name}/{family}：{len(files)} 个文件 × {len(config.points)} 个点位")
            for file_index, info in enumerate(files, start=1):
                now = monotonic()
                if file_index == 1 or now - last_report >= 5:
                    _notify(progress, f"读取 CLDAS：{file_index}/{len(files)} 个文件")
                    last_report = now
                samples = read_cldas_points(info.path, names, point_coordinates, source.timezone)
                for field in fields:
                    variable = source.variable(field).variable
                    for point_index, sample in enumerate(samples[variable]):
                        samples_by_field[field][point_index].append(
                            (sample.time, sample.value, sample.units, str(info.path))
                        )
            for field, point_rows in samples_by_field.items():
                for point_index, rows in enumerate(point_rows):
                    label = _label(source, field)
                    if len(cldas) > 1 or sum(key[2] == source.variable(field).product for key in groups) > 1:
                        label += f" / {source.name} / {family}"
                    key = (config.points[point_index].name, field)
                    cldas_curves.setdefault(key, []).append(
                        normalize_series(label, field, source.variable(field), rows, config.period)
                    )
    return cldas_curves


def plot_comparisons(
    config: Config, *, progress: Callable[[str], None] | None = None,
) -> Iterator[PlotResult]:
    """各来源按原生时次叠图；指标另行配对，可选报告处理进度。"""
    _notify(progress, f"开始检验：{len(config.points)} 个点位，{len(config.elements)} 个要素。")
    sources = _validate_sources(config)
    wrf = [source for source in sources if source.kind == "wrf" and _fields(source, config)]
    cldas = [source for source in sources if source.kind == "cldas" and _fields(source, config)]
    if cldas:
        _notify(progress, "索引 CLDAS 文件并选择最新制作版本…")
    index = index_cldas_files(cldas, period=config.period, progress=progress) if cldas else {}
    for source in cldas:
        for field in _fields(source, config):
            product = source.variable(field).product
            if not any(key[0] == source.name and key[2] == product for key in index):
                raise ValueError(f"sources.{source.name}: 配置时段内没有 CLDAS {product} 产品")
    station_curves = _regional_series(sources, config, progress)
    forecasts = {(point.name, field): [] for point in config.points for field in config.elements}
    map_frames = {}
    for source in wrf:
        map_frames[source.name] = {}
        for key, curve in _wrf_series(source, config, progress, map_frames=map_frames[source.name]).items():
            forecasts[key].append(curve)
    cldas_curves = _cldas_series(cldas, index, config, progress)
    output_dir = None
    stamp = f"{config.period.start.astimezone(timezone.utc):%Y%m%dT%H%M%SZ}_{config.period.end.astimezone(timezone.utc):%Y%m%dT%H%M%SZ}"
    for point_index, point in enumerate(config.points, start=1):
        try:
            _notify(progress, f"处理点位 {point_index}/{len(config.points)}：{point.name}")
            for field in config.elements:
                observations = list(cldas_curves.get((point.name, field), ()))
                observations.extend(station_curves.get((point.station_id, field), ()))
                _notify(progress, f"计算 {point.name} 的 {config.display_name(field)}指标…")
                comparisons = tuple(
                    comparison
                    for forecast in forecasts[point.name, field] for observation in observations
                    for comparison in compare_forecast(
                        forecast, observation, config.time_tolerance, field, config.precipitation_thresholds_mm,
                    )
                )
                curves = tuple(forecast.series for forecast in forecasts.pop((point.name, field))) + tuple(observations)
                name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", point.name).strip(" .") or "point"
                if output_dir is None:
                    _notify(progress, "创建本次独立输出目录…")
                    output_dir = create_run_directory(config)
                path = output_dir / f"{field}_{point_index:03d}_{name}_{stamp}.png"
                _notify(progress, f"绘制 {point.name} 的 {config.display_name(field)}…")
                plot_series(path, point, curves, config.display_timezone, period=config.period,
                            element_name=config.display_name(field))
                _notify(progress, f"写入 {path.name} 对应 CSV…")
                metrics_path = write_metrics(path.with_suffix(".csv"), comparisons)
                yield PlotResult(point, field, path, curves, metrics_path, comparisons)
        except (OSError, ValueError) as exc:
            raise ValueError(f"点位 {point.name}: {exc}") from exc
    if output_dir is not None:
        plot_maps(config, sources, index, map_frames, output_dir, lambda message: _notify(progress, message))
