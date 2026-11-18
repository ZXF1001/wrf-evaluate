"""配置到点位曲线和指标的直接流程；逐点处理，工作簿和文件索引复用。"""

from collections.abc import Callable, Iterator
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
from time import monotonic
from zoneinfo import ZoneInfo

from .catalog import iter_source_files
from .cldas import read_cldas_points
from .config import Config, ConfigError, PointConfig, SourceConfig
from .evaluation import Comparison, compare_forecast, write_metrics
from .forecasts import eligible_series, forecast_init_time, iter_wrf_files, run_order, select_latest, valid_from
from .matching import index_cldas_files, iter_cldas_input_files
from .maps import plot_maps, required_wrf_windows
from .output import create_run_directory
from .plotting import plot_series
from .series import Series, hourly_precipitation, normalize_series
from .station import iter_station_observations
from .variables import convert_value, normalized_statistic, relative_humidity, validate_variable
from .wrf import WrfPointLocator, iter_wrf_points_frames, read_wrf_header, resolve_wrf_variable


@dataclass(frozen=True)
class PlotResult:
    point: PointConfig
    element: str
    paths: tuple[Path, ...]
    series: tuple[Series, ...]
    metrics_path: Path | None
    comparisons: tuple[Comparison, ...]

    @property
    def path(self) -> Path | None:
        return self.paths[0] if self.paths else None


def _fields(source: SourceConfig, config: Config, *, points_only: bool = True) -> tuple[str, ...]:
    selected = config.point_elements if points_only or source.kind in {"station"} else config.elements
    return tuple(field for field in selected if source.variable(field) is not None)


def _validate_sources(config: Config) -> list[SourceConfig]:
    if config.period is None:
        raise ConfigError("plot 需要配置 [period] 的 start/end")
    if config.point_elements and not config.points:
        raise ConfigError("站点曲线或指标需要至少一个 [[station]]；仅空间图时可省略")
    sources = [source for source in config.sources if source.enabled]
    if config.point_elements:
        sources = list(dict.fromkeys(sources + [p.excel for p in config.points if p.excel is not None]))
    for source in sources:
        for field in _fields(source, config, points_only=False):
            spec = source.variable(field)
            try:
                validate_variable(field, spec)
                if spec.units is not None:
                    convert_value(field, None, spec.units, spec.time_kind, spec.interval)
                if source.kind in {"station"} and spec.units is None:
                    raise ValueError("Excel 须填写 units")
                if source.kind == "cldas" and any(item.product is None for _, item in spec.source_inputs):
                    raise ValueError("CLDAS 须填写文件名产品代码 product")
            except ValueError as exc:
                location = (f"station.excel.variables.{field}" if source.kind == "station"
                            else f"elements.{field}.sources.{source.name}")
                raise ConfigError(f"{location}: {exc}") from exc
    for field in config.point_elements:
        forecasts = [source for source in sources if source.kind == "wrf" and field in _fields(source, config)]
        observations = [source for source in sources if source.kind != "wrf" and field in _fields(source, config)]
        if not forecasts or not observations:
            raise ConfigError(f"{field}: 须至少有一个 WRF 和一个实况来源的变量映射")
        has_cldas = any(source.kind == "cldas" for source in observations)
        if not has_cldas:
            for point in config.points:
                if not any(
                    source == point.excel
                    for source in observations if source.kind in {"station"}
                ):
                    raise ConfigError(f"点位 {point.name}: {field} 没有实况，请核对本站 Excel、station_id 或启用 CLDAS")
    for field in config.fields_for("map"):
        for kind in ("wrf", "cldas"):
            if not any(source.kind == kind and source.variable(field) is not None for source in sources):
                raise ConfigError(f"{field}: 空间图需要 WRF 和 CLDAS 变量映射，缺少 {kind} 来源")
    # 已知不兼容的时间含义在扫描和取值前报错；仅叠图允许各来源各用自己的口径。
    for kind in ("metrics", "map"):
        for field in config.fields_for(kind):
            forecasts = [s for s in sources if s.kind == "wrf" and s.variable(field) is not None]
            observations = [s for s in sources if s.kind != "wrf" and s.variable(field) is not None
                            and (kind == "metrics" or s.kind == "cldas")]
            for obs in observations:
                observed = obs.variable(field)
                if kind == "map" and observed.time_kind == "cumulative":
                    raise ConfigError(f"{field}: 空间图的 CLDAS 须明确 instant/mean/sum 时间窗口")
                for forecast in forecasts:
                    statistics = {normalized_statistic(field, forecast.variable(field)),
                                  normalized_statistic(field, observed)}
                    if len(statistics) > 1 and not (
                        kind == "metrics" and field == "wind_speed" and statistics == {"instant", "mean"}
                    ):
                        raise ConfigError(f"{field}: {forecast.name} / {obs.name} 时间含义不同，不能计算{kind}")
    return sources


