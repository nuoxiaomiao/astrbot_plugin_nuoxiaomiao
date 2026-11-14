"""QQ 官方机器人（qq_official / qq_official_webhook）平台适配支持。"""

from __future__ import annotations

import contextlib
import contextvars
import copy
import re
from typing import Any, Callable

QQ_OFFICIAL_PLATFORM_NAMES = frozenset({"qq_official", "qq_official_webhook"})
PLACEHOLDER_SELF_IDS = frozenset({"qq_official", "unknown_selfid", ""})
QQ_OFFICIAL_PASSIVE_REPLY_LIMIT = 4
QQ_OFFICIAL_TEXT_LIMIT = 2000

QQ_OFFICIAL_UNSUPPORTED_FEATURES = frozenset(
    {
        "poke",
        "typing_notice",
        "input_status",
        "group_member_info",
        "history_messages",
        "group_list",
        "forward_message",
        "recall",
    }
)

QQ_OFFICIAL_RAW_MESSAGE_TYPES = frozenset(
    {"Message", "GroupMessage", "DirectMessage", "C2CMessage"}
)

_UNSUPPORTED_COMPONENT_TYPES = frozenset(
    {
        "AtAll",
        "Poke",
        "Face",
        "Json",
        "Share",
        "Contact",
        "Shake",
        "Dice",
        "RPS",
        "Forward",
        "Music",
    }
)
_FORWARD_COMPONENT_TYPES = frozenset({"Node", "Nodes"})

_AT_MARKUP_RE = re.compile(
    r"<(?:qqbot-at-user\b[^>]*|@!?[^\s>]+)>",
    re.IGNORECASE,
)

_PLATFORM_CAPABILITY_NOTE = (
    "QQ 官方机器人平台：群聊通常只收到 @ 机器人的消息（全量群消息需要平台单独授权）；"
    "用户和群的标识是 openid，不是 QQ 号；群聊主动消息默认关闭且需要平台主动消息权限；"
    "没有戳一戳、正在输入、成员资料、历史消息、群列表接口；出站 @ 只认 openid，"
    "引用只认平台 REFIDX，转发/表情/JSON 等组件会被清理或转成文本。"
)

_OUTBOUND_ACK_CAPTURE: contextvars.ContextVar[tuple[str, list[str]] | None] = (
    contextvars.ContextVar("nuoxiaomiao_qq_outbound_ack", default=None)
)


@contextlib.contextmanager
def capture_outbound_ack(target_id: str, captured: list[str]):
    """Scoped task-isolated capture for outbound remember_session_message_id calls."""
    token = _OUTBOUND_ACK_CAPTURE.set((str(target_id or ""), captured))
    try:
        yield
    finally:
        _OUTBOUND_ACK_CAPTURE.reset(token)


def current_outbound_ack_capture() -> tuple[str, list[str]] | None:
    return _OUTBOUND_ACK_CAPTURE.get()


def is_qq_official_platform_name(name: str | None) -> bool:
    return str(name or "") in QQ_OFFICIAL_PLATFORM_NAMES


def is_qq_official(event: Any) -> bool:
    try:
        return is_qq_official_platform_name(event.get_platform_name())
    except Exception:
        return False


def platform_meta(inst: Any) -> Any:
    meta = getattr(inst, "meta", None)
    if callable(meta):
        try:
            return meta()
        except Exception:
            return None
    return meta


def platform_name_of_inst(inst: Any) -> str:
    return str(getattr(platform_meta(inst), "name", "") or "")


def platform_id_of_inst(inst: Any) -> str:
    return str(getattr(platform_meta(inst), "id", "") or "")


def platform_id_to_name(platform_id: str | None, platform_insts: Any) -> str:
    pid = str(platform_id or "")
    if not pid:
        return ""
    for inst in platform_insts or []:
        if platform_id_of_inst(inst) == pid:
            return platform_name_of_inst(inst)
    return ""


def is_qq_official_platform_id(platform_id: str | None, platform_insts: Any) -> bool:
    return is_qq_official_platform_name(platform_id_to_name(platform_id, platform_insts))


