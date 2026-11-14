"""Unified proactive target & @ mention resolution across numeric and non-numeric IDs."""

from __future__ import annotations

from typing import Any

from astrbot.api import logger

if __package__ and "." in __package__:
    from ..infra.constants import DISPLAY_NAME
    from ..qq_official import at_id_deliverable, is_qq_official_platform_id
else:
    from infra.constants import DISPLAY_NAME
    from qq_official import at_id_deliverable, is_qq_official_platform_id


class ProactiveTargetResolver:
    """Resolve group/private targets and validate allowlist boundaries."""

    def __init__(self, plugin: Any, activity: Any) -> None:
        self.plugin = plugin
        self.activity = activity

    def person_allowed(self, user_id: str) -> bool:
        uid = str(user_id or "").strip()
        if not uid:
            return False
        return uid in self.plugin.proactive_target_whitelist or uid in self.activity.admin_ids()

    def group_allowed(self, group_id: str, group_name: str = "") -> bool:
        allow = self.plugin.proactive_allow_groups
        if not allow:
            return False
        gid = str(group_id or "").strip()
        gname = str(group_name or "").strip()
        return gid in allow or (bool(gname) and gname in allow)

    def find_activity_group(self, query: str) -> dict[str, Any] | None:
        target = str(query or "").strip()
        if not target:
            return None
        for session in self.activity.activity_sessions("group"):
            if session["conversation_id"] == target or session["name"] == target:
                return session
        low = target.lower()
        for session in self.activity.activity_sessions("group"):
            if low in session["name"].lower():
                return session
        return None

    def lookup_person_qq(self, name: str) -> tuple[str, str]:
        target = str(name or "").strip()
        if not target:
            return "", ""
        with self.activity.db.read_conn() as conn:
            row = conn.execute(
                """
                SELECT sender_id, sender_name FROM activity_messages
                WHERE sender_name = ? AND sender_id != '' AND sender_id != 'bot'
                ORDER BY id DESC LIMIT 1
                """,
                (target,),
            ).fetchone()
            if row is None:
                row = conn.execute(
                    """
                    SELECT sender_id, sender_name FROM activity_messages
                    WHERE instr(sender_name, ?) > 0 AND sender_id != '' AND sender_id != 'bot'
                    ORDER BY id DESC LIMIT 1
                    """,
                    (target,),
                ).fetchone()
        if not row:
            return "", ""
        return str(row[0] or ""), str(row[1] or "")

    def person_display_name(self, user_id: str) -> str:
        uid = str(user_id or "").strip()
        if not uid:
            return ""
        try:
            with self.activity.db.read_conn() as conn:
                row = conn.execute(
                    """
                    SELECT sender_name FROM activity_messages
                    WHERE sender_id = ? AND sender_name != ''
                    ORDER BY id DESC LIMIT 1
                    """,
                    (uid,),
                ).fetchone()
            if row:
                return str(row[0] or "")
        except Exception:
            pass
        return ""

    def match_allowed_group(self, value: str) -> tuple[str | None, str]:
        value = str(value or "").strip()
        allow = self.plugin.proactive_allow_groups
        if not value or not allow:
            return None, ""
        session = self.find_activity_group(value)
        if value in allow:
            if session:
                return session["conversation_id"], session["name"]
            return value, ""
        if session and (session["conversation_id"] in allow or session["name"] in allow):
            return session["conversation_id"], session["name"]
        return None, ""

    def event_is_admin(self, event: Any) -> bool:
        try:
            if event.is_admin():
                return True
        except Exception:
            pass
        sender_id = str(event.get_sender_id() or "").strip()
        return bool(sender_id) and sender_id in self.activity.admin_ids()

    def allowed_targets_hint(self, event: Any) -> dict[str, Any]:
        if not self.event_is_admin(event):
            return {}
        p = self.plugin
        groups = sorted(str(item) for item in p.proactive_allow_groups if str(item))
        people = sorted(
            {str(item) for item in p.proactive_target_whitelist if str(item)}
            | self.activity.admin_ids()
        )
        group_text = "、".join(groups[:20]) if groups else "（主人还没配置允许的群，proactive_allow_groups 为空）"
        person_text = "、".join(people[:20]) if people else "（没有可用的人）"
        platform_id = str(event.get_platform_id() or "").strip()
        is_qq_off = is_qq_official_platform_id(platform_id, self.activity.platform_insts())
        suffix = (
            "QQ 官方平台的用户/群标识是 openid（不是 QQ 号）；"
            "群主动消息还需要开启 qq_official_group_proactive 且平台允许。改好参数可以直接重试。"
            if is_qq_off
            else "群名的写法见 list_conversations 的结果；改好参数可以直接重试。"
        )
        return {
            "hint": f"可发往的群：{group_text}；可主动私聊的人：{person_text}。{suffix}"
        }

    def resolve_target(self, event: Any, target: str) -> dict[str, Any]:
        raw = str(target or "").strip()
        platform_id = str(event.get_platform_id() or "").strip()
        if not raw or raw in {"当前", "current", "这里", "这边"}:
            if event.is_private_chat():
                current = self.activity.current_conversation(event)
                sender_id = current[2] if current else ""
                if not sender_id:
                    return {"error": "拿不到当前会话信息。"}
                if not self.person_allowed(sender_id):
                    return {
                        "error": "主人没有允许主动私聊这个人（proactive_target_whitelist）。",
                        **self.allowed_targets_hint(event),
                    }
                return {
                    "platform_id": platform_id,
                    "kind": "private",
                    "id": sender_id,
                    "name": str(event.get_sender_name() or sender_id),
                    "umo": str(event.unified_msg_origin or ""),
                    "desc": "当前私聊",
                }
            group_id = str(event.get_group_id() or "").strip()
            if not group_id:
                return {"error": "拿不到当前会话信息。"}
            group = getattr(getattr(event, "message_obj", None), "group", None)
            group_name = str(getattr(group, "group_name", "") or "").strip()
            if not self.group_allowed(group_id, group_name):
                return {
                    "error": "主人没有允许在这个群里发主动消息（可在插件配置 proactive_allow_groups 里添加）。",
                    **self.allowed_targets_hint(event),
                }
            return {
                "platform_id": platform_id,
                "kind": "group",
                "id": group_id,
                "name": group_name or group_id,
                "umo": str(event.unified_msg_origin or ""),
                "desc": f"当前群「{group_name or group_id}」",
            }

        kind = ""
        value = raw
        for prefix in ("群:", "群：", "group:", "Group:"):
            if raw.startswith(prefix):
                kind = "group"
                value = raw[len(prefix) :].strip()
                break
        for prefix in ("私聊:", "私聊：", "private:", "Private:"):
            if raw.startswith(prefix):
                kind = "private"
                value = raw[len(prefix) :].strip()
                break

        if kind == "group":
            group_id, group_name = self.match_allowed_group(value)
            if not group_id:
                return {
                    "error": "这个群不在主人允许的主动消息名单里（proactive_allow_groups）。",
                    **self.allowed_targets_hint(event),
                }
            return {
                "platform_id": platform_id,
                "kind": "group",
                "id": group_id,
                "name": group_name or group_id,
                "umo": f"{platform_id}:GroupMessage:{group_id}",
                "desc": f"群「{group_name or group_id}」",
            }

        if kind == "private":
            qq = ""
            name = ""
            qq_official_private = is_qq_official_platform_id(platform_id, self.activity.platform_insts())
            if value.isdigit() and not qq_official_private:
                qq = value
                name = self.person_display_name(qq)
            else:
                qq, name = self.lookup_person_qq(value)
                if not qq and (self.person_display_name(value) or value in self.plugin.proactive_target_whitelist or value in self.activity.admin_ids()):
                    qq, name = value, self.person_display_name(value) or value
                if not qq:
                    hint = "试试 私聊:openid 或对方昵称。" if qq_official_private else "试试 私聊:QQ号 这种写法。"
                    return {"error": f"找不到这个人，{hint}"}
            if not self.person_allowed(qq):
                return {
                    "error": "主人没有允许主动私聊这个人（proactive_target_whitelist）。",
                    **self.allowed_targets_hint(event),
                }
            return {
                "platform_id": platform_id,
                "kind": "private",
                "id": qq,
                "name": name or qq,
                "umo": f"{platform_id}:FriendMessage:{qq}",
                "desc": f"私聊「{name or qq}」",
            }

        qq_official_platform = is_qq_official_platform_id(platform_id, self.activity.platform_insts())
        group_id, group_name = self.match_allowed_group(raw)
        if group_id:
            return {
                "platform_id": platform_id,
                "kind": "group",
                "id": group_id,
                "name": group_name or group_id,
                "umo": f"{platform_id}:GroupMessage:{group_id}",
                "desc": f"群「{group_name or group_id}」",
            }

        hint = (
            "用留空（当前会话）、私聊:openid、群:群名 或 群:群openid。"
            if qq_official_platform
            else "用留空（当前会话）、私聊:QQ、群:群名 或 群:群号。"
        )
        return {"error": f"看不懂目标。{hint}", **self.allowed_targets_hint(event)}

    def resolve_at_target(self, resolved: dict[str, Any], at: str) -> tuple[str, str]:
        at = str(at or "").strip()
        if not at or resolved.get("kind") != "group":
            return "", ""
        platform_id = str(resolved.get("platform_id") or "")
        qq_official = is_qq_official_platform_id(platform_id, self.activity.platform_insts())
        platform_name = "qq_official" if qq_official else ""
        try:
            with self.activity.db.read_conn() as conn:
                row = conn.execute(
                    """
                    SELECT sender_id, sender_name FROM activity_messages
                    WHERE platform_id = ? AND kind = 'group'
                      AND conversation_id = ?
                      AND (sender_id = ? OR sender_name = ?)
                    ORDER BY id DESC LIMIT 1
                    """,
                    (platform_id, resolved.get("id"), at, at),
                ).fetchone()
                if row is None and qq_official and not at.isdigit():
                    row = conn.execute(
                        """
                        SELECT sender_id, sender_name FROM activity_messages
                        WHERE platform_id = ? AND kind = 'group'
                          AND conversation_id = ?
                          AND (instr(sender_name, ?) > 0 OR instr(sender_id, ?) > 0)
                        ORDER BY id DESC LIMIT 1
                        """,
                        (platform_id, resolved.get("id"), at, at),
                    ).fetchone()
        except Exception:
            row = None
        if row:
            resolved_id = str(row[0] or "")
            resolved_name = str(row[1] or "")
            if not at_id_deliverable(platform_name, resolved_id):
                logger.info(
                    "[%s] QQ官方 @ 目标不可投递（只认 openid）: target=%s at=%s",
                    DISPLAY_NAME,
                    resolved.get("umo"),
                    at,
                )
                return "", ""
            return resolved_id, resolved_name
        if at.isdigit():
            if not at_id_deliverable(platform_name, at):
                logger.info(
                    "[%s] QQ官方无法@数字QQ号（只认 openid）: target=%s at=%s",
                    DISPLAY_NAME,
                    resolved.get("umo"),
                    at,
                )
                return "", ""
            return at, ""
        return "", ""
