"""只读 WRF 文件；先读时间和元数据，再按需读取单时刻二维变量。"""

from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import numpy as np
from netCDF4 import Dataset

from .config import PeriodConfig
from .spatial import GridPoint, SpatialMatchError, nearest_grid_point
from .variables import VariableConfig, convert_value, relative_humidity


class WrfReadError(ValueError):
    """WRF 文件结构或时间无法读取。"""


@dataclass(frozen=True)
class WrfHeader:
    path: Path
    times: tuple[datetime, ...]
    dimensions: tuple[tuple[str, int], ...]
    variables: frozenset[str]
    model_start_time: datetime | None

    @property
    def time_start(self) -> datetime:
        return self.times[0]

    @property
    def time_end(self) -> datetime:
        return self.times[-1]


@dataclass(frozen=True)
class WrfField:
    values: np.ma.MaskedArray
    units: str


@dataclass(frozen=True)
class WrfPointSample:
    row: int
    column: int
    distance_km: float
    values: dict[str, float | None]
    units: dict[str, str]


@dataclass(frozen=True)
class WrfPointFrame:
    path: Path
    time_index: int
    time: datetime
    row: int
    column: int
    distance_km: float
    values: dict[str, float | None]
    units: dict[str, str]
    model_start_time: datetime | None = None
    grid_coordinates: tuple[float, float] | None = None


WrfValue = float | np.ma.MaskedArray | None


def resolve_wrf_variable(
    spec: VariableConfig, values: Mapping[str, WrfValue], units: Mapping[str, str],
    on_invalid: Callable[[str], None] | None = None,
) -> tuple[WrfValue, str]:
    """直接字段、10 米风速或派生湿度；点值与空间场共用同一规则。"""
    if spec.inputs:
        return relative_humidity(
            {role: values[item.variable] for role, item in spec.inputs},
            {role: units[item.variable] for role, item in spec.inputs}, spec, on_invalid,
        ), "%"
    if not spec.components:
        return values[spec.variable], units[spec.variable]
    for name in spec.components:
        convert_value("wind_speed_10m", None, spec.units or units[name], spec.time_kind, spec.interval)
    u, v = (values[name] for name in spec.components)
    speed = None if u is None or v is None else np.hypot(u, v)
    return speed, "m/s"


def _read_times(dataset: Dataset, zone: ZoneInfo) -> tuple[datetime, ...]:
    if "Times" not in dataset.variables:
        raise WrfReadError("缺少 Times 变量")
    variable = dataset.variables["Times"]
    if variable.dimensions != ("Time", "DateStrLen") or variable.dtype != np.dtype("S1"):
        raise WrfReadError("Times 应为二维字符数组")
    # 禁止 netCDF4 自动合并字符串，保证读取行为不受 _Encoding 属性影响。
    variable.set_auto_chartostring(False)
    raw_times = variable[:]
    if np.ma.getmaskarray(raw_times).any():
        raise WrfReadError("Times 含缺测字符，不能作为有效时间")
    try:
        times = tuple(
            datetime.strptime(row.tobytes().decode("ascii").rstrip("\x00 "), "%Y-%m-%d_%H:%M:%S")
            .replace(tzinfo=zone)
            for row in raw_times
        )
    except (ValueError, UnicodeError) as exc:
        raise WrfReadError("Times 含无法解析的时间") from exc
    if not times or any(right <= left for left, right in zip(times, times[1:])):
        raise WrfReadError("Times 不能为空且须严格递增")
    return times


def _model_start_time(dataset: Dataset, zone: ZoneInfo) -> datetime | None:
    start = getattr(dataset, "SIMULATION_START_DATE", getattr(dataset, "START_DATE", None))
    try:
        return datetime.strptime(start, "%Y-%m-%d_%H:%M:%S").replace(tzinfo=zone) if start else None
    except (ValueError, TypeError) as exc:
        raise WrfReadError("模式起始时间属性格式错误") from exc


