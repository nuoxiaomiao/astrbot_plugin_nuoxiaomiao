"""Sparse read receipts: selected mentions never clear the rest of a channel."""

from __future__ import annotations


class Inbox:
    def __init__(self, service):
        self.service = service
        self.plugin = service.plugin
        self.store = service.store

    def allowed(self, session, event=None):
        p = self.plugin
        if session["kind"] == "private":
            if not p.activity_record_private:
                return False
            if (
                event is not None
                and p._is_self_wake_event(event)
                and not p.proactive_context_other_private
            ):
                return (
                    session["platform_id"],
                    "private",
                    session["conversation_id"],
                ) == p._current_conversation(event)
            return True
        return p._group_scope_allows(
            session["conversation_id"],
            session.get("name", session.get("conversation_name", "")),
            p.activity_group_whitelist,
            p.activity_group_blacklist,
            session["platform_id"],
        )

    def counts(self, persona, session, event=None):
        if not self.plugin.resident_inbox_enabled or not self.allowed(session, event):
            return {"unread": 0, "unread_mentions": 0}
        baseline = int(self.store.meta("inbox_baseline", "0"))
        return self.store.rows(
            """SELECT COUNT(*) AS unread,COALESCE(SUM(m.mentioned),0) AS unread_mentions
            FROM activity_messages m WHERE m.platform_id=? AND m.kind=? AND m.conversation_id=?
            AND m.id>? AND m.sender_id!='bot' AND NOT EXISTS
            (SELECT 1 FROM resident_reads r WHERE r.persona=? AND r.message_id=m.id)""",
            (
                session["platform_id"],
                session["kind"],
                session["conversation_id"],
                baseline,
                persona,
            ),
        )[0]

    def mark(self, persona, ids):
        ids = sorted({int(i) for i in ids if int(i) > 0})
        if not ids or not self.plugin.resident_inbox_enabled:
            return
        with self.store.transaction() as db:
            db.executemany(
                "INSERT OR IGNORE INTO resident_reads(persona,message_id) SELECT ?,id FROM activity_messages WHERE id=?",
                ((persona, i) for i in ids),
            )

    def prune(self):
        with self.store.transaction() as db:
            db.execute(
                "DELETE FROM resident_reads WHERE NOT EXISTS (SELECT 1 FROM activity_messages m WHERE m.id=resident_reads.message_id)"
            )

    async def consumed(self, event, response):
        if (
            not self.plugin.resident_inbox_enabled
            or response is None
            or getattr(response, "role", "assistant") == "err"
        ):
            return
        ids = event.get_extra("nuoxiaomiao_inbox_request_ids") or []
        if ids:
            persona, _ = await self.service.persona.resolve(event)
            self.mark(persona, ids)

    def request_ids(self, event):
        ids = list(event.get_extra("nuoxiaomiao_inbox_ids") or [])
        source_ids = event.get_extra("nuoxiaomiao_merged_source_ids") or []
        message_id = self.plugin._message_id(event)
        refs = list(source_ids) or ([message_id] if message_id else [])
        session = self.plugin._current_conversation(event)
        if refs and session:
            placeholders = ",".join("?" for _ in refs)
            rows = self.store.rows(
                f"SELECT id FROM activity_messages WHERE platform_id=? AND kind=? AND conversation_id=? AND message_id IN ({placeholders})",
                (*session, *refs),
            )
            ids.extend(row["id"] for row in rows)
        event.set_extra("nuoxiaomiao_inbox_request_ids", sorted(set(ids)))

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
        before_id = int(before_id)
        if before_id < 0:
            raise ValueError("分页游标必须大于零")
        if before_id and not self.store.rows(
            "SELECT id FROM activity_messages WHERE id=?", (before_id,)
        ):
            raise ValueError("分页游标不存在或记录已过期，请从最近记录重新读取")
        persona, _ = await self.service.persona.resolve(event)
        sessions = p._activity_sessions(kind or "all")
        if conversation:
            pieces = conversation.split(":", 2)
            if len(pieces) == 3 and pieces[1] in ("GroupMessage", "FriendMessage"):
                skind = "group" if pieces[1] == "GroupMessage" else "private"
                sessions = [
                    s
                    for s in sessions
                    if (s["platform_id"], s["kind"], s["conversation_id"])
                    == (pieces[0], skind, pieces[2])
                ]
            else:
                sessions = p._resolve_activity_conversations(conversation, kind)
                if len(sessions) > 1:
                    raise ValueError(
                        "会话名称不唯一，请使用 list_conversations 返回的完整 umo"
                    )
            if not sessions:
                raise ValueError("记录里找不到这个会话")
        sessions = [s for s in sessions if self.allowed(s, event)]
        if not sessions:
            return {
                "count": 0,
                "results": [],
                "has_more": False,
                "next_before_id": None,
            }
        where = []
        args = []
        scopes = []
        for session in sessions:
            scopes.append("(m.platform_id=? AND m.kind=? AND m.conversation_id=?)")
            args.extend(
                (session["platform_id"], session["kind"], session["conversation_id"])
            )
        where.append("(" + " OR ".join(scopes) + ")")
        if before_id:
            where.append("m.id<?")
            args.append(before_id)
        after_ts, error = p._parse_activity_time(after)
        if error:
            raise ValueError(error)
        if after_ts is not None:
            where.append("m.ts>=?")
            args.append(after_ts)
        if sender:
            where.append("(m.sender_id=? OR m.sender_name LIKE ?)")
            args.extend((sender, "%" + sender + "%"))
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
                    (
                        row["platform_id"],
                        row["kind"],
                        row["conversation_id"],
                        row["id"],
                    ),
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
        return {
            "count": len(rows),
            "results": list(reversed(rows)),
            "has_more": more,
            "next_before_id": min((r["id"] for r in rows), default=None),
            "marked_read": bool(mark_read),
        }
