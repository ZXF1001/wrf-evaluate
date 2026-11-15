"""为一次运行创建独立结果目录，并保存当时实际解析的原始配置。"""

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import Config, ConfigError


def create_run_directory(config: Config) -> Path:
    if config.config_bytes is None:
        raise ConfigError("输出结果前请通过 load_config 加载配置，以便保存本次配置副本")
    stamp = datetime.now(ZoneInfo(config.display_timezone)).strftime("%Y%m%d_%H%M%S_%f")
    path = config.output_dir / f"{config.output_name}_{stamp}"
    # 排他创建：即使两个进程碰巧重名，也不会覆盖已有结果。
    path.mkdir(parents=True, exist_ok=False)
    try:
        (path / "config.toml").write_bytes(config.config_bytes)
    except OSError:
        path.rmdir()
        raise
    return path