def read_wrf_header(path: str | Path, timezone: str = "UTC") -> WrfHeader:
    """只读取 WRF 时间、维度和变量名，不加载变量网格。"""
    path = Path(path)
    try:
        zone = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise WrfReadError(f"未知 WRF 时区: {timezone}") from exc

    try:
        with Dataset(path, mode="r") as dataset:
            times = _read_times(dataset, zone)
            model_start_time = _model_start_time(dataset, zone)
            dimensions = tuple((name, len(dimension)) for name, dimension in dataset.dimensions.items())
            variables = frozenset(dataset.variables)
    except (WrfReadError, OSError) as exc:
        raise WrfReadError(f"无法读取 WRF 文件 {path}: {exc}") from exc

    return WrfHeader(path, times, dimensions, variables, model_start_time)


def read_wrf_field(path: str | Path, name: str, time_index: int = 0) -> WrfField:
    """只加载一个时刻的地面场（含 XLAT/XLONG）；保留原单位和缺测掩码。

    三维大气变量和交错网格不在本函数处理范围内。降水仍为原累计量，
    不在读取阶段差分；风分量仍沿原网格方向，不在此旋转。
    """
    return read_wrf_fields(path, [name], time_index)[name]


def _validate_names(names: Sequence[str]) -> None:
    if not isinstance(names, Sequence) or isinstance(names, str) or not names:
        raise WrfReadError("names 须为非空变量名序列")
    if any(not isinstance(name, str) or not name.strip() for name in names):
        raise WrfReadError("names 须为非空变量名序列")
    if len(set(names)) != len(names):
        raise WrfReadError("names 不能包含重复变量名")


def _ground_variable(dataset: Dataset, name: str):
    if name not in dataset.variables:
        raise WrfReadError(f"缺少变量 {name}")
    variable = dataset.variables[name]
    if any(dimension.endswith("_stag") for dimension in variable.dimensions):
        raise WrfReadError(f"{name} 位于交错网格 {variable.dimensions}；地面检验不直接读取三维交错场，请使用 T2、U10/V10 等地面变量")
    if variable.dimensions != ("Time", "south_north", "west_east") or np.dtype(variable.dtype).kind not in "iuf":
        raise WrfReadError(f"{name} 不是 Time/south_north/west_east 地面变量，实际维度 {variable.dimensions}")
    return variable


class WrfFieldReader:
    """空间图共用当前文件和当前帧；换文件立即关闭，换帧释放已读场。

    配合 contextlib.closing 使用。内存只保留一帧已请求的字段，
    不预读其他变量、不落盘，返回数组不得被调用方原地修改。
    """

    def __init__(self):
        self.path = None
        self.dataset = None
        self.time_index = None
        self.fields: dict[str, WrfField] = {}

    def close(self) -> None:
        dataset, self.dataset = self.dataset, None
        self.path = self.time_index = None
        self.fields.clear()
        if dataset is not None:
            dataset.close()

    def read(self, path: str | Path, names: Sequence[str], time_index: int = 0) -> dict[str, WrfField]:
        if not isinstance(time_index, int) or isinstance(time_index, bool) or time_index < 0:
            raise WrfReadError("time_index 须为非负整数")
        _validate_names(names)
        path = Path(path)
        try:
            if self.path != path:
                self.close()
                self.dataset = Dataset(path, mode="r")
                self.path = path
            dataset = self.dataset
            # 先核查全部请求，缺少某个变量时不先加载其他大数组。
            for name in names:
                variable = _ground_variable(dataset, name)
                if time_index >= variable.shape[0]:
                    raise WrfReadError(f"time_index={time_index} 超出 {name} 时间范围")
            if self.time_index != time_index:
                self.fields.clear()
                self.time_index = time_index
            for name in names:
                if name in self.fields:
                    continue
                variable = dataset.variables[name]
                values = np.ma.masked_invalid(variable[time_index, :, :], copy=False)
                self.fields[name] = WrfField(values, getattr(variable, "units", ""))
            return {name: self.fields[name] for name in names}
        except (WrfReadError, OSError) as exc:
            self.close()
            raise WrfReadError(f"无法读取 WRF 文件 {path}: {exc}") from exc


