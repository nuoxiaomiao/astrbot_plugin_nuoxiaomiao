"""Shared runtime utilities: TTL cache, throttled logging, timezone, message helpers."""

from __future__ import annotations

import re
import time
from collections import OrderedDict
from datetime import datetime, timedelta
from typing import Any, Callable

from astrbot.api import logger

if __package__ and "." in __package__:
    from ..qq_official import clean_platform_text
else:
    from qq_official import clean_platform_text


class BoundedTTLCache:
    """In-memory key/value cache bounded by max_size and TTL."""

    def __init__(self, max_size: int = 512, ttl_seconds: float = 3600.0) -> None:
        self.max_size = max(1, int(max_size))
        self.ttl_seconds = float(ttl_seconds)
        self._data: OrderedDict[Any, tuple[float, Any]] = OrderedDict()

    def get(self, key: Any, default: Any = None, *, now: float | None = None) -> Any:
        item = self._data.get(key)
        if item is None:
            return default
        ts, value = item
        cur = time.monotonic() if now is None else now
        if self.ttl_seconds > 0 and cur - ts > self.ttl_seconds:
            self._data.pop(key, None)
            return default
        self._data.move_to_end(key)
        return value

    def set(self, key: Any, value: Any, *, now: float | None = None) -> None:
        cur = time.monotonic() if now is None else now
        self._data[key] = (cur, value)
        self._data.move_to_end(key)
        while len(self._data) > self.max_size:
            self._data.popitem(last=False)

    def pop(self, key: Any, default: Any = None) -> Any:
        item = self._data.pop(key, None)
        return item[1] if item is not None else default

    def prune(self, *, now: float | None = None) -> None:
        if self.ttl_seconds <= 0:
            return
        cur = time.monotonic() if now is None else now
        expired = [k for k, (ts, _) in self._data.items() if cur - ts > self.ttl_seconds]
        for key in expired:
            self._data.pop(key, None)

    def clear(self) -> None:
        self._data.clear()

    def __len__(self) -> int:
        return len(self._data)


class RateLimitedLogger:
    """Rate-limited warning/info logger with bounded key memory."""

    def __init__(self, interval_seconds: float = 300.0, max_keys: int = 512) -> None:
        self.interval_seconds = float(interval_seconds)
        self._last_at: OrderedDict[str, float] = OrderedDict()
        self._max_keys = max(16, int(max_keys))

    def should_log(self, key: str, interval: float | None = None, *, now: float | None = None) -> bool:
        cur = time.monotonic() if now is None else now
        step = self.interval_seconds if interval is None else float(interval)
        last = self._last_at.get(key)
        if last is not None and cur - last < step:
            return False
        self._last_at[key] = cur
        self._last_at.move_to_end(key)
        while len(self._last_at) > self._max_keys:
            self._last_at.popitem(last=False)
        return True

    def warning(self, key: str, message: str, *args: Any, interval: float | None = None) -> bool:
        if not self.should_log(key, interval=interval):
            return False
        logger.warning(message, *args)
        return True

    def info(self, key: str, message: str, *args: Any, interval: float | None = None) -> bool:
        if not self.should_log(key, interval=interval):
            return False
        logger.info(message, *args)
        return True

    def clear(self) -> None:
        self._last_at.clear()


def resolve_timezone(context: Any) -> Any:
    """Return configured IANA ZoneInfo from AstrBot context, or None."""
    try:
        from zoneinfo import ZoneInfo
    except Exception:
        return None
    try:
        config = context.get_config()
        tz_name = str(config.get("timezone") or "").strip() if hasattr(config, "get") else ""
        if tz_name:
            return ZoneInfo(tz_name)
    except Exception:
        pass
    return None


def now_in_tz(context: Any) -> datetime:
    tz = resolve_timezone(context)
    if tz is not None:
        try:
            return datetime.now(tz)
        except Exception:
            pass
    return datetime.now()


def timezone_label(context: Any) -> str:
    tz = resolve_timezone(context)
    if tz is not None:
        try:
            return str(tz)
        except Exception:
            pass
    return datetime.now().astimezone().tzname() or time.tzname[0] or "UTC"


