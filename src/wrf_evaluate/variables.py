"""Unified element names and source-specific variable metadata."""

from dataclasses import dataclass, replace
from datetime import timedelta
import re

import numpy as np


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
class InputVariable:
    """派生相对湿度所需的一个原始字段；单位可覆盖文件属性。"""

    variable: str
    units: str | None = None
    product: str | None = None


@dataclass(frozen=True)
class VariableConfig:
    """原始字段、风分量或湿度输入，以及单位和时间含义。"""

    variable: str | None
    units: str | None = None
    time_kind: str | None = "instant"
    interval: timedelta | None = None
    timestamp: str | None = None
    product: str | None = None
    components: tuple[str, ...] = ()
    inputs: tuple[tuple[str, InputVariable], ...] = ()

    @property
    def native_variables(self) -> tuple[str, ...]:
        if self.inputs:
            return tuple(dict.fromkeys(item.variable for _, item in self.inputs))
        return self.components or ((self.variable,) if self.variable else ())

    @property
    def source_inputs(self) -> tuple[tuple[str, InputVariable], ...]:
        """CLDAS 直接字段与派生字段共用产品/变量读取计划。"""
        if self.inputs:
            # 以水汽产品作为原生时间轴，不受 TOML 子表书写顺序影响。
            return tuple(sorted(self.inputs, key=lambda item: item[0] in {"temperature", "pressure"}))
        return (("value", InputVariable(self.variable, self.units, self.product)),)


