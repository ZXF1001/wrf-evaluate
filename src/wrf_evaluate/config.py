"""加载唯一配置入口；路径相对配置文件解析，时间使用 IANA 时区。"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import math
import os
import re
from pathlib import Path, PurePosixPath, PureWindowsPath
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

    def variable(self, field: str) -> VariableConfig | None:
        """未接入的要素没有隐式默认变量。"""
        return dict(self.variables).get(field)

    @property
    def variables(self) -> tuple[tuple[str, VariableConfig], ...]:
        return tuple((DEFAULT_VARIABLES.get(self.kind, {}) | dict(self.variable_mapping)).items())

    @property
    def column_mapping(self) -> tuple[tuple[str, str], ...]:
        return tuple((field, spec.variable) for field, spec in self.variables)


@dataclass(frozen=True)
class PointConfig:
    name: str
    longitude: float
    latitude: float
    station_id: str | None = None


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
    time_tolerance: timedelta
    precipitation_thresholds_mm: tuple[float, ...] = ()
    points: tuple[PointConfig, ...] = ()
    period: PeriodConfig | None = None
    output_name: str = "evaluate"
    config_bytes: bytes | None = field(default=None, repr=False)
    display_names: tuple[tuple[str, str], ...] = tuple(DEFAULT_DISPLAY_NAMES.items())
    elements: tuple[str, ...] = ("temperature_2m",)
    map_interval: timedelta = timedelta(hours=6)

    def display_name(self, field: str) -> str:
        """返回用户配置或默认的图中中文要素名称。"""
        return dict(self.display_names).get(field, DEFAULT_DISPLAY_NAMES[field])


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


def _name_mapping(value: object, defaults: Mapping[str, str], field: str) -> tuple[tuple[str, str], ...]:
    """只覆盖已填写项；未知字段和无效名称报错，不静默退回默认值。"""
    if not isinstance(value, Mapping):
        raise ConfigError(f"{field}: 应为名称映射表")
    if unknown := value.keys() - defaults.keys():
        raise ConfigError(f"{field}: 未知字段 {', '.join(map(str, unknown))}")
    return tuple(
        (key, _text(value.get(key, default), f"{field}.{key}").strip())
        for key, default in defaults.items()
    )


def parse_station_columns(value: object, field: str = "columns") -> tuple[tuple[str, str], ...]:
    """Return regional Excel header names from string/spec mappings."""
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


def _parse_points(value: object) -> tuple[PointConfig, ...]:
    if not isinstance(value, list):
        raise ConfigError("points: 应使用 [[points]] 配置点位列表")
    points = []
    names = set()
    for index, item in enumerate(value, start=1):
        field = f"points[{index}]"
        point = _table(item, {"name", "longitude", "latitude", "station_id"}, field)
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
        names.add(name)
        points.append(PointConfig(name, float(point["longitude"]), float(point["latitude"]), station_id))
    return tuple(points)


def load_config(path: str | Path) -> Config:
    """读取并校验配置，不创建目录、不打开原始数据文件。"""
    path = Path(path).expanduser().resolve()
    try:
        config_bytes = path.read_bytes()
        raw = tomllib.loads(config_bytes.decode("utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"无法读取配置 {path}: {exc}") from exc

    _table(raw, {"output", "sources", "matching", "metrics", "points", "period", "display_names", "elements", "maps"}, "config")
    map_interval = timedelta(hours=6)
    if "maps" in raw:
        options = _table(raw["maps"], {"interval"}, "maps")
        try:
            map_interval = duration(options.get("interval", "6h"))
        except ValueError as exc:
            raise ConfigError(f"maps: {exc}") from exc
    elements = raw.get("elements", ["temperature_2m"])
    if not isinstance(elements, list) or not elements or any(not isinstance(item, str) or item not in DISPLAY_NAMES for item in elements):
        raise ConfigError(f"elements: 应为非空要素数组，可选 {', '.join(DISPLAY_NAMES)}")
    if len(elements) != len(set(elements)):
        raise ConfigError("elements: 要素不能重复")
    display_names = _name_mapping(raw.get("display_names", {}), DEFAULT_DISPLAY_NAMES, "display_names")
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
    points = _parse_points(raw.get("points", []))
    metrics = _table(raw.get("metrics", {}), {"precipitation_thresholds_mm"}, "metrics")
    thresholds = metrics.get("precipitation_thresholds_mm", [])
    if not isinstance(thresholds, list) or any(
        type(value) not in (int, float) or not math.isfinite(value) or value <= 0
        for value in thresholds
    ):
        raise ConfigError("metrics.precipitation_thresholds_mm: 应为正的有限数值数组，单位 mm")
    if len(set(thresholds)) != len(thresholds):
        raise ConfigError("metrics.precipitation_thresholds_mm: 阈值不能重复")
    matching = _table(raw.get("matching", {}), {"time_tolerance_seconds"}, "matching")
    seconds = matching.get("time_tolerance_seconds", 60)
    if type(seconds) is not int or seconds < 0:
        raise ConfigError("matching.time_tolerance_seconds: 应为非负整数，0 表示精确配对")
    try:
        time_tolerance = timedelta(seconds=seconds)
    except OverflowError as exc:
        raise ConfigError("matching.time_tolerance_seconds: 数值过大") from exc
    output = _table(raw.get("output", {}), {"directory", "name", "timezone"}, "output")
    output_dir = _path(output.get("directory", "output"), path.parent, "output.directory")
    output_name = _text(output.get("name", "evaluate"), "output.name").strip()
    if output_name in {".", ".."} or output_name.endswith(".") or re.search(r'[<>:"/\\|?*\x00-\x1f]', output_name):
        raise ConfigError("output.name: 请填写单个目录名称，不使用路径分隔符或特殊字符")
    display_timezone = _timezone(output.get("timezone", "Asia/Shanghai"), "output.timezone")
    sources = raw.get("sources")
    if not isinstance(sources, dict) or not sources:
        raise ConfigError("sources: 至少配置一个数据源")

    parsed = []
    for name, value in sources.items():
        field = f"sources.{name}"
        source = _table(value, {"kind", "root", "patterns", "files", "timezone", "enabled", "variables", "min_lead_hours", "init_time"}, field)
        kind = _text(source.get("kind"), f"{field}.kind")
        if kind not in {"wrf", "cldas", "regional", "national", "ec"}:
            raise ConfigError(f"{field}.kind: 不支持的数据类型 {kind}")
        min_lead_hours = source.get("min_lead_hours", 0)
        if "min_lead_hours" in source and kind != "wrf":
            raise ConfigError(f"{field}.min_lead_hours: 当前仅 WRF 来源使用")
        if type(min_lead_hours) not in (int, float) or not math.isfinite(min_lead_hours) or min_lead_hours < 0:
            raise ConfigError(f"{field}.min_lead_hours: 应为非负有限数值，单位小时")
        try:
            timedelta(hours=min_lead_hours)
        except OverflowError as exc:
            raise ConfigError(f"{field}.min_lead_hours: 数值过大") from exc
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
        root = _path(source.get("root"), path.parent, f"{field}.root")
        timezone = _timezone(source.get("timezone"), f"{field}.timezone")
        variable_mapping = ()
        if "variables" in source:
            if kind not in DEFAULT_VARIABLES:
                raise ConfigError(f"{field}.variables: 当前仅 wrf/cldas/regional 数据源支持变量或表头映射")
            try:
                variable_mapping = parse_variables(source["variables"], kind)
            except ValueError as exc:
                raise ConfigError(f"{field}.variables: {exc}") from exc
        # Explicit mappings always validate; active selected defaults validate too.
        checked = dict(variable_mapping)
        if enabled and (kind != "regional" or source.get("files")):
            checked = {key: spec for key, spec in DEFAULT_VARIABLES.get(kind, {}).items()
                       if key in elements} | checked
        for element, spec in checked.items():
            if element in DISPLAY_NAMES:
                try:
                    validate_variable(element, spec)
                    if spec.units is not None:
                        convert_value(element, None, spec.units, spec.time_kind, spec.interval)
                    if kind == "cldas" and (spec.product is None or not re.fullmatch(r"[A-Z0-9]+", spec.product)):
                        raise ValueError("CLDAS 须填写文件名产品代码 product，例如 TMP、PRE")
                    if kind == "regional" and spec.units is None:
                        raise ValueError("Excel 没有单位属性，请填写 units")
                except ValueError as exc:
                    raise ConfigError(f"{field}.variables.{element}: {exc}") from exc
        patterns: tuple[str, ...] = ()
        files: tuple[Path, ...] = ()
        if kind in {"regional", "national"}:
            if "patterns" in source or "files" not in source:
                raise ConfigError(f"{field}: 站点来源请在 files 中明确列出 Excel，不使用 patterns")
            configured_files = source["files"]
            if not isinstance(configured_files, list):
                raise ConfigError(f"{field}.files: 应为相对 root 的文件路径数组")
            resolved_files = []
            for index, item in enumerate(configured_files, start=1):
                file_name = _text(item, f"{field}.files[{index}]")
                parts = file_name.split("/")
                if (
                    "\\" in file_name
                    or PurePosixPath(file_name).is_absolute()
                    or PureWindowsPath(file_name).drive
                    or "*" in file_name or "?" in file_name
                    or any(part in {"", ".", ".."} for part in parts)
                    or Path(file_name).suffix.lower() not in {".xls", ".xlsx"}
                ):
                    raise ConfigError(f"{field}.files[{index}]: 请填写 root 下的 .xls/.xlsx 相对路径: {file_name}")
                file_path = (root / file_name).resolve()
                if not file_path.is_relative_to(root):
                    raise ConfigError(f"{field}.files[{index}]: 文件不得指向 root 以外: {file_name}")
                if enabled and not file_path.is_file():
                    raise ConfigError(f"{field}.files[{index}]: 文件不存在或无法访问: {file_path}")
                resolved_files.append(file_path)
            if len(set(resolved_files)) != len(resolved_files):
                raise ConfigError(f"{field}.files: 文件路径不能重复")
            files = tuple(resolved_files)
        else:
            if "files" in source:
                raise ConfigError(f"{field}.files: 仅站点来源使用，其他来源请使用 patterns")
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
        parsed.append(SourceConfig(name, kind, root, patterns, timezone, enabled, files, variable_mapping, float(min_lead_hours), init_time))

    if not any(source.enabled for source in parsed):
        raise ConfigError("sources: 至少启用一个数据源")
    if output_dir.exists() and not output_dir.is_dir():
        raise ConfigError(f"output.directory: 已存在且不是目录: {output_dir}")
    return Config(
        tuple(parsed), output_dir, display_timezone, time_tolerance,
        tuple(map(float, thresholds)), points, period, output_name, config_bytes, display_names, tuple(elements), map_interval,
    )
