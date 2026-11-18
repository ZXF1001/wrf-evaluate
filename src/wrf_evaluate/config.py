"""加载唯一配置入口；路径相对配置文件解析，时间使用 IANA 时区。"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import math
import os
import re
from pathlib import Path, PurePosixPath, PureWindowsPath
from functools import cached_property
import tomllib
from types import MappingProxyType
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .variables import (
    DEFAULT_COLUMN_MAPPING, DEFAULT_VARIABLES, DISPLAY_NAMES, VariableConfig,
    convert_value, duration, parse_variables, validate_variable,
)


class ConfigError(ValueError):
    """可直接向用户展示的配置错误。"""


DEFAULT_DISPLAY_NAMES = MappingProxyType(DISPLAY_NAMES)


@dataclass(frozen=True)
class SeriesOptions:
    figsize: tuple[float, float] = (11, 4.8)
    dpi: int = 160
    y_limits: tuple[float, float] | None = None


@dataclass(frozen=True)
class MapOptions:
    interval: timedelta = timedelta(hours=6)
    figsize: tuple[float, float] = (12, 5)
    dpi: int = 160
    cmap: str = "viridis"
    color_limits: tuple[float, float] | None = None
    layout: str = "timeline"
    difference_cmap: str = "RdBu_r"
    difference_limit: float | None = None


@dataclass(frozen=True)
class MetricsOptions:
    precipitation_thresholds_mm: tuple[float, ...] = ()


@dataclass(frozen=True)
class PlotTask:
    element: str
    type: str
    options: SeriesOptions | MapOptions


@dataclass(frozen=True)
class SourceConfig:
    name: str
    kind: str
    root: Path
    patterns: tuple[str, ...]
    timezone: str
    enabled: bool
    files: tuple[Path, ...] = ()
    variable_mapping: tuple[tuple[str, VariableConfig], ...] = ()
    min_lead_hours: float = 0.0
    init_time: datetime | None = None
    max_lead_hours: float = 168.0
    sheet: str = "气象数据"
    time_format: str = "%Y%m%d%H%M%S"
    missing_values: tuple[str | float, ...] = ()

    def variable(self, field: str) -> VariableConfig | None:
        """未接入的要素没有隐式默认变量。"""
        return self._variable_lookup.get(field)

    @cached_property
    def _variable_lookup(self) -> dict[str, VariableConfig]:
        return DEFAULT_VARIABLES.get(self.kind, {}) | dict(self.variable_mapping)

    @property
    def variables(self) -> tuple[tuple[str, VariableConfig], ...]:
        return tuple(self._variable_lookup.items())

    @property
    def column_mapping(self) -> tuple[tuple[str, str], ...]:
        return tuple((field, spec.variable) for field, spec in self.variables)


@dataclass(frozen=True)
class PointConfig:
    name: str
    longitude: float
    latitude: float
    station_id: str | None = None
    time_tolerance: timedelta = timedelta(minutes=1)
    excel: SourceConfig | None = None


@dataclass(frozen=True)
class PeriodConfig:
    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        for time in (self.start, self.end):
            if not isinstance(time, datetime) or time.tzinfo is None or time.utcoffset() is None:
                raise ConfigError("period: 开始、结束时间须包含时区偏移，例如 2024-12-24T06:00:00+08:00")
        if self.end <= self.start:
            raise ConfigError("period: end 必须晚于 start")


@dataclass(frozen=True)
class Config:
    sources: tuple[SourceConfig, ...]
    output_dir: Path
    display_timezone: str
    points: tuple[PointConfig, ...] = ()
    period: PeriodConfig | None = None
    output_name: str = "evaluate"
    config_bytes: bytes | None = field(default=None, repr=False)
    display_names: tuple[tuple[str, str], ...] = tuple(DEFAULT_DISPLAY_NAMES.items())
    metrics: tuple[tuple[str, MetricsOptions], ...] = ()
    plots: tuple[PlotTask, ...] = ()
    source_config_bytes: bytes | None = field(default=None, repr=False)
    source_config_path: Path | None = None

    def plot_jobs(self, kind: str, element: str | None = None) -> tuple[tuple[str, str, SeriesOptions | MapOptions], ...]:
        """任务共享读取结果；序号使同一变量的多张图不会覆盖。"""
        return tuple((f"_plot{i:03d}", task.element, task.options)
                     for i, task in enumerate(self.plots, 1)
                     if task.type == kind and (element is None or task.element == element))

    def display_name(self, field: str) -> str:
        """返回用户配置或默认的图中中文要素名称。"""
        return dict(self.display_names).get(field, DEFAULT_DISPLAY_NAMES[field])

    def fields_for(self, output: str) -> tuple[str, ...]:
        if output == "metrics":
            return tuple(name for name, _ in self.metrics)
        return tuple(dict.fromkeys(task.element for task in self.plots if task.type == output))

    def metrics_options(self, element: str) -> MetricsOptions:
        return dict(self.metrics)[element]

    @property
    def elements(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys([task.element for task in self.plots] + [name for name, _ in self.metrics]))

    @property
    def point_elements(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(self.fields_for("series") + self.fields_for("metrics")))


def _table(value: object, allowed: set[str], field: str) -> dict:
    if not isinstance(value, dict):
        raise ConfigError(f"{field}: 应为配置表")
    if unknown := value.keys() - allowed:
        raise ConfigError(f"{field}: 未知配置项 {', '.join(sorted(unknown))}")
    return value


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{field}: 应为非空字符串")
    return value


def _timezone(value: object, field: str) -> str:
    name = _text(value, field)
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError(f"{field}: 未知时区或缺少时区数据库: {name}") from exc
    return name


def _path(value: object, base: Path, field: str) -> Path:
    text = _text(value, field)
    if os.name != "nt" and PureWindowsPath(text).drive:
        raise ConfigError(f"{field}: 请将 Windows 路径改为本机实际路径: {text}")
    path = Path(text).expanduser()
    return (path if path.is_absolute() else base / path).resolve()


def parse_station_columns(value: object, field: str = "columns") -> tuple[tuple[str, str], ...]:
    """读取站点 Excel 表头映射。"""
    defaults = dict(DEFAULT_COLUMN_MAPPING)
    # 原始表读取也可选择辐射列，但不为未知表头编造默认值。
    for key in ("surface_shortwave_down", "surface_longwave_down"):
        if isinstance(value, Mapping) and key in value:
            defaults[key] = value[key]
    if not isinstance(value, Mapping):
        raise ConfigError(f"{field}: 应为表头映射表")
    if unknown := value.keys() - defaults.keys():
        raise ConfigError(f"{field}: 未知字段 {', '.join(map(str, unknown))}")
    mapping = tuple(
        (key, _text(item.variable if isinstance(item, VariableConfig) else item,
                    f"{field}.{key}").strip())
        for key, item in (defaults | dict(value)).items()
    )
    if len({header for _, header in mapping}) != len(mapping):
        raise ConfigError(f"{field}: 映射后的 Excel 表头不能重复")
    return mapping


def _parse_points(value: object, *, base: Path,
                  output_dir: Path) -> tuple[PointConfig, ...]:
    group = "station"
    if not isinstance(value, list):
        raise ConfigError(f"{group}: 应使用 [[{group}]] 配置站点列表")
    points = []
    names = set()
    for index, item in enumerate(value, start=1):
        field = f"{group}[{index}]"
        allowed = {"name", "longitude", "latitude", "station_id"}
        allowed |= {"time_tolerance_seconds", "excel"}
        point = _table(item, allowed, field)
        name = _text(point.get("name"), f"{field}.name").strip()
        if name in names:
            raise ConfigError(f"{field}.name: 点位名称重复: {name}")
        for coordinate, limit in (("longitude", 180), ("latitude", 90)):
            number = point.get(coordinate)
            if type(number) not in (int, float) or not math.isfinite(number) or not -limit <= number <= limit:
                raise ConfigError(f"{field}.{coordinate}: 应为 [-{limit}, {limit}] 范围内的有限数值，单位为度")
        station_id = point.get("station_id")
        if station_id is not None:
            station_id = _text(station_id, f"{field}.station_id").strip()
        tolerance = _tolerance(point.get("time_tolerance_seconds", 60), f"{field}.time_tolerance_seconds")
        if "excel" not in point:
            raise ConfigError(f"{field}.excel.files: 必须明确指定本站的 Excel 文件")
        excel = _parse_station_excel(point["excel"], base, output_dir, f"{field}.excel")
        names.add(name)
        points.append(PointConfig(name, float(point["longitude"]), float(point["latitude"]), station_id,
                                  tolerance, excel))
    return tuple(points)


def _parse_station_excel(value: object, base: Path, output_dir: Path, field: str) -> SourceConfig:
    """单个站点的工作簿规则；路径相对本次运行配置，不搜索文件。"""
    options = _table(value, {"files", "timezone", "sheet", "variables", "time_format", "missing_values"}, field)
    files = options.get("files")
    if not isinstance(files, list) or not files:
        raise ConfigError(f"{field}.files: 请明确列出一个或多个 .xls/.xlsx 文件")
    paths = []
    for index, value in enumerate(files, 1):
        path = _path(value, base, f"{field}.files[{index}]")
        if path.suffix.lower() not in {".xls", ".xlsx"} or not path.is_file():
            raise ConfigError(f"{field}.files[{index}]: Excel 文件不存在或格式不支持: {path}")
        if path.is_relative_to(output_dir):
            raise ConfigError(f"{field}.files: 原始 Excel 不得位于输出目录内")
        paths.append(path)
    if len(set(paths)) != len(paths):
        raise ConfigError(f"{field}.files: 文件路径不能重复")
    try:
        variables = parse_variables(options.get("variables", {}), "station")
        for name, spec in variables:
            if name in DISPLAY_NAMES:
                if spec.units is None:
                    raise ValueError(f"{name}: Excel 须填写 units")
                convert_value(name, None, spec.units, spec.time_kind, spec.interval)
    except ValueError as exc:
        raise ConfigError(f"{field}.variables: {exc}") from exc
    missing = options.get("missing_values", [])
    if not isinstance(missing, list) or any(
        type(item) not in (str, int, float) or (isinstance(item, (int, float)) and not math.isfinite(item))
        for item in missing
    ):
        raise ConfigError(f"{field}.missing_values: 应为字符串或有限数值的数组")
    time_format = _text(options.get("time_format", "%Y%m%d%H%M%S"), f"{field}.time_format")
    try:
        sample = datetime(2024, 12, 24, 15, 34, 56)
        parsed = datetime.strptime(sample.strftime(time_format), time_format)
        if (parsed.year, parsed.month, parsed.day) != (2024, 12, 24) or not any(
            code in time_format for code in ("%H", "%I")
        ):
            raise ValueError("格式须包含年月日和小时")
    except ValueError as exc:
        raise ConfigError(f"{field}.time_format: {exc}") from exc
    return SourceConfig(
        "station", "station", base, (), _timezone(options.get("timezone", "UTC"), f"{field}.timezone"),
        True, tuple(paths), variables, sheet=_text(options.get("sheet", "气象数据"), f"{field}.sheet").strip(),
        time_format=time_format, missing_values=tuple(missing),
    )


def _pair(value: object, field: str, *, size: bool = False) -> tuple[float, float]:
    if (not isinstance(value, list) or len(value) != 2
            or any(type(item) not in (int, float) or not math.isfinite(item) for item in value)):
        raise ConfigError(f"{field}: 应为两个有限数值组成的数组")
    if (size and min(value) <= 0) or (not size and value[0] >= value[1]):
        raise ConfigError(f"{field}: 图尺寸须为正数，坐标/色标范围须下限小于上限")
    return tuple(map(float, value))


def _tolerance(value: object, field: str) -> timedelta:
    if type(value) is not int or value < 0:
        raise ConfigError(f"{field}: 应为非负整数，0 表示精确配对")
    try:
        return timedelta(seconds=value)
    except OverflowError as exc:
        raise ConfigError(f"{field}: 数值过大") from exc


def _thresholds(value: object, field: str) -> tuple[float, ...]:
    if (not isinstance(value, list) or any(
        type(item) not in (int, float) or not math.isfinite(item) or item <= 0 for item in value
    ) or len(set(value)) != len(value)):
        raise ConfigError(f"{field}: 应为不重复的正的有限数值数组，单位 mm")
    return tuple(map(float, value))


def _output_options(kind: str, value: object, field: str, *, element: str):
    allowed = {
        "series": {"figsize", "dpi", "y_limits"},
        "map": {"interval", "figsize", "dpi", "cmap", "color_limits", "layout", "difference_cmap", "difference_limit"},
        "metrics": {"precipitation_thresholds_mm"},
    }
    options = _table(value, allowed[kind], field)
    parsed = {}
    for key, item in options.items():
        location = f"{field}.{key}"
        if key in {"figsize", "y_limits", "color_limits"}:
            parsed[key] = _pair(item, location, size=key == "figsize")
        elif key == "dpi":
            if type(item) is not int or item <= 0:
                raise ConfigError(f"{location}: 应为正整数")
            parsed[key] = item
        elif key == "interval":
            try:
                parsed[key] = duration(item)
            except ValueError as exc:
                raise ConfigError(f"{location}: {exc}") from exc
        elif key in {"cmap", "difference_cmap"}:
            from matplotlib import colormaps
            parsed[key] = _text(item, location)
            if item not in colormaps:
                raise ConfigError(f"{location}: 未知 Matplotlib 色表 {item}")
        elif key == "layout":
            if item not in ("timeline", "separate"):
                raise ConfigError(f"{location}: 应为 timeline（所有时刻同图）或 separate（每时刻一图）")
            parsed[key] = item
        elif key == "difference_limit":
            if type(item) not in (int, float) or not math.isfinite(item) or item <= 0:
                raise ConfigError(f"{location}: 应为正的有限数值，差值色标采用 [-值, +值]")
            parsed[key] = float(item)
        elif key == "precipitation_thresholds_mm":
            if element != "precipitation":
                raise ConfigError(f"{location}: 仅降水使用")
            parsed[key] = _thresholds(item, location)
    return {"series": SeriesOptions, "map": MapOptions, "metrics": MetricsOptions}[kind](**parsed)


def _read_config(path: Path) -> tuple[bytes, dict]:
    try:
        config_bytes = path.read_bytes()
        raw = tomllib.loads(config_bytes.decode("utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"无法读取配置 {path}: {exc}") from exc
    return config_bytes, raw


def _parse_tasks(raw: dict, definitions: dict) -> tuple:
    """任务决定读取并集；同要素的多个图保留独立选项，共享原始数据。"""
    names, mappings = dict(DEFAULT_DISPLAY_NAMES), {}
    for name, value in _table(definitions, set(DISPLAY_NAMES), "elements").items():
        value = _table(value, {"name", "sources"}, f"elements.{name}")
        if "name" in value:
            names[name] = _text(value["name"], f"elements.{name}.name").strip()
        sources = value.get("sources", {})
        if not isinstance(sources, dict):
            raise ConfigError(f"elements.{name}.sources: 应为来源映射表")
        for source, mapping in sources.items():
            mappings.setdefault(source, {})[name] = mapping
    metrics, plots = {}, []
    for group in ("plots", "metrics"):
        tasks = raw.get(group, [])
        if not isinstance(tasks, list):
            raise ConfigError(f"{group}: 请使用 [[{group}]] 配置任务")
        for index, task in enumerate(tasks, 1):
            location = f"{group}[{index}]"
            if not isinstance(task, dict):
                raise ConfigError(f"{location}: 应为配置表")
            element = task.get("element")
            if not isinstance(element, str) or element not in DISPLAY_NAMES:
                raise ConfigError(f"{location}.element: 请填写统一英文要素名")
            kind = task.get("type") if group == "plots" else "metrics"
            if not isinstance(kind, str) or kind not in ({"series", "map"} if group == "plots" else {"metrics"}):
                raise ConfigError(f"{location}.type: 应为 series 或 map")
            keys = {"element", "type"} if group == "plots" else {"element"}
            options = _output_options(kind, {k: v for k, v in task.items() if k not in keys},
                                      location, element=element)
            if group == "plots":
                plots.append(PlotTask(element, kind, options))
            else:
                if element in metrics:
                    raise ConfigError(f"{location}: {element} 的指标任务重复")
                metrics[element] = options
    if not plots and not metrics:
        raise ConfigError("至少配置一个 [[plots]] 或 [[metrics]] 任务")
    return tuple(names.items()), mappings, tuple(plots), tuple(metrics.items())


def load_config(path: str | Path) -> Config:
    """读取并校验配置，不创建目录、不打开原始数据文件。"""
    path = Path(path).expanduser().resolve()
    config_bytes, raw = _read_config(path)
    _table(raw, {"source_config", "output", "station", "period", "plots", "metrics"}, "config")
    source_path = _path(raw.get("source_config"), path.parent, "source_config")
    source_bytes, stable = _read_config(source_path)
    _table(stable, {"sources", "elements"}, "source_config")
    source_base = source_path.parent
    display_names, element_mappings, plots, metrics = _parse_tasks(raw, stable.get("elements", {}))
    elements = {task.element for task in plots} | {name for name, _ in metrics}
    source_tables = stable.get("sources")
    period = None
    if "period" in raw:
        values = _table(raw["period"], {"start", "end"}, "period")
        bounds = []
        for name in ("start", "end"):
            value = values.get(name)
            try:
                bounds.append(datetime.fromisoformat(value) if isinstance(value, str) else value)
            except ValueError as exc:
                raise ConfigError(f"period.{name}: 应为含时区偏移的 ISO 日期时间") from exc
        period = PeriodConfig(*bounds)
    output = _table(raw.get("output", {}), {"directory", "name", "timezone"}, "output")
    output_dir = _path(output.get("directory", "output"), path.parent, "output.directory")
    points = _parse_points(raw.get("station", []), base=path.parent, output_dir=output_dir)
    output_name = _text(output.get("name", "evaluate"), "output.name").strip()
    if output_name in {".", ".."} or output_name.endswith(".") or re.search(r'[<>:"/\\|?*\x00-\x1f]', output_name):
        raise ConfigError("output.name: 请填写单个目录名称，不使用路径分隔符或特殊字符")
    display_timezone = _timezone(output.get("timezone", "Asia/Shanghai"), "output.timezone")
    sources = source_tables
    if not isinstance(sources, dict) or not sources:
        raise ConfigError("sources: 至少配置一个数据源")
    if unknown := element_mappings.keys() - sources.keys():
        raise ConfigError(f"elements 中引用了未定义的数据来源: {', '.join(sorted(unknown))}")

    parsed = []
    for name, value in sources.items():
        field = f"sources.{name}"
        source = _table(value, {"kind", "root", "patterns", "timezone", "enabled", "lead_hours", "init_time"}, field)
        kind = _text(source.get("kind"), f"{field}.kind")
        if kind not in {"wrf", "cldas"}:
            raise ConfigError(f"{field}.kind: 只支持 wrf/cldas；站点 Excel 请在 [[station]] 中配置")
        lead_hours = source.get("lead_hours", [0, 168])
        if "lead_hours" in source and kind != "wrf":
            raise ConfigError(f"{field}.lead_hours: 当前仅 WRF 来源使用")
        if (not isinstance(lead_hours, list) or len(lead_hours) != 2
                or any(type(v) not in (int, float) or not math.isfinite(v) for v in lead_hours)
                or not 0 <= lead_hours[0] <= lead_hours[1] or lead_hours[1] <= 0):
            raise ConfigError(f"{field}.lead_hours: 应为 [最小时效, 最大时效]，单位小时；"
                              "须满足 0 ≤ 最小 ≤ 最大，且最大为正数")
        min_lead_hours, max_lead_hours = lead_hours
        try:
            timedelta(hours=min_lead_hours)
            timedelta(hours=max_lead_hours)
        except OverflowError as exc:
            raise ConfigError(f"{field}.lead_hours: 预报时效数值过大") from exc
        init_time = source.get("init_time")
        if "init_time" in source:
            if kind != "wrf":
                raise ConfigError(f"{field}.init_time: 当前仅 WRF 来源使用")
            try:
                init_time = datetime.fromisoformat(init_time) if isinstance(init_time, str) else init_time
            except ValueError as exc:
                raise ConfigError(f"{field}.init_time: 应为含时区偏移的 ISO 日期时间") from exc
            if not isinstance(init_time, datetime) or init_time.tzinfo is None or init_time.utcoffset() is None:
                raise ConfigError(f"{field}.init_time: 须包含时区偏移，例如 2024-12-24T00:00:00Z")
        enabled = source.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ConfigError(f"{field}.enabled: 应为 true 或 false")
        root = _path(source.get("root"), source_base, f"{field}.root")
        timezone = _timezone(source.get("timezone"), f"{field}.timezone")
        own_mappings = element_mappings.get(name, {})
        try:
            variable_mapping = parse_variables(own_mappings, kind)
        except ValueError as exc:
            raise ConfigError(f"elements 字段映射 {name}: {exc}") from exc
        checked = {key: spec for key, spec in DEFAULT_VARIABLES.get(kind, {}).items() if key in elements}
        checked.update(variable_mapping)
        for element, spec in checked.items():
            if element in DISPLAY_NAMES:
                try:
                    validate_variable(element, spec)
                    if spec.units is not None:
                        convert_value(element, None, spec.units, spec.time_kind, spec.interval)
                    if kind == "cldas" and any(
                        item.product is None or not re.fullmatch(r"[A-Z0-9]+", item.product)
                        for _, item in spec.source_inputs
                    ):
                        raise ValueError("CLDAS 须填写文件名产品代码 product，例如 TMP、PRE")
                except ValueError as exc:
                    raise ConfigError(f"elements.{element}.sources.{name}: {exc}") from exc
        configured_patterns = source.get("patterns")
        if not isinstance(configured_patterns, list) or not configured_patterns:
            raise ConfigError(f"{field}.patterns: 应为非空字符串数组")
        for pattern in configured_patterns:
            _text(pattern, f"{field}.patterns")
            parts = pattern.split("/")
            if (
                "\\" in pattern
                or PurePosixPath(pattern).is_absolute()
                or PureWindowsPath(pattern).drive
                or any(part in {"", ".", ".."} for part in parts)
            ):
                raise ConfigError(f"{field}.patterns: 请使用相对路径和 / 分隔符: {pattern}")
            if "**" in pattern:
                raise ConfigError(f"{field}.patterns: 不支持 **；任意层级文件名可直接使用 *.nc 等模式: {pattern}")
        patterns = tuple(configured_patterns)
        if enabled:
            if not root.is_dir():
                raise ConfigError(f"{field}.root: 目录不存在或无法访问: {root}")
            if output_dir.is_relative_to(root) or root.is_relative_to(output_dir):
                raise ConfigError(f"output.directory: 不得与数据源目录重叠: {root}")
        parsed.append(SourceConfig(name, kind, root, patterns, timezone, enabled,
                                   variable_mapping=variable_mapping, min_lead_hours=float(min_lead_hours),
                                   init_time=init_time, max_lead_hours=float(max_lead_hours)))

    if not any(source.enabled for source in parsed):
        raise ConfigError("sources: 至少启用一个数据源")
    if output_dir.exists() and not output_dir.is_dir():
        raise ConfigError(f"output.directory: 已存在且不是目录: {output_dir}")
    return Config(
        tuple(parsed), output_dir, display_timezone, points, period, output_name, config_bytes, display_names,
        metrics, plots, source_bytes, source_path,
    )
