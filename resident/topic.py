"""话题概括：把一段未读群聊压成一句话题并落分段，供会话清单与点名通知使用。"""

from __future__ import annotations

import asyncio
import contextvars
import json
import time

from astrbot.api import logger

if __package__ and "." in __package__:
    from ..infra.constants import DISPLAY_NAME, UNTRUSTED_CLOSE, UNTRUSTED_NOTE, UNTRUSTED_OPEN
    from ..infra.providers import ProviderResolver
    from ..infra.runtime import BoundedTTLCache, RateLimitedLogger
    from .autonomy_budget import CURRENT_RUN
else:
    from infra.constants import DISPLAY_NAME, UNTRUSTED_CLOSE, UNTRUSTED_NOTE, UNTRUSTED_OPEN
    from infra.providers import ProviderResolver
    from infra.runtime import BoundedTTLCache, RateLimitedLogger
    from resident.autonomy_budget import CURRENT_RUN

TOPIC_PARSE_MAX_TEXT = 8000
TOPIC_ALIASES = (
    "甲", "乙", "丙", "丁", "戊", "己", "庚", "辛", "壬", "癸",
    "子", "丑", "寅", "卯", "辰", "巳", "午", "未", "申", "酉", "戌", "亥",
    "鼠", "牛", "虎", "兔", "龙", "蛇", "马", "羊", "猴", "鸡", "狗", "猪",
)
TOPIC_FAILURE_COOLDOWN_SECONDS = 60.0
TOPIC_WARN_INTERVAL_SECONDS = 60.0