def extract_components(event: Any) -> list[Any]:
    try:
        get_messages = getattr(event, "get_messages", None)
        if callable(get_messages):
            return list(get_messages() or [])
        message_obj = getattr(event, "message_obj", None)
        return list(getattr(message_obj, "message", None) or [])
    except Exception:
        return []


def extract_text(event: Any, *, strip_at_markup: bool = True) -> str:
    try:
        txt = getattr(event, "message_str", None)
        if txt is None:
            get = getattr(event, "get_message_str", None)
            if callable(get):
                txt = get()
        return clean_platform_text(event, txt or "", enabled=strip_at_markup)
    except Exception:
        return ""


_MEDIA_SUMMARY_MAX_TAGS = 8
_MEDIA_SUMMARY_MAX_CHARS = 120
_MEDIA_FILE_NAME_MAX_CHARS = 40
_STICKER_HINTS = ("sticker", "meme", "emoji", "face", "表情", "动画表情")


def _component_field(comp: Any, attr: str) -> Any:
    if isinstance(comp, dict):
        if attr in comp:
            return comp.get(attr)
        data = comp.get("data")
        if isinstance(data, dict) and attr in data:
            return data.get(attr)
        return None
    return getattr(comp, attr, None)


def _component_kind(comp: Any) -> str:
    if isinstance(comp, dict):
        return str(comp.get("type") or "").strip().lower()
    cname = type(comp).__name__
    if cname not in {"SimpleNamespace", "object", "Unknown", "BaseMessageComponent"}:
        return cname.lower()
    comp_type = getattr(comp, "type", None)
    val = getattr(comp_type, "value", comp_type)
    text = str(val or "").strip()
    if text and text.lower() != "unknown":
        return text.lower()
    return cname.lower()


def _is_sticker_image(comp: Any) -> bool:
    for attr in ("subtype", "sub_type", "subType"):
        val = _component_field(comp, attr)
        if val is None:
            continue
        val = getattr(val, "value", val)
        s = str(val).strip().lower()
        if s == "1" or any(hint in s for hint in _STICKER_HINTS):
            return True
    for attr in ("summary", "_type", "type"):
        val = _component_field(comp, attr)
        if val is None:
            continue
        val = getattr(val, "value", val)
        s = str(val).strip().lower()
        if s in {"", "image", "componenttype.image"}:
            continue
        if any(hint in s for hint in _STICKER_HINTS):
            return True
    return False


def extract_media_summary(event: Any) -> str:
    tags: list[str] = []
    total_chars = 0
    for comp in extract_components(event):
        kind = _component_kind(comp)
        tag = ""
        if kind == "image":
            tag = "[表情]" if _is_sticker_image(comp) else "[图片]"
        elif kind in {"face", "marketface", "mface"}:
            tag = "[表情]"
        elif kind in {"record", "audio"}:
            tag = "[语音]"
        elif kind == "video":
            tag = "[视频]"
        elif kind == "file":
            raw_name = _component_field(comp, "name") or _component_field(comp, "file_name") or ""
            fname = re.sub(r"\s+", " ", str(raw_name)).strip()
            if len(fname) > _MEDIA_FILE_NAME_MAX_CHARS:
                fname = fname[:_MEDIA_FILE_NAME_MAX_CHARS].rstrip() + "…"
            tag = f"[文件: {fname}]" if fname else "[文件]"
        elif kind in {"forward", "nodes"}:
            tag = "[合并转发]"
        elif kind == "share":
            tag = "[分享]"
        elif kind == "location":
            tag = "[位置]"
        if not tag:
            continue
        if tags and tags[-1] == tag:
            continue
        added_len = len(tag) if not tags else len(tag) + 1
        if len(tags) >= _MEDIA_SUMMARY_MAX_TAGS or (
            tags and total_chars + added_len > _MEDIA_SUMMARY_MAX_CHARS
        ):
            break
        tags.append(tag)
        total_chars += added_len
    return " ".join(tags)


def matches_any_keyword(text: str, keywords: Any) -> bool:
    if not text or not keywords:
        return False
    return any(keyword and keyword in text for keyword in keywords)


def message_id_of(event: Any) -> str:
    try:
        mid = getattr(getattr(event, "message_obj", None), "message_id", "")
        return str(mid or "").strip()
    except Exception:
        return ""


