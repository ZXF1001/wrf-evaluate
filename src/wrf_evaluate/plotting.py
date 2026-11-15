"""各来源在同一子图保留原生时间分辨率，不在绘图阶段配对。"""

from collections.abc import Sequence
import math
from pathlib import Path
from zoneinfo import ZoneInfo

from matplotlib import dates, font_manager, rc_context
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure

from .config import DEFAULT_DISPLAY_NAMES, PeriodConfig, PointConfig
from .series import Series


def plot_style() -> dict:
    """曲线图与空间图共用中文字体设置。"""
    available = {font.name for font in font_manager.fontManager.ttflist}
    preferred = ("Noto Sans CJK SC", "Source Han Sans SC", "Microsoft YaHei", "SimHei", "WenQuanYi Zen Hei")
    fonts = [name for name in preferred if name in available] + ["DejaVu Sans"]
    return {"font.family": fonts, "font.size": 10}


def plot_series(
    path: str | Path, point: PointConfig, series: Sequence[Series], timezone: str,
    *, period: PeriodConfig | None = None,
    element_name: str = DEFAULT_DISPLAY_NAMES["temperature_2m"],
) -> Path:
    """同一子图绘制各来源的原始采样点和连线，输出 PNG；None 处断线。

    输入须已统一单位。只转换显示时区，不插值、重采样或计算指标。
    使用无窗口画布，不依赖桌面环境；每次仅保留当前图。
    """
    if not series or not any(value is not None for curve in series for value in curve.values):
        raise ValueError("没有可绘制的数据")
    if len({curve.label for curve in series}) != len(series):
        raise ValueError("曲线名称不能重复，请给不同来源设置不同名称")
    if len({curve.unit for curve in series}) != 1:
        raise ValueError("同一张图须使用统一单位")
    zone = ZoneInfo(timezone)
    path = Path(path)
    if path.suffix.lower() != ".png":
        raise ValueError("输出路径须以 .png 结尾")
    with rc_context(plot_style()):
        figure = Figure(figsize=(11, 4.8), layout="constrained")
        FigureCanvasAgg(figure)
        ax = figure.subplots()
        for curve in series:
            times = [time.astimezone(zone) for time in curve.times]
            values = [math.nan if value is None else value for value in curve.values]
            ax.plot(times, values, marker=".", markersize=4, linewidth=1.3, label=curve.label)
        ax.set_title(f"{point.name} | {element_name}对比\n{point.longitude:g}° E, {point.latitude:g}° N")
        ax.set_ylabel(f"{element_name}（{series[0].unit}）")
        ax.set_xlabel(f"时间（{timezone}）")
        locator = dates.AutoDateLocator(tz=zone, minticks=4, maxticks=9)
        ax.xaxis.set_major_locator(locator)
        ax.xaxis.set_major_formatter(dates.ConciseDateFormatter(
            locator, tz=zone, offset_formats=["%Y-%m-%d"] * 6,
        ))
        if period is not None:
            ax.set_xlim(period.start.astimezone(zone), period.end.astimezone(zone))
        ax.grid(alpha=0.25)
        ax.legend(loc="best", frameon=False)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            figure.savefig(path, dpi=160, format="png")
        finally:
            figure.clear()
    return path
