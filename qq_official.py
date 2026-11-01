"""QQ 官方机器人（qq_official / qq_official_webhook）平台适配支持。

本模块只提供纯函数：不 import AstrBot 核心，任何对象只要暴露与 AstrBot 事件
一致的最小接口（get_platform_name / get_self_id / get_messages 等）即可直接使用，
因此可以脱离 AstrBot 运行环境做单元测试。

背景事实（AstrBot 4.29.3 qq_official 适配器）：
- 入站群消息 sender/group 都是 openid；机器人在群内被 @ 时 self_id = 机器人
  openid，无 mention 时 self_id 为字面量 "qq_official"。
- 入站私聊（C2C）恒在消息链头部塞一个 At(qq="qq_official") 占位，self_id 为
  字面量 "unknown_selfid"。
- 出站 At 只认 openid（序列化为 <qqbot-at-user id="..." />）；数字 QQ 号、
  "qq_official"/"unknown_selfid" 等占位值都会被原样发出、成为非法标记。
- event.send 是被动回复：复用入站 msg_id，窗口 5 分钟（botpy 文档）；
  群聊主动消息依赖平台"主动消息"权限与配额，且适配器的群场景缓存
  （_session_scene）为进程内存、重启即丢。
"""

from __future__ import annotations

import re
from typing import Any

# AstrBot qq_official 平台适配器 meta().name 的可能取值。
QQ_OFFICIAL_PLATFORM_NAMES = frozenset({"qq_official", "qq_official_webhook"})

# 适配器拿不到真实 self_id 时填的占位值（见 qqofficial_platform_adapter.py）：
# 群无 mention 时 self_id="qq_official"；私聊 C2C 时 self_id="unknown_selfid"。
PLACEHOLDER_SELF_IDS = frozenset({"qq_official", "unknown_selfid", ""})

# 出站链里这些组件在 QQ 官方无法表达（适配器静默丢弃或序列化失败）。
# Poke/Face 在 qq_official 的 _parse_to_qqofficial 里被直接忽略（日志 debug），
# 保留也不会报错；这里只处理真正会产生非法文本的 At 类。
_UNSUPPORTED_AT_TYPES = ("AtAll",)

# 正文里残留的 @ 标记：其他用户/其他机器人的 <qqbot-at-user id="..." /> 或
# <@!id> 会原样留在 message_str 里（适配器只剥除对机器人自身的 @）。
_AT_MARKUP_RE = re.compile(
    r"<(?:qqbot-at-user\b[^>]*|@!?[^\s>]+)>",
    re.IGNORECASE,
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


def sanitize_outbound_components(components: list[Any], event: Any) -> list[Any]:
    """清洗要发给 QQ 官方的组件链。

    仅对 QQ 官方生效（其它平台原样返回）：
    - 丢弃占位 At（qq 为 "qq_official"/"unknown_selfid"/""）——序列化会变成
      <qqbot-at-user id="qq_official" /> 这类非法标记；
    - 丢弃纯数字 At（QQ 号无法在 QQ 官方渲染，只能发 openid）；
    - 丢弃 AtAll（适配器静默丢弃，留着只会给调用方"发出去了"的错觉）。
    """
    if not is_qq_official(event):
        return list(components)
    cleaned: list[Any] = []
    for comp in components:
        cname = type(comp).__name__
        if cname == "At":
            qq = str(getattr(comp, "qq", "") or "")
            if qq in PLACEHOLDER_SELF_IDS or qq.isdigit():
                continue
            cleaned.append(comp)
            continue
        if cname in _UNSUPPORTED_AT_TYPES:
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
    """
    if chain is None:
        return
    try:
        chain.use_markdown_ = not bool(enabled)
    except Exception:
        pass


def plan_bubbles(
    segments: list[str],
    max_bubbles: int,
    join_separator: str = "\n",
) -> list[str]:
    """把拟人回复的分条压到被动回复窗口内的条数上限。

    QQ 官方对同一 msg_id 的被动回复有次数/时效限制，气泡超过上限时把多出来的
    段落合并进最后一条（保持顺序）。max_bubbles <= 0 视为不限制。
    """
    segs = [s for s in (segments or []) if s is not None]
    if not segs:
        return segs
    try:
        cap = int(max_bubbles)
    except (TypeError, ValueError):
        cap = 4
    if cap <= 0 or len(segs) <= cap:
        return segs
    tail = str(join_separator or "\n").join(segs[cap - 1 :])
    return segs[: cap - 1] + [tail]


def at_id_deliverable(platform_name: str | None, at_id: Any) -> bool:
    """这个 @ 目标 id 在目标平台上能不能直接发。

    QQ 官方只认 openid（非纯数字、长度与格式由平台决定）；纯数字 QQ 号发出去
    只会变成非法标记。其它平台一律放行（保持旧行为）。
    """
    if not is_qq_official_platform_name(platform_name):
        return True
    value = str(at_id or "").strip()
    return bool(value) and not value.isdigit()


def qq_official_group_send_ready(platform_inst: Any, group_id: str | None) -> tuple[bool, str]:
    """预检 QQ 官方群聊主动消息是否具备投递条件。

    群主动消息在适配器里只有当 _session_scene 记成 "group" 时才会走群 API；
    该缓存是进程内存（收到该群入站消息时写入），重启后到该群首次来消息前
    都是 None，此时 send_by_session 会静默丢弃或误走频道 API。
    """
    gid = str(group_id or "").strip()
    if not gid:
        return False, "缺少群标识（group_openid）"
    scene: Any = None
    try:
        scenes = getattr(platform_inst, "_session_scene", None)
        if scenes is not None and hasattr(scenes, "get"):
            scene = scenes.get(gid)
    except Exception:
        scene = None
    if scene == "group":
        return True, ""
    if scene == "channel":
        return False, "目标被识别为频道会话，不支持群主动消息"
    return False, "QQ 官方该群尚未建立消息窗口（重启后需先收到该群的消息）"


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