def read_wrf_fields(
    path: str | Path, names: Sequence[str], time_index: int = 0,
) -> dict[str, WrfField]:
    """一次打开并关闭文件，读取指定时刻的二维地面场，保留原单位和缺测。"""
    with closing(WrfFieldReader()) as reader:
        return reader.read(path, names, time_index)


def sample_wrf_station(
    fields: dict[str, WrfField], longitude: float, latitude: float,
) -> WrfPointSample:
    """从已读取的时次网格场提取站点最近格点值，不改变变量单位。"""
    if "XLONG" not in fields or "XLAT" not in fields:
        raise WrfReadError("站点取值需要同时提供 XLONG 和 XLAT")
    grid_longitude = fields["XLONG"].values
    grid_latitude = fields["XLAT"].values
    point = _locate_point(longitude, latitude, grid_longitude, grid_latitude)

    values = {}
    units = {}
    for name, field in fields.items():
        if name in {"XLONG", "XLAT"}:
            continue
        if field.values.shape != grid_longitude.shape:
            raise WrfReadError(f"{name} 网格形状与 XLAT/XLONG 不同")
        value = field.values[point.row, point.column]
        values[name] = None if np.ma.is_masked(value) or not np.isfinite(value) else float(value)
        units[name] = field.units
    return WrfPointSample(point.row, point.column, point.distance_km, values, units)


def _locate_point(longitude: float, latitude: float, grid_longitude: np.ndarray, grid_latitude: np.ndarray) -> GridPoint:
    try:
        point = nearest_grid_point(longitude, latitude, grid_longitude, grid_latitude)
    except SpatialMatchError as exc:
        raise WrfReadError(str(exc)) from exc
    if not point.inside_domain:
        raise WrfReadError(f"站点位于 WRF 网格范围外，最近格点距离 {point.distance_km:.2f} km")
    return point


class WrfPointLocator:
    """本次读取复用上一帧定位；跨文件仍逐帧核对坐标，不假定网格固定。"""

    def __init__(self, points: Sequence[tuple[float, float]]):
        self.points = tuple(points)
        self.grid = None
        self.locations = ()

    def locate(self, grid: tuple[np.ma.MaskedArray, ...]) -> tuple[GridPoint, ...]:
        same_grid = self.grid is not None and all(
            np.array_equal(current.data, previous.data)
            and np.array_equal(np.ma.getmaskarray(current), np.ma.getmaskarray(previous))
            for current, previous in zip(grid, self.grid, strict=True)
        )
        if not same_grid:
            locations = {}
            for longitude, latitude in self.points:
                key = longitude, latitude
                if key not in locations:
                    try:
                        locations[key] = _locate_point(longitude, latitude, *grid)
                    except WrfReadError as exc:
                        raise WrfReadError(f"点位 ({longitude:g}, {latitude:g}): {exc}") from exc
            self.locations = tuple(locations[point] for point in self.points)
            self.grid = grid
        return self.locations


