"""全局主脑共享记忆：把聊天轮次的主脑历史切到唯一 master 会话。"""

from __future__ import annotations

import asyncio
import json

from astrbot.api import logger


class Master:
    """主脑会话解析、绑定与聊天串行锁。"""

    def __init__(self, service):
        self.service = service
        self.plugin = service.plugin
        self.host = service.host
        self.store = service.store
        self._brain_lock = asyncio.Lock()
        self._brain_owner: asyncio.Task | None = None
        self._auto_bind_lock = asyncio.Lock()

    async def resolve_master_umo(self) -> str:
        p = self.plugin
        if not p.shared_memory_enabled:
            return ""
        configured = str(getattr(p, "master_umo", "") or "").strip()
        if configured:
            return configured
        if not p.master_auto_bind:
            return ""
        return str(self.store.meta("master_auto_umo", "") or "").strip()

    async def maybe_auto_bind(self, event) -> None:
        p = self.plugin
        if not (p.shared_memory_enabled and p.master_auto_bind):
            return
        if str(getattr(p, "master_umo", "") or "").strip():
            return
        try:
            if not event.is_private_chat():
                return
        except Exception:
            return
        sender = str(event.get_sender_id() or "").strip()
        if not sender or sender not in self.host.admin_ids():
            return
        umo = str(event.unified_msg_origin or "").strip()
        if not umo:
            return
        async with self._auto_bind_lock:
            if self.store.meta("master_auto_umo", ""):
                return
            self.store.set_meta("master_auto_umo", umo)
            logger.info("[糯小喵] 已自动绑定全局主脑会话: %s", umo)

    async def acquire_brain(self) -> bool:
        """让当前任务取得主脑锁；已持有时复用，返回是否为新取得。"""
        task = asyncio.current_task()
        if self._brain_owner is task:
            return False
        if not await self.resolve_master_umo():
            return False
        acquired = False
        try:
            await self._brain_lock.acquire()
            acquired = True
            self._brain_owner = task
            return True
        except BaseException:
            if acquired and self._brain_owner is not task:
                try:
                    self._brain_lock.release()
                except RuntimeError:
                    pass
            raise

    def release_brain(self) -> None:
        task = asyncio.current_task()
        if self._brain_owner is not None and task is not None and self._brain_owner is not task:
            return
        if self._brain_owner is None and not self._brain_lock.locked():
            return
        self._brain_owner = None
        try:
            self._brain_lock.release()
        except RuntimeError:
            pass

    @staticmethod
    def _parse_history(conv) -> list:
        raw = getattr(conv, "history", "[]")
        if isinstance(raw, list):
            return list(raw)
        if isinstance(raw, str) and raw.strip():
            try:
                parsed = json.loads(raw)
                return parsed if isinstance(parsed, list) else []
            except Exception:
                return []
        return []

    async def bind_master(self, event, req) -> bool:
        master_umo = await self.resolve_master_umo()
        if not master_umo:
            return False
        try:
            if event.get_extra("nuoxiaomiao_no_brain", False):
                return False
        except Exception:
            pass
        if self.host.is_self_wake_event(event):
            return False
        conv_mgr = getattr(self.host.context, "conversation_manager", None)
        if conv_mgr is None:
            return False
        try:
            cid = await conv_mgr.get_curr_conversation_id(master_umo)
            if not cid:
                parts = str(master_umo).split(":", 2)
                platform_id = parts[0] if len(parts) == 3 else None
                cid = await conv_mgr.new_conversation(master_umo, platform_id=platform_id)
            master_conv = await conv_mgr.get_conversation(
                master_umo, cid, create_if_not_exists=True
            )
            if master_conv is None:
                return False
            req.conversation = master_conv
            req.contexts = self._parse_history(master_conv)
            return True
        except Exception as exc:
            logger.debug("[糯小喵] 绑定主脑历史跳过: %s", exc)
            return False

    def describe(self) -> str:
        p = self.plugin
        if not p.shared_memory_enabled:
            return "已关闭"
        configured = str(getattr(p, "master_umo", "") or "").strip()
        if configured:
            return f"显式配置：{configured}"
        auto = str(self.store.meta("master_auto_umo", "") or "").strip()
        return f"自动绑定：{auto or '（尚未绑定，等管理员私聊）'}"
