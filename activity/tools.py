"""LLM tools for inspecting conversation lists, message history, and kept images."""

from __future__ import annotations

import json
import time
from typing import Any, Callable

from astrbot.api import logger

if __package__ and "." in __package__:
    from ..infra.constants import (
        ACTIVITY_PARTICIPANTS_WINDOW_SECONDS,
        ACTIVITY_SESSION_MATCH_LIMIT,
        DISPLAY_NAME,
    )
    from .service import ActivityService
else:
    from infra.constants import (
        ACTIVITY_PARTICIPANTS_WINDOW_SECONDS,
        ACTIVITY_SESSION_MATCH_LIMIT,
        DISPLAY_NAME,
    )
    from activity.service import ActivityService


class ActivityTools:
    """Handlers for list_conversations, read_conversation_messages, and read_image."""

    def __init__(
        self,
        plugin: Any,
        activity: ActivityService,
        resident_getter: Callable[[], Any],
    ) -> None:
        self.plugin = plugin
        self.activity = activity
        self._resident_getter = resident_getter

    def unavailable_error(self) -> str | None:
        p = self.plugin
        if not p.enabled:
            return json.dumps({"error": "插件当前已停用"}, ensure_ascii=False)
        if not p.activity_buffer_enabled or not p.activity_tool_enabled:
            return json.dumps({"error": "跨会话聊天查看功能已关闭"}, ensure_ascii=False)
        return None

    @staticmethod
    def denied_error() -> str:
        return json.dumps({"error": "只有管理员可以查看其它会话的聊天记录"}, ensure_ascii=False)

    async def list_conversations(self, event: Any, kind: str = "all", limit: int = 20) -> str:
        try:
            unavailable = self.unavailable_error()
            if unavailable:
                return unavailable
            if not self.activity.activity_permission_ok(event):
                return self.denied_error()
            wanted = str(kind or "all").strip().lower()
            if wanted not in {"all", "group", "private"}:
                wanted = "all"
            try:
                limit_value = max(1, min(50, int(limit)))
            except (TypeError, ValueError):
                limit_value = 20
            current = self.activity.current_conversation(event)
            recent_since = time.time() - ACTIVITY_PARTICIPANTS_WINDOW_SECONDS
            conversations: list[dict[str, Any]] = []
            resident = self._resident_getter()
            persona, _ = await resident.persona.resolve(event)
            is_wake = bool(event.get_extra("nuoxiaomiao_self_wake"))
            sessions = await self.activity.db.run(self.activity.activity_sessions, wanted)
            for session in sessions:
                if not resident.inbox.allowed(session, event):
                    continue
                key = (session["platform_id"], session["kind"], session["conversation_id"])
                if current is not None and key == current and not is_wake:
                    continue
                umo = f"{session['platform_id']}:{'GroupMessage' if session['kind'] == 'group' else 'FriendMessage'}:{session['conversation_id']}"
                conversations.append(
                    {
                        "kind": session["kind"],
                        "id": session["conversation_id"],
                        "name": self.activity.display_conversation_name(
                            session["platform_id"],
                            session["kind"],
                            session["conversation_id"],
                            session["name"],
                        ),
                        "platform": session["platform_id"],
                        "messages": session["messages"],
                        "umo": umo,
                        **resident.inbox.counts(persona, session, event),
                        "topic": resident.topic.session_topic(umo),
                        "recent_speakers": self.activity.activity_participants(
                            session["platform_id"],
                            session["kind"],
                            session["conversation_id"],
                            recent_since,
                        ),
                        "last_time": time.strftime("%Y-%m-%d %H:%M", time.localtime(session["last_ts"])),
                        "last_text": session["last_text"],
                    }
                )
                if len(conversations) >= limit_value:
                    break
            return json.dumps({"count": len(conversations), "conversations": conversations}, ensure_ascii=False)
        except Exception as exc:
            logger.warning("[%s] 列出会话失败: %s", DISPLAY_NAME, exc)
            return json.dumps({"error": "列出会话时出错"}, ensure_ascii=False)

    async def read_conversation_messages(
        self,
        event: Any,
        conversation: str = "",
        kind: str = "",
        after: str = "",
        sender: str = "",
        keyword: str = "",
        limit: int = 20,
        mode: str = "",
        before_id: int = 0,
        mark_read: bool = False,
    ) -> str:
        try:
            unavailable = self.unavailable_error()
            if unavailable:
                return unavailable
            if not self.activity.activity_permission_ok(event):
                return self.denied_error()
            resident = self._resident_getter()
            if mode or before_id or mark_read or (
                ":" in str(conversation)
                and (":GroupMessage:" in str(conversation) or ":FriendMessage:" in str(conversation))
            ):
                result = await resident.inbox.read(
                    event, conversation, kind, after, sender, keyword, limit, mode or "recent", before_id, mark_read
                )
                return json.dumps(result, ensure_ascii=False)

            conversation = str(conversation or "").strip()
            kind = str(kind or "").strip().lower()
            after = str(after or "").strip()
            sender = str(sender or "").strip()
            keyword = str(keyword or "").strip()
            if kind and kind not in {"group", "private"}:
                return json.dumps({"error": "kind 只能是 group 或 private"}, ensure_ascii=False)
            after_ts, time_error = self.activity.parse_activity_time(after)
            if time_error:
                return json.dumps({"error": time_error}, ensure_ascii=False)
            try:
                limit_value = max(1, min(50, int(limit)))
            except (TypeError, ValueError):
                limit_value = 20
            note = ""
            if conversation:
                sessions = self.activity.resolve_activity_conversations(conversation, kind)
                if not sessions:
                    return json.dumps(
                        {"error": "记录里找不到这个会话；可以调用 list_conversations 看看有哪些。"},
                        ensure_ascii=False,
                    )
            elif not any((after, sender, keyword)):
                current = self.activity.current_conversation(event)
                sessions = [
                    s
                    for s in self.activity.activity_sessions(kind or "all")
                    if current is None or (s["platform_id"], s["kind"], s["conversation_id"]) != current
                ][:ACTIVITY_SESSION_MATCH_LIMIT]
                note = "未指定筛选条件，已返回最近的记录；可用 conversation/sender/after/keyword 继续缩小范围。"
            else:
                sessions = None
            all_sessions = sessions if sessions is not None else self.activity.activity_sessions(kind or "all")
            allowed_sessions = [item for item in all_sessions if resident.inbox.allowed(item, event)]
            results = await self.activity.db.run(
                self.activity.query_activity_messages,
                allowed_sessions,
                kind if not conversation else "",
                after_ts,
                sender,
                keyword,
                limit_value,
            )
            payload: dict[str, Any] = {"count": len(results), "results": results}
            if note:
                payload["note"] = note
            if self.plugin.topic_summary_enabled and len(allowed_sessions) == 1:
                s = allowed_sessions[0]
                topic = resident.topic.session_topic(
                    f"{s['platform_id']}:{'GroupMessage' if s['kind'] == 'group' else 'FriendMessage'}:{s['conversation_id']}"
                )
                if topic:
                    payload["topic"] = topic
            return json.dumps(payload, ensure_ascii=False)
        except ValueError as exc:
            return json.dumps({"error": str(exc)}, ensure_ascii=False)
        except Exception as exc:
            logger.warning("[%s] 查看会话消息失败: %s", DISPLAY_NAME, exc)
            return json.dumps({"error": "查看会话消息时出错"}, ensure_ascii=False)

    async def read_image(
        self,
        event: Any,
        conversation: str = "",
        message_id: int = 0,
        index: int = 0,
        question: str = "",
    ) -> str:
        try:
            unavailable = self.unavailable_error()
            if unavailable:
                return unavailable
            if not self.activity.activity_permission_ok(event):
                return self.denied_error()
            if not self.plugin.vision_enabled:
                return json.dumps({"error": "群聊识图功能已关闭"}, ensure_ascii=False)
            conversation = str(conversation or "").strip()
            try:
                message_id_value = int(message_id)
            except (TypeError, ValueError):
                return json.dumps({"error": "message_id 必须是数字"}, ensure_ascii=False)
            if message_id_value <= 0:
                return json.dumps(
                    {"error": "message_id 缺失；先用 read_conversation_messages 找到带图消息的 id"},
                    ensure_ascii=False,
                )
            if not conversation:
                return json.dumps(
                    {"error": "conversation 缺失；先用 list_conversations 拿到会话 umo"},
                    ensure_ascii=False,
                )
            resident = self._resident_getter()
            row = resident.store.rows(
                "SELECT platform_id,kind,conversation_id,message_id FROM activity_messages WHERE id=?",
                (message_id_value,),
            )
            if not row or not row[0]["message_id"]:
                return json.dumps({"error": "找不到这条消息，或该消息没有可留底的图片"}, ensure_ascii=False)
            record = row[0]
            expected_umo = (
                f"{record['platform_id']}:{'GroupMessage' if record['kind'] == 'group' else 'FriendMessage'}:{record['conversation_id']}"
            )
            if expected_umo != conversation:
                return json.dumps({"error": "这条记录不属于你指定的会话，请用该会话自己的 id"}, ensure_ascii=False)
            answer = await resident.vision.read_image(
                conversation, record["message_id"], index, str(question or "")
            )
            return json.dumps({"description": answer}, ensure_ascii=False)
        except Exception as exc:
            logger.warning("[%s] 查看图片失败: %s", DISPLAY_NAME, exc)
            return json.dumps({"error": "查看图片时出错"}, ensure_ascii=False)
