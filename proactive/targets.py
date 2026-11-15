"""Unified proactive target & @ mention resolution across numeric and non-numeric IDs."""

from __future__ import annotations

import re
from typing import Any

from astrbot.api import logger

if __package__ and "." in __package__:
    from ..infra import constants as _constants
    from ..infra.constants import DISPLAY_NAME, SELF_WAKE_EVENT_EXTRA
    from ..qq_official import (
        at_id_deliverable,
        is_qq_official,
        is_qq_official_platform_id,
        is_qq_official_platform_name,
        platform_id_of_inst,
        platform_name_of_inst,
    )
else:
    import infra.constants as _constants
    from infra.constants import DISPLAY_NAME, SELF_WAKE_EVENT_EXTRA
    from qq_official import (
        at_id_deliverable,
        is_qq_official,
        is_qq_official_platform_id,
        is_qq_official_platform_name,
        platform_id_of_inst,
        platform_name_of_inst,
    )

_UMO_TYPE_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_PRIVATE_UMO_TOKENS = ("friend", "private", "c2c", "dm", "direct")
_GROUP_UMO_TOKENS = ("group", "channel", "guild", "room", "supergroup", "other")
_DEFAULT_SELF_WAKE_UMO = "nuoxiaomiao:FriendMessage:self_wake"


def _self_wake_umo() -> str:
    return str(getattr(_constants, "SELF_WAKE_UMO", _DEFAULT_SELF_WAKE_UMO) or _DEFAULT_SELF_WAKE_UMO)


