"""Configuration parsing, typed runtime view, diagnostics, and migration."""

from __future__ import annotations

import copy
from dataclasses import dataclass
import hashlib
import json
import math
import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Any, Callable

if __package__:
    from .infra.constants import (
        DEFAULT_ACTIVITY_PREFETCH_KEYWORDS,
        DEFAULT_CUSTOM_TOOL_CALL_PROMPT,
        DEFAULT_LMEM_GATEWAY_KEYWORDS,
        LEGACY_CUSTOM_TOOL_CALL_PROMPT,
        LOCALIZED_IMAGE_DIR_NAME,
    )
else:
    from infra.constants import (
        DEFAULT_ACTIVITY_PREFETCH_KEYWORDS,
        DEFAULT_CUSTOM_TOOL_CALL_PROMPT,
        DEFAULT_LMEM_GATEWAY_KEYWORDS,
        LEGACY_CUSTOM_TOOL_CALL_PROMPT,
        LOCALIZED_IMAGE_DIR_NAME,
    )

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
DEFAULT_POKE_REPLY_PROMPT = (
    "{username} 戳了你一下, 请你用一句话回复一下, 确保符合人设的同时夹带生气甚至愤怒的情绪, 考虑上下文, 确保通顺不突兀"
)
_LIST_SPLIT_RE = re.compile(r"[\n,，、;；]+")
BLANK_BUILTIN_KEYS = frozenset({
    "poke_reply_prompt",
    "tool_call_prompt",
    "lmem_gateway_keywords",
    "activity_prefetch_keywords",
})