def _wrf_map_index(
    source: SourceConfig, config: Config, progress,
    windows: tuple[tuple[datetime, datetime], ...],
) -> dict:
    """仅空间图的来源只索引文件内时间，不读点位、坐标或气象场。"""
    runs = {}
    count = 0
    last_report = monotonic() - 5
    for path in iter_wrf_files(source, config.period, progress, windows=windows):
        count += 1
        now = monotonic()
        if now - last_report >= 5:
            _notify(progress, f"{source.name} 空间图时间索引：打开第 {count} 个候选文件，{path.name}")
            last_report = now
        header = read_wrf_header(path, source.timezone)
        start = forecast_init_time(source, path, header.model_start_time)
        cutoff = valid_from(source, start)
        latest = start + timedelta(hours=source.max_lead_hours) if start else None
        for index, time in enumerate(header.times):
            if (not config.period.start <= time <= config.period.end
                    or (cutoff is not None and time < cutoff)
                    or (latest is not None and time > latest)):
                continue
            frames = runs.setdefault(start, {})
            if time in frames:
                raise ValueError(f"同一起报有重复时次 {time.isoformat()}: {frames[time][0]} 与 {path}")
            frames[time] = path, index
    _notify(progress, f"{source.name} 空间图索引完成：{count} 个候选文件，{sum(map(len, runs.values()))} 个时次")
    # 无可比较时次由空间图流程逐项说明，避免打开无关文件后才发现没有出图任务。
    return runs


def _notify(progress: Callable[[str], None] | None, message: str) -> None:
    if progress is not None:
        progress(message)