def _raw_message_type_names(raw: Any) -> set[str]:
    if raw is None:
        return set()
    try:
        return {cls.__name__ for cls in type(raw).__mro__}
    except Exception:
        return {type(raw).__name__}


def is_supported_raw_message(raw: Any) -> bool:
    return bool(_raw_message_type_names(raw) & QQ_OFFICIAL_RAW_MESSAGE_TYPES)


def scene_of_event(event: Any) -> str:
    raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
    names = _raw_message_type_names(raw)
    if "GroupMessage" in names:
        return "group"
    if "C2CMessage" in names:
        return "c2c"
    if "DirectMessage" in names:
        return "guild_dm"
    if "Message" in names:
        return "channel"
    try:
        if event.is_private_chat():
            return "c2c"
        if event.get_group_id():
            return "group"
    except Exception:
        pass
    return "unknown"


def _clone_with_attrs(comp: Any, **updates: Any) -> Any:
    cloned = copy.copy(comp)
    for attr, value in updates.items():
        try:
            setattr(cloned, attr, value)
        except Exception:
            pass
    return cloned


def normalize_inbound_components(
    components: list[Any],
    event: Any = None,
    strip_markup: bool = True,
) -> tuple[list[Any], int]:
    """清理入站消息链：不原位修改输入对象，返回浅拷贝后的新列表。"""
    cleaned: list[Any] = []
    dropped = 0
    for comp in components or []:
        cname = type(comp).__name__
        if cname == "At":
            qq = str(getattr(comp, "qq", "") or "")
            if qq in PLACEHOLDER_SELF_IDS:
                dropped += 1
                continue
            cleaned.append(comp)
            continue
        if cname == "AtAll":
            dropped += 1
            continue
        if cname == "Plain":
            if strip_markup:
                text = str(getattr(comp, "text", "") or "")
                cleaned_text = strip_at_markup(text)
                if cleaned_text != text:
                    cleaned.append(_clone_with_attrs(comp, text=cleaned_text))
                    continue
            cleaned.append(comp)
            continue
        if cname == "Reply":
            if strip_markup:
                updates: dict[str, Any] = {}
                for attr in ("message_str", "text"):
                    value = getattr(comp, attr, None)
                    if isinstance(value, str):
                        stripped = strip_at_markup(value)
                        if stripped != value:
                            updates[attr] = stripped
                rchain = getattr(comp, "chain", None)
                if isinstance(rchain, list) and rchain:
                    new_rchain, _ = normalize_inbound_components(rchain, event=event, strip_markup=True)
                    updates["chain"] = new_rchain
                if updates:
                    cleaned.append(_clone_with_attrs(comp, **updates))
                    continue
            cleaned.append(comp)
            continue
        cleaned.append(comp)
    return cleaned, dropped


def _expand_forward_components(
    components: list[Any],
    info: dict[str, Any],
    *,
    _depth: int = 0,
    _seen: set[int] | None = None,
) -> list[Any]:
    """把 Node/Nodes 递归展开成顶层组件，带深度与循环引用保护。"""
    if _depth > 8:
        return list(components or [])
    seen = _seen if _seen is not None else set()
    expanded: list[Any] = []
    for comp in components or []:
        cname = type(comp).__name__
        if cname not in _FORWARD_COMPONENT_TYPES:
            expanded.append(comp)
            continue
        cid = id(comp)
        if cid in seen:
            continue
        seen.add(cid)
        info["flattened"] = int(info.get("flattened") or 0) + 1
        nested = getattr(comp, "content", None) if cname == "Node" else getattr(comp, "nodes", None)
        if nested is not None and not isinstance(nested, (str, bytes)):
            items = list(nested) if isinstance(nested, (list, tuple, set)) else [nested]
            if items:
                expanded.extend(
                    _expand_forward_components(items, info, _depth=_depth + 1, _seen=seen)
                )
    return expanded


