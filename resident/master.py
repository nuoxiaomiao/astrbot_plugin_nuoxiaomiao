"""全局主脑共享记忆：把聊天轮次的主脑历史切到唯一 master 会话。

行为参考：astrbot_plugin_astrmia（Kalo & Qingyi）公开文档描述的能力——所有
会话绑定同一个"主脑"会话，跨群/跨私聊连续记忆；并发聊天串行持锁防丢历史。
本模块是独立实现：绑定时机、锁的生命周期与降级路径均为本插件原创。
"""

from __future__ import annotations

import asyncio
import json

from astrbot.api import logger


class Master:
    """主脑会话解析、绑定与聊天串行锁。"""

    def __init__(self, service):
        self.service = service
        self.plugin = service.plugin
        self.store = service.store
        # 聊天回合级串行锁：同一时刻只有一个回合持有主脑历史，防并发写丢。
        self._brain_lock = asyncio.Lock()
        self._brain_owner: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # 主脑会话解析
    # ------------------------------------------------------------------

    async def resolve_master_umo(self) -> str:
        """解析主脑会话：显式配置优先，其次自动绑定值；未启用返回空串。"""
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
        """管理员私聊消息首次出现时自动把该会话绑定为主脑并持久化。"""
        p = self.plugin
        if not (p.shared_memory_enabled and p.master_auto_bind):
            return
        if str(getattr(p, "master_umo", "") or "").strip():
            return
        if self.store.meta("master_auto_umo", ""):
            return
        try:
            if not event.is_private_chat():
                return
        except Exception:
            return
        sender = str(event.get_sender_id() or "").strip()
        if not sender or sender not in p._admin_ids():
            return
        umo = str(event.unified_msg_origin or "").strip()
        if not umo:
            return
        self.store.set_meta("master_auto_umo", umo)
        logger.info("[糯小喵] 已自动绑定全局主脑会话: %s", umo)

    # ------------------------------------------------------------------
    # 主脑锁
    # ------------------------------------------------------------------

    async def acquire_brain(self) -> bool:
        """让当前任务取得主脑锁；已持有时复用，返回是否为新取得。"""
        task = asyncio.current_task()
        if self._brain_owner is task:
            return False
        await self._brain_lock.acquire()
        self._brain_owner = task
        return True

    def release_brain(self) -> None:
        self._brain_owner = None
        try:
            self._brain_lock.release()
        except RuntimeError:
            pass

    # ------------------------------------------------------------------
    # 请求绑定
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_history(conv) -> list:
        """反序列化会话历史，兼容字符串与列表两种存储形式。"""
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
        """把请求的历史切到主脑会话；失败或不在适用范围时保持原样返回 False。"""
        master_umo = await self.resolve_master_umo()
        if not master_umo:
            return False
        if self.plugin._is_self_wake_event(event):
            # 自醒轮保留自己的旧历史供思考，不抢占主脑上下文。
            return False
        conv_mgr = getattr(self.plugin.context, "conversation_manager", None)
        if conv_mgr is None:
            return False
        try:
            cid = await conv_mgr.get_curr_conversation_id(master_umo)
            if not cid:
                parts = str(master_umo).split(":", 2)
                platform_id = parts[0] if len(parts) == 3 else None
                cid = await conv_mgr.new_conversation(
                    master_umo, platform_id=platform_id
                )
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
        """状态页用的主脑说明。"""
        p = self.plugin
        if not p.shared_memory_enabled:
            return "已关闭"
        configured = str(getattr(p, "master_umo", "") or "").strip()
        if configured:
            return f"显式配置：{configured}"
        auto = str(self.store.meta("master_auto_umo", "") or "").strip()
        return f"自动绑定：{auto or '（尚未绑定，等管理员私聊）'}"
