"""Chat activity capture, DB persistence, background pruning, and message queries."""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any, Callable

from astrbot.api import logger

if __package__ and "." in __package__:
    from ..infra.constants import (
        ACTIVITY_ABSOLUTE_MAX_ROWS,
        ACTIVITY_PRUNE_INTERVAL_SECONDS,
        ACTIVITY_QUERY_MAX_SESSIONS,
        ACTIVITY_SESSION_MATCH_LIMIT,
        BOT_ACTIVITY_SENDER_ID,
        DISPLAY_NAME,
        SELF_WAKE_RUN_RETENTION_DAYS,
        SYNTHETIC_EVENT_EXTRA,
        ActivityEntry,
    )
    from ..infra.database import ActivityDatabase
    from ..infra.runtime import (
        BoundedTTLCache,
        RateLimitedLogger,
        admin_ids_of,
        extract_components,
        extract_text,
        message_id_of,
        now_in_tz,
        parse_activity_time,
    )
    from ..qq_official import (
        display_name_fallback,
        is_qq_official,
        is_qq_official_platform_id,
        platform_id_of_inst,
    )
else:
    from infra.constants import (
        ACTIVITY_ABSOLUTE_MAX_ROWS,
        ACTIVITY_PRUNE_INTERVAL_SECONDS,
        ACTIVITY_QUERY_MAX_SESSIONS,
        ACTIVITY_SESSION_MATCH_LIMIT,
        BOT_ACTIVITY_SENDER_ID,
        DISPLAY_NAME,
        SELF_WAKE_RUN_RETENTION_DAYS,
        SYNTHETIC_EVENT_EXTRA,
        ActivityEntry,
    )
    from infra.database import ActivityDatabase
    from infra.runtime import (
        BoundedTTLCache,
        RateLimitedLogger,
        admin_ids_of,
        extract_components,
        extract_text,
        message_id_of,
        now_in_tz,
        parse_activity_time,
    )
    from qq_official import (
        display_name_fallback,
        is_qq_official,
        is_qq_official_platform_id,
        platform_id_of_inst,
    )


