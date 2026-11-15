"""Unified element names and source-specific variable metadata."""

from dataclasses import dataclass, replace
from datetime import timedelta
import re


DISPLAY_NAMES = {
    "temperature_2m": "2m气温",
    "relative_humidity_2m": "2m相对湿度",
    "wind_speed_10m": "10m风速",
    "precipitation": "降水量",
    "surface_shortwave_down": "地表向下短波辐射",
    "surface_longwave_down": "地表向下长波辐射",
}
UNITS = {
    "temperature_2m": "℃",
    "relative_humidity_2m": "%",
    "wind_speed_10m": "m/s",
    "precipitation": "mm",
    "surface_shortwave_down": "W/m²",
    "surface_longwave_down": "W/m²",
}


@dataclass(frozen=True)
class VariableConfig:
    """One native variable and how its values represent time and physical units."""

    variable: str
    units: str | None = None
    time_kind: str | None = "instant"
    interval: timedelta | None = None
    timestamp: str | None = None
    product: str | None = None


# Defaults are only for fields verified in the supplied data templates.
# Station precipitation deliberately has no default time_kind: verify its meaning first.
DEFAULT_VARIABLES = {
    "wrf": {
        "temperature_2m": VariableConfig("T2"),
    },
    "cldas": {
        "temperature_2m": VariableConfig("TAIR", product="TMP"),
    },
    "regional": {
        "station_id": VariableConfig("区站号"),
        "time": VariableConfig("观测时间"),
        "temperature_2m": VariableConfig("气温(°)", "degC"),
        "relative_humidity_2m": VariableConfig("相对湿度(%RH)", "%"),
        "wind_speed_10m": VariableConfig(
            "10min平均风速(m/s)", "m/s", "mean", timedelta(minutes=10), "end",
        ),
        "precipitation": VariableConfig("小时降水量(mm)", "mm", None),
    },
}

DEFAULT_COLUMN_MAPPING = {
    key: spec.variable for key, spec in DEFAULT_VARIABLES["regional"].items()
}


def duration(value: object) -> timedelta:
    """Parse a positive integer duration without evaluating expressions."""
    match = re.fullmatch(r"([1-9][0-9]*)(s|min|h|d)", value) if isinstance(value, str) else None
    if match is None:
        raise ValueError("interval 应为正整数加 s/min/h/d，例如 5min、1h")
    try:
        seconds = int(match[1]) * {"s": 1, "min": 60, "h": 3600, "d": 86400}[match[2]]
        return timedelta(seconds=seconds)
    except OverflowError as exc:
        raise ValueError("interval 过大") from exc


def parse_variables(value: object, kind: str) -> tuple[tuple[str, VariableConfig], ...]:
    """Parse [sources.<name>.variables] while retaining safe template defaults."""
    if not isinstance(value, dict):
        raise ValueError("应为字段映射表")
    allowed = set(DISPLAY_NAMES) | ({"station_id", "time"} if kind == "regional" else set())
    if unknown := value.keys() - allowed:
        raise ValueError(f"未知字段 {', '.join(sorted(unknown))}；请使用统一英文要素名")

    defaults = DEFAULT_VARIABLES.get(kind, {})
    result = {}
    for field, options in value.items():
        if not isinstance(options, dict):
            raise ValueError(
                f"{field}: 请使用子表并填写 variable，例如 "
                f"[sources.<名称>.variables.{field}]"
            )
        simple_field = field in {"station_id", "time"}
        allowed_options = {"variable"} if simple_field else {
            "variable", "units", "time_kind", "interval", "timestamp",
        } | ({"product"} if kind == "cldas" else set())
        if unknown := options.keys() - allowed_options:
            raise ValueError(f"{field}: 未知配置项 {', '.join(sorted(unknown))}")

        base = defaults.get(field)
        if base is None and "variable" not in options:
            raise ValueError(f"{field}.variable: 请指定一个原始变量名")
        updates = {}
        for key in ("variable", "units", "time_kind", "timestamp", "product"):
            if key not in options:
                continue
            text = options[key]
            if not isinstance(text, str) or not text.strip():
                raise ValueError(f"{field}.{key}: 应为非空字符串")
            updates[key] = text.strip()
        if "interval" in options:
            updates["interval"] = duration(options["interval"])

        # Changing the statistic invalidates any interval inherited from a template.
        if "time_kind" in updates and base and updates["time_kind"] != base.time_kind:
            base = replace(base, interval=None, timestamp=None)
        spec = replace(base or VariableConfig(options["variable"]), **updates)
        if not simple_field:
            validate_variable(field, spec)
        result[field] = spec

    if kind == "regional":
        merged = defaults | result
        names = [spec.variable for spec in merged.values()]
        if len(names) != len(set(names)):
            raise ValueError("映射后的 Excel 表头不能重复")
    return tuple(result.items())