class Topic:
    """事件驱动的话题概括器：满足阈值才在后台调用一次轻量模型。"""

    def __init__(self, service):
        self.service = service
        self.plugin = service.plugin
        self.host = service.host
        self.store = service.store
        self._locks: dict[str, asyncio.Lock] = {}
        self._in_flight: set[str] = set()
        self._failed_at = BoundedTTLCache(max_size=512, ttl_seconds=600.0)
        self._rate_log = RateLimitedLogger(interval_seconds=TOPIC_WARN_INTERVAL_SECONDS)

    def _warn_throttled(self, key: str, message: str, *args) -> None:
        self._rate_log.warning(key, message, *args)

    def _mark_failed(self, umo: str) -> None:
        self._failed_at.set(umo, time.monotonic())

    @staticmethod
    def _split_umo(umo: str) -> tuple[str, str, str] | None:
        parts = str(umo or "").split(":", 2)
        if len(parts) != 3:
            return None
        platform, typ, cid = parts
        kind = "group" if typ == "GroupMessage" else "private"
        return platform, kind, cid

    def topic(self, umo: str) -> str:
        return str(self.store.topic_state(umo)["topic"] or "")

    def segments(self, umo: str, limit: int = 3) -> list[dict]:
        return self.store.topic_segments(umo, limit)

    def _pending_count(self, umo: str, cursor_id: int) -> int:
        parsed = self._split_umo(umo)
        if parsed is None:
            return 0
        platform, kind, cid = parsed
        rows = self.store.rows(
            "SELECT COUNT(*) AS cnt FROM activity_messages "
            "WHERE platform_id=? AND kind=? AND conversation_id=? AND id>?",
            (platform, kind, cid, int(cursor_id or 0)),
        )
        return int(rows[0]["cnt"]) if rows else 0

    def _fetch_pending(self, umo: str, cursor_id: int, limit: int) -> list[dict]:
        parsed = self._split_umo(umo)
        if parsed is None:
            return []
        platform, kind, cid = parsed
        return self.store.rows(
            "SELECT id,sender_id,sender_name,text,ts,mentioned "
            "FROM activity_messages "
            "WHERE platform_id=? AND kind=? AND conversation_id=? AND id>? "
            "ORDER BY id ASC LIMIT ?",
            (platform, kind, cid, int(cursor_id or 0), max(1, int(limit))),
        )

    def _fetch_latest(self, umo: str, cursor_id: int, limit: int) -> list[dict]:
        parsed = self._split_umo(umo)
        if parsed is None:
            return []
        platform, kind, cid = parsed
        rows = self.store.rows(
            "SELECT id,sender_id,sender_name,text,ts,mentioned "
            "FROM activity_messages "
            "WHERE platform_id=? AND kind=? AND conversation_id=? AND id>? "
            "ORDER BY id DESC LIMIT ?",
            (platform, kind, cid, int(cursor_id or 0), max(1, int(limit))),
        )
        return list(reversed(rows))

    def needs_refresh(self, umo: str, *, ignore_in_flight: bool = False) -> bool:
        p = self.plugin
        if not p.topic_summary_enabled:
            return False
        if not ignore_in_flight and umo in self._in_flight:
            return False
        now_mono = time.monotonic()
        last_fail = self._failed_at.get(umo)
        if last_fail is not None and now_mono - float(last_fail) < TOPIC_FAILURE_COOLDOWN_SECONDS:
            return False
        state = self.store.topic_state(umo)
        if time.time() - state["topic_updated_at"] < p.topic_cooldown_seconds:
            return False
        pending = self._pending_count(umo, state["topic_to_id"])
        return pending >= p.topic_min_unread

    async def maybe_summarize(self, umo: str) -> bool:
        if not umo or not self.needs_refresh(umo):
            return False
        self._spawn(umo)
        return True

    def _spawn(self, umo: str) -> None:
        if umo in self._in_flight:
            return
        self._in_flight.add(umo)
        # Clear CURRENT_RUN inside background task copy so foreground run context does not leak.
        ctx = contextvars.copy_context()

        def _create():
            CURRENT_RUN.set(None)
            return asyncio.create_task(self._run_guarded(umo))

        task = ctx.run(_create)
        bg = self.host.bg_tasks
        bg.add(task)
        task.add_done_callback(bg.discard)

    async def _run_guarded(self, umo: str) -> None:
        lock = self._locks.setdefault(umo, asyncio.Lock())
        try:
            async with lock:
                if self.needs_refresh(umo, ignore_in_flight=True):
                    await self.summarize(umo)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._warn_throttled(f"topic-bg:{umo}", "[%s] 后台话题概括异常: umo=%s err=%s", DISPLAY_NAME, umo, exc)
        finally:
            self._in_flight.discard(umo)
            if not lock.locked() and len(self._locks) > 512:
                self._locks.pop(umo, None)

    def _build_material(self, umo: str, rows: list[dict], backfill: int = 0) -> str:
        alias_of: dict[str, str] = {}
        lines: list[str] = []
        for row in rows:
            sender_id = str(row["sender_id"] or "").strip()
            name = str(row["sender_name"] or "").strip()
            if sender_id in {"bot", DISPLAY_NAME} or name == DISPLAY_NAME:
                label = "机器人自己"
            elif name and name != sender_id:
                label = name
            else:
                if sender_id not in alias_of:
                    if len(alias_of) >= len(TOPIC_ALIASES):
                        alias_of[sender_id] = f"匿名{len(alias_of) + 1}"
                    else:
                        alias_of[sender_id] = TOPIC_ALIASES[len(alias_of) % len(TOPIC_ALIASES)]
                label = alias_of[sender_id]
            text = str(row["text"] or "").replace("\n", " ").strip()
            if not text:
                continue
            if row.get("mentioned"):
                text = f"(@了机器人) {text}"
            lines.append(f"{label}：{text[: self.plugin.topic_line_max_chars]}")
        if not lines:
            return ""
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
            f"消息如下：\n{UNTRUSTED_OPEN}\n{material}\n{UNTRUSTED_CLOSE}\n{UNTRUSTED_NOTE}"
        )

    @staticmethod
    def _parse_topic(text: str, max_chars: int) -> tuple[str, str]:
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

    async def resolve_provider(self, umo: str):
        resolver = ProviderResolver(self.host.context)
        light_id = str(getattr(self.plugin, "topic_light_provider_id", "") or "").strip()
        if light_id:
            provider = await resolver.by_id(light_id)
            if provider is not None:
                return provider
            self._warn_throttled(
                f"topic-provider:{light_id}",
                "[糯小喵] 话题概括模型 %s 不可用，本轮跳过（topic_light_provider_id）",
                light_id,
            )
            return None
        return await resolver.session_default(umo=umo)

    async def summarize(self, umo: str) -> bool:
        p = self.plugin
        if not p.topic_summary_enabled:
            return False
        state = self.store.topic_state(umo)
        cursor = state["topic_to_id"]
        segment = p.topic_segment_msg_count
        pending = self._pending_count(umo, cursor)
        backlog = pending > 2 * segment
        if backlog:
            rows = self._fetch_latest(umo, cursor, segment + p.topic_segment_backfill_max)
            logger.info("[%s] 话题概括积压 %d 条，跳到最新 %d 条: umo=%s", DISPLAY_NAME, pending, len(rows), umo)
        else:
            rows = self._fetch_pending(umo, cursor, segment + p.topic_segment_backfill_max)
        if len(rows) < p.topic_min_unread:
            self._mark_failed(umo)
            return False
        backfill = max(0, len(rows) - segment)
        window = rows if backlog else rows[:segment]
        material = self._build_material(umo, window if not backlog else rows, backfill if backlog else 0)
        if not material:
            # 纯图片/空文本段落没有文字可概括时直接推进游标，防止每 60 秒死循环空转。
            last_id = int(window[-1]["id"])
            self.store.set_topic_state(umo, state["topic"], last_id, time.time())
            return False
        provider = await self.resolve_provider(umo)
        if provider is None or not callable(getattr(provider, "text_chat", None)):
            self._mark_failed(umo)
            self._warn_throttled("topic-no-provider", "[%s] 话题概括跳过：没有可用模型", DISPLAY_NAME)
            return False
        if CURRENT_RUN.get() is not None:
            provider = self.service.autonomy.wrap_provider(provider)
        try:
            resp = await asyncio.wait_for(
                provider.text_chat(prompt=self._build_prompt(material)),
                timeout=p.topic_timeout_seconds,
            )
            raw = ProviderResolver.extract_text(resp)
        except asyncio.TimeoutError:
            self._mark_failed(umo)
            self._warn_throttled(f"topic-timeout:{umo}", "[%s] 话题概括超时: umo=%s", DISPLAY_NAME, umo)
            return False
        except Exception as exc:
            self._mark_failed(umo)
            self._warn_throttled(f"topic-call:{umo}", "[%s] 话题概括调用失败: umo=%s err=%s", DISPLAY_NAME, umo, exc)
            return False
        topic, summary = self._parse_topic(raw, p.topic_max_chars)
        if not topic and not summary:
            self._mark_failed(umo)
            self._warn_throttled(f"topic-parse:{umo}", "[%s] 话题概括输出无法解析: umo=%s", DISPLAY_NAME, umo)
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
        logger.info("[%s] 话题概括完成: umo=%s rows=%d topic=%s", DISPLAY_NAME, umo, len(rows), topic)
        return True

    def session_topic(self, umo: str) -> str:
        try:
            return self.topic(umo)[: self.plugin.topic_max_chars]
        except Exception:
            return ""

    def describe(self) -> str:
        light_id = str(getattr(self.plugin, "topic_light_provider_id", "") or "").strip()
        return f"模型：{light_id or '会话默认模型'}"