def normalize_outbound_components(
    components: list[Any],
    event: Any = None,
    *,
    content_mode: bool = False,
    flatten_forward: bool = True,
    inbound_message_id: str = "",
    strip_markup: bool = True,
) -> tuple[list[Any], dict[str, Any]]:
    """把出站链清洗成 QQ 官方能表达的最小集合（不原位修改输入对象）。"""
    info: dict[str, Any] = {
        "dropped": 0,
        "flattened": 0,
        "dropped_types": {},
        "content_mode": bool(content_mode),
    }
    source = list(components or [])
    if flatten_forward:
        source = _expand_forward_components(source, info)

    inbound = str(inbound_message_id or "")
    if not inbound and event is not None:
        inbound = str(getattr(getattr(event, "message_obj", None), "message_id", "") or "")

    cleaned: list[Any] = []
    for comp in source:
        cname = type(comp).__name__
        if cname == "At":
            qq = str(getattr(comp, "qq", "") or "")
            if qq in PLACEHOLDER_SELF_IDS or qq == "all" or qq.isdigit():
                info["dropped"] += 1
                info["dropped_types"][cname] = info["dropped_types"].get(cname, 0) + 1
                continue
            cleaned.append(comp)
            continue
        if cname in _UNSUPPORTED_COMPONENT_TYPES:
            info["dropped"] += 1
            info["dropped_types"][cname] = info["dropped_types"].get(cname, 0) + 1
            continue
        if cname == "Reply":
            reply_id = str(getattr(comp, "id", "") or "").strip()
            keep = bool(reply_id.startswith("REFIDX_")) or (bool(inbound) and reply_id == inbound)
            if not keep:
                info["dropped"] += 1
                info["dropped_types"][cname] = info["dropped_types"].get(cname, 0) + 1
                continue
            cleaned.append(comp)
            continue
        if cname == "Plain" and strip_markup:
            text = str(getattr(comp, "text", "") or "")
            cleaned_text = strip_at_markup(text)
            if cleaned_text != text:
                cleaned.append(_clone_with_attrs(comp, text=cleaned_text))
                continue
        cleaned.append(comp)
    return cleaned, info


def sanitize_outbound_components(components: list[Any], event: Any) -> list[Any]:
    """向后兼容的轻清洗入口：对 QQ 官方剔除占位/数字/all At 与 AtAll。"""
    if not is_qq_official(event):
        return list(components)
    cleaned: list[Any] = []
    for comp in components or []:
        cname = type(comp).__name__
        if cname == "At":
            qq = str(getattr(comp, "qq", "") or "")
            if qq in PLACEHOLDER_SELF_IDS or qq == "all" or qq.isdigit():
                continue
            cleaned.append(comp)
            continue
        if cname == "AtAll":
            continue
        cleaned.append(comp)
    return cleaned


def is_mergeable_at(comp: Any, event: Any) -> bool:
    if type(comp).__name__ != "At":
        return False
    qq = str(getattr(comp, "qq", "") or "")
    try:
        self_id = str(event.get_self_id() or "")
    except Exception:
        self_id = ""
    if qq == self_id:
        return True
    if is_qq_official(event) and qq in PLACEHOLDER_SELF_IDS:
        return True
    return False


def apply_content_mode(chain: Any, enabled: bool) -> None:
    if chain is None:
        return
    try:
        chain.use_markdown_ = False if enabled else None
    except Exception:
        pass


def split_text_for_limit(text: Any, limit: int) -> list[str]:
    value = str(text or "")
    if not value:
        return []
    try:
        size = int(limit)
    except (TypeError, ValueError):
        size = 0
    if size <= 0 or len(value) <= size:
        return [value]

    separators = ("\n", "。", "！", "？", "!", "?", "；", ";")
    parts: list[str] = []
    remaining = value
    while len(remaining) > size:
        window = remaining[:size]
        cut = -1
        for separator in separators:
            index = window.rfind(separator)
            if index >= 0:
                candidate = index + len(separator)
                if candidate > cut:
                    cut = candidate
        if cut <= 0:
            index = window.rfind(" ")
            if index > 0:
                cut = index + 1
        if cut <= 0:
            cut = size
        parts.append(remaining[:cut])
        remaining = remaining[cut:]
    if remaining:
        parts.append(remaining)
    return parts