# Defaults are only for fields verified in the supplied data templates.
# Station precipitation deliberately has no default time_kind: verify its meaning first.
DEFAULT_VARIABLES = {
    "wrf": {
        "temperature_2m": VariableConfig("T2"),
        "wind_speed_10m": VariableConfig(None, components=("U10", "V10")),
        "relative_humidity_2m": VariableConfig(None, inputs=(
            ("mixing_ratio", InputVariable("Q2")),
            ("temperature", InputVariable("T2")),
            ("pressure", InputVariable("PSFC")),
        )),
    },
    "cldas": {
        "temperature_2m": VariableConfig("TAIR", product="TMP"),
        "relative_humidity_2m": VariableConfig(None, inputs=(
            ("specific_humidity", InputVariable("QAIR", product="SHU")),
            ("temperature", InputVariable("TAIR", product="TMP")),
            ("pressure", InputVariable("PAIR", product="PRS")),
        )),
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
        if kind == "wrf" and field == "wind_speed_10m":
            allowed_options.add("components")
        if kind in {"wrf", "cldas"} and field == "relative_humidity_2m":
            allowed_options.add("inputs")
        if unknown := options.keys() - allowed_options:
            raise ValueError(f"{field}: 未知配置项 {', '.join(sorted(unknown))}")

        base = defaults.get(field)
        if base is None and not {"variable", "components", "inputs"}.intersection(options):
            raise ValueError(f"{field}.variable: 请指定原始变量名")
        updates = {}
        if "inputs" in options:
            if {"variable", "product", "components"}.intersection(options):
                raise ValueError(f"{field}: inputs 不能与 variable/product/components 混用")
            updates.update(inputs=_parse_humidity_inputs(options["inputs"], kind),
                           variable=None, product=None, components=())
        elif "components" in options:
            if "variable" in options:
                raise ValueError(f"{field}: variable 与 components 只能配置一项")
            components = options["components"]
            if (not isinstance(components, list) or len(components) != 2
                    or any(not isinstance(item, str) or not item.strip() for item in components)
                    or len({item.strip() for item in components}) != 2):
                raise ValueError(f"{field}.components: WRF 10 米风速须为两个不同的原始分量名")
            updates["components"] = tuple(item.strip() for item in components)
            updates["variable"] = None
        elif "variable" in options:
            text = options["variable"]
            if not isinstance(text, str) or not text.strip():
                raise ValueError(f"{field}.variable: 应为非空字符串")
            text = text.strip()
            if kind == "wrf" and field == "wind_speed_10m" and text.upper() in {"U10", "V10", "U", "V"}:
                raise ValueError(f"{field}.variable: {text} 是风分量，不是风速；请使用 components = [\"U10\", \"V10\"]")
            updates["variable"] = text
            updates["components"] = ()
            updates["inputs"] = ()
        for key in ("units", "time_kind", "timestamp", "product"):
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
        spec = replace(base or VariableConfig(options.get("variable")), **updates)
        if not simple_field:
            validate_variable(field, spec)
        result[field] = spec

    if kind == "regional":
        merged = defaults | result
        names = [spec.variable for spec in merged.values()]
        if len(names) != len(set(names)):
            raise ValueError("映射后的 Excel 表头不能重复")
    return tuple(result.items())


def _parse_humidity_inputs(value: object, kind: str) -> tuple[tuple[str, InputVariable], ...]:
    if not isinstance(value, dict) or set(value) not in (
        {"temperature", "pressure", "mixing_ratio"},
        {"temperature", "pressure", "specific_humidity"},
    ):
        raise ValueError("inputs 须有 temperature、pressure，以及 mixing_ratio 或 specific_humidity 二选一")
    result = []
    for role, options in value.items():
        allowed = {"variable", "units"} | ({"product"} if kind == "cldas" else set())
        if not isinstance(options, dict) or options.keys() - allowed:
            raise ValueError(f"inputs.{role}: 只接受 {', '.join(sorted(allowed))}")
        if "variable" not in options or (kind == "cldas" and "product" not in options):
            raise ValueError(f"inputs.{role}: 须填写 variable；CLDAS 还须填写 product")
        if any(not isinstance(text, str) or not text.strip() for text in options.values()):
            raise ValueError(f"inputs.{role}: 字段值须为非空字符串")
        item = InputVariable(**{key: text.strip() for key, text in options.items()})
        if item.product is not None and not re.fullmatch(r"[A-Z0-9]+", item.product):
            raise ValueError(f"inputs.{role}.product: 产品代码须为大写字母或数字")
        result.append((role, item))
    return tuple(result)


def validate_variable(field: str, spec: VariableConfig) -> None:
    if spec.inputs:
        if field != "relative_humidity_2m" or spec.variable or spec.components or spec.product:
            raise ValueError("inputs 仅用于派生 relative_humidity_2m，不能同时映射直接字段")
        if spec.time_kind != "instant":
            raise ValueError("派生相对湿度只接受同一时刻的瞬时输入，不能用平均基础量推算平均湿度")
        if spec.units not in (None, "%"):
            raise ValueError("派生相对湿度输出单位固定为 %；原始单位请放在 inputs 对应字段中")
        if set(dict(spec.inputs)) not in (
            {"temperature", "pressure", "mixing_ratio"},
            {"temperature", "pressure", "specific_humidity"},
        ) or len(spec.inputs) != 3:
            raise ValueError("相对湿度输入须包含温度、气压及一种明确的水汽量")
        for role, item in spec.inputs:
            if item.units is not None:
                convert_value(role, None, item.units, "instant")
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
    if field in {"temperature_2m", "temperature"}:
        if unit in {"k", "kelvin"}:
            offset = -273.15
        elif unit not in {"c", "℃", "°c", "degc", "degree_celsius", "degrees_celsius"}:
            raise ValueError(f"无法识别温度单位 {units!r}")
    elif field == "pressure":
        if unit in {"hpa", "mb", "mbar"}:
            scale = 100.0
        elif unit != "pa":
            raise ValueError(f"气压单位须为 Pa 或 hPa: {units!r}")
    elif field in {"mixing_ratio", "specific_humidity"}:
        if unit in {"g/kg", "gkg-1", "gkg^-1"}:
            scale = 0.001
        elif unit not in {"kg/kg", "kgkg-1", "kgkg^-1", "1"}:
            raise ValueError(f"水汽混合比/比湿单位须为 kg/kg 或 g/kg: {units!r}")
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
    if field == "relative_humidity_2m":
        result = float(np.clip(result, 0.0, 100.0))
    if field == "precipitation" and result < 0:
        raise ValueError("有效降水量不能为负，请核对缺测码与累计规则")
    return result


def relative_humidity(values: dict, units: dict[str, str], spec: VariableConfig):
    """液态水基准：Bolton 饱和水汽压；混合比和比湿使用各自的水汽压公式。

    标量和掩码数组共用计算；缺测传播，结果限制在 0–100%。
    """
    inputs = dict(spec.inputs)
    converted = {
        role: convert_value(role, values[role], item.units or units[role], "instant")
        for role, item in inputs.items()
    }
    if any(value is None for value in converted.values()):
        return None
    temperature = np.ma.masked_invalid(np.ma.asarray(converted["temperature"], dtype=np.float64))
    pressure = np.ma.masked_invalid(np.ma.asarray(converted["pressure"], dtype=np.float64))
    specific = "specific_humidity" in converted
    moisture = np.ma.masked_invalid(np.ma.asarray(
        converted["specific_humidity" if specific else "mixing_ratio"], dtype=np.float64,
    ))
    if (np.ma.any(temperature <= -243.5) or np.ma.any(pressure <= 0)
            or np.ma.any(moisture < 0) or (specific and np.ma.any(moisture >= 1))):
        raise ValueError("相对湿度输入无效：请核对温度、正气压、非负水汽量；比湿须小于 1 kg/kg")
    epsilon = 0.622
    vapor_pressure = (moisture * pressure / (epsilon + (1 - epsilon) * moisture)
                      if specific else moisture * pressure / (epsilon + moisture))
    if np.ma.any(vapor_pressure >= pressure):
        raise ValueError("相对湿度输入无效：水汽分压须小于总气压")
    saturation = 611.2 * np.ma.exp(17.67 * temperature / (temperature + 243.5))
    result = np.ma.clip(np.ma.masked_invalid(100.0 * vapor_pressure / saturation), 0.0, 100.0)
    if result.ndim == 0:
        return None if np.ma.is_masked(result) else float(result)
    return result
