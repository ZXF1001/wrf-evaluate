"""只读扫描原始文件；不解压、不解析观测时间、不修改数据。"""

from bisect import insort
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from fnmatch import fnmatchcase
import os
from pathlib import Path

from .config import SourceConfig


@dataclass(frozen=True)
class ScanResult:
    name: str
    file_count: int
    total_bytes: int
    examples: tuple[str, ...]


def _matches(path_parts: tuple[str, ...], pattern_parts: tuple[str, ...]) -> bool:
    """文件名匹配任意层级；带目录的规则锚定来源根目录。"""
    if len(pattern_parts) == 1:
        return fnmatchcase(path_parts[-1], pattern_parts[0])
    return len(path_parts) == len(pattern_parts) and all(
        fnmatchcase(path_part, pattern_part)
        for path_part, pattern_part in zip(path_parts, pattern_parts)
    )


def scan_source(source: SourceConfig, example_limit: int = 3) -> ScanResult:
    """单次遍历，只保存待扫描目录、统计与少量示例。

    *.nc 匹配任意层级文件名；含 / 的模式从数据源根目录逐段匹配，
    如 2024/12/*.nc。模式扫描跳过符号链接；站点来源只统计配置的文件。
    """
    if example_limit < 0:
        raise ValueError("example_limit 不能为负数")
    if not source.enabled:
        return ScanResult(source.name, 0, 0, ())

    count = total_bytes = 0
    examples: list[str] = []
    for path in iter_source_files(source):
        relative = path.relative_to(source.root).as_posix()
        count += 1
        total_bytes += path.stat().st_size
        if example_limit and (len(examples) < example_limit or relative < examples[-1]):
            insort(examples, relative)
            if len(examples) > example_limit:
                examples.pop()
    return ScanResult(source.name, count, total_bytes, tuple(examples))


def iter_source_files(
    source: SourceConfig, *, include_directory: Callable[[Path], bool] | None = None,
) -> Iterator[Path]:
    """站点直接返回配置文件列表；网格来源流式扫描，不保存全量清单。"""
    if not source.enabled:
        return
    if source.kind in {"regional", "national"}:
        yield from source.files
        return
    patterns = [tuple(pattern.split("/")) for pattern in source.patterns]
    pending = [source.root]
    while pending:
        directory = pending.pop()
        if include_directory is not None and not include_directory(directory):
            continue
        with os.scandir(directory) as entries:
            for entry in entries:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    path = Path(entry.path)
                    parts = path.relative_to(source.root).parts
                    # 带目录的规则只进入可能匹配的前缀，避免遍历国家站等无关目录。
                    if any(
                        len(pattern) == 1 or (
                            len(parts) < len(pattern)
                            and all(fnmatchcase(part, rule) for part, rule in zip(parts, pattern))
                        )
                        for pattern in patterns
                    ):
                        pending.append(path)
                    continue
                if entry.is_file(follow_symlinks=False):
                    path = Path(entry.path)
                    if any(_matches(path.relative_to(source.root).parts, pattern) for pattern in patterns):
                        yield path
