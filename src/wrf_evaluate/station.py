"""按各站映射直接读取原始 Excel 长表；不重采样、不去重。"""

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


def _time(value: object, zone: ZoneInfo, time_format: str) -> datetime:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str):
        text = value.strip()
        if time_format == "%Y%m%d%H%M%S":
            # 常见五分钟长表每月数千行，固定格式直接拆分，避免逐行 strptime 开销。
            if len(text) != 14 or not text.isascii() or not text.isdigit():
                raise ValueError("时间应为14位数字文本，与 excel.time_format 一致")
            result = datetime(int(text[:4]), int(text[4:6]), int(text[6:8]),
                              int(text[8:10]), int(text[10:12]), int(text[12:14]))
        else:
            result = datetime.strptime(text, time_format)
    else:
        raise ValueError("时间须为文本或 Excel 日期时间单元格")
    return result.replace(tzinfo=zone) if result.tzinfo is None else result.astimezone(zone)


def _number(value: object, missing_values: tuple[str | float, ...] = ()) -> float | None:
    if value is None or isinstance(value, str) and not value.strip():
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError("应为数值或空白")
    if value in missing_values or str(value).strip() in missing_values:
        return None
    result = float(value)
    if result in missing_values:
        return None
    if not math.isfinite(result):
        raise ValueError("数值须有限；特殊缺测码请在 excel.missing_values 中配置")
    return result


def iter_station_observations(
    path: str | Path,
    timezone: str,
    column_mapping: Mapping[str, str] | None = None,
    *, fields: tuple[str, ...] | None = None,
    station_ids: set[str] | None = None, period: PeriodConfig | None = None,
    single_station: bool = False,
    progress: Callable[[str], None] | None = None,
    sheet_name: str = _SHEET,
    time_format: str = "%Y%m%d%H%M%S",
    missing_values: tuple[str | float, ...] = (),
) -> Iterator[StationObservation]:
    """按表内顺序返回所配置列的原值，时区必须显式提供。

    支持同一表头格式的 xls/xlsx；可用 column_mapping 按规范字段名映射变化的表头。
    缺测保留为 None，异常值附原始行号报错。可先筛站号和时段，再解析气象数值。
    一次打开一个工作簿，底层仍需加载工作表，但不额外构造全表 Python 列表。
    未指定站号时可用 single_station 核验整表只有一个站号，不从文件名猜测。
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
            sheet = workbook.get_sheet_by_name(sheet_name)
            # 已核实的区域站和国家站长表均从 A1 开始；其他布局不静默套用。
            if sheet.start != (0, 0):
                raise StationReadError(f"{path} [{sheet_name}]: 表头须从 A1 开始")
            rows = sheet.iter_rows()
            header = [str(cell).strip() for cell in next(rows, ())]
            indices = []
            selected = ("station_id", "time") + (tuple(mapping)[2:] if fields is None else fields)
            for field in selected:
                name = mapping[field]
                if header.count(name) != 1:
                    raise StationReadError(f"{path} [{sheet_name}]: 字段 {field} 映射列 {name!r} 缺失或重复")
                indices.append(header.index(name))

            last_report = monotonic()
            detected_id = None
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
                location = f"{path} [{sheet_name}] 第 {row_number} 行"
                if not isinstance(station_id, str) or not station_id.strip():
                    raise StationReadError(f"{location} {mapping['station_id']}: 应为非空文本")
                station_id = station_id.strip()
                if single_station:
                    if detected_id is not None and station_id != detected_id:
                        raise StationReadError(
                            f"{location}: 工作表包含多个站号（{detected_id}、{station_id}），请配置 station_id 筛选"
                        )
                    detected_id = station_id
                if station_ids is not None and station_id not in station_ids:
                    continue
                try:
                    time = _time(raw_time, zone, time_format)
                except ValueError as exc:
                    raise StationReadError(f"{location} {mapping['time']}: {raw_time!r}: {exc}") from exc
                if period is not None and not period.start <= time <= period.end:
                    continue
                numbers = []
                for field, value in zip(selected[2:], raw_numbers):
                    try:
                        numbers.append(_number(value, missing_values))
                    except (ValueError, OverflowError) as exc:
                        raise StationReadError(f"{location} {field}（{mapping[field]}）: {value!r}: {exc}") from exc
                yield StationObservation(path, row_number, station_id, time, dict(zip(selected[2:], numbers)))
    except (CalamineError, OSError) as exc:
        raise StationReadError(f"无法读取站点文件 {path} [{sheet_name}]: {exc}") from exc
