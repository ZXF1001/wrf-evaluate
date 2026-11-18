"""命令行入口：运行时自动校验配置，输出进度与错误。"""

import argparse
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .catalog import iter_source_files, scan_source
from .config import Config, ConfigError, SourceConfig, load_config


def _show_stations(config: Config) -> None:
    print(f"配置站点: {len(config.points)} 个")
    for point in config.points:
        tolerance = point.time_tolerance
        station_id = f"，实测站号 {point.station_id}" if point.station_id is not None else ""
        print(f"  {point.name}: 经度 {point.longitude:g}，纬度 {point.latitude:g}{station_id}；"
              f"瞬时指标容差 ±{tolerance.total_seconds():g} 秒")
        if point.excel is not None:
            print(f"    Excel: {len(point.excel.files)} 个；时区 {point.excel.timezone}；工作表 {point.excel.sheet}")
            for path in point.excel.files:
                print(f"      {path}")


def _inspect_cldas(source: SourceConfig) -> None:
    from .cldas import read_cldas_header

    # 只累计产品摘要，不保留逐文件头或所有时次的坐标数组。
    summaries = {}
    total = 0
    for path in iter_source_files(source):
        header = read_cldas_header(path, source.timezone)
        key = (header.product, tuple(sorted(header.variables)), header.shape)
        count, start, end = summaries.get(key, (0, header.time, header.time))
        summaries[key] = count + 1, min(start, header.time), max(end, header.time)
        total += 1
    print(f"{source.name}: {total} 个 CLDAS 文件（文件名末尾为有效时间，范围内不保证连续）")
    for (product, variables, shape), (count, start, end) in sorted(summaries.items()):
        print(f"  {product} ({', '.join(variables)}): {count} 个文件，网格 (LAT, LON): {shape}")
        print(f"    {start.isoformat()} -> {end.isoformat()}")
    if not total:
        print("  未匹配文件，请检查 root 和 patterns。")