def prepare_outbound_chains(
    message: Any,
    *,
    event: Any = None,
    content_mode: bool = True,
    flatten_forward: bool = True,
    inbound_message_id: str = "",
    strip_markup: bool = True,
    text_limit: int = QQ_OFFICIAL_TEXT_LIMIT,
    derive_fn: Callable[[Any, list[Any]], Any] | None = None,
    plain_cls: Any = None,
    chain_cls: Any = None,
) -> tuple[list[Any], dict[str, Any]]:
    """统一 QQ 官方出站清洗、保序分段与纯文本模式装配。"""
    chain = list(getattr(message, "chain", None) or [])
    if not chain:
        return [], {"dropped": 0, "flattened": 0, "dropped_types": {}}
    cleaned, info = normalize_outbound_components(
        chain,
        event=event,
        content_mode=content_mode,
        flatten_forward=flatten_forward,
        inbound_message_id=inbound_message_id,
        strip_markup=strip_markup,
    )
    if not cleaned:
        return [], info

    if plain_cls is None:
        from astrbot.core.message.components import Plain as _Plain

        plain_cls = _Plain
    if chain_cls is None:
        from astrbot.core.message.message_event_result import MessageChain as _MessageChain

        chain_cls = _MessageChain

    def _make_chain(comps: list[Any]) -> Any:
        if derive_fn is not None:
            item = derive_fn(message, comps)
        else:
            derive = getattr(message, "derive", None)
            if callable(derive):
                try:
                    item = derive(list(comps))
                except Exception:
                    item = chain_cls(list(comps))
            else:
                item = chain_cls(list(comps))
        if content_mode:
            apply_content_mode(item, True)
        return item

    # Merge adjacent Plain components in place while keeping non-Plain positions intact.
    coalesced: list[Any] = []
    for comp in cleaned:
        if type(comp).__name__ == "Plain":
            txt = str(getattr(comp, "text", "") or "")
            if coalesced and type(coalesced[-1]).__name__ == "Plain":
                prev_txt = str(getattr(coalesced[-1], "text", "") or "")
                coalesced[-1] = plain_cls(prev_txt + txt)
            else:
                coalesced.append(plain_cls(txt))
        else:
            coalesced.append(comp)

    limit = int(text_limit or 0)
    # Split oversized Plain components while keeping At/Media relative order.
    segments: list[list[Any]] = [[]]
    current_len = 0
    for comp in coalesced:
        if type(comp).__name__ != "Plain":
            segments[-1].append(comp)
            continue
        txt = str(getattr(comp, "text", "") or "")
        if not txt.strip():
            continue
        if limit <= 0 or (current_len + len(txt) <= limit):
            segments[-1].append(plain_cls(txt))
            current_len += len(txt)
            continue
        chunks = split_text_for_limit(txt, limit)
        for idx, chunk in enumerate(chunks):
            if not chunk:
                continue
            if idx == 0 and current_len == 0:
                segments[-1].append(plain_cls(chunk))
                current_len += len(chunk)
            elif idx == 0 and current_len + len(chunk) <= limit:
                segments[-1].append(plain_cls(chunk))
                current_len += len(chunk)
            else:
                segments.append([plain_cls(chunk)])
                current_len = len(chunk)

    chains: list[Any] = []
    for seg in segments:
        filtered = [
            comp
            for comp in seg
            if not (type(comp).__name__ == "Plain" and not str(getattr(comp, "text", "") or "").strip())
        ]
        if filtered:
            chains.append(_make_chain(filtered))
    return chains, info


def session_last_message_id(platform_inst: Any, session_id: str | None) -> str:
    sid = str(session_id or "")
    if not sid:
        return ""
    try:
        cache = getattr(platform_inst, "_session_last_message_id", None)
        if cache is not None and hasattr(cache, "get"):
            return str(cache.get(sid) or "")
    except Exception:
        return ""
    return ""


def delivery_ack(platform_inst: Any, session_id: str | None) -> str:
    return session_last_message_id(platform_inst, session_id)


