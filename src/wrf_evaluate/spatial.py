"""配置点位到曲线网格或一维经纬度轴网格的最近球面格点。"""

from dataclasses import dataclass

import numpy as np


class SpatialMatchError(ValueError):
    """网格坐标或站点位置不能用于空间定位。"""


@dataclass(frozen=True, slots=True)
class GridPoint:
    row: int
    column: int
    distance_km: float
    inside_domain: bool


def _validate_point(longitude: float, latitude: float) -> None:
    if any(isinstance(value, (bool, np.bool_)) or not isinstance(value, (int, float, np.integer, np.floating))
           for value in (longitude, latitude)):
        raise SpatialMatchError("站点经纬度须为有效十进制度坐标")
    if not np.isfinite(longitude) or not np.isfinite(latitude) or not -180 <= longitude <= 180 or not -90 <= latitude <= 90:
        raise SpatialMatchError("站点经纬度须为有效十进制度坐标")


def nearest_rectilinear_point(
    longitude: float, latitude: float, longitudes: np.ndarray, latitudes: np.ndarray,
) -> GridPoint:
    """对单调经纬度轴做球面最近点搜索，内存与轴长相关，不构造二维网格。

    支持升降序；范围按最外层格点中心定义，不外扩半格。暂不支持跨日界线。
    """
    _validate_point(longitude, latitude)
    axes = []
    for values, limit in ((longitudes, 180), (latitudes, 90)):
        axis = np.ma.asarray(values)
        if axis.ndim != 1 or axis.size < 2 or axis.dtype.kind not in "iuf":
            raise SpatialMatchError("经纬度轴须为至少含两个格点的一维数值数组")
        data = axis.data.astype(np.float64, copy=False)
        if np.ma.getmaskarray(axis).any() or not np.isfinite(data).all() or (np.abs(data) > limit).any():
            raise SpatialMatchError("经纬度轴含缺测或超范围坐标")
        if not (np.all(np.diff(data) > 0) or np.all(np.diff(data) < 0)):
            raise SpatialMatchError("经纬度轴须严格单调")
        axes.append(data)
    lons, lats = axes
    if np.ptp(lons) >= 180:
        raise SpatialMatchError("仅支持经度跨度小于 180° 且不跨日界线的经纬网格")
    delta_lon = np.deg2rad((lons - longitude + 180) % 360 - 180)
    column = int(np.argmin(np.abs(delta_lon)))
    lat1, lat2 = np.deg2rad(latitude), np.deg2rad(lats)
    distance = np.sin((lat2 - lat1) / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin(delta_lon[column] / 2) ** 2
    row = int(np.argmin(distance))
    distance_km = 2 * 6371.0088 * np.arcsin(np.sqrt(np.clip(distance[row], 0, 1)))
    inside = lons.min() <= longitude <= lons.max() and lats.min() <= latitude <= lats.max()
    return GridPoint(row, column, float(distance_km), bool(inside))


def _inside_grid_boundary(longitude: np.ndarray, latitude: np.ndarray, point_lon: float, point_lat: float) -> bool:
    """判断是否落在区域网格最外层格点中心围成的多边形内，包含边界。"""
    boundary_lon = np.concatenate((
        longitude[0, :], longitude[1:, -1], longitude[-1, -2::-1], longitude[-2:0:-1, 0],
    ))
    boundary_lat = np.concatenate((
        latitude[0, :], latitude[1:, -1], latitude[-1, -2::-1], latitude[-2:0:-1, 0],
    ))
    # 以网格而非站点为经度展开中心，避免把对跖点误判为域内。
    origin = boundary_lon[0]
    x = (boundary_lon - origin + 180) % 360 - 180
    y = boundary_lat
    if np.ptp(x) >= 180:
        raise SpatialMatchError("仅支持经度跨度小于 180° 的区域网格")
    px = (point_lon - origin + 180) % 360 - 180
    x2, y2 = np.roll(x, -1), np.roll(y, -1)
    dx, dy = x2 - x, y2 - y
    length = np.hypot(dx, dy)
    if abs(np.sum(x * y2 - x2 * y)) < 1e-12:
        raise SpatialMatchError("WRF 网格边界退化，不能确定覆盖范围")
    tolerance = 1e-10  # 单位为度，仅用于浮点边界判断。
    on_edge = (
        (length > 0)
        & (np.abs((px - x) * dy - (point_lat - y) * dx) <= tolerance * length)
        & (px >= np.minimum(x, x2) - tolerance) & (px <= np.maximum(x, x2) + tolerance)
        & (point_lat >= np.minimum(y, y2) - tolerance) & (point_lat <= np.maximum(y, y2) + tolerance)
    )
    if on_edge.any():
        return True
    crossing = (y > point_lat) != (y2 > point_lat)
    crossing_x = x[crossing] + (point_lat - y[crossing]) * dx[crossing] / dy[crossing]
    return bool(np.count_nonzero(px < crossing_x) % 2)


def nearest_grid_point(
    station_longitude: float,
    station_latitude: float,
    grid_longitudes: np.ndarray,
    grid_latitudes: np.ndarray,
) -> GridPoint:
    """区域二维网格的球面最近点；保留域外标记，等距时取行优先首点。"""
    _validate_point(station_longitude, station_latitude)
    longitude = np.ma.asarray(grid_longitudes)
    latitude = np.ma.asarray(grid_latitudes)
    if longitude.shape != latitude.shape or longitude.ndim != 2 or not longitude.size:
        raise SpatialMatchError("WRF 经纬度须为形状一致的非空二维数组")
    if longitude.dtype.kind not in "iuf" or latitude.dtype.kind not in "iuf":
        raise SpatialMatchError("WRF 经纬度必须为数值数组")

    longitude_values = longitude.data.astype(np.float64, copy=False)
    latitude_values = latitude.data.astype(np.float64, copy=False)
    valid = (
        ~np.ma.getmaskarray(longitude)
        & ~np.ma.getmaskarray(latitude)
        & np.isfinite(longitude_values)
        & np.isfinite(latitude_values)
        & (longitude_values >= -180)
        & (longitude_values <= 180)
        & (latitude_values >= -90)
        & (latitude_values <= 90)
    )
    if not valid.any():
        raise SpatialMatchError("WRF 网格没有有效经纬度")
    if longitude.shape[0] < 2 or longitude.shape[1] < 2 or not valid.all():
        raise SpatialMatchError("WRF 经纬度须为完整二维网格，且至少包含 2×2 个格点")

    lat1 = np.deg2rad(station_latitude)
    lat2 = np.deg2rad(latitude_values)
    delta_lat = lat2 - lat1
    delta_lon = np.deg2rad((longitude_values - station_longitude + 180) % 360 - 180)
    haversine = np.sin(delta_lat / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin(delta_lon / 2) ** 2
    # 大圆距离是 haversine 的单调函数；只对选中点计算实际距离。
    flat_index = int(np.argmin(haversine))
    row, column = np.unravel_index(flat_index, haversine.shape)
    distance_km = 2 * 6371.0088 * np.arcsin(np.sqrt(np.clip(haversine[row, column], 0, 1)))
    inside_domain = _inside_grid_boundary(longitude_values, latitude_values, station_longitude, station_latitude)
    return GridPoint(int(row), int(column), float(distance_km), inside_domain)
