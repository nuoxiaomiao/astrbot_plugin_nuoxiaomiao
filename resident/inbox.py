"""Sparse read receipts: selected mentions never clear the rest of a channel."""

from __future__ import annotations

import time

if __package__ and "." in __package__:
    from ..infra.constants import (
        ACTIVITY_QUERY_MAX_SESSIONS,
        DISPLAY_NAME,
        SYNTHETIC_EVENT_EXTRA,
        UNTRUSTED_CLOSE,
        UNTRUSTED_NOTE,
        UNTRUSTED_OPEN,
    )
    from ..infra.runtime import RateLimitedLogger
else:
    from infra.constants import (
        ACTIVITY_QUERY_MAX_SESSIONS,
        DISPLAY_NAME,
        SYNTHETIC_EVENT_EXTRA,
        UNTRUSTED_CLOSE,
        UNTRUSTED_NOTE,
        UNTRUSTED_OPEN,
    )
    from infra.runtime import RateLimitedLogger

_RATE_LOG = RateLimitedLogger(interval_seconds=60.0)


def _normalize_ids(ids) -> list[int]:
    out = set()
    for value in ids or ():
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if number > 0:
            out.add(number)
    return sorted(out)


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class Inbox:
    def __init__(self, service):
        self.service = service
        self.plugin = service.plugin
        self.host = service.host
        self.store = service.store

    def allowed(self, session, event=None):
        p = self.plugin
        if session["kind"] == "private":
            if not p.activity_record_private:
                return False
            if (
                event is not None
                and self.host.is_self_wake_event(event)
                and not p.proactive_context_other_private
            ):
                return (
                    session["platform_id"],
                    "private",
                    session["conversation_id"],
                ) == self.host.current_conversation(event)
            return True
        return self.host.group_scope_allows(
            session["conversation_id"],
            session.get("name", session.get("conversation_name", "")),
            p.activity_group_whitelist,
            p.activity_group_blacklist,
            session["platform_id"],
        )

    def counts(self, persona, session, event=None, exclude_ids=None):
        if not self.plugin.resident_inbox_enabled or not self.allowed(session, event):
            return {"unread": 0, "unread_mentions": 0}
        baseline = int(self.store.meta("inbox_baseline", "0"))
        excluded = _normalize_ids(exclude_ids)
        exclude = ""
        if excluded:
            exclude = " AND m.id NOT IN (" + ",".join("?" for _ in excluded) + ")"
        return self.store.rows(
            """SELECT COUNT(*) AS unread,COALESCE(SUM(m.mentioned),0) AS unread_mentions
            FROM activity_messages m WHERE m.platform_id=? AND m.kind=? AND m.conversation_id=?
            AND m.id>? AND m.sender_id!='bot'"""
            + exclude
            + """ AND NOT EXISTS
            (SELECT 1 FROM resident_reads r WHERE r.persona=? AND r.message_id=m.id)""",
            (
                session["platform_id"],
                session["kind"],
                session["conversation_id"],
                baseline,
                *excluded,
                persona,
            ),
        )[0]

    def mark(self, persona, ids):
        ids = _normalize_ids(ids)
        if not ids or not self.plugin.resident_inbox_enabled:
            return
        try:
            self.store.inbox_repo.mark_ids(persona, ids)
        except Exception as exc:
            _RATE_LOG.warning("mark", "[%s] 标记已读失败: %s", DISPLAY_NAME, exc)

    def mark_channel(self, persona, current):
        if not self.plugin.resident_inbox_enabled or not current:
            return
        platform, kind, conversation_id = current
        baseline = int(self.store.meta("inbox_baseline", "0"))
        try:
            with self.store.transaction() as db:
                db.execute(
                    "INSERT OR IGNORE INTO resident_reads(persona,message_id) "
                    "SELECT ?,id FROM activity_messages WHERE platform_id=? AND kind=? "
                    "AND conversation_id=? AND id>? AND sender_id!='bot'",
                    (persona, platform, kind, conversation_id, baseline),
                )
        except Exception as exc:
            _RATE_LOG.warning("mark_channel", "[%s] 整频道标读失败: %s", DISPLAY_NAME, exc)

    def silence_candidate(self, event, keyword_woken=False):
        p = self.plugin
        if not p.unread_inbox_enabled:
            return False
        if self.host.is_poke_event(event) or self.host.is_typing_notice(event):
            return False
        if event.get_extra(SYNTHETIC_EVENT_EXTRA) or self.host.is_self_wake_event(event):
            return False
        if keyword_woken or getattr(event, "is_at_or_wake_command", False):
            return False
        if self.host.is_command_event(event):
            return False
        if not event.get_group_id():
            return False
        if not getattr(p, "activity_buffer_enabled", True):
            return False
        if not self.host.event_group_allowed(
            event,
            getattr(p, "activity_group_whitelist", set()),
            getattr(p, "activity_group_blacklist", set()),
        ):
            return False
        if not self.host.event_group_allowed(
            event,
            getattr(p, "inbox_group_whitelist", set()),
            getattr(p, "inbox_group_blacklist", set()),
        ):
            return False
        return True

    async def notice(self, event, persona, limit=0, exclude_ids=None):
        p = self.plugin
        limit = max(1, min(30, int(limit or getattr(p, "mention_notice_limit", 20))))
        current = self.host.current_conversation(event)
        if current is None:
            return "", []
        sessions = [
            s
            for s in self.host.activity_sessions("all")
            if (s["platform_id"], s["kind"], s["conversation_id"]) == current
        ]
        if not sessions:
            return "", []
        session = sessions[0]
        counts = self.counts(persona, session, event, exclude_ids)
        baseline = int(self.store.meta("inbox_baseline", "0"))
        excluded = _normalize_ids(exclude_ids)
        sql = (
            "SELECT id,ts,text,sender_id,sender_name,mentioned FROM activity_messages "
            "WHERE platform_id=? AND kind=? AND conversation_id=? AND id>? AND sender_id!='bot'"
        )
        args: list = [
            session["platform_id"],
            session["kind"],
            session["conversation_id"],
            baseline,
        ]
        if excluded:
            sql += " AND id NOT IN (" + ",".join("?" for _ in excluded) + ")"
            args.extend(excluded)
        sql += (
            " AND NOT EXISTS (SELECT 1 FROM resident_reads r WHERE r.persona=? AND r.message_id=activity_messages.id) "
            "ORDER BY id DESC"
        )
        args.append(persona)
        if limit > 0:
            sql += " LIMIT ?"
            args.append(limit)
        rows = list(reversed(self.store.rows(sql, tuple(args))))
        name = self.host.display_conversation_name(
            session["platform_id"],
            session["kind"],
            session["conversation_id"],
            session.get("name", ""),
        )
        topic = ""
        if p.topic_summary_enabled:
            umo = (
                f"{session['platform_id']}:"
                f"{'GroupMessage' if session['kind'] == 'group' else 'FriendMessage'}:"
                f"{session['conversation_id']}"
            )
            topic = self.service.topic.session_topic(umo)
        unread = int(counts.get("unread", 0) or 0)
        mentions = int(counts.get("unread_mentions", 0) or 0)
        head = f"[信箱通知] 会话《{name}》有人提到你。"
        if unread:
            head += f"未读 {unread} 条，其中 @你 {mentions} 条。"
        if topic:
            head += f"近期话题：{topic}。"
        if not rows:
            return head, []
        lines = [head, "最近未读：", UNTRUSTED_OPEN]
        max_chars = getattr(p, "activity_buffer_max_text_chars", 600)
        for row in rows:
            sender = self.host.display_user_name(
                session["platform_id"],
                str(row["sender_id"] or ""),
                str(row["sender_name"] or ""),
            ) or "某人"
            when = time.strftime("%H:%M", time.localtime(float(row["ts"])))
            text = str(row["text"] or "")[:max_chars]
            tag = "（@你）" if row["mentioned"] else ""
            lines.append(f"- {when} {sender}{tag}：{text}")
        lines.extend([UNTRUSTED_CLOSE, UNTRUSTED_NOTE])
        return "\n".join(lines), [int(row["id"]) for row in rows]

    def prune(self):
        try:
            self.store.inbox_repo.prune_orphan_reads()
        except Exception as exc:
            _RATE_LOG.warning("prune", "[%s] 清理已读回执失败: %s", DISPLAY_NAME, exc)

    async def consumed(self, event, response):
        if (
            not self.plugin.resident_inbox_enabled
            or response is None
            or getattr(response, "role", "assistant") == "err"
        ):
            return
        ids = list(event.get_extra("nuoxiaomiao_inbox_request_ids") or [])
        ids.extend(event.get_extra("nuoxiaomiao_inbox_shown_ids") or [])
        if ids:
            persona, _ = await self.service.persona.resolve(event)
            self.mark(persona, ids)

    def request_ids(self, event):
        ids = list(event.get_extra("nuoxiaomiao_inbox_ids") or [])
        source_ids = event.get_extra("nuoxiaomiao_merged_source_ids") or []
        message_id = self.host.message_id(event)
        refs = list(source_ids) or ([message_id] if message_id else [])
        session = self.host.current_conversation(event)
        if refs and session:
            placeholders = ",".join("?" for _ in refs)
            rows = self.store.rows(
                f"SELECT id FROM activity_messages WHERE platform_id=? AND kind=? AND conversation_id=? AND message_id IN ({placeholders})",
                (*session, *refs),
            )
            ids.extend(row["id"] for row in rows)
        resolved = sorted(set(ids))
        event.set_extra("nuoxiaomiao_inbox_request_ids", resolved)
        if not event.get_extra("nuoxiaomiao_inbox_ids") and resolved:
            event.set_extra("nuoxiaomiao_inbox_ids", resolved)

    async def read(
        self,
        event,
        conversation="",
        kind="",
        after="",
        sender="",
        keyword="",
        limit=20,
        mode="recent",
        before_id=0,
        mark_read=False,
    ):
        p = self.plugin
        if not p.resident_inbox_enabled and (mode != "recent" or mark_read):
            raise ValueError("信箱功能已关闭")
        if mode not in ("recent", "unread", "mentions"):
            raise ValueError("mode 须为 recent / unread / mentions")
        if kind not in ("", "group", "private"):
            raise ValueError("kind 须为 group / private")
        limit = max(1, min(50, int(limit)))
        before_id = max(0, int(before_id))
        persona, _ = await self.service.persona.resolve(event)
        sessions = self.host.activity_sessions(kind or "all")
        if conversation:
            if ":" in str(conversation):
                pieces = str(conversation).split(":", 2)
                if len(pieces) != 3 or pieces[1] not in ("GroupMessage", "FriendMessage"):
                    raise ValueError("会话标识格式错误，请使用 list_conversations 返回的完整 umo")
                skind = "group" if pieces[1] == "GroupMessage" else "private"
                sessions = [
                    s
                    for s in sessions
                    if (s["platform_id"], s["kind"], s["conversation_id"])
                    == (pieces[0], skind, pieces[2])
                ]
            else:
                sessions = self.host.resolve_activity_conversations(conversation, kind)
                if len(sessions) > 1:
                    raise ValueError("会话名称不唯一，请使用 list_conversations 返回的完整 umo")
            if not sessions:
                raise ValueError("记录里找不到这个会话")
        sessions = [s for s in sessions if self.allowed(s, event)]
        if not sessions:
            return {"count": 0, "results": [], "has_more": False, "next_before_id": None}
        sessions = sessions[:ACTIVITY_QUERY_MAX_SESSIONS]
        where = []
        args: list = []
        scopes = []
        for session in sessions:
            scopes.append("(m.platform_id=? AND m.kind=? AND m.conversation_id=?)")
            args.extend((session["platform_id"], session["kind"], session["conversation_id"]))
        where.append("(" + " OR ".join(scopes) + ")")
        if before_id and not self.store.rows(
            "SELECT m.id FROM activity_messages m WHERE m.id=? AND (" + " OR ".join(scopes) + ")",
            (before_id, *args),
        ):
            raise ValueError("分页游标不存在或记录已过期，请从最近记录重新读取")
        if before_id:
            where.append("m.id<?")
            args.append(before_id)
        after_ts, error = self.host.parse_activity_time(after)
        if error:
            raise ValueError(error)
        if after_ts is not None:
            where.append("m.ts>=?")
            args.append(after_ts)
        if sender:
            where.append("(m.sender_id=? OR m.sender_name LIKE ? ESCAPE '\\')")
            args.extend((sender, "%" + _escape_like(sender) + "%"))
        if keyword:
            where.append("instr(m.text,?)>0")
            args.append(keyword)
        if mode in ("unread", "mentions"):
            where.extend(
                [
                    "m.sender_id!='bot'",
                    "m.id>?",
                    "NOT EXISTS (SELECT 1 FROM resident_reads r WHERE r.persona=? AND r.message_id=m.id)",
                ]
            )
            args.extend((int(self.store.meta("inbox_baseline", "0")), persona))
        if mode == "mentions":
            where.append("m.mentioned=1")
        rows = self.store.rows(
            "SELECT m.* FROM activity_messages m WHERE "
            + " AND ".join(where)
            + " ORDER BY m.id DESC LIMIT ?",
            (*args, limit + 1),
        )
        more = len(rows) > limit
        rows = rows[:limit]
        if mode == "mentions" and rows:
            expanded = {row["id"]: row for row in rows}
            for row in rows:
                context = self.store.rows(
                    "SELECT * FROM activity_messages WHERE platform_id=? AND kind=? AND conversation_id=? AND id<? ORDER BY id DESC LIMIT 10",
                    (row["platform_id"], row["kind"], row["conversation_id"], row["id"]),
                )
                for item in context:
                    if not before_id or item["id"] < before_id:
                        expanded.setdefault(item["id"], item)
            rows = sorted(expanded.values(), key=lambda r: r["id"], reverse=True)
            more = more or len(rows) > 50
            rows = rows[:50]
        if mark_read:
            self.mark(persona, [row["id"] for row in rows])
        for row in rows:
            row["umo"] = (
                f"{row['platform_id']}:{'GroupMessage' if row['kind'] == 'group' else 'FriendMessage'}:{row['conversation_id']}"
            )
            row["truncated"] = bool(row["truncated"])
        payload: dict = {
            "count": len(rows),
            "results": list(reversed(rows)),
            "has_more": more,
            "next_before_id": min((r["id"] for r in rows), default=None),
            "marked_read": bool(mark_read),
        }
        if p.topic_summary_enabled and len(sessions) == 1:
            session = sessions[0]
            umo = (
                f"{session['platform_id']}:"
                f"{'GroupMessage' if session['kind'] == 'group' else 'FriendMessage'}:"
                f"{session['conversation_id']}"
            )
            topic = self.service.topic.session_topic(umo)
            if topic:
                payload["topic"] = topic
        return payload