class ProactiveTargetResolver:
    """Resolve group/private targets and validate allowlist boundaries."""

    def __init__(self, plugin: Any, activity: Any) -> None:
        self.plugin = plugin
        self.activity = activity

    @staticmethod
    def _event_platform_id(event: Any) -> str:
        get_pid = getattr(event, "get_platform_id", None)
        if callable(get_pid):
            try:
                return str(get_pid() or "").strip()
            except Exception:
                return ""
        return str(getattr(event, "platform_id", "") or "").strip()

    @classmethod
    def _is_self_wake_session(cls, event: Any) -> bool:
        umo = str(getattr(event, "unified_msg_origin", "") or "").strip()
        return umo == _self_wake_umo() or cls._event_platform_id(event) == "nuoxiaomiao"

    def _is_qq_official_context(self, event: Any) -> bool:
        if is_qq_official(event):
            return True
        insts = self.activity.platform_insts()
        platform_id = self._event_platform_id(event)
        if not self._is_self_wake_session(event) and platform_id:
            return is_qq_official_platform_id(platform_id, insts)
        return bool(insts) and all(
            is_qq_official_platform_name(platform_name_of_inst(inst))
            for inst in insts
        )

    def _allow_umo_entries(self, kind: str) -> list[tuple[str, str, str, str]]:
        allow = (
            self.plugin.proactive_allow_groups
            if kind == "group"
            else self.plugin.proactive_target_whitelist
        )
        items: list[tuple[str, str, str, str]] = []
        for raw_item in allow or ():
            parsed = self._parse_umo(str(raw_item))
            if parsed is not None and parsed[3] == kind:
                items.append(parsed)
        return items

    def _preferred_umo_from_allow(self, kind: str, platform_id: str, cid: str, default_umo: str) -> str:
        for plat, msg_type, target_id, _ in self._allow_umo_entries(kind):
            if plat == platform_id and target_id == cid:
                return f"{plat}:{msg_type}:{target_id}"
        return default_umo

    def person_allowed(self, user_id: str, platform_id: str = "", umo: str = "") -> bool:
        uid = str(user_id or "").strip()
        if not uid:
            return False
        allow = self.plugin.proactive_target_whitelist
        if uid in allow or uid in self.activity.admin_ids():
            return True
        if umo and umo in allow:
            return True
        if platform_id:
            if f"{platform_id}:FriendMessage:{uid}" in allow:
                return True
            for plat, _msg_type, target_id, _ in self._allow_umo_entries("private"):
                if plat == platform_id and target_id == uid:
                    return True
        return False

    def _whitelist_has_private_id(self, user_id: str) -> bool:
        uid = str(user_id or "").strip()
        if not uid:
            return False
        if uid in self.plugin.proactive_target_whitelist:
            return True
        return any(target_id == uid for _plat, _msg_type, target_id, _ in self._allow_umo_entries("private"))

    def group_allowed(self, group_id: str, group_name: str = "", platform_id: str = "", umo: str = "") -> bool:
        allow = self.plugin.proactive_allow_groups
        if not allow:
            return False
        gid = str(group_id or "").strip()
        gname = str(group_name or "").strip()
        if gid in allow or (bool(gname) and gname in allow):
            return True
        if umo and umo in allow:
            return True
        if platform_id and gid:
            if f"{platform_id}:GroupMessage:{gid}" in allow:
                return True
            for plat, _msg_type, target_id, _ in self._allow_umo_entries("group"):
                if plat == platform_id and target_id == gid:
                    return True
        return False

    def _matching_activity_groups(self, query: str) -> list[dict[str, Any]]:
        target = str(query or "").strip()
        if not target:
            return []
        sessions = list(self.activity.activity_sessions("group"))
        exact = [
            session
            for session in sessions
            if session["conversation_id"] == target or session["name"] == target
        ]
        if exact:
            return exact
        low = target.lower()
        return [
            session
            for session in sessions
            if low in session["name"].lower()
        ]

    def find_activity_group(self, query: str) -> dict[str, Any] | None:
        matches = self._matching_activity_groups(query)
        return matches[0] if matches else None

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

    def person_display_name(self, user_id: str, platform_id: str = "") -> str:
        uid = str(user_id or "").strip()
        if not uid:
            return ""
        pid = str(platform_id or "").strip()
        try:
            with self.activity.db.read_conn() as conn:
                if pid:
                    row = conn.execute(
                        """
                        SELECT sender_name FROM activity_messages
                        WHERE platform_id = ? AND sender_id = ? AND sender_name != ''
                        ORDER BY id DESC LIMIT 1
                        """,
                        (pid, uid),
                    ).fetchone()
                    if row:
                        return str(row[0] or "")
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

    def _match_allowed_group_session(self, value: str) -> tuple[str | None, str, dict[str, Any] | None, str]:
        value = str(value or "").strip()
        allow = self.plugin.proactive_allow_groups
        if not value or not allow:
            return None, "", None, ""
        umo_parsed = self._parse_umo(value)
        if umo_parsed is not None and umo_parsed[3] == "group":
            plat, msg_type, cid, _ = umo_parsed
            if plat == "nuoxiaomiao":
                return None, "", None, ""
            umo = f"{plat}:{msg_type}:{cid}"
            group_name = self._session_name_for(plat, "group", cid)
            if self.group_allowed(cid, group_name, platform_id=plat, umo=umo) or umo in allow:
                matched_session = next(
                    (
                        s
                        for s in self.activity.activity_sessions("group")
                        if s.get("platform_id") == plat and s.get("conversation_id") == cid
                    ),
                    None,
                )
                return cid, group_name, matched_session, umo
            return None, "", None, ""
        matches = self._matching_activity_groups(value)
        for session in matches:
            plat = str(session.get("platform_id") or "").strip()
            cid = str(session.get("conversation_id") or "").strip()
            name = str(session.get("name") or "").strip()
            sess_umo = str(session.get("umo") or "")
            if self.group_allowed(cid, name, platform_id=plat, umo=sess_umo):
                preferred_umo = self._preferred_umo_from_allow("group", plat, cid, sess_umo)
                return cid, name, session, preferred_umo
        if value in allow:
            session = matches[0] if matches else None
            if session:
                plat = str(session.get("platform_id") or "").strip()
                cid = str(session.get("conversation_id") or "").strip()
                sess_umo = str(session.get("umo") or "")
                preferred_umo = self._preferred_umo_from_allow("group", plat, cid, sess_umo)
                return cid, str(session.get("name") or ""), session, preferred_umo
            return value, "", None, ""
        umo_matches = [
            (plat, msg_type, cid)
            for plat, msg_type, cid, _ in self._allow_umo_entries("group")
            if cid == value and plat != "nuoxiaomiao"
        ]
        if len(umo_matches) == 1:
            plat, msg_type, cid = umo_matches[0]
            group_name = self._session_name_for(plat, "group", cid)
            return cid, group_name, None, f"{plat}:{msg_type}:{cid}"
        return None, "", None, ""

    def match_allowed_group(self, value: str) -> tuple[str | None, str]:
        group_id, group_name, _session, _umo = self._match_allowed_group_session(value)
        return group_id, group_name

    @staticmethod
    def _parse_umo(value: str) -> tuple[str, str, str, str] | None:
        text = str(value or "").strip()
        if ":" not in text:
            return None
        parts = text.split(":", 2)
        if len(parts) != 3:
            return None
        plat, msg_type, cid = (part.strip() for part in parts)
        if not plat or not msg_type or not cid:
            return None
        if any(ch.isspace() for ch in plat) or not _UMO_TYPE_RE.fullmatch(msg_type):
            return None
        low = msg_type.lower()
        if msg_type == "FriendMessage" or any(tok in low for tok in _PRIVATE_UMO_TOKENS):
            return plat, msg_type, cid, "private"
        if msg_type in ("GroupMessage", "OtherMessage") or any(tok in low for tok in _GROUP_UMO_TOKENS):
            return plat, msg_type, cid, "group"
        return None

    def _session_name_for(self, platform_id: str, kind: str, cid: str) -> str:
        try:
            sessions = self.activity.activity_sessions(kind)
            for session in sessions:
                if session.get("platform_id") == platform_id and session.get("conversation_id") == cid:
                    return str(session.get("name") or "")
            for session in sessions:
                if session.get("conversation_id") == cid:
                    return str(session.get("name") or "")
        except Exception:
            pass
        return ""

    def _resolve_target_platform(
        self,
        kind: str,
        target_id: str,
        fallback_platform: str,
        session: dict[str, Any] | None = None,
    ) -> str:
        if session:
            sess_pid = str(session.get("platform_id") or "").strip()
            if sess_pid and sess_pid != "nuoxiaomiao":
                return sess_pid
        try:
            for item in self.activity.activity_sessions(kind):
                pid = str(item.get("platform_id") or "").strip()
                if item.get("conversation_id") == target_id and pid and pid != "nuoxiaomiao":
                    return pid
        except Exception:
            pass
        if kind == "private":
            try:
                with self.activity.db.read_conn() as conn:
                    row = conn.execute(
                        """
                        SELECT platform_id FROM activity_messages
                        WHERE sender_id = ? AND sender_id != '' AND sender_id != 'bot'
                          AND platform_id != '' AND platform_id != 'nuoxiaomiao'
                        ORDER BY id DESC LIMIT 1
                        """,
                        (target_id,),
                    ).fetchone()
                if row and str(row[0] or "").strip():
                    return str(row[0]).strip()
            except Exception:
                pass
        self_wake = getattr(self.plugin, "self_wake", None)
        if self_wake is not None and callable(getattr(self_wake, "activity_platform_of", None)):
            try:
                pid = str(self_wake.activity_platform_of(kind, target_id) or "").strip()
                if pid and pid != "nuoxiaomiao":
                    return pid
            except Exception:
                pass
        allow_plats = {
            plat
            for plat, _msg_type, cid, _ in self._allow_umo_entries(kind)
            if cid == target_id and plat and plat != "nuoxiaomiao"
        }
        if len(allow_plats) == 1:
            return next(iter(allow_plats))
        fb = str(fallback_platform or "").strip()
        if fb and fb != "nuoxiaomiao":
            return fb
        inst_pids: list[str] = []
        for inst in self.activity.platform_insts() or []:
            pid = platform_id_of_inst(inst)
            if pid and pid != "nuoxiaomiao" and pid not in inst_pids:
                inst_pids.append(pid)
        if len(inst_pids) == 1:
            return inst_pids[0]
        return ""

    def event_is_admin(self, event: Any) -> bool:
        try:
            if event.is_admin():
                return True
        except Exception:
            pass
        get_extra = getattr(event, "get_extra", None)
        if callable(get_extra):
            try:
                if get_extra(SELF_WAKE_EVENT_EXTRA):
                    return True
            except Exception:
                pass
        if self._is_self_wake_session(event):
            return True
        get_sender = getattr(event, "get_sender_id", None)
        sender_id = str((get_sender() if callable(get_sender) else "") or "").strip()
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
        is_qq_off = self._is_qq_official_context(event)
        suffix = (
            "QQ 官方平台的用户/群标识是 openid（不是 QQ 号）；"
            "群主动消息还需要开启 qq_official_group_proactive 且平台允许。改好参数可以直接重试。"
            if is_qq_off
            else "群名的写法可调用 read_conversation_messages（不传 conversation）查看；改好参数可以直接重试。"
        )
        return {
            "hint": f"可发往的群：{group_text}；可主动私聊的人：{person_text}。{suffix}"
        }

    def _resolve_umo_target(
        self,
        event: Any,
        plat: str,
        msg_type: str,
        cid: str,
        umo_kind: str,
    ) -> dict[str, Any]:
        umo = f"{plat}:{msg_type}:{cid}"
        if plat == "nuoxiaomiao":
            return {
                "error": "当前处于独立自醒会话，请明确指定真实平台的发送目标（如 群:群名、私聊:账号 或真实平台 umo）。",
                **self.allowed_targets_hint(event),
            }
        if umo_kind == "group":
            group_name = self._session_name_for(plat, "group", cid)
            if not (self.group_allowed(cid, group_name, platform_id=plat, umo=umo) or umo in self.plugin.proactive_allow_groups):
                return {
                    "error": "这个群不在主人允许的主动消息名单里（proactive_allow_groups）。",
                    **self.allowed_targets_hint(event),
                }
            return {
                "platform_id": plat,
                "kind": "group",
                "id": cid,
                "name": group_name or cid,
                "umo": umo,
                "desc": f"群「{group_name or cid}」",
            }
        if not self.person_allowed(cid, platform_id=plat, umo=umo):
            return {
                "error": "主人没有允许主动私聊这个人（proactive_target_whitelist）。",
                **self.allowed_targets_hint(event),
            }
        name = self._session_name_for(plat, "private", cid) or self.person_display_name(cid, platform_id=plat)
        return {
            "platform_id": plat,
            "kind": "private",
            "id": cid,
            "name": name or cid,
            "umo": umo,
            "desc": f"私聊「{name or cid}」",
        }

    def resolve_target(self, event: Any, target: str) -> dict[str, Any]:
        raw = str(target or "").strip()
        is_self_wake = self._is_self_wake_session(event)
        platform_id = "" if is_self_wake else self._event_platform_id(event)
        if not raw or raw in {"当前", "current", "这里", "这边"}:
            if is_self_wake:
                return {
                    "error": "当前处于独立自醒会话，请明确指定发送目标（如 群:群名、私聊:账号 或完整 umo）。",
                    **self.allowed_targets_hint(event),
                }
            if event.is_private_chat():
                current = self.activity.current_conversation(event)
                sender_id = current[2] if current else ""
                if not sender_id:
                    return {"error": "拿不到当前会话信息。"}
                umo = str(getattr(event, "unified_msg_origin", "") or "")
                if not self.person_allowed(sender_id, platform_id=platform_id, umo=umo):
                    return {
                        "error": "主人没有允许主动私聊这个人（proactive_target_whitelist）。",
                        **self.allowed_targets_hint(event),
                    }
                return {
                    "platform_id": platform_id,
                    "kind": "private",
                    "id": sender_id,
                    "name": str(event.get_sender_name() or sender_id),
                    "umo": umo,
                    "desc": "当前私聊",
                }
            group_id = str(event.get_group_id() or "").strip()
            if not group_id:
                return {"error": "拿不到当前会话信息。"}
            group = getattr(getattr(event, "message_obj", None), "group", None)
            group_name = str(getattr(group, "group_name", "") or "").strip()
            umo = str(getattr(event, "unified_msg_origin", "") or "")
            if not self.group_allowed(group_id, group_name, platform_id=platform_id, umo=umo):
                return {
                    "error": "主人没有允许在这个群里发主动消息（可在插件配置 proactive_allow_groups 里添加）。",
                    **self.allowed_targets_hint(event),
                }
            return {
                "platform_id": platform_id,
                "kind": "group",
                "id": group_id,
                "name": group_name or group_id,
                "umo": umo,
                "desc": f"当前群「{group_name or group_id}」",
            }

        kind = ""
        value = raw
        for prefix in ("群:", "群：", "group:", "Group:"):
            if raw.startswith(prefix):
                kind = "group"
                value = raw[len(prefix) :].strip()
                break
        if not kind:
            for prefix in ("私聊:", "私聊：", "private:", "Private:"):
                if raw.startswith(prefix):
                    kind = "private"
                    value = raw[len(prefix) :].strip()
                    break

        if not kind:
            umo_parsed = self._parse_umo(raw)
            if umo_parsed is not None:
                return self._resolve_umo_target(event, *umo_parsed)
        else:
            umo_parsed = self._parse_umo(value)
            if umo_parsed is not None and umo_parsed[3] == kind:
                return self._resolve_umo_target(event, *umo_parsed)

        if kind == "group":
            group_id, group_name, session, matched_umo = self._match_allowed_group_session(value)
            if not group_id:
                return {
                    "error": "这个群不在主人允许的主动消息名单里（proactive_allow_groups）。",
                    **self.allowed_targets_hint(event),
                }
            target_platform = self._resolve_target_platform("group", group_id, platform_id, session=session)
            if not target_platform or target_platform == "nuoxiaomiao":
                return {
                    "error": "无法确定该群所在平台，请明确指定完整 umo（如 平台:GroupMessage:群号）。",
                    **self.allowed_targets_hint(event),
                }
            umo = matched_umo or self._preferred_umo_from_allow(
                "group", target_platform, group_id, f"{target_platform}:GroupMessage:{group_id}"
            )
            return {
                "platform_id": target_platform,
                "kind": "group",
                "id": group_id,
                "name": group_name or group_id,
                "umo": umo,
                "desc": f"群「{group_name or group_id}」",
            }

        if kind == "private":
            qq = ""
            name = ""
            qq_official_private = self._is_qq_official_context(event)
            if value.isdigit() and not qq_official_private:
                qq = value
                name = self.person_display_name(qq)
            else:
                qq, name = self.lookup_person_qq(value)
                if not qq and (
                    self.person_display_name(value)
                    or self._whitelist_has_private_id(value)
                    or value in self.activity.admin_ids()
                ):
                    qq, name = value, self.person_display_name(value) or value
                if not qq:
                    hint = "试试 私聊:openid 或对方昵称。" if qq_official_private else "试试 私聊:QQ号 这种写法。"
                    return {"error": f"找不到这个人，{hint}"}
            target_platform = self._resolve_target_platform("private", qq, platform_id)
            default_umo = f"{target_platform}:FriendMessage:{qq}" if target_platform else ""
            umo = (
                self._preferred_umo_from_allow("private", target_platform, qq, default_umo)
                if target_platform
                else ""
            )
            if not self.person_allowed(qq, platform_id=target_platform, umo=umo):
                return {
                    "error": "主人没有允许主动私聊这个人（proactive_target_whitelist）。",
                    **self.allowed_targets_hint(event),
                }
            if not target_platform or target_platform == "nuoxiaomiao":
                return {
                    "error": "无法确定该目标所在平台，请明确指定完整 umo（如 平台:FriendMessage:账号）。",
                    **self.allowed_targets_hint(event),
                }
            resolved_name = name or self.person_display_name(qq, platform_id=target_platform) or qq
            return {
                "platform_id": target_platform,
                "kind": "private",
                "id": qq,
                "name": resolved_name,
                "umo": umo,
                "desc": f"私聊「{resolved_name}」",
            }

        qq_official_platform = self._is_qq_official_context(event)
        group_id, group_name, session, matched_umo = self._match_allowed_group_session(raw)
        if group_id:
            target_platform = self._resolve_target_platform("group", group_id, platform_id, session=session)
            if not target_platform or target_platform == "nuoxiaomiao":
                return {
                    "error": "无法确定该群所在平台，请明确指定完整 umo（如 平台:GroupMessage:群号）。",
                    **self.allowed_targets_hint(event),
                }
            umo = matched_umo or self._preferred_umo_from_allow(
                "group", target_platform, group_id, f"{target_platform}:GroupMessage:{group_id}"
            )
            return {
                "platform_id": target_platform,
                "kind": "group",
                "id": group_id,
                "name": group_name or group_id,
                "umo": umo,
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
