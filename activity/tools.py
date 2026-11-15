"""LLM tool handler for unified cross-conversation activity inspection."""

from __future__ import annotations

import json
from typing import Any, Callable

from astrbot.api import logger

if __package__ and "." in __package__:
    from ..infra.constants import DISPLAY_NAME
    from .service import ActivityService
else:
    from infra.constants import DISPLAY_NAME
    from activity.service import ActivityService


class ActivityTools:
    """Handler for read_conversation_messages."""

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

    async def read_conversation_messages(
        self,
        event: Any,
        conversation: str = "",
        kind: str = "",
        before_id: int = 0,
        after: str = "",
        sender: str = "",
        keyword: str = "",
        only_unread: bool = False,
        limit: int = 20,
    ) -> str:
        try:
            unavailable = self.unavailable_error()
            if unavailable:
                return unavailable
            if not self.activity.activity_permission_ok(event):
                return self.denied_error()
            resident = self._resident_getter()
            result = await resident.inbox.read(
                event,
                conversation=conversation,
                kind=kind,
                before_id=before_id,
                after=after,
                sender=sender,
                keyword=keyword,
                only_unread=only_unread,
                limit=limit,
            )
            return json.dumps(result, ensure_ascii=False)
        except ValueError as exc:
            return json.dumps({"error": str(exc)}, ensure_ascii=False)
        except Exception as exc:
            logger.warning("[%s] 查看会话消息失败: %s", DISPLAY_NAME, exc)
            return json.dumps({"error": "查看会话消息时出错"}, ensure_ascii=False)
