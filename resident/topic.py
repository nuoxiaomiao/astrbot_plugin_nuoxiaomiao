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

from .autonomy import CURRENT_RUN

# 模型输出里夹带 JSON 时的最大扫描长度（防无限长回复拖慢解析）。
TOPIC_PARSE_MAX_TEXT = 8000
# 别名表：不认识的人用「甲乙丙…」代替，不暴露裸 ID。天干 + 地支 + 生肖
# 共 34 个不同代号，远超常见会话的陌生人数；再多就退化成「匿名N」。
TOPIC_ALIASES = (
    "甲", "乙", "丙", "丁", "戊", "己", "庚", "辛", "壬", "癸",  # 天干
    "子", "丑", "寅", "卯", "辰", "巳", "午", "未", "申", "酉", "戌", "亥",  # 地支
    "鼠", "牛", "虎", "兔", "龙", "蛇", "马", "羊", "猴", "鸡", "狗", "猪",  # 生肖
)
# 失败退避：失败后这段时间内不再触发概括，避免每条未读消息都重试一次模型。
TOPIC_FAILURE_COOLDOWN_SECONDS = 60.0
# 同一原因的告警最小间隔：模型不可用可能持续很久，别把日志刷满。
TOPIC_WARNING_INTERVAL_SECONDS = 300.0


