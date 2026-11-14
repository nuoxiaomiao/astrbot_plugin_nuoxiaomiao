"""QQ 官方机器人（qq_official / qq_official_webhook）平台适配支持。

本模块只提供纯函数：不 import AstrBot 核心，任何对象只要暴露与 AstrBot 事件
一致的最小接口（get_platform_name / get_self_id / get_messages 等）即可直接使用，
因此可以脱离 AstrBot 运行环境做单元测试。

背景事实（AstrBot 4.29.3 qq_official 适配器）：
- 入站群消息 sender/group 都是 openid；机器人在群内被 @ 时 self_id = 机器人
  openid，无 mention 时 self_id 为字面量 "qq_official"。
- 入站私聊（C2C）恒在消息链头部塞一个 At(qq="qq_official") 占位，self_id 为
  字面量 "unknown_selfid"。
- 出站 At 只认 openid，且适配器会把它序列化成正文里的
  <qqbot-at-user id="..." />；数字 QQ 号、"qq_official"/"unknown_selfid"
  等占位值会被原样发出、成为非法标记。
- 主动消息（无 msg_id 的推送通道）不会渲染上面那串标记，用户只会看到原始
  文本。因此本插件的主动消息一律不带 @（见 main.py _deliver_proactive_message），
  补记速览/历史时同样不写 @。
- event.send 是被动回复：复用入站 msg_id，窗口 5 分钟（botpy 文档）；
  群聊主动消息依赖平台"主动消息"权限与配额，且适配器的群场景缓存
  （_session_scene）为进程内存、重启即丢。
- 群消息通常不包含 sender nickname / group_name；QQ 官方没有成员资料、历史
  消息、群列表 API。主动消息/名单/管理员需要使用 openid。
"""

from __future__ import annotations

import re
from typing import Any

# AstrBot qq_official 平台适配器 meta().name 的可能取值。
QQ_OFFICIAL_PLATFORM_NAMES = frozenset({"qq_official", "qq_official_webhook"})

# 适配器拿不到真实 self_id 时填的占位值（见 qqofficial_platform_adapter.py）：
# 群无 mention 时 self_id="qq_official"；私聊 C2C 时 self_id="unknown_selfid"。
PLACEHOLDER_SELF_IDS = frozenset({"qq_official", "unknown_selfid", ""})

# QQ 官方被动回复的保守条数。平台常见上限为 5 条；插件默认只发 4 条，
# 给表情/失败通知等留一条余量。
QQ_OFFICIAL_PASSIVE_REPLY_LIMIT = 4

# 单条纯文本的保守上限（可按配置覆盖）；QQ 官方在超长时可能直接拒绝。
QQ_OFFICIAL_TEXT_LIMIT = 2000

# 这些能力在 QQ 官方机器人 API 上没有对应事件/接口，配置页与日志需要明说。
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

# botpy 能够作为 event.send 消息源的入站对象类型名。
QQ_OFFICIAL_RAW_MESSAGE_TYPES = frozenset(
    {"Message", "GroupMessage", "DirectMessage", "C2CMessage"}
)

# 出站链里这些组件在 QQ 官方无法表达（适配器静默丢弃或序列化失败）。
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

# 正文里残留的 @ 标记：其他用户/其他机器人的 <qqbot-at-user id="..." /> 或
# <@!id> 会原样留在 message_str 里（适配器只剥除对机器人自身的 @）。
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


def is_qq_official_platform_name(name: str | None) -> bool:
    """平台适配器名是不是 QQ 官方（如 event.get_platform_name() 的值）。"""
    return str(name or "") in QQ_OFFICIAL_PLATFORM_NAMES


def is_qq_official(event: Any) -> bool:
    """事件是否来自 QQ 官方平台。"""
    try:
        return is_qq_official_platform_name(event.get_platform_name())
    except Exception:
        return False


def platform_meta(inst: Any) -> Any:
    """取平台实例的 meta 信息，兼容两种写法。

    AstrBot 真实实现是 meta() 方法；测试桩/旧代码里有直接把 meta 当属性挂的。
    """
    meta = getattr(inst, "meta", None)
    if callable(meta):
        try:
            return meta()
        except Exception:
            return None
    return meta


def platform_name_of_inst(inst: Any) -> str:
    """平台实例的适配器名（如 qq_official / aiocqhttp）。"""
    return str(getattr(platform_meta(inst), "name", "") or "")


def platform_id_of_inst(inst: Any) -> str:
    """平台实例的配置 id（如「糯小喵-agent专用」）。"""
    return str(getattr(platform_meta(inst), "id", "") or "")


def platform_id_to_name(platform_id: str | None, platform_insts: Any) -> str:
    """把平台实例的配置 id（如「糯小喵-agent专用」）反查成适配器名。

    event.get_platform_id() 返回的是 cmd_config 里配置的 id，而判断平台能力
    需要的是适配器 meta().name（如 "qq_official"）。platform_insts 通常是
    context.platform_manager.platform_insts。
    """
    pid = str(platform_id or "")
    if not pid:
        return ""
    for inst in platform_insts or []:
        if platform_id_of_inst(inst) == pid:
            return platform_name_of_inst(inst)
    return ""


