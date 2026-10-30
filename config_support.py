"""Configuration parsing and migration shared by the dashboard and runtime.

Storage values stay intact on native AstrBot loads.  The runtime view only
replaces malformed values; dashboard saves use the stricter schema limits.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Any


CURRENT_CONFIG_NAME = "astrbot_plugin_nuoxiaomiao_config.json"
LEGACY_CONFIG_NAME = "astrbot_plugin_nuoxiaomiao_guard_config.json"
CONFIG_ALIASES = {
    "merge_enabled": "debounce_enabled",
    "poke_reply_debounce_seconds": "poke_reply_cooldown_seconds",
}
QUIET_TIME_FIELDS = ("proactive_quiet_hours_start", "proactive_quiet_hours_end")
WAKE_FLOOR = "proactive_silent_wake_floor_minutes"
WAKE_CEIL = "proactive_silent_wake_ceil_minutes"
WAKE_MEAN = "proactive_silent_wake_target_mean_minutes"
TRUE_VALUES = {"1", "true", "yes", "on", "启用", "是"}
FALSE_VALUES = {"0", "false", "no", "off", "禁用", "否"}


def fold_config_aliases(config: dict[str, Any]) -> dict[str, Any]:
    """Explicit current keys take precedence over their two supported aliases."""
    result = copy.deepcopy(dict(config))
    for old, new in CONFIG_ALIASES.items():
        if old in result:
            if new not in result:
                result[new] = result[old]
            del result[old]
    return result


def _parse_value(value: Any, type_name: str) -> Any:
    if type_name == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            text = value.strip().lower()
            if text in TRUE_VALUES:
                return True
            if text in FALSE_VALUES:
                return False
        if isinstance(value, (int, float)) and value in (0, 1):
            return bool(value)
        raise ValueError("请填写启用或关闭")
    if type_name in {"int", "float"}:
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise ValueError("请填写有限数值")
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            raise ValueError("请填写有限数值") from None
        if not math.isfinite(number):
            raise ValueError("数值必须有限，不能使用 NaN 或无穷大")
        if type_name == "int":
            if not number.is_integer():
                raise ValueError("请填写整数")
            return int(value) if not isinstance(value, str) else int(number)
        return number
    if type_name in {"string", "text"}:
        if not isinstance(value, str):
            raise ValueError("请填写文本")
        return value  # Custom prose, whitespace and line breaks are preserved.
    if type_name == "list":
        if value is None:
            return []
        if isinstance(value, str):
            items = value.replace("，", "\n").replace(",", "\n").splitlines()
        elif isinstance(value, (list, tuple, set)):
            items = value
        else:
            raise ValueError("请填写列表，或每行填写一项")
        result: list[str] = []
        for item in items:
            if not isinstance(item, (str, int)) or isinstance(item, bool):
                raise ValueError("列表中的每项必须是文本或 ID")
            text = str(item).strip()
            if text and text not in result:
                result.append(text)
        return result
    return copy.deepcopy(value)


def normalize_config(
    config: dict[str, Any],
    schema: dict[str, Any],
    *,
    runtime: bool = False,
    include_defaults: bool = False,
) -> tuple[dict[str, Any], dict[str, str]]:
    """Return a parsed full mapping and field errors, without mutating inputs.

    Runtime parsing keeps finite historical numbers outside dashboard slider
    limits.  Existing runtime lower/upper constraints still apply in main.py.
    Invalid types and nonfinite numbers use field defaults in that view only.
    """
    result = fold_config_aliases(config)
    if runtime or include_defaults:
        for key, spec in schema.items():
            if isinstance(spec, dict) and key not in result:
                result[key] = copy.deepcopy(spec.get("default"))
    errors: dict[str, str] = {}
    for key, spec in schema.items():
        if not isinstance(spec, dict) or key not in result:
            continue
        type_name = str(spec.get("type", "string"))
        try:
            value = _parse_value(result[key], type_name)
        except (TypeError, ValueError, OverflowError) as exc:
            errors[key] = str(exc)
            if runtime:
                try:
                    result[key] = _parse_value(spec.get("default"), type_name)
                except (TypeError, ValueError, OverflowError):
                    result[key] = copy.deepcopy(spec.get("default"))
            continue
        result[key] = value
        slider = spec.get("slider")
        if type_name in {"int", "float"} and isinstance(slider, dict):
            lower, upper = slider.get("min"), slider.get("max")
            if lower is not None and value < lower:
                errors[key] = f"不得小于 {lower}"
            elif upper is not None and value > upper:
                errors[key] = f"不得大于 {upper}"
        if key in QUIET_TIME_FIELDS:
            text = value.strip()
            if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", text):
                errors[key] = "请使用合法的 HH:MM 时间，例如 08:00（00:00 至 23:59）"
                if runtime:
                    result[key] = spec.get("default", "00:00")
            else:
                result[key] = text

    wake_keys = (WAKE_FLOOR, WAKE_CEIL, WAKE_MEAN)
    if all(key in result for key in wake_keys):
        values = [result[key] for key in wake_keys]
        if all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in values):
            floor, ceil, mean = values
            if ceil < floor + 2:
                errors[WAKE_CEIL] = "安静上限必须至少比下限多 2 分钟"
                errors.setdefault(WAKE_FLOOR, "安静下限必须至少比上限少 2 分钟")
            if not floor + 1 <= mean <= ceil - 1:
                errors[WAKE_MEAN] = "目标均值必须位于下限 + 1 与上限 − 1 分钟之间"
            # Match the existing runtime's relational constraints, not UI maxima.
            if runtime:
                floor = max(1, floor)
                ceil = max(floor + 2, ceil)
                result[WAKE_FLOOR] = floor
                result[WAKE_CEIL] = ceil
                result[WAKE_MEAN] = min(max(floor + 1, mean), ceil - 1)
    return result, errors


def config_diagnostics(
    config: dict[str, Any], schema: dict[str, Any], effective: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    """Explain bad stored settings without rejecting native plugin startup."""
    _, errors = normalize_config(config, schema, include_defaults=True)
    view = effective if effective is not None else normalize_config(config, schema, runtime=True)[0]
    diagnostics = [
        {"code": "invalid_config", "message": f"{schema.get(key, {}).get('description', key)}：{message}",
         "severity": "warning", "fields": [key]}
        for key, message in errors.items()
    ]
    for key in schema:
        if key in config and key in view and config[key] != view[key]:
            diagnostics.append({
                "code": "effective_value_differs", "severity": "warning", "fields": [key],
                "message": f"{schema[key].get('description', key)}的保存值与当前生效值不同；页面保留保存值，修改后可重新保存。",
            })
    return diagnostics


def json_safe_config(value: Any) -> Any:
    """Keep malformed native numbers editable without emitting invalid JSON."""
    if isinstance(value, float) and not math.isfinite(value):
        return "NaN" if math.isnan(value) else ("Infinity" if value > 0 else "-Infinity")
    if isinstance(value, dict):
        return {key: json_safe_config(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe_config(item) for item in value]
    return value


def atomic_write_bytes(path: Path, payload: bytes) -> None:
    """Replace a file only after its complete new contents have been flushed."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.tmp-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def atomic_write_json(path: Path, config: dict[str, Any]) -> None:
    payload = (json.dumps(config, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    atomic_write_bytes(path, payload)


def migrate_alias_file(path: Path) -> bool:
    """Back up original bytes and fold aliases once; current keys always win."""
    path = Path(path)
    if not path.is_file():
        return False
    original = path.read_bytes()
    config = json.loads(original.decode("utf-8-sig"))
    if not isinstance(config, dict):
        raise ValueError(f"配置文件必须是对象：{path.name}")
    if not any(key in config for key in CONFIG_ALIASES):
        return False
    migrated = fold_config_aliases(config)
    digest = hashlib.sha256(original).hexdigest()[:12]
    backup = path.with_name(f"{path.name}.bak-alias-{digest}")
    if not backup.exists():
        atomic_write_bytes(backup, original)
    atomic_write_json(path, migrated)
    return True


def migrate_config_dir(config_dir: Path, user_default_path: Path | None = None) -> str:
    """Migrate before AstrBot adds schema defaults that could shadow aliases."""
    config_dir = Path(config_dir)
    current, legacy = config_dir / CURRENT_CONFIG_NAME, config_dir / LEGACY_CONFIG_NAME
    copied = False
    if not current.exists() and legacy.is_file():
        atomic_write_bytes(current, legacy.read_bytes())
        copied = True
    changed = migrate_alias_file(current)
    if user_default_path is not None:
        changed = migrate_alias_file(user_default_path) or changed
    if copied:
        return f"已迁移配置: {legacy} -> {current}"
    if changed:
        return f"已迁移旧配置键并备份原文件: {current}"
    if current.exists():
        return f"保留现有配置，未覆盖: {current}"
    return f"无需迁移，未找到旧配置: {legacy}"


def preload_config_migration(module_name: str, plugin_dir: Path, config_dir: Path) -> str | None:
    """Only the real AstrBot plugin import is allowed to touch saved config."""
    if module_name != "data.plugins.astrbot_plugin_nuoxiaomiao.main":
        return None
    current = Path(config_dir) / CURRENT_CONFIG_NAME
    user_default = Path(plugin_dir) / "data" / "user_default_config.json"
    source = current if current.is_file() else Path(config_dir) / LEGACY_CONFIG_NAME
    if source.is_file():
        config = json.loads(source.read_text(encoding="utf-8-sig"))
        override = config.get("user_default_config_path") if isinstance(config, dict) else None
        if isinstance(override, str) and override.strip():
            user_default = Path(override.strip())
    return migrate_config_dir(config_dir, user_default)
