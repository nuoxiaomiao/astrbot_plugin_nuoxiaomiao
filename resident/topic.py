"""话题概括：把一段未读群聊压成一句话题并落分段，供会话清单与点名通知使用。

行为参考：astrbot_plugin_astrmia（Kalo & Qingyi）公开文档描述的能力——群聊
攒够一段后由轻量模型概括成一句话话题，模型不必翻完长记录就能知道"那边在聊
什么"。本模块是独立实现：分段、节流、别名匿名与提示词均为本插件原创。
"""

from __future__ import annotations

import asyncio
import json
import time

from astrbot.api import logger

# 模型输出里夹带 JSON 时的最大扫描长度（防无限长回复拖慢解析）。
TOPIC_PARSE_MAX_TEXT = 8000
# 别名表：不认识的人用「甲乙丙…」代替，不暴露裸 ID。
TOPIC_ALIASES = ("甲", "乙", "丙", "丁", "戊", "己", "庚", "辛", "壬", "癸")


class Topic:
    """话题概括的调度、模型调用与分段落库。"""

    def __init__(self, service):
        self.service = service
        self.plugin = service.plugin
        self.store = service.store
        # 每个会话一把概括锁：避免同一会话的两条消息同时触发两轮概括。
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock(self, umo: str) -> asyncio.Lock:
        return self._locks.setdefault(umo, asyncio.Lock())

    # ------------------------------------------------------------------
    # 对外调度
    # ------------------------------------------------------------------

    async def maybe_summarize(self, umo: str) -> bool:
        """满足开关、冷却与最小条数时触发一轮概括；已开跑或条件不足返回 False。"""
        p = self.plugin
        if not p.topic_summary_enabled or not umo:
            return False
        state = self.store.topic_state(umo)
        if state["topic_updated_at"] + p.topic_cooldown_seconds > time.time():
            return False
        pending = self._pending_count(umo, state["topic_to_id"])
        if pending < p.topic_min_unread:
            return False
        self._spawn(umo)
        return True

    def _spawn(self, umo: str) -> None:
        task = asyncio.create_task(self._run_guarded(umo))
        self.plugin._resident_bg_tasks.add(task)
        task.add_done_callback(self.plugin._resident_bg_tasks.discard)

    async def _run_guarded(self, umo: str) -> None:
        async with self._lock(umo):
            try:
                await self.summarize(umo)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug("[糯小喵] 话题概括失败: %s", exc)

    # ------------------------------------------------------------------
    # 数据查询
    # ------------------------------------------------------------------

    def topic(self, umo: str) -> str:
        """当前会话的最新话题文本（没有则空串）。"""
        return self.store.topic_state(umo)["topic"]

    def _pending_count(self, umo: str, cursor: int) -> int:
        rows = self.store.rows(
            "SELECT COUNT(*) AS n FROM activity_messages WHERE "
            "platform_id=? AND kind=? AND conversation_id=? AND id>? AND sender_id!='bot'",
            (*self._umo_parts(umo), int(cursor)),
        )
        return rows[0]["n"] if rows else 0

    @staticmethod
    def _umo_parts(umo: str) -> tuple[str, str, str]:
        """把 umo 拆成 (platform_id, kind, conversation_id)；拆不开返回空组。"""
        pieces = str(umo).split(":", 2)
        if len(pieces) != 3 or pieces[1] not in ("GroupMessage", "FriendMessage"):
            return ("", "", "")
        return (pieces[0], "group" if pieces[1] == "GroupMessage" else "private", pieces[2])

    def _fetch_pending(self, umo: str, cursor: int, limit: int) -> list[dict]:
        parts = self._umo_parts(umo)
        if not parts[0]:
            return []
        return self.store.rows(
            "SELECT id,ts,text,sender_id,sender_name,mentioned FROM activity_messages "
            "WHERE platform_id=? AND kind=? AND conversation_id=? AND id>? "
            "ORDER BY id LIMIT ?",
            (*parts, int(cursor), int(limit)),
        )

    # ------------------------------------------------------------------
    # 材料与提示词
    # ------------------------------------------------------------------

    def _build_material(self, umo: str, rows: list[dict], backfill: int) -> str:
        """把消息行拼成模型输入；有昵称的用昵称，陌生人不暴露裸 ID。"""
        alias_of: dict[str, str] = {}
        lines: list[str] = []
        for row in rows:
            sender_id = str(row["sender_id"] or "")
            name = str(row["sender_name"] or "").strip()
            if sender_id in {"bot", "糯小喵"} or name == "糯小喵":
                label = "机器人自己"
            elif name and name != sender_id:
                label = name
            else:
                if sender_id not in alias_of:
                    alias_of[sender_id] = TOPIC_ALIASES[
                        len(alias_of) % len(TOPIC_ALIASES)
                    ]
                label = alias_of[sender_id]
            text = str(row["text"] or "").replace("\n", " ").strip()
            if not text:
                continue
            if row.get("mentioned"):
                text = f"(@了机器人) {text}"
            lines.append(f"{label}：{text[: self.plugin.topic_line_max_chars]}")
        if not lines:
            return ""
        head = f"（以上是最近 {len(lines)} 条未读消息，{backfill} 条为往前补的上下文）"
        return "\n".join([head, *lines])

    def _build_prompt(self, material: str) -> str:
        max_chars = self.plugin.topic_max_chars
        return (
            "下面是一段群里（或私聊里）最近的未读消息，说话人已用昵称或甲乙丙等"
            f"代号代替。请用一句不超过 {max_chars} 个字的话概括这段对话「正在聊什么」，"
            "再给一句不超过 60 个字的简要总结。只输出 JSON："
            '{"topic":"正在聊什么的一句话","summary":"简要总结"}\n'
            "不要解释，不要输出 JSON 以外的内容。\n"
            f"消息如下：\n{material}"
        )

    @staticmethod
    def _parse_topic(text: str, max_chars: int) -> tuple[str, str]:
        """从模型输出里抠 JSON，返回 (topic, summary)；抠不出返回空对。"""
        raw = str(text or "").strip()
        if not raw:
            return "", ""
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end <= start:
            return "", ""
        chunk = raw[start : end + 1][:TOPIC_PARSE_MAX_TEXT]
        try:
            data = json.loads(chunk)
        except Exception:
            return "", ""
        if not isinstance(data, dict):
            return "", ""
        topic = str(data.get("topic") or "").strip()
        summary = str(data.get("summary") or "").strip()
        return topic[:max_chars], summary[:200]

    # ------------------------------------------------------------------
    # 模型调用
    # ------------------------------------------------------------------

    async def resolve_provider(self, umo: str = ""):
        """解析话题模型：配置指定 → 会话默认模型；拿不到返回 None。"""
        manager = getattr(self.plugin.context, "provider_manager", None)
        if manager is None:
            return None
        light_id = str(getattr(self.plugin, "topic_light_provider_id", "") or "").strip()
        if light_id:
            try:
                provider = await manager.get_provider_by_id(light_id)
            except Exception:
                provider = None
            if provider is not None and callable(getattr(provider, "text_chat", None)):
                return provider
        try:
            get_async = getattr(manager, "get_using_provider_async", None)
            if callable(get_async):
                provider = await get_async(umo=umo or None)
            else:
                from astrbot.core.provider.entities import ProviderType

                provider = manager.get_using_provider(ProviderType.CHAT_COMPLETION)
        except Exception:
            provider = None
        if provider is not None and callable(getattr(provider, "text_chat", None)):
            return provider
        return None

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------

    async def summarize(self, umo: str) -> bool:
        """对一段未读消息跑一次概括并落库；条件不满足或失败返回 False。"""
        p = self.plugin
        if not p.topic_summary_enabled:
            return False
        state = self.store.topic_state(umo)
        cursor = state["topic_to_id"]
        backfill_max = p.topic_segment_backfill_max
        rows = self._fetch_pending(umo, cursor, p.topic_segment_msg_count + backfill_max)
        if len(rows) < p.topic_min_unread:
            return False
        backfill = max(0, len(rows) - p.topic_segment_msg_count)
        material_rows = rows[: p.topic_segment_msg_count]
        material = self._build_material(umo, material_rows, backfill)
        if not material:
            return False
        provider = await self.resolve_provider(umo)
        if provider is None:
            logger.warning("[糯小喵] 话题概括跳过：没有可用模型")
            return False
        try:
            resp = await asyncio.wait_for(
                provider.text_chat(prompt=self._build_prompt(material)),
                timeout=p.topic_timeout_seconds,
            )
            raw = str(getattr(resp, "completion_text", "") or "").strip()
        except asyncio.TimeoutError:
            logger.debug("[糯小喵] 话题概括超时")
            return False
        except Exception as exc:
            logger.debug("[糯小喵] 话题概括调用失败: %s", exc)
            return False
        topic, summary = self._parse_topic(raw, p.topic_max_chars)
        if not topic and not summary:
            return False
        if not summary:
            summary = topic
        now = time.time()
        first = material_rows[0]
        last = material_rows[-1]
        self.store.append_topic_segment(
            umo,
            int(first["id"]),
            int(last["id"]),
            float(first["ts"]),
            float(last["ts"]),
            summary,
        )
        self.store.set_topic_state(umo, topic, int(last["id"]), now)
        logger.info(
            "[糯小喵] 话题概括完成: umo=%s rows=%d topic=%s",
            umo,
            len(material_rows),
            topic,
        )
        return True

    # ------------------------------------------------------------------
    # 会话清单注入
    # ------------------------------------------------------------------

    def session_topic(self, umo: str) -> str:
        """给 list_conversations 用的简短话题；空会话返回空串。"""
        try:
            return self.topic(umo)[: self.plugin.topic_max_chars]
        except Exception:
            return ""

    def describe(self) -> str:
        """状态页用的概括器说明（模型来源与最近一次运行信息）。"""
        light_id = str(getattr(self.plugin, "topic_light_provider_id", "") or "").strip()
        return f"模型：{light_id or '会话默认模型'}"