class Topic:
    """话题概括的调度、模型调用与分段落库。"""

    def __init__(self, service):
        self.service = service
        self.plugin = service.plugin
        self.store = service.store
        # 每个会话一把概括锁：避免同一会话的两条消息同时触发两轮概括。
        self._locks: dict[str, asyncio.Lock] = {}
        # 失败退避：umo -> 最近一次失败时刻。
        self._failed_at: dict[str, float] = {}
        # 正在概括的会话：并发触发时只跑一轮。
        self._in_flight: set[str] = set()
        # 告警限频：告警键 -> 最近一次告警时刻。
        self._warned_at: dict[str, float] = {}

    def _lock(self, umo: str) -> asyncio.Lock:
        return self._locks.setdefault(umo, asyncio.Lock())

    def _warn_throttled(self, key: str, message: str, *args) -> None:
        """同一原因限频告警：生产上失败要看得见，但别把日志刷满。"""
        now = time.time()
        if now - self._warned_at.get(key, 0.0) < TOPIC_WARNING_INTERVAL_SECONDS:
            return
        self._warned_at[key] = now
        logger.warning(message, *args)

    def _mark_failed(self, umo: str) -> None:
        self._failed_at[umo] = time.time()

    # ------------------------------------------------------------------
    # 对外调度
    # ------------------------------------------------------------------

    async def maybe_summarize(self, umo: str) -> bool:
        """满足开关、冷却与最小条数时触发一轮概括；已开跑或条件不足返回 False。"""
        p = self.plugin
        if not p.topic_summary_enabled or not umo:
            return False
        now = time.time()
        # 上次失败先退避：否则繁忙群每条消息都会重试一次模型。
        if self._failed_at.get(umo, 0.0) + TOPIC_FAILURE_COOLDOWN_SECONDS > now:
            return False
        if umo in self._in_flight:
            return False
        state = self.store.topic_state(umo)
        if state["topic_updated_at"] + p.topic_cooldown_seconds > now:
            return False
        pending = self._pending_count(umo, state["topic_to_id"])
        if pending < p.topic_min_unread:
            return False
        self._in_flight.add(umo)
        self._spawn(umo)
        return True

    def _spawn(self, umo: str) -> None:
        task = asyncio.create_task(self._run_guarded(umo))
        self.plugin._resident_bg_tasks.add(task)
        task.add_done_callback(self.plugin._resident_bg_tasks.discard)

    async def _run_guarded(self, umo: str) -> None:
        try:
            async with self._lock(umo):
                # 排队等锁期间可能已有别的任务概括完：拿锁后复查冷却，避免重复跑。
                state = self.store.topic_state(umo)
                if (
                    state["topic_updated_at"] + self.plugin.topic_cooldown_seconds
                    > time.time()
                ):
                    return
                await self.summarize(umo)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._warn_throttled(
                f"topic-run:{umo}", "[糯小喵] 话题概括失败: umo=%s err=%s", umo, exc
            )
        finally:
            self._in_flight.discard(umo)

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
            "AND sender_id!='bot' "
            "ORDER BY id LIMIT ?",
            (*parts, int(cursor), int(limit)),
        )

    def _fetch_latest(self, umo: str, cursor: int, limit: int) -> list[dict]:
        """取最近一段未读（积压时跳读）：先倒序取最近 N 条再反转成正序。"""
        parts = self._umo_parts(umo)
        if not parts[0]:
            return []
        rows = self.store.rows(
            "SELECT id,ts,text,sender_id,sender_name,mentioned FROM activity_messages "
            "WHERE platform_id=? AND kind=? AND conversation_id=? AND id>? "
            "AND sender_id!='bot' "
            "ORDER BY id DESC LIMIT ?",
            (*parts, int(cursor), int(limit)),
        )
        rows.reverse()
        return rows

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
                    if len(alias_of) >= len(TOPIC_ALIASES):
                        # 代号用完不再回绕，改用匿名编号（审查 M12）。
                        alias_of[sender_id] = f"匿名{len(alias_of) + 1}"
                    else:
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
        # 补段是段末之后更新的消息，方向是「后续」而不是「往前补」。
        head = f"（以上共 {len(lines)} 条"
        if backfill > 0:
            head += f"，后 {backfill} 条为衔接用的后续消息"
        head += "）"
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

    async def _provider_by_id(self, manager, provider_id: str):
        """按 ID 取模型；取不到返回 None（异常只记一条限频告警）。"""
        get_by_id = getattr(manager, "get_provider_by_id", None)
        if not callable(get_by_id):
            return None
        try:
            return await get_by_id(provider_id)
        except Exception as exc:
            self._warn_throttled(
                f"topic-provider-err:{provider_id}",
                "[糯小喵] 话题概括模型 %s 获取异常: %s",
                provider_id,
                exc,
            )
            return None

    async def _session_default_provider(self, manager, umo: str):
        """会话默认模型：优先核心异步接口，旧核心退回同步接口。"""
        try:
            from astrbot.core.provider.entities import ProviderType
        except Exception:
            return None
        get_async = getattr(manager, "get_using_provider_async", None)
        if callable(get_async):
            try:
                # 核心签名是 get_using_provider_async(provider_type, umo=None)，
                # provider_type 必填；漏传会抛 TypeError 被吞成「没有模型」。
                return await get_async(ProviderType.CHAT_COMPLETION, umo=umo or None)
            except TypeError:
                # 早期核心不接受 umo 关键字：退化为只传类型。
                try:
                    return await get_async(ProviderType.CHAT_COMPLETION)
                except Exception as exc:
                    self._warn_throttled(
                        "topic-default-provider",
                        "[糯小喵] 话题概括回退默认模型失败: %s",
                        exc,
                    )
                    return None
            except Exception as exc:
                self._warn_throttled(
                    "topic-default-provider",
                    "[糯小喵] 话题概括回退默认模型失败: %s",
                    exc,
                )
                return None
        get_sync = getattr(manager, "get_using_provider", None)
        if not callable(get_sync):
            return None
        try:
            return get_sync(ProviderType.CHAT_COMPLETION)
        except Exception as exc:
            self._warn_throttled(
                "topic-default-provider", "[糯小喵] 话题概括回退默认模型失败: %s", exc
            )
            return None

    async def resolve_provider(self, umo: str = ""):
        """解析话题模型：显式指定就只用它；未指定才回退会话默认模型。"""
        manager = getattr(self.plugin.context, "provider_manager", None)
        if manager is None:
            return None
        light_id = str(getattr(self.plugin, "topic_light_provider_id", "") or "").strip()
        if light_id:
            provider = await self._provider_by_id(manager, light_id)
            if provider is not None and callable(getattr(provider, "text_chat", None)):
                return provider
            # 明确指定了模型却拿不到：本轮跳过而不是悄悄换成别的模型。
            self._warn_throttled(
                f"topic-provider:{light_id}",
                "[糯小喵] 话题概括模型 %s 不可用，本轮跳过（topic_light_provider_id）",
                light_id,
            )
            return None
        return await self._session_default_provider(manager, umo)

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
        segment = p.topic_segment_msg_count
        pending = self._pending_count(umo, cursor)
        backlog = pending > 2 * segment
        if backlog:
            # 积压太多时不从最旧追：直接看最新一段，否则话题永远落后于群现状。
            rows = self._fetch_latest(
                umo, cursor, segment + p.topic_segment_backfill_max
            )
            logger.info(
                "[糯小喵] 话题概括积压 %d 条，跳到最新 %d 条: umo=%s",
                pending,
                len(rows),
                umo,
            )
        else:
            rows = self._fetch_pending(
                umo, cursor, segment + p.topic_segment_backfill_max
            )
        if len(rows) < p.topic_min_unread:
            self._mark_failed(umo)
            return False
        backfill = max(0, len(rows) - segment)
        # 材料带上补段（段末之后更近的消息）；游标仍只推进到本段末行。
        window = rows if backlog else rows[:segment]
        material = self._build_material(umo, rows, backfill)
        if not material:
            self._mark_failed(umo)
            return False
        provider = await self.resolve_provider(umo)
        if provider is None or not callable(getattr(provider, "text_chat", None)):
            self._mark_failed(umo)
            self._warn_throttled(
                "topic-no-provider", "[糯小喵] 话题概括跳过：没有可用模型"
            )
            return False
        # 自主运行进行中时，背景概括同样计入本轮 token 预算（审查 H12）。
        if CURRENT_RUN.get() is not None:
            provider = self.service.autonomy.wrap_provider(provider)
        try:
            resp = await asyncio.wait_for(
                provider.text_chat(prompt=self._build_prompt(material)),
                timeout=p.topic_timeout_seconds,
            )
            raw = str(getattr(resp, "completion_text", "") or "").strip()
        except asyncio.TimeoutError:
            self._mark_failed(umo)
            self._warn_throttled(
                f"topic-timeout:{umo}", "[糯小喵] 话题概括超时: umo=%s", umo
            )
            return False
        except Exception as exc:
            self._mark_failed(umo)
            self._warn_throttled(
                f"topic-call:{umo}",
                "[糯小喵] 话题概括调用失败: umo=%s err=%s",
                umo,
                exc,
            )
            return False
        topic, summary = self._parse_topic(raw, p.topic_max_chars)
        if not topic and not summary:
            self._mark_failed(umo)
            self._warn_throttled(
                f"topic-parse:{umo}", "[糯小喵] 话题概括输出无法解析: umo=%s", umo
            )
            return False
        if not summary:
            summary = topic
        now = time.time()
        first = window[0]
        last = window[-1]
        self.store.append_topic_segment(
            umo,
            int(first["id"]),
            int(last["id"]),
            float(first["ts"]),
            float(last["ts"]),
            summary,
        )
        self.store.set_topic_state(umo, topic, int(last["id"]), now)
        self._failed_at.pop(umo, None)
        logger.info(
            "[糯小喵] 话题概括完成: umo=%s rows=%d topic=%s",
            umo,
            len(rows),
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
