from __future__ import annotations

import argparse
import sys
from pathlib import Path


try:
    from config_support import CURRENT_CONFIG_NAME, LEGACY_CONFIG_NAME, migrate_config_dir
except ModuleNotFoundError:
    # Support invoking this file directly from outside the plugin directory.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from config_support import CURRENT_CONFIG_NAME, LEGACY_CONFIG_NAME, migrate_config_dir


def migrate_config(config_dir: Path, user_default_path: Path | None = None) -> str:
    """Copy a legacy file and atomically migrate its supported key aliases."""
    return migrate_config_dir(config_dir, user_default_path)


def _default_config_dir() -> Path:
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path

        return Path(get_astrbot_data_path()) / "config"
    except Exception as exc:
        raise RuntimeError(
            "无法定位 AstrBot 配置目录，请使用 --config-dir 指定"
        ) from exc


def main() -> int:
    parser = argparse.ArgumentParser(description="迁移糯小喵插件旧配置文件")
    parser.add_argument(
        "--config-dir",
        type=Path,
        help="AstrBot data/config 目录；省略时自动定位",
    )
    parser.add_argument(
        "--user-default",
        type=Path,
        help="同时迁移个人默认快照；省略时不读取插件外的快照路径",
    )
    args = parser.parse_args()
    print(migrate_config(args.config_dir or _default_config_dir(), args.user_default))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