class ActivityService:
    """Manage activity_messages capture, display naming, queries, and retention."""

    def __init__(
        self,
        plugin: Any,
        db: ActivityDatabase,
        guard: Any,
        merger: Any,
        resident_getter: Callable[[], Any],
        *,
        on_proactive_pruned: Callable[[int], None] | None = None,
    ) -> None:
        self.plugin = plugin
        self.db = db
        self.guard = guard
        self.merger = merger
        self._resident_getter = resident_getter
        self._on_proactive_pruned = on_proactive_pruned
        self._qq_group_names = BoundedTTLCache(max_size=1024, ttl_seconds=86400.0)
        self._qq_group_miss = BoundedTTLCache(max_size=1024, ttl_seconds=600.0)
        self._qq_name_lock = asyncio.Lock()
        self._rate_log = RateLimitedLogger(interval_seconds=60.0)
        self._last_prune_ts = 0.0
        self._prune_task: asyncio.Task[None] | None = None

    def platform_insts(self) -> list[Any]:
        try:
            manager = getattr(self.plugin.context, "platform_manager", None)
            return list(getattr(manager, "platform_insts", None) or [])
        except Exception:
            return []

    def platform_inst_by_id(self, platform_id: str) -> Any | None:
        target = str(platform_id or "")
        for inst in self.platform_insts():
            if platform_id_of_inst(inst) == target:
                return inst
        return None

    def admin_ids(self) -> set[str]:
        return admin_ids_of(
            self.plugin.context,
            getattr(self.plugin, "qq_official_admin_openids", set()) or set(),
        )

    def activity_permission_ok(self, event: Any) -> bool:
        if not self.plugin.activity_require_admin:
            return True
        try:
            if event.is_admin():
                return True
        except Exception:
            pass
        sender_id = str(event.get_sender_id() or "").strip()
        return bool(sender_id) and sender_id in self.admin_ids()

    def conversation_display_name(self, event: Any, kind: str) -> str:
        try:
            if kind == "private":
                return str(event.get_sender_name() or "").strip()
            group = getattr(getattr(event, "message_obj", None), "group", None)
            name = str(getattr(group, "group_name", "") or "").strip()
            if name:
                return name
            platform_id = str(event.get_platform_id() or "").strip()
            group_id = str(event.get_group_id() or "").strip()
            if platform_id and group_id:
                return str(self._qq_group_names.get((platform_id, group_id), "") or "")
            return ""
        except Exception:
            return ""

    def display_user_name(self, platform_id: str, user_id: str, name: str) -> str:
        value = str(name or "").strip()
        if value:
            return value
        uid = str(user_id or "").strip()
        try:
            if is_qq_official_platform_id(platform_id, self.platform_insts()):
                return display_name_fallback(uid, "private")
        except Exception:
            pass
        return uid

    def display_conversation_name(
        self, platform_id: str, kind: str, conversation_id: str, name: str
    ) -> str:
        value = str(name or "").strip()
        if value:
            return value
        cid = str(conversation_id or "").strip()
        if not cid:
            return "未知会话"
        if kind == "group":
            cached = self._qq_group_names.get((str(platform_id or ""), cid), "")
            if cached:
                return str(cached)
        try:
            if is_qq_official_platform_id(platform_id, self.platform_insts()):
                return display_name_fallback(cid, kind)
        except Exception:
            pass
        return cid

    async def ensure_qq_official_group_name(self, event: Any) -> str:
        if not is_qq_official(event):
            return ""
        group_id = str(event.get_group_id() or "").strip()
        platform_id = str(event.get_platform_id() or "").strip()
        if not group_id or not platform_id:
            return ""
        key = (platform_id, group_id)
        cached = str(self._qq_group_names.get(key, "") or "")
        if cached:
            return cached
        group = getattr(getattr(event, "message_obj", None), "group", None)
        name = str(getattr(group, "group_name", "") or "").strip()
        if name:
            self._qq_group_names.set(key, name)
            return name
        if self._qq_group_miss.get(key):
            return ""
        async with self._qq_name_lock:
            cached = str(self._qq_group_names.get(key, "") or "")
            if cached:
                return cached
            if self._qq_group_miss.get(key):
                return ""
            get_group = getattr(event, "get_group", None)
            if callable(get_group):
                try:
                    info = await asyncio.wait_for(get_group(group_id), timeout=3.0)
                    resolved_name = str(getattr(info, "group_name", "") or "").strip()
                    if resolved_name:
                        self._qq_group_names.set(key, resolved_name)
                        return resolved_name
                except Exception as exc:
                    logger.debug("[%s] QQ官方获取群名失败: group=%s err=%s", DISPLAY_NAME, group_id, exc)
            self._qq_group_miss.set(key, True)
            return ""

    def activity_session_of(self, event: Any) -> tuple[str, str, str, str] | None:
        sender_id = str(event.get_sender_id() or "").strip()
        if event.is_private_chat():
            if not self.plugin.activity_record_private:
                return None
            conv_id = ""
            try:
                parts = str(getattr(event, "unified_msg_origin", "") or "").split(":")
                if len(parts) >= 3:
                    conv_id = ":".join(parts[2:]).strip()
            except Exception:
                conv_id = ""
            if not conv_id:
                conv_id = sender_id
            if not conv_id:
                return None
            return ("private", conv_id, self.conversation_display_name(event, "private"), sender_id)
        group_id = str(event.get_group_id() or "").strip()
        if group_id:
            return ("group", group_id, self.conversation_display_name(event, "group"), sender_id)
        return None

    def current_conversation(self, event: Any) -> tuple[str, str, str] | None:
        platform_id = str(event.get_platform_id() or "").strip()
        if event.is_private_chat():
            parts = str(getattr(event, "unified_msg_origin", "") or "").split(":", 2)
            if len(parts) == 3 and parts[1] == "FriendMessage" and parts[2].strip():
                cid = parts[2].strip()
            else:
                cid = str(event.get_sender_id() or "").strip()
            return (platform_id, "private", cid) if cid else None
        group_id = str(event.get_group_id() or "").strip()
        if group_id:
            return (platform_id, "group", group_id)
        return None

    def record_activity(self, entry: ActivityEntry) -> int:
        with self.db.transaction() as conn:
            if entry.message_id:
                existing = conn.execute(
                    "SELECT id FROM activity_messages WHERE platform_id=? AND kind=? "
                    "AND conversation_id=? AND sender_id=? AND message_id=?",
                    (
                        entry.platform_id,
                        entry.kind,
                        entry.conversation_id,
                        entry.sender_id,
                        entry.message_id,
                    ),
                ).fetchone()
                if existing:
                    return int(existing[0])
            cursor = conn.execute(
                """
                INSERT INTO activity_messages
                    (platform_id, kind, conversation_id, conversation_name,
                     sender_id, sender_name, text, ts, message_id, mentioned, truncated)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry.platform_id,
                    entry.kind,
                    entry.conversation_id,
                    entry.conversation_name,
                    entry.sender_id,
                    entry.sender_name,
                    entry.text,
                    entry.ts,
                    entry.message_id,
                    1 if entry.mentioned else 0,
                    1 if entry.truncated else 0,
                ),
            )
            return int(cursor.lastrowid or 0)

    def recent_bot_texts(self, event: Any, limit: int = 20) -> list[str]:
        session = self.activity_session_of(event)
        if session is None:
            return []
        try:
            platform_id = str(event.get_platform_id() or "").strip()
            kind, conversation_id, _name, _sender = session
            with self.db.read_conn() as conn:
                rows = conn.execute(
                    """
                    SELECT text FROM activity_messages
                    WHERE platform_id = ? AND kind = ? AND conversation_id = ?
                      AND sender_id = ?
                    ORDER BY id DESC LIMIT ?
                    """,
                    (platform_id, kind, conversation_id, BOT_ACTIVITY_SENDER_ID, limit),
                ).fetchall()
            out: list[str] = []
            for row in rows:
                t = re.sub(r"\s+", "", str(row[0] or ""))
                if t:
                    out.append(t)
            return out
        except Exception as exc:
            logger.debug("[%s] 读取最近机器人发言失败: %s", DISPLAY_NAME, exc)
            return []

    def record_bot_reply(self, event: Any, text: str) -> None:
        p = self.plugin
        try:
            if not (p.enabled and p.activity_buffer_enabled and p.activity_record_bot_replies):
                return
            if not self.guard.event_group_allowed(event, p.activity_group_whitelist, p.activity_group_blacklist):
                return
            body = str(text or "").strip()
            if not body:
                return
            session = self.activity_session_of(event)
            if session is None:
                return
            kind, conversation_id, conversation_name, _sender_id = session
            if len(body) > p.activity_buffer_max_text_chars:
                body = body[: p.activity_buffer_max_text_chars] + "…"
            self.record_activity(
                ActivityEntry(
                    platform_id=str(event.get_platform_id() or "").strip(),
                    ts=time.time(),
                    sender_id=BOT_ACTIVITY_SENDER_ID,
                    sender_name=DISPLAY_NAME,
                    conversation_id=conversation_id,
                    conversation_name=conversation_name,
                    kind=kind,
                    text=body,
                )
            )
        except Exception as exc:
            logger.debug("[%s] 记录机器人回复失败: %s", DISPLAY_NAME, exc)

    def mentioned_by_real_at(self, event: Any, text: str = "") -> bool:
        if event.get_extra("nuoxiaomiao_qq_was_at"):
            return True
        self_id = str(event.get_self_id() or "").strip()
        for comp in extract_components(event):
            cname = type(comp).__name__
            if cname == "AtAll":
                continue
            if cname == "At":
                qq = str(getattr(comp, "qq", "") or "").strip()
                if qq and qq != "all" and self_id and qq == self_id:
                    return True
            elif cname == "Reply":
                sid = str(getattr(comp, "sender_id", "") or "").strip()
                if sid and self_id and sid == self_id:
                    return True
        if is_qq_official(event) and bool(getattr(event, "is_at_or_wake_command", False)):
            if str(event.get_group_id() or "").strip():
                return True
        body = text if text else extract_text(event, strip_at_markup=bool(getattr(self.plugin, "qq_official_strip_at_markup", True)))
        prefix = self.guard.effective_wake_prefix(event)
        if prefix and prefix in body:
            return True
        for pfx in self.guard.wake_prefixes(event):
            if pfx and pfx in body:
                return True
        return False

    async def capture_chat_activity(self, event: Any) -> None:
        p = self.plugin
        try:
            if not p.enabled or not p.activity_buffer_enabled:
                return
            if self.guard.is_poke_event(event) or self.guard.is_typing_notice(event):
                return
            message_id = message_id_of(event)
            if self.merger.is_synthetic_id(message_id) or event.get_extra(SYNTHETIC_EVENT_EXTRA):
                return
            sender_id = str(event.get_sender_id() or "").strip()
            if not sender_id or sender_id == str(event.get_self_id() or ""):
                return
            if sender_id in p.blocked_users:
                return
            if self.guard.is_command_event(event):
                return
            if is_qq_official(event) and event.get_group_id():
                await self.ensure_qq_official_group_name(event)
            if not self.guard.event_group_allowed(event, p.activity_group_whitelist, p.activity_group_blacklist):
                return
            text = extract_text(event, strip_at_markup=bool(getattr(p, "qq_official_strip_at_markup", True)))
            if not text:
                if not (p.vision_enabled and message_id):
                    return
                text = ""
            truncated = len(text) > p.activity_buffer_max_text_chars
            if truncated:
                text = text[: p.activity_buffer_max_text_chars] + "…"
            session = self.activity_session_of(event)
            if session is None:
                return
            kind, conversation_id, conversation_name, _sender_id = session
            sender_name = str(event.get_sender_name() or "").strip()
            if not sender_name and is_qq_official(event):
                sender_name = display_name_fallback(sender_id, "private")
            if is_qq_official(event) and kind == "private" and not str(conversation_name or "").strip():
                conversation_name = sender_name or display_name_fallback(conversation_id, "private")

            activity_id = await self.db.run(
                self.record_activity,
                ActivityEntry(
                    platform_id=str(event.get_platform_id() or "").strip(),
                    ts=time.time(),
                    sender_id=sender_id,
                    sender_name=sender_name,
                    conversation_id=conversation_id,
                    conversation_name=conversation_name,
                    kind=kind,
                    text=text,
                    message_id=message_id,
                    mentioned=self.mentioned_by_real_at(event, text),
                    truncated=truncated,
                ),
            )
            event.set_extra("nuoxiaomiao_inbox_ids", [activity_id])
            resident = self._resident_getter()
            if resident is not None and p.vision_enabled and message_id:
                task = asyncio.create_task(resident.vision.keep_message_images(event, activity_id, message_id))
                p._resident_bg_tasks.add(task)
                task.add_done_callback(p._resident_bg_tasks.discard)
            self.schedule_prune_if_due()
        except Exception as exc:
            self._rate_log.warning("capture_activity", "[%s] 记录会话消息失败: %s", DISPLAY_NAME, exc)

    def schedule_prune_if_due(self) -> None:
        now = time.time()
        if now - self._last_prune_ts < ACTIVITY_PRUNE_INTERVAL_SECONDS:
            return
        if self._prune_task is not None and not self._prune_task.done():
            return
        self._last_prune_ts = now
        self._prune_task = asyncio.create_task(self.run_prune_once())

    async def run_prune_once(self) -> None:
        p = self.plugin
        now = time.time()
        cutoff = now - p.activity_retention_days * 86400
        self_wake_cutoff = now - SELF_WAKE_RUN_RETENTION_DAYS * 86400

        def _prune_db() -> list[int]:
            removed_pending: list[int] = []
            with self.db.transaction() as conn:
                conn.execute("DELETE FROM activity_messages WHERE ts < ?", (cutoff,))
                overflow = conn.execute(
                    "SELECT COUNT(*) - ? FROM activity_messages", (ACTIVITY_ABSOLUTE_MAX_ROWS,)
                ).fetchone()[0]
                if overflow > 0:
                    conn.execute(
                        "DELETE FROM activity_messages WHERE id IN (SELECT id FROM activity_messages ORDER BY id ASC LIMIT ?)",
                        (overflow,),
                    )
                conn.execute(
                    "DELETE FROM proactive_pending WHERE status IN ('fired', 'failed', 'skipped', 'unknown') AND done_ts > 0 AND done_ts < ?",
                    (cutoff,),
                )
                expired_rows = conn.execute(
                    "SELECT id FROM proactive_pending WHERE status = 'pending' AND fire_ts < ?",
                    (cutoff,),
                ).fetchall()
                removed_pending = [int(r[0]) for r in expired_rows]
                if removed_pending:
                    conn.execute(
                        "DELETE FROM proactive_pending WHERE status = 'pending' AND fire_ts < ?",
                        (cutoff,),
                    )
                conn.execute(
                    "DELETE FROM resident_runs WHERE started < ? AND status NOT IN ('pending','preparing','running','paused','stopped','sending') "
                    "AND NOT EXISTS (SELECT 1 FROM resident_deferred d JOIN proactive_pending p ON p.id=d.pending_id WHERE d.run_id=resident_runs.id AND p.status IN ('pending','sending'))",
                    (self_wake_cutoff,),
                )
            return removed_pending

        try:
            pruned_pending_ids = await self.db.run(_prune_db)
            if self._on_proactive_pruned is not None:
                for pid in pruned_pending_ids:
                    self._on_proactive_pruned(pid)
            resident = self._resident_getter()
            if resident is not None:
                await self.db.run(resident.inbox.prune)
                await self.db.run(resident.store.cleanup_runs, p.activity_retention_days)
                await self.db.run(resident.vision.cleanup)
                sessions = await self.db.run(self.activity_sessions, "all")
                active_umos = {s["umo"] for s in sessions}
                await self.db.run(resident.store.topic_repo.prune_stale_topics, active_umos)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("[%s] 清理过期记录失败: %s", DISPLAY_NAME, exc)

    async def stop_prune_loop(self) -> None:
        task = self._prune_task
        self._prune_task = None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def activity_sessions(self, kind: str = "all") -> list[dict[str, Any]]:
        where = ""
        params: list[Any] = []
        if kind in {"group", "private"}:
            where = "WHERE kind = ?"
            params.append(kind)
        with self.db.read_conn() as conn:
            rows = conn.execute(
                f"""
                SELECT a.platform_id, a.kind, a.conversation_id,
                       COALESCE(NULLIF(MAX(a.conversation_name), ''), a.conversation_id) AS name,
                       COUNT(*) AS messages,
                       MAX(a.ts) AS last_ts,
                       (
                           SELECT b.text FROM activity_messages b
                           WHERE b.platform_id = a.platform_id
                             AND b.kind = a.kind
                             AND b.conversation_id = a.conversation_id
                           ORDER BY b.ts DESC, b.id DESC LIMIT 1
                       ) AS last_text
                FROM activity_messages a
                {where}
                GROUP BY a.platform_id, a.kind, a.conversation_id
                ORDER BY last_ts DESC
                """,
                params,
            ).fetchall()
        return [
            {
                "platform_id": str(platform_id),
                "kind": str(s_kind),
                "conversation_id": str(conversation_id),
                "name": str(name or conversation_id),
                "umo": f"{platform_id}:{'GroupMessage' if s_kind == 'group' else 'FriendMessage'}:{conversation_id}",
                "messages": int(messages or 0),
                "last_ts": float(last_ts or 0),
                "last_text": str(last_text or ""),
            }
            for platform_id, s_kind, conversation_id, name, messages, last_ts, last_text in rows
        ]

    def activity_participants(
        self, platform_id: str, kind: str, conversation_id: str, since_ts: float
    ) -> list[str]:
        with self.db.read_conn() as conn:
            rows = conn.execute(
                """
                SELECT sender_id, MAX(sender_name) AS name, MAX(ts) AS last_ts
                FROM activity_messages
                WHERE platform_id = ? AND kind = ? AND conversation_id = ?
                  AND sender_id != ? AND ts >= ?
                GROUP BY sender_id
                ORDER BY last_ts DESC LIMIT 8
                """,
                (platform_id, kind, conversation_id, BOT_ACTIVITY_SENDER_ID, since_ts),
            ).fetchall()
        return [
            self.display_user_name(platform_id, str(sid or ""), str(sname or ""))
            for sid, sname, _ in rows
            if self.display_user_name(platform_id, str(sid or ""), str(sname or ""))
        ]

    def resolve_activity_conversations(self, query: str, kind: str = "") -> list[dict[str, Any]]:
        target = str(query or "").strip()
        if not target:
            return []
        sessions = self.activity_sessions(kind or "all")
        exact = [s for s in sessions if s["conversation_id"] == target or s["name"] == target]
        if exact:
            return exact[:ACTIVITY_SESSION_MATCH_LIMIT]
        low = target.lower()
        partial = [s for s in sessions if low in s["name"].lower() or low in s["conversation_id"].lower()]
        return partial[:ACTIVITY_SESSION_MATCH_LIMIT]

    def parse_activity_time(self, raw: str) -> tuple[float | None, str | None]:
        return parse_activity_time(raw, now_fn=lambda: now_in_tz(self.plugin.context))

    def query_activity_messages(
        self,
        sessions: list[dict[str, Any]] | None,
        kind: str,
        after_ts: float | None,
        sender: str,
        keyword: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        where: list[str] = []
        params: list[Any] = []
        if sessions is not None:
            if not sessions:
                return []
            if len(sessions) > ACTIVITY_QUERY_MAX_SESSIONS:
                sessions = sessions[:ACTIVITY_QUERY_MAX_SESSIONS]
            clauses = ["(platform_id = ? AND kind = ? AND conversation_id = ?)" for _ in sessions]
            for s in sessions:
                params.extend([s["platform_id"], s["kind"], s["conversation_id"]])
            where.append("(" + " OR ".join(clauses) + ")")
        if kind:
            where.append("kind = ?")
            params.append(kind)
        if after_ts is not None:
            where.append("ts >= ?")
            params.append(after_ts)
        if sender:
            where.append("(sender_id = ? OR instr(sender_name, ?) > 0)")
            params.extend([sender, sender])
        if keyword:
            where.append("instr(text, ?) > 0")
            params.append(keyword)
        sql = (
            "SELECT platform_id, kind, conversation_id, conversation_name, "
            "sender_id, sender_name, text, ts, id, truncated FROM activity_messages"
        )
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY ts DESC, id DESC LIMIT ?"
        params.append(limit)
        with self.db.read_conn() as conn:
            rows = conn.execute(sql, params).fetchall()
        rows = list(reversed(rows))
        return [
            {
                "id": record_id,
                "truncated": bool(truncated),
                "kind": s_kind,
                "conversation": self.display_conversation_name(
                    platform_id, s_kind, conversation_id, conversation_name
                ),
                "conversation_id": conversation_id,
                "platform": platform_id,
                "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)),
                "sender": self.display_user_name(platform_id, sender_id, sender_name),
                "sender_id": sender_id,
                "text": text,
            }
            for (
                platform_id,
                s_kind,
                conversation_id,
                conversation_name,
                sender_id,
                sender_name,
                text,
                ts,
                record_id,
                truncated,
            ) in rows
        ]