def admin_ids_of(context: Any, extra_admins: set[str] | frozenset[str] = frozenset()) -> set[str]:
    admins: set[str] = set()
    try:
        config = context.get_config()
        values = config.get("admins_id", []) if hasattr(config, "get") else []
        admins.update(str(item).strip() for item in values or [] if str(item).strip())
    except Exception:
        pass
    admins.update(str(item).strip() for item in extra_admins if str(item).strip())
    return admins


def group_scope_allows(
    group_id: str,
    group_name: str,
    whitelist: set[str] | frozenset[str],
    blacklist: set[str] | frozenset[str],
    platform_id: str = "",
) -> bool:
    gid = str(group_id or "").strip()
    gname = str(group_name or "").strip()
    identifiers = {gid, gname} - {""}
    if platform_id:
        identifiers.add(f"{platform_id}:GroupMessage:{gid}")
    if blacklist and identifiers.intersection(blacklist):
        return False
    if whitelist and not identifiers.intersection(whitelist):
        return False
    return True


def plugin_name_of_tool(tool: Any, star_map: dict | None) -> str:
    """Resolve owning AstrBot plugin registered name for a tool ('' for built-ins)."""
    mp = str(getattr(tool, "handler_module_path", "") or "")
    if not mp:
        return ""
    mapping = star_map or {}
    if mp in mapping:
        return str(getattr(mapping[mp], "name", "") or "")
    best = ""
    best_key_len = -1
    for key, meta in mapping.items():
        key_s = str(key or "")
        if not key_s:
            continue
        if key_s == mp or key_s.endswith(mp) or mp.startswith(key_s):
            name = str(getattr(meta, "name", "") or "")
            if name and len(key_s) > best_key_len:
                best = name
                best_key_len = len(key_s)
    if best:
        return best
    parts = mp.split(".")
    if len(parts) >= 2 and parts[0] == "data" and parts[1] == "plugins":
        return parts[2] if len(parts) > 2 else ""
    return ""


def parse_activity_time(
    raw: str, *, now_fn: Callable[[], datetime] | None = None
) -> tuple[float | None, str | None]:
    text = str(raw or "").strip()
    if not text:
        return None, None
    if text.endswith("前"):
        text = text[:-1].strip()
    now = now_fn() if now_fn is not None else datetime.now()
    if text in {"今天", "today", "今日"}:
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return start.timestamp(), None
    if text in {"昨天", "yesterday", "昨日"}:
        start = (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        return start.timestamp(), None
    if text in {"前天", "大前天"}:
        start = (now - timedelta(days=2 if text == "前天" else 3)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        return start.timestamp(), None
    matched = re.fullmatch(r"(\d+(?:\.\d+)?)", text)
    if matched:
        value = float(matched.group(1))
        if value > 1e12:
            value /= 1000.0
        return value, None
    relative = re.fullmatch(r"(\d+)\s*(秒钟|秒|分钟|分|小时|时|天|日)", text)
    if relative:
        amount = int(relative.group(1))
        unit = relative.group(2)
        seconds = {
            "秒钟": 1,
            "秒": 1,
            "分钟": 60,
            "分": 60,
            "小时": 3600,
            "时": 3600,
            "天": 86400,
            "日": 86400,
        }[unit]
        return (now - timedelta(seconds=amount * seconds)).timestamp(), None
    short = re.fullmatch(r"(\d+)\s*([smhd])", text, re.IGNORECASE)
    if short:
        amount = int(short.group(1))
        seconds = {"s": 1, "m": 60, "h": 3600, "d": 86400}[short.group(2).lower()]
        return (now - timedelta(seconds=amount * seconds)).timestamp(), None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%m-%d", "%H:%M"):
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if fmt == "%H:%M":
            parsed = parsed.replace(year=now.year, month=now.month, day=now.day)
        elif fmt == "%m-%d":
            parsed = parsed.replace(year=now.year)
            if parsed > now:
                parsed = parsed.replace(year=now.year - 1)
        if now.tzinfo is not None and parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=now.tzinfo)
        return parsed.timestamp(), None
    return (
        None,
        "看不懂这个时间。可以用 2026-10-06 12:00、10-06、30分钟前、2小时前、昨天、今天 这种写法。",
    )
