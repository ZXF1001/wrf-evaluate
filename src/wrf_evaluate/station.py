"""直接读取区域站原始 Excel；不重采样、不去重、不解释降水累计规则。"""

from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime
import math
from pathlib import Path
from time import monotonic
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from python_calamine import CalamineError, CalamineWorkbook

from .config import ConfigError, DEFAULT_COLUMN_MAPPING, PeriodConfig, parse_station_columns


class StationReadError(ValueError):
    """站点表结构或单元格内容无法读取。"""


@dataclass(frozen=True, slots=True)
class StationObservation:
    path: Path
    row: int
    station_id: str
    time: datetime
    values: dict[str, float | None]  # 原始单位；时间统计与单位转换由序列处理完成。


_SHEET = "气象数据"


def _time(value: object, zone: ZoneInfo) -> datetime:
    # 样例使用 14 位文本，保留秒；不根据文件名或年度目录推断日期。
    if not isinstance(value, str) or len(value) != 14 or not value.isascii() or not value.isdigit():
        raise ValueError("时间应为 YYYYMMDDhhmmss 文本")
    return datetime(
        int(value[:4]), int(value[4:6]), int(value[6:8]),
        int(value[8:10]), int(value[10:12]), int(value[12:14]), tzinfo=zone,
    )


def _number(value: object) -> float | None:
    if value is None or isinstance(value, str) and not value.strip():
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError("应为数值或空白")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("数值须有限；尚未定义特殊缺测码")
    return result


def iter_regional_observations(
    path: str | Path,
    timezone: str,
    column_mapping: Mapping[str, str] | None = None,
    *, fields: tuple[str, ...] | None = None,
    station_ids: set[str] | None = None, period: PeriodConfig | None = None,
    progress: Callable[[str], None] | None = None,
) -> Iterator[StationObservation]:
    """按表内顺序返回气温、相对湿度、10 分钟平均风速和降水原始列值，时区必须显式提供。

    支持同一表头格式的 xls/xlsx；可用 column_mapping 按规范字段名映射变化的表头。
    缺测保留为 None，异常值附原始行号报错。可先筛站号和时段，再解析气象数值。
    一次打开一个工作簿，底层仍需加载工作表，但不额外构造全表 Python 列表。
    正常读完或报错会关闭工作簿；提前停止遍历时，调用方应 close() 此生成器。
    """
    path = Path(path)
    try:
        mapping = dict(parse_station_columns(DEFAULT_COLUMN_MAPPING if column_mapping is None else column_mapping))
    except ConfigError as exc:
        raise StationReadError(f"{path}: {exc}") from exc
    try:
        zone = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
        raise StationReadError(f"未知站点时区: {timezone}") from exc

    try:
        with CalamineWorkbook.from_path(path) as workbook:
            sheet = workbook.get_sheet_by_name(_SHEET)
            # 已核实的区域站模板从 A1 开始；其他布局不静默套用。
            if sheet.start != (0, 0):
                raise StationReadError(f"{path} [{_SHEET}]: 表头须从 A1 开始")
            rows = sheet.iter_rows()
            header = [str(cell).strip() for cell in next(rows, ())]
            indices = []
            selected = ("station_id", "time") + (tuple(mapping)[2:] if fields is None else fields)
            for field in selected:
                name = mapping[field]
                if header.count(name) != 1:
                    raise StationReadError(f"{path} [{_SHEET}]: 字段 {field} 映射列 {name!r} 缺失或重复")
                indices.append(header.index(name))

            last_report = monotonic()
            for row_number, row in enumerate(rows, start=2):
                if progress is not None and row_number % 1024 == 0:
                    now = monotonic()
                    if now - last_report >= 5:
                        progress(f"读取站点 Excel {path.name}：已检查 {row_number - 1:,} 条记录")
                        last_report = now
                if all(cell is None or cell == "" for cell in row):
                    continue
                values = [row[index] if index < len(row) else None for index in indices]
                station_id, raw_time, *raw_numbers = values
                location = f"{path} [{_SHEET}] 第 {row_number} 行"
                if not isinstance(station_id, str) or not station_id.strip():
                    raise StationReadError(f"{location} {mapping['station_id']}: 应为非空文本")
                station_id = station_id.strip()
                if station_ids is not None and station_id not in station_ids:
                    continue
                try:
                    time = _time(raw_time, zone)
                except ValueError as exc:
                    raise StationReadError(f"{location} {mapping['time']}: {raw_time!r}: {exc}") from exc
                if period is not None and not period.start <= time <= period.end:
                    continue
                numbers = []
                for field, value in zip(selected[2:], raw_numbers):
                    try:
                        numbers.append(_number(value))
                    except (ValueError, OverflowError) as exc:
                        raise StationReadError(f"{location} {field}（{mapping[field]}）: {value!r}: {exc}") from exc
                yield StationObservation(path, row_number, station_id, time, dict(zip(selected[2:], numbers)))
    except (CalamineError, OSError) as exc:
        raise StationReadError(f"无法读取站点文件 {path} [{_SHEET}]: {exc}") from exc
