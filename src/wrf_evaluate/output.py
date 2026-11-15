"""为一次运行创建独立结果目录，并保存当时实际解析的原始配置。"""

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import Config, ConfigError


def create_run_directory(config: Config) -> Path:
    if config.config_bytes is None:
        raise ConfigError("输出结果前请通过 load_config 加载配置，以便保存本次配置副本")
    stamp = datetime.now(ZoneInfo(config.display_timezone)).strftime("%Y%m%d_%H%M%S_%f")
    # 排他创建并在时钟精度碰撞时加序号，避免快速连续运行失败或覆盖。
    sequence = 0
    while True:
        suffix = f"_{sequence:03d}" if sequence else ""
        path = config.output_dir / f"{config.output_name}_{stamp}{suffix}"
        try:
            path.mkdir(parents=True, exist_ok=False)
            break
        except FileExistsError:
            if not path.exists():
                raise  # 父路径被文件占用等错误不是运行名称碰撞。
            sequence += 1
    try:
        (path / "config.toml").write_bytes(config.config_bytes)
    except OSError:
        (path / "config.toml").unlink(missing_ok=True)
        path.rmdir()
        raise
    return path