def iter_wrf_points_frames(
    path: str | Path,
    names: Sequence[str],
    timezone: str,
    points: Sequence[tuple[float, float]],
    *,
    period: PeriodConfig | None = None,
    init_time: datetime | None = None,
    min_lead_hours: float = 0,
    max_lead_hours: float = 168,
    locator: WrfPointLocator | None = None,
) -> Iterator[tuple[WrfPointFrame, ...]]:
    """一次打开文件，按时次返回所有点位值；坐标读取与定位结果复用。

    每帧检查坐标；可传入同一 locator 跨文件复用定位，移动网格重新定位。提前停止遍历时
    须 close() 此生成器或使用 contextlib.closing，保证文件及时关闭。
    指定 period 时包含两个端点，范围外时次不读取坐标或气象场。
    """
    path = Path(path)
    _validate_names(names)
    if not points:
        raise WrfReadError("points 须至少包含一个经纬度点位")
    if locator is None:
        locator = WrfPointLocator(points)
    elif locator.points != tuple(points):
        raise WrfReadError("复用定位的点位列表与本次读取不一致")
    if any(name in {"XLAT", "XLONG"} for name in names):
        raise WrfReadError("names 不应包含坐标变量 XLAT/XLONG")
    try:
        zone = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise WrfReadError(f"未知 WRF 时区: {timezone}") from exc

    try:
        with Dataset(path, mode="r") as dataset:
            times = _read_times(dataset, zone)
            if period is not None and not any(period.start <= time <= period.end for time in times):
                return
            model_start_time = _model_start_time(dataset, zone)
            run_start = init_time or model_start_time
            if (not np.isfinite(min_lead_hours) or min_lead_hours < 0
                    or not np.isfinite(max_lead_hours) or max_lead_hours <= 0
                    or min_lead_hours > max_lead_hours):
                raise WrfReadError("预报时效须满足 0 ≤ min_lead_hours ≤ max_lead_hours，且最大时效为正")
            if min_lead_hours and run_start is None:
                raise WrfReadError("min_lead_hours 需要 WRF 文件内或配置中的起报时间")
            valid_start = run_start + timedelta(hours=min_lead_hours) if run_start else None
            valid_end = run_start + timedelta(hours=max_lead_hours) if run_start else None
            selected_indices = [
                index for index, time in enumerate(times)
                if (period is None or period.start <= time <= period.end)
                and (valid_start is None or time >= valid_start)
                and (valid_end is None or time <= valid_end)
            ]
            if not selected_indices:
                return
            for name in ("XLAT", "XLONG", *names):
                variable = _ground_variable(dataset, name)
                if variable.shape[0] != len(times):
                    raise WrfReadError(f"{name} 的时次数与 Times 不一致")

            units = {name: getattr(dataset.variables[name], "units", "") for name in names}
            for time_index in selected_indices:
                time = times[time_index]
                grid = tuple(np.ma.masked_invalid(dataset.variables[name][time_index, :, :], copy=False)
                             for name in ("XLONG", "XLAT"))
                locations = locator.locate(grid)
                # 单点索引避免 netCDF4 多个索引数组产生笛卡尔积；重复格点只读一次。
                samples = {}
                for point in locations:
                    key = point.row, point.column
                    if key not in samples:
                        values = {}
                        for name in names:
                            value = dataset.variables[name][time_index, point.row, point.column]
                            values[name] = None if np.ma.is_masked(value) or not np.isfinite(value) else float(value)
                        samples[key] = values
                yield tuple(
                    WrfPointFrame(path, time_index, time, point.row, point.column, point.distance_km,
                                  samples[point.row, point.column], units, model_start_time,
                                  tuple(float(axis[point.row, point.column]) for axis in grid))
                    for point in locations
                )
    except (WrfReadError, OSError) as exc:
        raise WrfReadError(f"无法读取 WRF 文件 {path}: {exc}") from exc


def iter_wrf_point_frames(
    path: str | Path,
    names: Sequence[str],
    timezone: str,
    longitude: float,
    latitude: float,
    *,
    period: PeriodConfig | None = None,
    init_time: datetime | None = None,
    min_lead_hours: float = 0,
    max_lead_hours: float = 168,
) -> Iterator[WrfPointFrame]:
    """单点读取接口；多点批处理请使用 iter_wrf_points_frames。"""
    with closing(iter_wrf_points_frames(
        path, names, timezone, ((longitude, latitude),), period=period,
        init_time=init_time, min_lead_hours=min_lead_hours, max_lead_hours=max_lead_hours,
    )) as frames:
        for batch in frames:
            yield batch[0]