def _platform_scene(platform_inst: Any, conversation_id: str) -> str:
    sid = str(conversation_id or "")
    if not sid:
        return ""
    try:
        scenes = getattr(platform_inst, "_session_scene", None)
        if scenes is not None and hasattr(scenes, "get"):
            return str(scenes.get(sid) or "")
    except Exception:
        return ""
    return ""


def group_send_state(
    platform_inst: Any,
    group_id: str | None,
    allow_channel: bool = True,
) -> tuple[bool, str, str]:
    gid = str(group_id or "").strip()
    if not gid:
        return False, "", "缺少群标识（group_openid）"
    scene = _platform_scene(platform_inst, gid)
    if scene == "group":
        return True, "group", ""
    if scene == "channel":
        if not allow_channel:
            return False, scene, "目标被识别为频道会话，不支持群主动消息"
        if not session_last_message_id(platform_inst, gid):
            return False, scene, "QQ 官方该频道尚未建立消息窗口（重启后需先收到消息）"
        return True, scene, ""
    if not scene:
        return False, "", "QQ 官方该群尚未建立消息窗口（重启后需先收到该群的消息）"
    return False, scene, "目标被识别为不支持的会话类型"


def qq_official_group_send_ready(
    platform_inst: Any, group_id: str | None
) -> tuple[bool, str]:
    ready, _scene, reason = group_send_state(platform_inst, group_id, allow_channel=False)
    return ready, reason


def delivery_ready(
    platform_inst: Any,
    kind: str,
    conversation_id: str | None,
    *,
    allow_group: bool = False,
) -> tuple[bool, str]:
    if platform_inst is None:
        return False, "找不到平台适配器实例"
    cid = str(conversation_id or "").strip()
    if not cid:
        return False, "缺少会话标识"
    kind_value = str(kind or "").strip()
    if kind_value == "private":
        return True, ""
    if kind_value != "group":
        return False, "不支持的主动消息目标类型"
    if not allow_group:
        return False, "QQ 官方群聊主动消息未开启（qq_official_group_proactive）"
    ready, _scene, reason = group_send_state(platform_inst, cid, allow_channel=True)
    return ready, reason


_PERMISSION_ERROR_MARKERS = (
    "无权限",
    "没有权限",
    "主动消息失败",
    "permission denied",
    "forbidden",
    "not allowed",
    "权限不足",
)


def is_permission_error(exc: BaseException) -> bool:
    try:
        text = str(exc or "").lower()
    except Exception:
        text = ""
    if any(marker.lower() in text for marker in _PERMISSION_ERROR_MARKERS):
        return True
    try:
        names = {cls.__name__ for cls in type(exc).__mro__}
    except Exception:
        return False
    return bool(
        names
        & {
            "ForbiddenError",
            "PermissionError",
            "MethodNotAllowedError",
            "UnauthorizedError",
            "AuthenticationFailedError",
        }
    )


def at_id_deliverable(platform_name: str | None, at_id: Any) -> bool:
    if not is_qq_official_platform_name(platform_name):
        return True
    value = str(at_id or "").strip()
    if not value or value in PLACEHOLDER_SELF_IDS or value == "all":
        return False
    return not value.isdigit()


def strip_at_markup(text: Any) -> str:
    if not text:
        return ""
    cleaned = _AT_MARKUP_RE.sub("", str(text))
    return re.sub(r"[ \t]+", " ", cleaned).strip()


def clean_platform_text(event: Any, text: Any, enabled: bool = True) -> str:
    value = str(text or "").strip()
    if enabled and is_qq_official(event):
        return strip_at_markup(value)
    return value


def display_name_fallback(value: Any, kind: str = "group") -> str:
    text = str(value or "").strip()
    if not text:
        return "未知会话"
    tail = text[-8:] if len(text) > 8 else text
    prefix = "群" if str(kind or "") == "group" else "用户"
    return f"{prefix}·{tail}"


def platform_capability_note() -> str:
    return _PLATFORM_CAPABILITY_NOTE