def validate_variable(field: str, spec: VariableConfig) -> None:
    if spec.time_kind not in {"instant", "mean", "sum", "cumulative"}:
        raise ValueError(
            f"{field}.time_kind: 须明确填写 instant / mean / sum / cumulative"
        )
    if spec.time_kind in {"mean", "sum"}:
        if spec.interval is None or spec.timestamp not in {"start", "end"}:
            raise ValueError(f"{field}: mean/sum 须填写 interval 和 timestamp = start/end")
    elif spec.interval is not None or spec.timestamp is not None:
        raise ValueError(f"{field}: instant/cumulative 不使用 interval 或 timestamp")
    if field == "precipitation" and spec.time_kind not in {"sum", "cumulative"}:
        raise ValueError("precipitation: 只支持 sum 时段雨量或 cumulative 累计雨量")
    if field in {"temperature_2m", "relative_humidity_2m", "wind_speed_10m"} and spec.time_kind not in {"instant", "mean"}:
        raise ValueError(f"{field}: 只支持 instant 或 mean")


def convert_value(
    field: str, value: float | None, units: str, time_kind: str,
    interval: timedelta | None = None,
) -> float | None:
    """Convert supported units into the common comparison units."""
    unit = units.strip().lower().replace(" ", "").replace("²", "2").replace("**", "^")
    scale, offset = 1.0, 0.0
    if field == "temperature_2m":
        if unit in {"k", "kelvin"}:
            offset = -273.15
        elif unit not in {"c", "℃", "°c", "degc", "degree_celsius", "degrees_celsius"}:
            raise ValueError(f"无法识别温度单位 {units!r}")
    elif field == "relative_humidity_2m":
        if unit in {"1", "fraction"}:
            scale = 100.0
        elif unit not in {"%", "%rh", "percent"}:
            raise ValueError(f"相对湿度单位应为 % 或 1，不能直接映射比湿: {units!r}")
    elif field == "wind_speed_10m":
        if unit not in {"m/s", "ms-1", "ms^-1"}:
            raise ValueError(f"风速单位应为 m/s: {units!r}")
    elif field == "precipitation":
        if unit == "m":
            scale = 1000.0
        elif unit in {"mm/h", "mmh-1", "mmh^-1"}:
            if time_kind != "sum" or interval is None:
                raise ValueError("mm/h 须明确配置 sum 和 interval，按该时段平均雨强换算雨量")
            scale = interval.total_seconds() / 3600
        elif unit not in {"mm", "kg/m2", "kgm-2", "kgm^-2"}:
            raise ValueError(f"不支持的降水单位: {units!r}；雨量用 mm、m 或 kg/m²，时段平均雨强用 mm/h")
    elif field.startswith("surface_"):
        energy_units = {"j/m2", "jm-2", "jm^-2"}
        flux_units = {"w/m2", "wm-2", "wm^-2"}
        allowed = energy_units if time_kind in {"sum", "cumulative"} else flux_units
        expected = "J/m²" if time_kind in {"sum", "cumulative"} else "W/m²"
        if unit not in allowed:
            raise ValueError(f"{field} ({time_kind}) 单位应为 {expected}: {units!r}")

    if value is None:
        return None
    result = value * scale + offset
    if field == "precipitation" and result < 0:
        raise ValueError("有效降水量不能为负，请核对缺测码与累计规则")
    return result