def _station_series(
    sources: list[SourceConfig], config: Config,
    progress: Callable[[str], None] | None = None,
) -> dict:
    # 文件和解析规则相同时合并站号筛选；不同站点的表头/时区不互相污染。
    jobs, records = {}, {}
    for source in sources:
        fields = _fields(source, config)
        if source.kind not in {"station"} or not fields:
            continue
        targets = [p for p in config.points if p.excel == source]
        if not targets:
            continue
        for point in targets:
            for field in fields:
                records[point.name, source, field] = []
        for path in iter_source_files(source):
            key = path, source.timezone, source.sheet, source.variables, fields, source.time_format, source.missing_values
            recipients = jobs.setdefault(key, {})
            for point in targets:
                recipients.setdefault(point.station_id, set()).add((point.name, source))
    station_ids = {p.name: p.station_id for p in config.points}
    last_report = monotonic() - 5
    for count, ((path, zone, sheet, variables, fields, time_format, missing_values), recipients) in enumerate(jobs.items(), 1):
        now = monotonic()
        if count == 1 or now - last_report >= 5:
            _notify(progress, f"读取站点 Excel：{count:,}/{len(jobs):,}，{path.name}")
            last_report = now
        mapping = {name: spec.variable for name, spec in variables}
        specs = dict(variables)
        with closing(iter_station_observations(
            path, zone, mapping, fields=fields, sheet_name=sheet,
            station_ids=None if None in recipients else set(recipients),
            single_station=None in recipients, period=config.period, progress=progress,
            time_format=time_format, missing_values=missing_values,
        )) as rows:
            for row in rows:
                targets = recipients.get(row.station_id, set()) | recipients.get(None, set())
                for point_name, source in targets:
                    detected_id = station_ids[point_name]
                    if detected_id is not None and detected_id != row.station_id:
                        raise ValueError(
                            f"站点 {point_name}: Excel 文件的站号不一致（{detected_id}、{row.station_id}），"
                            "请核对 excel.files 或配置 station_id 筛选"
                        )
                    station_ids[point_name] = row.station_id
                    for field in fields:
                        records[point_name, source, field].append(
                            (row.time, row.values[field], specs[field].units, f"{path} 第 {row.row} 行")
                        )
    if jobs:
        _notify(progress, f"站点观测读取完成：检查 {len(jobs):,} 个 Excel 读取任务")
    result = {}
    for (point_name, source, field), rows in records.items():
        station_id = station_ids[point_name]
        if not rows:
            raise ValueError(
                f"站点 {point_name}（station_id={station_id}）在配置时段内无记录；"
                "请核对 excel.files、station_id 和 period"
            )
        label = _label(source, field, station_id=station_id)
        result.setdefault((point_name, field), []).append(
            normalize_series(label, field, source.variable(field), rows, config.period)
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
    elif spec.time_kind == "sum" and field != "precipitation":
        name += f"（{spec.interval.total_seconds() / 60:g}分钟量）"
    elif spec.time_kind == "cumulative" and field != "precipitation":
        name += "（累计差分）"
    elif field == "precipitation" and spec.time_kind == "cumulative":
        name += "（逐小时雨量）"
    elif spec.window == "hour_to_date":
        name += "（本小时雨量）"
    elif field == "wind_speed":
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
    superseded = 0
    seen_frames, latest_runs = {}, {}
    instant_fields = tuple(field for field in fields if specs[field].time_kind == "instant")
    interval_fields = tuple(field for field in fields if specs[field].time_kind != "instant")
    coordinates = tuple((point.longitude, point.latitude) for point in config.points)
    locator = WrfPointLocator(coordinates)
    last_report = monotonic() - 5
    reported_starts = set()
    _notify(progress, f"读取 WRF 来源 {source.name}：一次提取 {len(coordinates)} 个点位…")
    # 已知起报从新到旧处理；未知起报仍根据文件属性核对，不从文件名猜测。
    paths = sorted(iter_wrf_files(source, config.period, progress))
    paths.sort(key=lambda path: run_order(forecast_init_time(source, path, require=False)), reverse=True)
    for path in paths:
        files_read += 1
        now = monotonic()
        if files_read == 1 or now - last_report >= 5:
            _notify(progress, f"{source.name}：打开候选文件 {files_read:,}，已取 {frames_read:,} 个时次；当前 {path.name}")
            last_report = now
        directory_start = forecast_init_time(source, path, require=False)
        selected_fields = {}

        def frame_variables(time, index, model_start_time):
            nonlocal superseded
            start = directory_start or forecast_init_time(source, path, model_start_time)
            key = start, time
            if key in seen_frames:
                raise ValueError(f"同一起报有重复时次 {time.isoformat()}: {seen_frames[key]} 与 {path}")
            seen_frames[key] = path
            if map_frames is not None:
                map_frames.setdefault(start, {})[time] = path, index
            selected = fields
            # 时段字段保留完整批次；同帧的瞬时字段可以独立跳过。
            if instant_fields:
                if time in latest_runs and run_order(latest_runs[time]) > run_order(start):
                    superseded += 1
                    selected = interval_fields
                else:
                    latest_runs[time] = start
            selected_fields[time] = selected
            return tuple(dict.fromkeys(name for field in selected for name in specs[field].native_variables))

        with closing(iter_wrf_points_frames(
            path, names, source.timezone, coordinates, period=config.period,
            init_time=directory_start, min_lead_hours=source.min_lead_hours,
            max_lead_hours=source.max_lead_hours,
            locator=locator,
            frame_variables=frame_variables,
        )) as frames:
            start = None
            for batch in frames:
                frames_read += 1
                if start is None:
                    start = forecast_init_time(source, path, batch[0].model_start_time)
                if start not in reported_starts:
                    _notify(progress, f"{source.name} 起报：{start.isoformat() if start else '未提供'}；"
                            f"最低预报时效 {source.min_lead_hours:g} 小时")
                    if start is not None:
                        cutoff = valid_from(source, start).astimezone(ZoneInfo(config.display_timezone))
                        _notify(progress, f"{source.name} 该批次允许使用的起点：{cutoff:%Y-%m-%d %H:%M}"
                                f"（{config.display_timezone}）；实际覆盖以文件内时次和有效数值为准")
                    reported_starts.add(start)
                if directory_start is not None and batch[0].model_start_time not in (None, directory_start):
                    mismatch = (directory_start, batch[0].model_start_time)
                    if mismatch not in reported_starts:
                        _notify(progress, f"{path}: 采用目录/配置起报 {directory_start.isoformat()}；"
                                f"文件模式起始时间为 {batch[0].model_start_time.isoformat()}")
                        reported_starts.add(mismatch)
                if start not in run_records:
                    run_records[start] = {(point.name, field): [] for point in config.points for field in fields}
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
                    for field in selected_fields[frame.time]:
                        location = f"{path} Time[{frame.time_index}] {frame.time.isoformat()} 点位 {point.name}"
                        report = (lambda message: progress(f"警告：{location}: {message}")) if progress else None
                        try:
                            value, units = resolve_wrf_variable(specs[field], frame.values, frame.units, report)
                        except ValueError as exc:
                            raise ValueError(f"{location}: {exc}") from exc
                        run_records[start][point.name, field].append(
                            (frame.time, value, units, f"{path} Time[{frame.time_index}]")
                        )
    _notify(progress, f"{source.name} 取值完成：打开 {files_read:,} 个候选文件，时段内 {frames_read:,} 个时次")
    if superseded:
        message = f"{source.name}：跳过 {superseded:,} 个被较新起报覆盖的旧时次的瞬时字段取值"
        message += "；时段字段仍保留完整批次" if interval_fields else "，未读取其坐标和气象值"
        _notify(progress, message)
    if not run_records:
        raise ValueError(f"sources.{source.name}: 配置时段内没有满足 lead_hours 范围的 WRF 时次")
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
                    if curve is not None and field == "precipitation":
                        curve = hourly_precipitation(curve)
                    latest = start + timedelta(hours=source.max_lead_hours) if start is not None else None
                    curve = eligible_series(curve, valid_from(source, start), latest)
                except ValueError as exc:
                    raise ValueError(f"点位 {point.name}: {exc}") from exc
                if curve is not None:
                    runs.append((start, curve))
            if not runs:
                if field == "precipitation":
                    raise ValueError(f"点位 {point.name}: 没有完整的整点一小时 WRF 降水区间，请检查 period、预报时效和数据覆盖")
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
    """每个文件只打开一次；直接字段与派生湿度共用点位样本。"""
    curves = {}
    coordinates = tuple((point.longitude, point.latitude) for point in config.points)
    for source in cldas:
        fields = _fields(source, config)
        if not fields:
            continue
        plans, jobs = [], {}
        records, missing = {}, {}
        for field in fields:
            spec = source.variable(field)
            for family, time, files in iter_cldas_input_files(source.name, spec, index):
                for point in config.points:
                    records.setdefault((field, family, point.name), [])
                if spec.time_kind in {"mean", "sum"}:
                    start = time if spec.timestamp == "start" else time - spec.interval
                    if not config.period.start <= start < start + spec.interval <= config.period.end:
                        continue
                plans.append((field, family, time, files))
                if any(info is None for info in files.values()):
                    continue  # 保留缺测记录，但不打开无法组成湿度的剩余输入。
                for role, item in spec.source_inputs:
                    info = files[role]
                    if info is not None:
                        jobs.setdefault(info.path, set()).add(item.variable)
        samples = {}
        last_report = monotonic() - 5
        _notify(progress, f"读取 CLDAS {source.name}：{len(jobs)} 个文件 × {len(coordinates)} 个点位")
        for number, (path, names) in enumerate(jobs.items(), start=1):
            now = monotonic()
            if number == 1 or now - last_report >= 5:
                _notify(progress, f"读取 CLDAS：{number}/{len(jobs)} 个文件")
                last_report = now
            samples[path] = read_cldas_points(path, tuple(sorted(names)), coordinates, source.timezone)
        for field, family, time, files in plans:
            spec = source.variable(field)
            incomplete = any(info is None for info in files.values())
            if incomplete:
                missing[field, family] = missing.get((field, family), 0) + 1
            for point_index, point in enumerate(config.points):
                units = "%"
                location = f"{source.name}/{family} {time.isoformat()}"
                if incomplete:
                    value = None
                else:
                    selected = {role: samples[files[role].path][item.variable][point_index]
                                for role, item in spec.source_inputs}
                    if spec.inputs:
                        positions = [sample.grid_coordinates for sample in selected.values()]
                        if any(position is None for position in positions) or len(set(positions)) != 1:
                            raise ValueError(f"{location}: 湿度基础变量的最近格点经纬度不一致，不能组合")
                        try:
                            paths = ", ".join(str(sample.path) for sample in selected.values())
                            report = (lambda message: progress(
                                f"警告：{location} 点位 {point.name} ({paths}): {message}"
                            )) if progress else None
                            value = relative_humidity({role: s.value for role, s in selected.items()},
                                                      {role: s.units for role, s in selected.items()}, spec, report)
                        except ValueError as exc:
                            raise ValueError(f"{location} 点位 {point.name}: {exc}") from exc
                    else:
                        sample = selected["value"]
                        value, units, location = sample.value, sample.units, str(sample.path)
                records.setdefault((field, family, point.name), []).append((time, value, units, location))
        for (field, family), count in missing.items():
            _notify(progress, f"{source.name}/{family} {field}：{count} 个时次缺少同刻基础产品，保留缺测")
        for (field, family, point_name), rows in records.items():
            label = _label(source, field)
            families = {fam for f, fam, _ in records if f == field}
            if len(cldas) > 1 or len(families) > 1:
                label += f" / {source.name} / {family}"
            curves.setdefault((point_name, field), []).append(
                normalize_series(label, field, source.variable(field), rows, config.period)
            )
    return curves


def plot_comparisons(
    config: Config, *, progress: Callable[[str], None] | None = None,
) -> Iterator[PlotResult]:
    """各来源叠图（WRF 降水合成逐小时量）；指标另行配对，支持进度回调。"""
    _notify(progress, f"开始检验：{len(config.points)} 个点位，{len(config.elements)} 个要素。")
    sources = _validate_sources(config)
    for field in config.elements:
        outputs = [label for kind, label in (("series", "时序图"), ("map", "空间图"), ("metrics", "指标 CSV"))
                   if field in config.fields_for(kind)]
        _notify(progress, f"{config.display_name(field)}（{field}）：{'、'.join(outputs)}")
    wrf = [source for source in sources if source.kind == "wrf" and _fields(source, config, points_only=False)]
    cldas = [source for source in sources if source.kind == "cldas" and _fields(source, config, points_only=False)]
    products = {
        source.name: {item.product for field in _fields(source, config, points_only=False)
                      for _, item in source.variable(field).source_inputs}
        for source in cldas
    }
    if cldas:
        _notify(progress, "扫描 CLDAS 文件名并选择所需产品及最新制作版本…")
    index = index_cldas_files(cldas, period=config.period, progress=progress,
                              products_by_source=products) if cldas else {}
    for source in cldas:
        for field in _fields(source, config, points_only=False):
            for _, item in source.variable(field).source_inputs:
                if not any(key[0] == source.name and key[2] == item.product for key in index):
                    raise ValueError(f"sources.{source.name}: 配置时段内没有 CLDAS {item.product} 产品")
    station_curves = _station_series(sources, config, progress)
    forecasts = {(point.name, field): [] for point in config.points for field in config.point_elements}
    map_frames = {}
    for source in wrf:
        needs_maps = any(source.variable(field) is not None for field in config.fields_for("map"))
        if _fields(source, config):
            frames = {} if needs_maps else None
            for key, curve in _wrf_series(source, config, progress, map_frames=frames).items():
                forecasts[key].append(curve)
            if frames is not None:
                map_frames[source.name] = frames
        elif needs_maps:
            windows = required_wrf_windows(config, source, cldas, index)
            map_frames[source.name] = _wrf_map_index(source, config, progress, windows)
    cldas_curves = _cldas_series(cldas, index, config, progress)
    output_dir = None
    display_zone = ZoneInfo(config.display_timezone)
    stamp = f"{config.period.start.astimezone(timezone.utc):%Y%m%dT%H%M%SZ}_{config.period.end.astimezone(timezone.utc):%Y%m%dT%H%M%SZ}"
    points = config.points if config.point_elements else ()
    for point_index, point in enumerate(points, start=1):
        try:
            _notify(progress, f"处理点位 {point_index}/{len(config.points)}：{point.name}")
            for field in config.point_elements:
                observations = list(cldas_curves.get((point.name, field), ()))
                observations.extend(station_curves.get((point.name, field), ()))
                forecasts_for_field = forecasts.pop((point.name, field))
                comparisons = ()
                if field in config.fields_for("metrics"):
                    metric_options = config.metrics_options(field)
                    _notify(progress, f"计算 {point.name} 的 {config.display_name(field)}指标…")
                    comparisons = tuple(
                        comparison
                        for forecast in forecasts_for_field for observation in observations
                        for comparison in compare_forecast(
                            forecast, observation,
                            point.time_tolerance, field,
                            metric_options.precipitation_thresholds_mm,
                        )
                    )
                    for comparison in comparisons:
                        message = (f"{comparison.forecast} / {comparison.observation}："
                                   f"时间配对 {comparison.matched_times:,}，有效样本 {comparison.metrics.sample_count:,}")
                        if comparison.metrics.sample_count == 0:
                            message += "；未计算有效指标，请核对时间覆盖、时间窗口及缺测情况"
                        _notify(progress, message)
                curves = tuple(forecast.series for forecast in forecasts_for_field) + tuple(observations)
                for curve in curves:
                    valid_times = [time for time, value in zip(curve.times, curve.values) if value is not None]
                    if valid_times:
                        coverage = (f"首末有效时次 {valid_times[0].astimezone(display_zone):%Y-%m-%d %H:%M}"
                                    f" 至 {valid_times[-1].astimezone(display_zone):%Y-%m-%d %H:%M}，"
                                    f"有效点 {len(valid_times):,}/{len(curve.times):,}")
                        first_index = curve.times.index(valid_times[0])
                        covered_start = curve.bounds[first_index][0] if curve.bounds else valid_times[0]
                        if covered_start > config.period.start:
                            gap_hours = (covered_start - config.period.start).total_seconds() / 3600
                            coverage += (
                                f"；开头未覆盖 {gap_hours:g} 小时，配置起点"
                                f" {config.period.start.astimezone(display_zone):%Y-%m-%d %H:%M}"
                            )
                    else:
                        coverage = f"无有效数值（0/{len(curve.times):,}）"
                    _notify(progress, f"{point.name} / {config.display_name(field)} / {curve.label}：{coverage}")
                name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", point.name).strip(" .") or "point"
                if output_dir is None:
                    _notify(progress, "创建本次独立输出目录…")
                    output_dir = create_run_directory(config)
                base = output_dir / f"{field}_{point_index:03d}_{name}_{stamp}"
                metrics_path = None
                paths = []
                for suffix, _, options in config.plot_jobs("series", field):
                    path = Path(f"{base}{suffix}.png")
                    _notify(progress, f"绘制 {point.name} 的 {config.display_name(field)}…")
                    plot_series(path, point, curves, config.display_timezone, period=config.period,
                                element_name=config.display_name(field), options=options)
                    paths.append(path)
                if field in config.fields_for("metrics"):
                    _notify(progress, f"写入 {field} 指标 CSV…")
                    metrics_path = write_metrics(Path(f"{base}.csv"), comparisons, unit=curves[0].unit)
                yield PlotResult(point, field, tuple(paths), curves, metrics_path, comparisons)
        except (OSError, ValueError) as exc:
            raise ValueError(f"点位 {point.name}: {exc}") from exc
    if config.fields_for("map"):
        if output_dir is None:
            output_dir = create_run_directory(config)
        plot_maps(config, sources, index, map_frames, output_dir, lambda message: _notify(progress, message))