def is_qq_official_platform_id(platform_id: str | None, platform_insts: Any) -> bool:
    """按平台实例配置 id 判断是不是 QQ 官方平台。"""
    return is_qq_official_platform_name(platform_id_to_name(platform_id, platform_insts))


def _raw_message_type_names(raw: Any) -> set[str]:
    """原始消息对象的类名及其 MRO 类名。

    AstrBot 适配器用 PatchedGroupMessage / PatchedC2CMessage 等子类接收消息，
    type(raw).__name__ 是 "PatchedGroupMessage" 而不是 botpy 的 "GroupMessage"；
    只看类名会把所有真实 QQ 官方的消息源误判为不支持。
    """
    if raw is None:
        return set()
    try:
        return {cls.__name__ for cls in type(raw).__mro__}
    except Exception:
        return {type(raw).__name__}


def is_supported_raw_message(raw: Any) -> bool:
    """event.send 是否能处理这个 botpy 入站对象。

    QQOfficialMessageEvent._post_send_one 只接受 botpy 的 Message/GroupMessage/
    DirectMessage/C2CMessage；其它类型（含 None）会直接 return None，既不抛错
    也不发送。插件必须在调用前自行拦截，避免把静默丢弃当成成功。

    用 MRO 判断，兼容适配器的 Patched* 子类与测试桩。
    """
    return bool(_raw_message_type_names(raw) & QQ_OFFICIAL_RAW_MESSAGE_TYPES)


def scene_of_event(event: Any) -> str:
    """事件场景：group / channel / c2c / guild_dm / unknown。

    QQ 官方适配器把频道消息也伪装成 GROUP_MESSAGE，所以不能用 message type
    判断，只能看 botpy 原始对象类型；用 MRO 兼容 Patched* 子类。
    """
    raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
    names = _raw_message_type_names(raw)
    # 先判具体类型：DirectMessage/C2CMessage 可能继承 Message，顺序不能反。
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


def normalize_inbound_components(
    components: list[Any],
    event: Any = None,
    strip_markup: bool = True,
) -> tuple[list[Any], int]:
    """清理入站消息链：去掉 QQ 官方占位 At 和正文里的原始 @ 标记。

    返回 (新组件列表, 丢弃的组件数)。只应作用于 QQ 官方事件；其它平台调用方
    自己判断。真实 openid 的 At（群里 @ 机器人）保留，否则 WakingCheck 与插件
    的唤醒判断会失效。
    """
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
                    try:
                        comp.text = cleaned_text
                    except Exception:
                        pass
            cleaned.append(comp)
            continue
        if cname == "Reply":
            if strip_markup:
                for attr in ("message_str", "text"):
                    value = getattr(comp, attr, None)
                    if isinstance(value, str):
                        try:
                            setattr(comp, attr, strip_at_markup(value))
                        except Exception:
                            pass
            cleaned.append(comp)
            continue
        cleaned.append(comp)
    return cleaned, dropped


def _expand_forward_components(components: list[Any], info: dict[str, Any]) -> list[Any]:
    """把 Node/Nodes 递归展开成顶层组件，避免适配器静默忽略整段转发。"""
    expanded: list[Any] = []
    for comp in components or []:
        cname = type(comp).__name__
        if cname not in _FORWARD_COMPONENT_TYPES:
            expanded.append(comp)
            continue
        info["flattened"] = int(info.get("flattened") or 0) + 1
        if cname == "Node":
            nested = getattr(comp, "content", None)
        else:
            nested = getattr(comp, "nodes", None)
        if nested:
            expanded.extend(_expand_forward_components(list(nested), info))
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
    """把出站链清洗成 QQ 官方能表达的最小集合。

    - 展开 Node/Nodes（可关）；
    - 丢弃占位/数字 At、AtAll、Poke/Face/Json 等适配器不认识的组件；
    - Reply 只保留 REFIDX_ 或本条入站消息 id，避免适配器回退成"引用当前消息"；
    - 返回 dropped 统计供调用方记日志。

    合法 openid At 保留：适配器会序列化成 <qqbot-at-user>，这是上游既有行为。
    """
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
        inbound = str(
            getattr(getattr(event, "message_obj", None), "message_id", "") or ""
        )

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
            keep = bool(reply_id.startswith("REFIDX_")) or (
                bool(inbound) and reply_id == inbound
            )
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
                try:
                    comp.text = cleaned_text
                except Exception:
                    pass
        cleaned.append(comp)
    return cleaned, info