def _match_times(config: Config) -> None:
    from .matching import iter_time_matches

    summaries = {}
    tolerances = {p.time_tolerance
                  for p in config.points} or {timedelta(minutes=1)}
    log_timezone = ZoneInfo(config.display_timezone)

    def progress(message: str) -> None:
        now = datetime.now(log_timezone)
        print(f"[{now:%Y-%m-%d %H:%M:%S}] {message}", flush=True)

    # 用最大容差找一次最近候选，再按各站点容差汇总，避免重复扫描原始文件。
    for match in iter_time_matches(config.sources, max(tolerances),
                                   period=config.period, progress=progress):
        for tolerance in tolerances:
            key = (tolerance, match.forecast.source, match.cldas_source, match.family, match.product)
            paired, missing, max_offset = summaries.get(key, (0, 0, 0.0))
            if match.observation is None or abs(match.offset) > tolerance:
                missing += 1
            else:
                paired += 1
                max_offset = max(max_offset, abs(match.offset.total_seconds()))
            summaries[key] = paired, missing, max_offset
    print("最近时次候选按站点容差分别汇总（含边界）；CLDAS 取最新制作版本。")
    print("WRF 每个有效时刻选满足 lead_hours 范围的最新起报；这里只查找时间候选，同一实况仍可重复对应。")
    print("仅核验时间候选，不读取气象场；未检查降水时段或执行插值、指标计算。")
    for (tolerance, wrf, cldas, family, product), (paired, missing, max_offset) in sorted(summaries.items()):
        offset = f"{max_offset:g} 秒" if paired else "无配对"
        print(f"  容差 ±{tolerance.total_seconds():g} 秒，{wrf} -> {cldas} {family}/{product}: "
              f"配对 {paired}，未配对 {missing}，最大绝对时差 {offset}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="WRF 检验工具")
    parser.add_argument("--config", default="config.toml", help="配置文件路径，默认 config.toml")
    commands = parser.add_subparsers(dest="command", required=True)
    scan = commands.add_parser("scan", help="统计原始文件数量、大小及少量示例")
    scan.add_argument("--source", help="仅扫描指定来源，如 wrf 或 cldas")
    commands.add_parser("inspect", help="读取 WRF/CLDAS 元数据并显示配置点位")
    commands.add_parser("plot", help="按配置要素输出对比 PNG 和指标 CSV")
    commands.add_parser("match", help="按配置的时间容差核验 WRF—CLDAS 配对覆盖，不计算指标")
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config)
        enabled = [source for source in config.sources if source.enabled]
        if args.command == "plot":
            log_timezone = ZoneInfo(config.display_timezone)

            def progress(message: str) -> None:
                now = datetime.now(log_timezone)
                print(f"[{now:%Y-%m-%d %H:%M:%S}] {message}", flush=True)

            progress("加载检验与绘图模块…")
            from .workflow import plot_comparisons

            for result in plot_comparisons(config, progress=progress):
                for path in result.paths:
                    progress(f"已生成 {result.point.name}: {path}")
                if result.metrics_path is not None:
                    progress(f"指标: {result.metrics_path}")
            progress("本次检验完成。")
            return 0

        if args.command == "match":
            _match_times(config)
            return 0

        if args.command == "inspect":
            from .wrf import read_wrf_header

            wrf_sources = [source for source in enabled if source.kind == "wrf"]
            cldas_sources = [source for source in enabled if source.kind == "cldas"]
            if not wrf_sources and not cldas_sources and not config.points:
                raise ConfigError("没有启用 WRF/CLDAS 数据源或配置点位")
            for source in wrf_sources:
                count = 0
                start = end = None
                grids = set()
                common_variables = None
                for path in iter_source_files(source):
                    header = read_wrf_header(path, source.timezone)
                    count += 1
                    start = min(start, header.time_start) if start else header.time_start
                    end = max(end, header.time_end) if end else header.time_end
                    dimensions = dict(header.dimensions)
                    grids.add((dimensions.get("south_north"), dimensions.get("west_east")))
                    common_variables = header.variables if common_variables is None else common_variables & header.variables
                print(f"{source.name}: {count} 个 WRF 文件")
                if count:
                    print(f"  文件内有效时间: {start.isoformat()} -> {end.isoformat()}（范围内不保证连续）")
                    print(f"  网格 (south_north, west_east): {sorted(grids, key=str)}")
                    names = ("T2", "Q2", "PSFC", "U10", "V10", "RAINC", "RAINNC", "XLAT", "XLONG")
                    print("  全部文件均包含: " + ", ".join(name for name in names if name in common_variables))
                else:
                    print("  未匹配文件，请检查 root 和 patterns。")
            for source in cldas_sources:
                _inspect_cldas(source)
            _show_stations(config)
            return 0

        if args.source:
            enabled = [source for source in enabled if source.name == args.source]
            if not enabled:
                raise ConfigError(f"未找到已启用的数据源: {args.source}")
        print("仅统计文件，不读取有效时间；未执行时间筛选、配对或检验。")
        for source in enabled:
            try:
                result = scan_source(source)
            except OSError as exc:
                raise ConfigError(f"sources.{source.name}: 扫描失败: {exc}") from exc
            print(f"{result.name}: {result.file_count:,} 个文件，{result.total_bytes / 1024**3:.3f} GiB")
            for example in result.examples:
                print(f"  {example}")
            if not result.file_count:
                if source.kind in {"station"}:
                    print("  提示: files 为空，尚未选入实测 Excel；请在配置中明确填写文件路径。")
                else:
                    print("  提示: 没有匹配文件，请检查 root 和 patterns。")
        return 0
    except (OSError, ValueError) as exc:
        print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] 错误: {exc}；若已生成部分文件，请以此错误为准，本次未完成。", file=sys.stderr, flush=True)
        return 2