def load_schema(plugin_dir: Path | None = None) -> dict[str, Any]:
    base = plugin_dir if plugin_dir is not None else Path(__file__).resolve().parent
    try:
        data = json.loads((base / "_conf_schema.json").read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def schema_defaults(schema: dict[str, Any] | None = None) -> dict[str, Any]:
    src = schema if schema is not None else load_schema()
    return {
        key: copy.deepcopy(spec.get("default"))
        for key, spec in src.items()
        if isinstance(spec, dict)
    }


def split_list_text(raw: str) -> list[str]:
    return [part.strip() for part in _LIST_SPLIT_RE.split(raw) if part.strip()]


def fold_config_aliases(config: dict[str, Any]) -> dict[str, Any]:
    """Explicit current keys take precedence over their two supported aliases."""
    result = copy.deepcopy(dict(config))
    for old, new in CONFIG_ALIASES.items():
        if old in result:
            if new not in result:
                result[new] = result[old]
            del result[old]
    return result


def parse_quiet_time_minutes(value: Any, fallback: int = 0) -> int:
    text = str(value or "").strip()
    if not text:
        return fallback
    if ":" in text:
        try:
            hour_str, minute_str = text.split(":", 1)
            hour = max(0, min(24, int(hour_str)))
            minute = max(0, min(59, int(minute_str)))
            return min(24 * 60, hour * 60 + minute)
        except (TypeError, ValueError):
            return fallback
    try:
        return max(0, min(24 * 60, int(float(text))))
    except (TypeError, ValueError):
        return fallback


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
        return value
    if type_name == "list":
        if value is None:
            return []
        if isinstance(value, str):
            items: Any = split_list_text(value)
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
    """Return a parsed full mapping and field errors, without mutating inputs."""
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
            if runtime:
                floor = max(1, floor)
                ceil = max(floor + 2, ceil)
                result[WAKE_FLOOR] = floor
                result[WAKE_CEIL] = ceil
                result[WAKE_MEAN] = min(max(floor + 1, mean), ceil - 1)
    return result, errors


@dataclass
class PluginConfig:
    """Typed runtime configuration view derived from normalized schema config."""

    raw_runtime: dict[str, Any]
    errors: dict[str, str]
    enabled: bool
    include_group: bool
    include_private: bool
    blocked_users: set[str]
    user_whitelist: set[str]
    user_blacklist: set[str]
    guard_group_whitelist: set[str]
    guard_group_blacklist: set[str]
    poke_group_whitelist: set[str]
    poke_group_blacklist: set[str]
    poke_reply_enabled: bool
    poke_reply_prompt: str
    poke_reply_cooldown_seconds: float
    trigger_rounds: int
    window_seconds: int
    meme_tags: list[str]
    meme_cooldown_seconds: int
    merge_idle_seconds: int
    merge_max_total_seconds: int
    merge_join_separator: str
    merge_localize_images: bool
    merge_localize_images_dir: Path
    force_wake_prefix: str
    qq_official_enabled: bool
    qq_official_content_mode: bool
    qq_official_group_proactive: bool
    qq_official_strip_at_markup: bool
    qq_official_admin_openids: set[str]
    qq_official_quote_trigger: bool
    qq_official_text_chars: int
    qq_official_flatten_forward: bool
    keyword_wake_enabled: bool
    keyword_wake_list: set[str]
    keyword_wake_group_whitelist: set[str]
    keyword_wake_group_blacklist: set[str]
    debounce_enabled: bool
    debounce_dynamic_enabled: bool
    debounce_group_enabled: bool
    debounce_private_enabled: bool
    debounce_user_whitelist: set[str]
    debounce_group_whitelist: set[str]
    debounce_group_blacklist: set[str]
    debounce_fast_idle_seconds: float
    debounce_slow_idle_seconds: float
    debounce_wake_idle_seconds: float
    debounce_typing_enabled: bool
    debounce_typing_hold_seconds: float
    debounce_long_chars: int
    debounce_end_punctuation: str
    debounce_max_parts: int
    tool_call_prompt_enabled: bool
    tool_call_prompt: str
    activity_buffer_enabled: bool
    activity_record_private: bool
    activity_group_whitelist: set[str]
    activity_group_blacklist: set[str]
    activity_record_bot_replies: bool
    activity_buffer_max_text_chars: int
    activity_retention_days: int
    activity_require_admin: bool
    activity_tool_enabled: bool
    activity_hint_enabled: bool
    activity_db_path: str
    activity_prefetch_enabled: bool
    activity_prefetch_keywords: list[str]
    proactive_enabled: bool
    proactive_hint_enabled: bool
    proactive_allow_groups: set[str]
    proactive_target_whitelist: set[str]
    proactive_max_pending_per_person: int
    proactive_daily_limit_per_person: int
    proactive_cooldown_seconds: int
    proactive_context_other_sessions: bool
    proactive_context_other_private: bool
    proactive_silent_wake_enabled: bool
    proactive_judge_provider_id: str
    proactive_silent_wake_floor_minutes: float
    proactive_silent_wake_ceil_minutes: float
    proactive_silent_wake_target_mean_minutes: float
    proactive_silent_wake_daily_budget: int
    proactive_silent_wake_max_steps: int
    proactive_silent_wake_allow_tools: bool
    proactive_quiet_start_minutes: int
    proactive_quiet_end_minutes: int
    capability_hint_text: str
    capability_context_digest: bool
    lmem_gateway_enabled: bool
    lmem_gateway_keywords: set[str]
    lmem_gateway_top_k: int
    lmem_gateway_timeout_seconds: float
    lmem_gateway_max_chars: int
    lmem_gateway_plugin_name: str
    lmem_gateway_recall_tool_name: str
    shared_memory_enabled: bool
    master_umo: str
    master_auto_bind: bool
    unread_inbox_enabled: bool
    inbox_group_whitelist: set[str]
    inbox_group_blacklist: set[str]
    mention_notice_enabled: bool
    mention_notice_limit: int
    topic_summary_enabled: bool
    topic_light_provider_id: str
    topic_min_unread: int
    topic_cooldown_seconds: int
    topic_max_chars: int
    topic_line_max_chars: int
    topic_timeout_seconds: float
    topic_segment_msg_count: int
    topic_segment_backfill_max: int
    vision_enabled: bool
    vision_provider_ids: list[str]
    image_keep_days: int
    vision_image_max: int
    vision_brief_max_chars: int
    vision_read_max_chars: int
    vision_timeout_seconds: float
    vision_cache_ttl_days: int
    resident_inbox_enabled: bool
    resident_state_enabled: bool
    resident_extra_tools: set[str]
    resident_run_token_limit: int
    resident_daily_token_limit: int


def _clamp_int(val: Any, default: int, low: int | None = None, high: int | None = None) -> int:
    try:
        n = int(val)
    except (TypeError, ValueError):
        n = default
    if low is not None:
        n = max(low, n)
    if high is not None:
        n = min(high, n)
    return n


def _clamp_float(val: Any, default: float, low: float | None = None, high: float | None = None) -> float:
    try:
        n = float(val)
        if not math.isfinite(n):
            n = default
    except (TypeError, ValueError):
        n = default
    if low is not None:
        n = max(low, n)
    if high is not None:
        n = min(high, n)
    return n


def _str_list(val: Any) -> list[str]:
    if isinstance(val, list):
        return [str(x).strip() for x in val if str(x).strip()]
    if isinstance(val, str):
        return split_list_text(val)
    return []


def _tags(val: Any) -> list[str]:
    items = _str_list(val)
    result: list[str] = []
    for item in items:
        low = item.lower()
        if low not in result:
            result.append(low)
    return result or ["angry", "baka"]


def build_plugin_config(
    raw_config: dict[str, Any],
    schema: dict[str, Any],
    data_dir_resolver: Callable[[], Path],
) -> PluginConfig:
    rt, errors = normalize_config(raw_config, schema, runtime=True)

    raw_tool_prompt = str(rt.get("tool_call_prompt", "") or "").strip()
    if not raw_tool_prompt or raw_tool_prompt == LEGACY_CUSTOM_TOOL_CALL_PROMPT:
        raw_tool_prompt = DEFAULT_CUSTOM_TOOL_CALL_PROMPT

    raw_img_dir = str(rt.get("merge_localize_images_dir", "") or "").strip()
    if raw_img_dir:
        img_dir = Path(raw_img_dir).expanduser()
    else:
        img_dir = data_dir_resolver() / LOCALIZED_IMAGE_DIR_NAME
    try:
        img_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

    wake_floor = _clamp_float(rt.get(WAKE_FLOOR, 5), 5.0, 1.0)
    wake_ceil = _clamp_float(rt.get(WAKE_CEIL, 60), 60.0, wake_floor + 2.0)
    wake_mean = _clamp_float(rt.get(WAKE_MEAN, 30), 30.0, wake_floor + 1.0, wake_ceil - 1.0)

    raw_lmem = _str_list(rt.get("lmem_gateway_keywords", []))
    raw_prefetch = _str_list(rt.get("activity_prefetch_keywords", []))

    return PluginConfig(
        raw_runtime=rt,
        errors=errors,
        enabled=bool(rt.get("enabled", True)),
        include_group=bool(rt.get("include_group", True)),
        include_private=bool(rt.get("include_private", True)),
        blocked_users=set(_str_list(rt.get("blocked_users", []))),
        user_whitelist=set(_str_list(rt.get("user_whitelist", []))),
        user_blacklist=set(_str_list(rt.get("user_blacklist", []))),
        guard_group_whitelist=set(_str_list(rt.get("guard_group_whitelist", []))),
        guard_group_blacklist=set(_str_list(rt.get("guard_group_blacklist", []))),
        poke_group_whitelist=set(_str_list(rt.get("poke_group_whitelist", []))),
        poke_group_blacklist=set(_str_list(rt.get("poke_group_blacklist", []))),
        poke_reply_enabled=bool(rt.get("poke_reply_enabled", True)),
        poke_reply_prompt=str(rt.get("poke_reply_prompt", "") or DEFAULT_POKE_REPLY_PROMPT),
        poke_reply_cooldown_seconds=_clamp_float(rt.get("poke_reply_cooldown_seconds", 0.5), 0.5, 0.1, 3.0),
        trigger_rounds=_clamp_int(rt.get("trigger_rounds", 3), 3, 2),
        window_seconds=_clamp_int(rt.get("window_seconds", 180), 180, 1),
        meme_tags=_tags(rt.get("meme_tags", ["angry", "baka"])),
        meme_cooldown_seconds=_clamp_int(rt.get("meme_cooldown_seconds", 60), 60, 0),
        merge_idle_seconds=_clamp_int(rt.get("merge_idle_seconds", 4), 4, 0),
        merge_max_total_seconds=_clamp_int(rt.get("merge_max_total_seconds", 12), 12, 1),
        merge_join_separator=str(rt.get("merge_join_separator", "\n") or "\n"),
        merge_localize_images=bool(rt.get("merge_localize_images", True)),
        merge_localize_images_dir=img_dir,
        force_wake_prefix=str(rt.get("force_wake_prefix", "") or "").strip(),
        qq_official_enabled=bool(rt.get("qq_official_enabled", True)),
        qq_official_content_mode=bool(rt.get("qq_official_content_mode", True)),
        qq_official_group_proactive=bool(rt.get("qq_official_group_proactive", False)),
        qq_official_strip_at_markup=bool(rt.get("qq_official_strip_at_markup", True)),
        qq_official_admin_openids=set(_str_list(rt.get("qq_official_admin_openids", []))),
        qq_official_quote_trigger=bool(rt.get("qq_official_quote_trigger", True)),
        qq_official_text_chars=_clamp_int(rt.get("qq_official_text_chars", 2000), 2000, 0),
        qq_official_flatten_forward=bool(rt.get("qq_official_flatten_forward", True)),
        keyword_wake_enabled=bool(rt.get("keyword_wake_enabled", False)),
        keyword_wake_list=set(_str_list(rt.get("keyword_wake_list", []))),
        keyword_wake_group_whitelist=set(_str_list(rt.get("keyword_wake_group_whitelist", []))),
        keyword_wake_group_blacklist=set(_str_list(rt.get("keyword_wake_group_blacklist", []))),
        debounce_enabled=bool(rt.get("debounce_enabled", True)),
        debounce_dynamic_enabled=bool(rt.get("debounce_dynamic_enabled", True)),
        debounce_group_enabled=bool(rt.get("debounce_group_enabled", True)),
        debounce_private_enabled=bool(rt.get("debounce_private_enabled", True)),
        debounce_user_whitelist=set(_str_list(rt.get("debounce_user_whitelist", []))),
        debounce_group_whitelist=set(_str_list(rt.get("debounce_group_whitelist", []))),
        debounce_group_blacklist=set(_str_list(rt.get("debounce_group_blacklist", []))),
        debounce_fast_idle_seconds=_clamp_float(rt.get("debounce_fast_idle_seconds", 0.0), 0.0, 0.0),
        debounce_slow_idle_seconds=_clamp_float(rt.get("debounce_slow_idle_seconds", 1.2), 1.2, 0.1),
        debounce_wake_idle_seconds=_clamp_float(rt.get("debounce_wake_idle_seconds", 4.0), 4.0, 0.0),
        debounce_typing_enabled=bool(rt.get("debounce_typing_enabled", True)),
        debounce_typing_hold_seconds=_clamp_float(rt.get("debounce_typing_hold_seconds", 5.0), 5.0, 0.5),
        debounce_long_chars=_clamp_int(rt.get("debounce_long_chars", 12), 12, 1),
        debounce_end_punctuation=str(rt.get("debounce_end_punctuation", "") or "。！？!?…~"),
        debounce_max_parts=_clamp_int(rt.get("debounce_max_parts", 8), 8, 1),
        tool_call_prompt_enabled=bool(rt.get("tool_call_prompt_enabled", True)),
        tool_call_prompt=raw_tool_prompt,
        activity_buffer_enabled=bool(rt.get("activity_buffer_enabled", True)),
        activity_record_private=bool(rt.get("activity_record_private", True)),
        activity_group_whitelist=set(_str_list(rt.get("activity_group_whitelist", []))),
        activity_group_blacklist=set(_str_list(rt.get("activity_group_blacklist", []))),
        activity_record_bot_replies=bool(rt.get("activity_record_bot_replies", True)),
        activity_buffer_max_text_chars=_clamp_int(rt.get("activity_buffer_max_text_chars", 200), 200, 10),
        activity_retention_days=_clamp_int(rt.get("activity_retention_days", 7), 7, 1, 365),
        activity_require_admin=bool(rt.get("activity_require_admin", True)),
        activity_tool_enabled=bool(rt.get("activity_tool_enabled", True)),
        activity_hint_enabled=bool(rt.get("activity_hint_enabled", True)),
        activity_db_path=str(rt.get("activity_db_path", "") or "").strip(),
        activity_prefetch_enabled=bool(rt.get("activity_prefetch_enabled", True)),
        activity_prefetch_keywords=raw_prefetch or list(DEFAULT_ACTIVITY_PREFETCH_KEYWORDS),
        proactive_enabled=bool(rt.get("proactive_enabled", True)),
        proactive_hint_enabled=bool(rt.get("proactive_hint_enabled", True)),
        proactive_allow_groups=set(_str_list(rt.get("proactive_allow_groups", []))),
        proactive_target_whitelist=set(_str_list(rt.get("proactive_target_whitelist", []))),
        proactive_max_pending_per_person=_clamp_int(rt.get("proactive_max_pending_per_person", 3), 3, 1),
        proactive_daily_limit_per_person=_clamp_int(rt.get("proactive_daily_limit_per_person", 10), 10, 1),
        proactive_cooldown_seconds=_clamp_int(rt.get("proactive_cooldown_minutes", 10), 10, 0) * 60,
        proactive_context_other_sessions=bool(rt.get("proactive_context_other_sessions", True)),
        proactive_context_other_private=bool(rt.get("proactive_context_other_private", False)),
        proactive_silent_wake_enabled=bool(rt.get("proactive_silent_wake_enabled", True)),
        proactive_judge_provider_id=str(rt.get("proactive_judge_provider_id", "") or "").strip(),
        proactive_silent_wake_floor_minutes=wake_floor,
        proactive_silent_wake_ceil_minutes=wake_ceil,
        proactive_silent_wake_target_mean_minutes=wake_mean,
        proactive_silent_wake_daily_budget=_clamp_int(rt.get("proactive_silent_wake_daily_budget", 15), 15, 0),
        proactive_silent_wake_max_steps=_clamp_int(rt.get("proactive_silent_wake_max_steps", 8), 8, 1, 30),
        proactive_silent_wake_allow_tools=bool(rt.get("proactive_silent_wake_allow_tools", True)),
        proactive_quiet_start_minutes=parse_quiet_time_minutes(rt.get("proactive_quiet_hours_start", "00:00"), 0),
        proactive_quiet_end_minutes=parse_quiet_time_minutes(rt.get("proactive_quiet_hours_end", "08:00"), 8 * 60),
        capability_hint_text=str(rt.get("capability_hint_text", "") or "").strip(),
        capability_context_digest=bool(rt.get("capability_context_digest", False)),
        lmem_gateway_enabled=bool(rt.get("lmem_gateway_enabled", True)),
        lmem_gateway_keywords=set(raw_lmem) if raw_lmem else set(DEFAULT_LMEM_GATEWAY_KEYWORDS),
        lmem_gateway_top_k=_clamp_int(rt.get("lmem_gateway_top_k", 4), 4, 1, 20),
        lmem_gateway_timeout_seconds=_clamp_float(rt.get("lmem_gateway_timeout_seconds", 2.5), 2.5, 0.3),
        lmem_gateway_max_chars=_clamp_int(rt.get("lmem_gateway_max_chars", 600), 600, 50),
        lmem_gateway_plugin_name=str(rt.get("lmem_gateway_plugin_name", "") or "LivingMemory").strip(),
        lmem_gateway_recall_tool_name=str(rt.get("lmem_gateway_recall_tool_name", "") or "recall_long_term_memory").strip(),
        shared_memory_enabled=bool(rt.get("shared_memory_enabled", False)),
        master_umo=str(rt.get("master_umo", "") or "").strip(),
        master_auto_bind=bool(rt.get("master_auto_bind", True)),
        unread_inbox_enabled=bool(rt.get("unread_inbox_enabled", False)),
        inbox_group_whitelist=set(_str_list(rt.get("inbox_group_whitelist", []))),
        inbox_group_blacklist=set(_str_list(rt.get("inbox_group_blacklist", []))),
        mention_notice_enabled=bool(rt.get("mention_notice_enabled", False)),
        mention_notice_limit=_clamp_int(rt.get("mention_notice_limit", 20), 20, 1, 30),
        topic_summary_enabled=bool(rt.get("topic_summary_enabled", False)),
        topic_light_provider_id=str(rt.get("topic_light_provider_id", "") or "").strip(),
        topic_min_unread=_clamp_int(rt.get("topic_min_unread", 5), 5, 1),
        topic_cooldown_seconds=_clamp_int(rt.get("topic_cooldown_seconds", 180), 180, 0),
        topic_max_chars=_clamp_int(rt.get("topic_max_chars", 24), 24, 4, 60),
        topic_line_max_chars=_clamp_int(rt.get("topic_line_max_chars", 120), 120, 20, 300),
        topic_timeout_seconds=_clamp_float(rt.get("topic_timeout_seconds", 10), 10.0, 1.0),
        topic_segment_msg_count=_clamp_int(rt.get("topic_segment_msg_count", 25), 25, 5, 200),
        topic_segment_backfill_max=_clamp_int(rt.get("topic_segment_backfill_max", 5), 5, 0, 50),
        vision_enabled=bool(rt.get("vision_enabled", False)),
        vision_provider_ids=_str_list(rt.get("vision_provider_ids", [])),
        image_keep_days=_clamp_int(rt.get("image_keep_days", 3), 3, 0),
        vision_image_max=_clamp_int(rt.get("vision_image_max", 6), 6, 1, 20),
        vision_brief_max_chars=_clamp_int(rt.get("vision_brief_max_chars", 80), 80, 10, 300),
        vision_read_max_chars=_clamp_int(rt.get("vision_read_max_chars", 500), 500, 50, 2000),
        vision_timeout_seconds=_clamp_float(rt.get("vision_timeout_seconds", 60), 60.0, 3.0),
        vision_cache_ttl_days=_clamp_int(rt.get("vision_cache_ttl_days", 30), 30, 1),
        resident_inbox_enabled=bool(rt.get("resident_inbox_enabled", True)),
        resident_state_enabled=bool(rt.get("resident_state_enabled", True)),
        resident_extra_tools=set(_str_list(rt.get("resident_extra_tools", []))),
        resident_run_token_limit=_clamp_int(rt.get("resident_run_token_limit", 0), 0, 0),
        resident_daily_token_limit=_clamp_int(rt.get("resident_daily_token_limit", 0), 0, 0),
    )


def config_diagnostics(
    config: dict[str, Any], schema: dict[str, Any], effective: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    """Explain bad stored settings without rejecting native plugin startup."""
    _, errors = normalize_config(config, schema, include_defaults=True)
    view = effective if effective is not None else normalize_config(config, schema, runtime=True)[0]
    diagnostics = [
        {
            "code": "invalid_config",
            "message": f"{schema.get(key, {}).get('description', key)}：{message}",
            "severity": "warning",
            "fields": [key],
        }
        for key, message in errors.items()
    ]
    for key in schema:
        if key in config and key in view and config[key] != view[key]:
            if key in BLANK_BUILTIN_KEYS and not config[key]:
                continue
            diagnostics.append(
                {
                    "code": "effective_value_differs",
                    "severity": "warning",
                    "fields": [key],
                    "message": (
                        f"{schema[key].get('description', key)}的保存值与当前生效值不同；"
                        "页面保留保存值，修改后可重新保存。"
                    ),
                }
            )
    return diagnostics


def json_safe_config(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return "NaN" if math.isnan(value) else ("Infinity" if value > 0 else "-Infinity")
    if isinstance(value, dict):
        return {key: json_safe_config(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe_config(item) for item in value]
    return value


def atomic_write_bytes(path: Path, payload: bytes) -> None:
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


def is_astrbot_plugin_module(module_name: str) -> bool:
    parts = str(module_name or "").split(".")
    return len(parts) == 4 and parts[0] == "data" and parts[1] == "plugins" and parts[3] == "main"


def preload_config_migration(module_name: str, plugin_dir: Path, config_dir: Path) -> str | None:
    """Only real AstrBot StarManager plugin imports touch saved config."""
    if not is_astrbot_plugin_module(module_name):
        return None
    current = Path(config_dir) / CURRENT_CONFIG_NAME
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

        plugin_data = Path(get_astrbot_plugin_data_path()) / Path(plugin_dir).name
    except Exception:  # noqa: BLE001
        plugin_data = Path(plugin_dir) / "data"
    user_default = plugin_data / "user_default_config.json"
    legacy_snapshot = Path(plugin_dir) / "data" / "user_default_config.json"
    if legacy_snapshot.is_file() and not user_default.is_file():
        try:
            plugin_data.mkdir(parents=True, exist_ok=True)
            atomic_write_bytes(user_default, legacy_snapshot.read_bytes())
        except Exception:  # noqa: BLE001
            pass
    source = current if current.is_file() else Path(config_dir) / LEGACY_CONFIG_NAME
    if source.is_file():
        try:
            config = json.loads(source.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"配置文件 JSON 格式错误（{source.name}）：{exc}") from exc
        override = config.get("user_default_config_path") if isinstance(config, dict) else None
        if isinstance(override, str) and override.strip():
            user_default = Path(override.strip())
    return migrate_config_dir(config_dir, user_default)