def sanitize_outbound_components(components: list[Any], event: Any) -> list[Any]:
    """清洗要发给 QQ 官方的组件链（向后兼容入口）。

    仅对 QQ 官方生效（其它平台原样返回）：
    - 丢弃占位 At（qq 为 "qq_official"/"unknown_selfid"/""）——序列化会变成
      <qqbot-at-user id="qq_official" /> 这类非法标记；
    - 丢弃纯数字 At（QQ 号无法在 QQ 官方渲染，只能发 openid）；
    - 丢弃 AtAll 与适配器无法表达的组件。
    """
    if not is_qq_official(event):
        return list(components)
    # 兼容入口保持旧语义：只处理 At/AtAll，不碰 Reply/Forward 等组件；
    # 完整清洗由 normalize_outbound_components 在统一发送管线上完成。
    cleaned: list[Any] = []
    for comp in components or []:
        cname = type(comp).__name__
        if cname == "At":
            qq = str(getattr(comp, "qq", "") or "")
            if qq in PLACEHOLDER_SELF_IDS or qq.isdigit():
                continue
            cleaned.append(comp)
            continue
        if cname == "AtAll":
            continue
        cleaned.append(comp)
    return cleaned


def is_mergeable_at(comp: Any, event: Any) -> bool:
    """这条 At 算不算"没有实质内容、可被安全合并"（用于防抖的放行判断）。

    QQ 官方私聊入站恒带 At(qq="qq_official") 占位、群无 mention 时 self_id
    也是 "qq_official"，这些都不代表用户在 @ 谁，合并时不应当作可合并内容。
    仅对 QQ 官方平台生效，其它平台保持原有语义（At.qq == self_id 才无内容）。
    """
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
    """强制消息链走普通文本（不使用 markdown）。

    QQ 官方 event.send 在 use_markdown_ 为 None 时默认发 markdown（msg_type=2），
    群里没有 markdown 权限时会先被平台拒绝、再由适配器去掉 msg_id 重试（变成
    主动消息）。插件侧统一声明纯文本可以完全绕开这段失败舞蹈。

    enabled=False 时恢复 None（跟随平台默认），不反向强制 markdown。
    """
    if chain is None:
        return
    try:
        chain.use_markdown_ = False if enabled else None
    except Exception:
        pass


def split_text_for_limit(text: Any, limit: int) -> list[str]:
    """把长文本按 limit 切成保序、不丢字的多段。

    优先在换行/中文标点后断开，其次空格，最后硬切；不会为了美观删掉字符。
    limit <= 0 时不切分。
    """
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


def session_last_message_id(platform_inst: Any, session_id: str | None) -> str:
    """读适配器缓存的最近消息 id（send_by_session 成功后会更新）。"""
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
    """主动投递后的轻量确认依据：最近消息 id。"""
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
    """预检 QQ 官方群/频道主动消息是否具备投递条件。

    返回 (ready, scene, reason)。群主动消息在适配器里只有当 _session_scene
    记成 "group" 时才会走群 API；频道则要求 _session_last_message_id 非空
    （_send_by_session_common 的频道分支无 msg_id 会跳过）。
    """
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
    """预检 QQ 官方群聊主动消息是否具备投递条件（旧签名，群场景专用）。"""
    ready, _scene, reason = group_send_state(
        platform_inst, group_id, allow_channel=False
    )
    return ready, reason


def delivery_ready(
    platform_inst: Any,
    kind: str,
    conversation_id: str | None,
    *,
    allow_group: bool = False,
) -> tuple[bool, str]:
    """主动消息投递前的平台能力预检（群/频道/私聊）。"""
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
    ready, _scene, reason = group_send_state(
        platform_inst, cid, allow_channel=True
    )
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
    """QQ 平台是不是明确拒绝了这次发送（权限/配额/白名单）。

    这类错误是确定失败，不能当 unknown 让模型以为“可能已经发出去了”；
    unknown 只留给网络超时、API 返回 None 等真正不确定的情况。
    """
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
        }
    )


def at_id_deliverable(platform_name: str | None, at_id: Any) -> bool:
    """这个 @ 目标 id 在目标平台上能不能直接发。

    QQ 官方只认 openid（非纯数字、长度与格式由平台决定）；纯数字 QQ 号发出去
    只会变成非法标记。其它平台一律放行（保持旧行为）。
    """
    if not is_qq_official_platform_name(platform_name):
        return True
    value = str(at_id or "").strip()
    if not value or value in PLACEHOLDER_SELF_IDS or value == "all":
        return False
    return not value.isdigit()


def strip_at_markup(text: Any) -> str:
    """清掉正文里残留的 @ 标记（<qqbot-at-user .../>、<@!id>）。"""
    if not text:
        return ""
    cleaned = _AT_MARKUP_RE.sub("", str(text))
    return re.sub(r"[ \t]+", " ", cleaned).strip()


def clean_platform_text(event: Any, text: Any, enabled: bool = True) -> str:
    """按平台规则清理要进入速览库 / LLM 的正文文本。"""
    value = str(text or "").strip()
    if enabled and is_qq_official(event):
        return strip_at_markup(value)
    return value


def display_name_fallback(value: Any, kind: str = "group") -> str:
    """openid 没有昵称时给用户看的短标签（不写回真实名称字段）。"""
    text = str(value or "").strip()
    if not text:
        return "未知会话"
    tail = text[-8:] if len(text) > 8 else text
    prefix = "群" if str(kind or "") == "group" else "用户"
    return f"{prefix}·{tail}"


def platform_capability_note() -> str:
    """平台边界文案：日志、能力提醒、README 共用。"""
    return _PLATFORM_CAPABILITY_NOTE
