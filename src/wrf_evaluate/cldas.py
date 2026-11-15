"""只读 CLDAS 小时产品；文件名提供有效时间，网格场按需加载。"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import numpy as np
from netCDF4 import Dataset

from .spatial import GridPoint, SpatialMatchError, nearest_rectilinear_point


class CldasReadError(ValueError):
    """CLDAS 文件名或网格结构无法读取。"""


@dataclass(frozen=True)
class CldasHeader:
    path: Path
    product: str
    time: datetime
    latitudes: np.ndarray
    longitudes: np.ndarray
    variables: frozenset[str]

    @property
    def shape(self) -> tuple[int, int]:
        return self.latitudes.size, self.longitudes.size


@dataclass(frozen=True)
class CldasField:
    values: np.ma.MaskedArray
    units: str


@dataclass(frozen=True)
class CldasPointSample:
    path: Path
    time: datetime
    grid_point: GridPoint
    value: float | None
    units: str
    grid_coordinates: tuple[float, float] | None = None


@dataclass(frozen=True)
class CldasFileInfo:
    path: Path
    product: str
    time: datetime
    production_time: datetime
    family: str  # 如 NRT_ASI_0P0625；不同范围、分辨率的文件不互相替换。


def _read_axis(dataset: Dataset, name: str) -> np.ndarray:
    if name not in dataset.variables:
        raise CldasReadError(f"缺少 {name} 坐标")
    variable = dataset.variables[name]
    if variable.dimensions != (name,) or np.dtype(variable.dtype).kind not in "iuf":
        raise CldasReadError(f"{name} 应为一维数值坐标")
    values = np.ma.masked_invalid(variable[:], copy=False)
    if not values.size or np.ma.getmaskarray(values).any():
        raise CldasReadError(f"{name} 坐标为空或含缺测值")
    values = np.asarray(values)
    # 保留源文件方向，允许递增或递减，拒绝重复和乱序坐标。
    if not (np.all(values[1:] > values[:-1]) or np.all(values[1:] < values[:-1])):
        raise CldasReadError(f"{name} 坐标须严格单调")
    return values


def parse_cldas_filename(path: str | Path, timezone: str = "UTC") -> CldasFileInfo:
    """解析标准 CLDAS 文件名的有效时间和制作时间，不打开文件。"""
    path = Path(path)
    match = re.search(
        r"_([0-9]{14})_P_CLDAS_([A-Z0-9]+_[A-Z0-9]+_[A-Z0-9]+)_HOR-([A-Z0-9]+)-([0-9]{10})\.nc$",
        path.name,
    )
    if match is None:
        raise CldasReadError(f"无法识别 CLDAS 制作时间或小时产品有效时间: {path.name}")
    try:
        zone = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise CldasReadError(f"未知 CLDAS 时区: {timezone}") from exc
    try:
        time = datetime.strptime(match[4], "%Y%m%d%H").replace(tzinfo=zone)
        production_time = datetime.strptime(match[1], "%Y%m%d%H%M%S").replace(tzinfo=zone)
    except ValueError as exc:
        raise CldasReadError(f"CLDAS 有效时间或制作时间格式错误: {path.name}") from exc
    return CldasFileInfo(path, match[3], time, production_time, match[2])


def read_cldas_header(path: str | Path, timezone: str = "UTC") -> CldasHeader:
    """读取有效时间、产品、一维经纬度轴及二维场名称，不加载网格场。

    不能使用父目录日期或文件名前面的制作时间作为有效时间；时区由来源配置指定。
    """
    path = Path(path)
    info = parse_cldas_filename(path, timezone)

    try:
        with Dataset(path, mode="r") as dataset:
            latitudes = _read_axis(dataset, "LAT")
            longitudes = _read_axis(dataset, "LON")
            variables = frozenset(
                name for name, variable in dataset.variables.items()
                if variable.dimensions == ("LAT", "LON") and np.dtype(variable.dtype).kind in "iuf"
            )
            if not variables:
                raise CldasReadError("缺少 LAT/LON 二维数值场")
    except (CldasReadError, OSError) as exc:
        raise CldasReadError(f"无法读取 CLDAS 文件 {path}: {exc}") from exc
    return CldasHeader(path, info.product, info.time, latitudes, longitudes, variables)


def read_cldas_field(
    path: str | Path, name: str, *, selection: tuple[slice, slice] | None = None,
) -> CldasField:
    """只读取指定 LAT/LON 二维场；保留原单位、轴方向和缺测掩码。"""
    try:
        with Dataset(path, mode="r") as dataset:
            if name not in dataset.variables:
                raise CldasReadError(f"缺少变量 {name}")
            variable = dataset.variables[name]
            if variable.dimensions != ("LAT", "LON") or np.dtype(variable.dtype).kind not in "iuf":
                raise CldasReadError(f"{name} 不是 LAT/LON 二维数值场")
            values = np.ma.masked_invalid(variable[selection or (slice(None), slice(None))], copy=False)
            return CldasField(values, getattr(variable, "units", ""))
    except (CldasReadError, OSError) as exc:
        raise CldasReadError(f"无法读取 CLDAS 文件 {path}: {exc}") from exc


def read_cldas_point(
    path: str | Path, name: str, longitude: float, latitude: float, timezone: str = "UTC",
) -> CldasPointSample:
    """读取一维坐标轴和一个最近格点值，不加载气象变量的二维场。"""
    return read_cldas_points(path, (name,), ((longitude, latitude),), timezone)[name][0]


def read_cldas_points(
    path: str | Path, names: Sequence[str], points: Sequence[tuple[float, float]],
    timezone: str = "UTC",
) -> dict[str, tuple[CldasPointSample, ...]]:
    """一次打开文件提取多个变量和点位；坐标共用，重复格点只读一次。

    返回各变量按 points 顺序排列的样本，不加载完整二维气象场。
    """
    if isinstance(names, str) or not names or any(not isinstance(name, str) or not name.strip() for name in names):
        raise CldasReadError("names 须为非空变量名序列")
    if len(set(names)) != len(names) or not points:
        raise CldasReadError("names 不能重复，points 不能为空")
    info = parse_cldas_filename(path, timezone)
    try:
        with Dataset(path, mode="r") as dataset:
            for name in names:
                if name not in dataset.variables:
                    raise CldasReadError(f"缺少变量 {name}")
                variable = dataset.variables[name]
                if variable.dimensions != ("LAT", "LON") or np.dtype(variable.dtype).kind not in "iuf":
                    raise CldasReadError(f"{name} 不是 LAT/LON 二维数值场")
            longitudes, latitudes = _read_axis(dataset, "LON"), _read_axis(dataset, "LAT")
            locations = []
            for longitude, latitude in points:
                point = nearest_rectilinear_point(longitude, latitude, longitudes, latitudes)
                if not point.inside_domain:
                    raise CldasReadError(
                        f"点位 ({longitude:g}, {latitude:g}) 位于 CLDAS 网格范围外，"
                        f"最近格点距离 {point.distance_km:.2f} km"
                    )
                locations.append(point)
            samples = {}
            for name in names:
                variable = dataset.variables[name]
                values = {}
                for point in locations:
                    key = point.row, point.column
                    if key not in values:
                        value = variable[key]
                        values[key] = None if np.ma.is_masked(value) or not np.isfinite(value) else float(value)
                units = getattr(variable, "units", "")
                samples[name] = tuple(
                    CldasPointSample(info.path, info.time, point, values[point.row, point.column], units,
                                     (float(longitudes[point.column]), float(latitudes[point.row])))
                    for point in locations
                )
            return samples
    except (CldasReadError, SpatialMatchError, OSError) as exc:
        raise CldasReadError(f"无法读取 CLDAS 文件 {path}: {exc}") from exc
