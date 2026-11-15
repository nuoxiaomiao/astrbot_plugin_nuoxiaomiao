"""Channel-watermarked inbox, sparse read receipts, and unified message reader."""

from __future__ import annotations

from datetime import datetime
import time
from typing import Any

if __package__ and "." in __package__:
    from ..infra.constants import (
        ACTIVITY_PARTICIPANTS_WINDOW_SECONDS,
        ACTIVITY_QUERY_MAX_SESSIONS,
        BOT_ACTIVITY_SENDER_ID,
        DISPLAY_NAME,
    )
    from ..infra.runtime import RateLimitedLogger, resolve_timezone
else:
    from infra.constants import (
        ACTIVITY_PARTICIPANTS_WINDOW_SECONDS,
        ACTIVITY_QUERY_MAX_SESSIONS,
        BOT_ACTIVITY_SENDER_ID,
        DISPLAY_NAME,
    )
    from infra.runtime import RateLimitedLogger, resolve_timezone

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
        watermark = self.store.inbox_repo.get_watermark(
            persona,
            session["platform_id"],
            session["kind"],
            session["conversation_id"],
        )
        effective_baseline = max(baseline, watermark or 0)
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
                effective_baseline,
                *excluded,
                persona,
            ),
        )[0]

    def batch_counts(self, persona, sessions, event=None):
        if not sessions:
            return {}
        result = {
            (
                str(s["platform_id"]),
                str(s["kind"]),
                str(s["conversation_id"]),
            ): {"unread": 0, "unread_mentions": 0}
            for s in sessions
        }
        if not self.plugin.resident_inbox_enabled:
            return result
        allowed_sessions = [s for s in sessions if self.allowed(s, event)]
        if not allowed_sessions:
            return result
        baseline = int(self.store.meta("inbox_baseline", "0"))
        result.update(
            self.store.inbox_repo.batch_counts(persona, baseline, allowed_sessions)
        )
        return result

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
            self.store.inbox_repo.mark_channel_watermark(
                persona, platform, kind, conversation_id, baseline, time.time()
            )
        except Exception as exc:
            _RATE_LOG.warning("mark_channel", "[%s] 整频道标读失败: %s", DISPLAY_NAME, exc)

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

    def _display_conv_name(
        self, platform_id: str, kind: str, conversation_id: str, name: str
    ) -> str:
        activity = getattr(self.plugin, "activity", None)
        if activity is not None and callable(
            getattr(activity, "display_conversation_name", None)
        ):
            return activity.display_conversation_name(
                platform_id, kind, conversation_id, name
            )
        return self.host.display_conversation_name(
            platform_id, kind, conversation_id, name
        )

    def _display_user_name(
        self, platform_id: str, sender_id: str, sender_name: str
    ) -> str:
        activity = getattr(self.plugin, "activity", None)
        if activity is not None and callable(
            getattr(activity, "display_user_name", None)
        ):
            return activity.display_user_name(platform_id, sender_id, sender_name)
        return self.host.display_user_name(platform_id, sender_id, sender_name)

    def _format_ts(self, ts: float) -> str:
        tz = resolve_timezone(self.plugin.context)
        if tz is not None:
            try:
                return datetime.fromtimestamp(float(ts or 0), tz=tz).strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
            except Exception:
                pass
        return datetime.fromtimestamp(float(ts or 0)).strftime("%Y-%m-%d %H:%M:%S")

    def _format_row(
        self,
        row: dict[str, Any],
        unread_ids: set[int],
        *,
        suppress_mention_status: bool = False,
    ) -> dict[str, Any]:
        plat = str(row.get("platform_id") or "")
        skind = str(row.get("kind") or "")
        cid = str(row.get("conversation_id") or "")
        sid = str(row.get("sender_id") or "")
        is_self = sid == BOT_ACTIVITY_SENDER_ID
        mentioned = bool(row.get("mentioned"))
        row_id = int(row["id"])
        if not mentioned or suppress_mention_status:
            mention_status = ""
        elif row_id in unread_ids:
            mention_status = "new"
        else:
            mention_status = "old"
        mention_age = ""
        if mentioned and not suppress_mention_status:
            age_sec = max(0, int(time.time() - float(row.get("ts") or 0)))
            if age_sec < 60:
                age_str = "刚刚"
            elif age_sec < 3600:
                age_str = f"{age_sec // 60}分钟前"
            elif age_sec < 86400:
                age_str = f"{age_sec // 3600}小时{(age_sec % 3600) // 60}分钟前"
            else:
                age_str = f"{age_sec // 86400}天前"
            mention_age = f"{age_str}（不是新消息）" if mention_status == "old" else age_str
        return {
            "id": row_id,
            "umo": (
                f"{plat}:{'GroupMessage' if skind == 'group' else 'FriendMessage'}:{cid}"
            ),
            "conversation": self._display_conv_name(
                plat, skind, cid, str(row.get("conversation_name") or "")
            ),
            "kind": skind,
            "sender_id": sid,
            "sender": (
                "（我）"
                if is_self
                else self._display_user_name(
                    plat, sid, str(row.get("sender_name") or "")
                )
            ),
            "is_self": is_self,
            "time": self._format_ts(float(row.get("ts") or 0)),
            "text": str(row.get("text") or ""),
            "mentioned": mentioned,
            "mention_status": mention_status,
            "mention_age": mention_age,
            "truncated": bool(row.get("truncated")),
        }

    def _resolve_target_session(
        self, event: Any, conversation: str, norm_kind: str
    ) -> dict[str, Any]:
        if ":" in conversation:
            pieces = conversation.split(":", 2)
            if len(pieces) == 3 and all(piece.strip() for piece in pieces):
                plat = pieces[0].strip()
                msg_type = pieces[1].strip()
                cid = pieces[2].strip()
                low_mt = msg_type.lower()
                is_msg_type = msg_type in {
                    "GroupMessage",
                    "FriendMessage",
                    "OtherMessage",
                } or any(
                    token in low_mt
                    for token in (
                        "message",
                        "friend",
                        "private",
                        "group",
                        "c2c",
                        "dm",
                        "channel",
                    )
                )
                if is_msg_type:
                    if msg_type == "GroupMessage":
                        skind = "group"
                    elif msg_type == "FriendMessage":
                        skind = "private"
                    else:
                        skind = (
                            "private"
                            if any(
                                token in low_mt
                                for token in ("friend", "private", "c2c", "dm")
                            )
                            else "group"
                        )
                    if not norm_kind or norm_kind == skind:
                        all_sessions = self.host.activity_sessions(norm_kind or "all")
                        matched = [
                            s
                            for s in all_sessions
                            if (s["platform_id"], s["kind"], s["conversation_id"])
                            == (plat, skind, cid)
                        ]
                        if matched:
                            if not self.allowed(matched[0], event):
                                raise ValueError("无权限查看该会话")
                            return matched[0]

        matches = self.host.resolve_activity_conversations(conversation, norm_kind)
        matches = [s for s in matches if self.allowed(s, event)]
        if len(matches) > 1:
            candidates = ", ".join(
                s.get("umo")
                or f"{s['platform_id']}:{'GroupMessage' if s['kind'] == 'group' else 'FriendMessage'}:{s['conversation_id']}"
                for s in matches[:5]
            )
            raise ValueError(f"会话名称不唯一，请使用完整 umo：{candidates}")
        if not matches:
            raise ValueError("记录里找不到这个会话；可不传 conversation 查看全部会话。")
        return matches[0]

    def _unread_ids_for_rows(
        self, persona: str, rows: list[dict[str, Any]]
    ) -> set[int]:
        if not self.plugin.resident_inbox_enabled or not rows:
            return set()
        inbox_baseline = int(self.store.meta("inbox_baseline", "0"))
        candidates = [
            r
            for r in rows
            if r.get("mentioned")
            and r.get("sender_id") != BOT_ACTIVITY_SENDER_ID
            and int(r["id"]) > inbox_baseline
        ]
        if not candidates:
            return set()
        wm_cache: dict[tuple[str, str, str], int] = {}
        above_wm: list[int] = []
        for r in candidates:
            key = (
                str(r.get("platform_id") or ""),
                str(r.get("kind") or ""),
                str(r.get("conversation_id") or ""),
            )
            if key not in wm_cache:
                wm = self.store.inbox_repo.get_watermark(persona, *key)
                wm_cache[key] = max(inbox_baseline, wm or 0)
            if int(r["id"]) > wm_cache[key]:
                above_wm.append(int(r["id"]))
        if not above_wm:
            return set()
        placeholders = ",".join("?" for _ in above_wm)
        read_rows = self.store.rows(
            f"SELECT message_id FROM resident_reads WHERE persona=? AND message_id IN ({placeholders})",
            (persona, *above_wm),
        )
        read_set = {int(r["message_id"]) for r in read_rows}
        return {mid for mid in above_wm if mid not in read_set}

    def _read_directory_sync(
        self, event: Any, persona: str, kind_raw: str, only_unread: bool, limit: int
    ) -> dict[str, Any]:
        p = self.plugin
        sessions = self.host.activity_sessions(
            "" if kind_raw in ("", "all") else kind_raw
        )
        sessions = [s for s in sessions if self.allowed(s, event)]
        if not self.host.is_self_wake_event(event):
            current = self.host.current_conversation(event)
            if current is not None:
                sessions = [
                    s
                    for s in sessions
                    if (s["platform_id"], s["kind"], s["conversation_id"]) != current
                ]
        counts_map = (
            self.batch_counts(persona, sessions, event)
            if p.resident_inbox_enabled
            else {}
        )
        if only_unread:
            sessions = [
                s
                for s in sessions
                if counts_map.get(
                    (s["platform_id"], s["kind"], s["conversation_id"]), {}
                ).get("unread", 0)
                > 0
            ]
        total = len(sessions)

        def _sort_key(s: dict[str, Any]) -> tuple[int, int, int, int, float]:
            key = (s["platform_id"], s["kind"], s["conversation_id"])
            c = counts_map.get(key, {})
            unread = int(c.get("unread", 0) or 0)
            mentions = int(c.get("unread_mentions", 0) or 0)
            last_ts = float(s.get("last_ts") or 0)
            return (-int(mentions > 0), -int(unread > 0), -mentions, -unread, -last_ts)

        sessions.sort(key=_sort_key)
        selected = sessions[:limit]
        recent_since = time.time() - ACTIVITY_PARTICIPANTS_WINDOW_SECONDS
        batch_fn = getattr(self.host, "batch_activity_participants", None)
        if callable(batch_fn):
            speakers_map = batch_fn(selected, recent_since)
        else:
            speakers_map = self.plugin.activity.batch_activity_participants(
                selected, recent_since
            )
        items: list[dict[str, Any]] = []
        for s in selected:
            key = (s["platform_id"], s["kind"], s["conversation_id"])
            c = counts_map.get(key, {})
            umo = (
                s.get("umo")
                or f"{s['platform_id']}:{'GroupMessage' if s['kind'] == 'group' else 'FriendMessage'}:{s['conversation_id']}"
            )
            items.append(
                {
                    "umo": umo,
                    "kind": s["kind"],
                    "name": self._display_conv_name(
                        s["platform_id"],
                        s["kind"],
                        s["conversation_id"],
                        str(s.get("name", "")),
                    ),
                    "unread": int(c.get("unread", 0) or 0),
                    "unread_mentions": int(c.get("unread_mentions", 0) or 0),
                    "recent_speakers": list(speakers_map.get(key, [])),
                    "last_time": self._format_ts(float(s.get("last_ts") or 0)),
                    "last_text": str(s.get("last_text") or ""),
                }
            )
        return {
            "count": len(items),
            "total_sessions": total,
            "hidden_count": max(0, total - len(items)),
            "conversations": items,
        }

    def _read_incremental_sync(
        self,
        event: Any,
        persona: str,
        conversation: str,
        norm_kind: str,
        limit: int,
    ) -> dict[str, Any]:
        target = self._resolve_target_session(event, conversation, norm_kind)
        plat = str(target["platform_id"])
        skind = str(target["kind"])
        cid = str(target["conversation_id"])

        if not self.plugin.resident_inbox_enabled:
            rows = self.store.rows(
                "SELECT m.* FROM activity_messages m "
                "WHERE m.platform_id=? AND m.kind=? AND m.conversation_id=? "
                "ORDER BY m.id DESC LIMIT ?",
                (plat, skind, cid, limit + 1),
            )
            has_more = len(rows) > limit
            display = list(reversed(rows[:limit]))
            formatted = [self._format_row(r, set()) for r in display]
            return {
                "count": len(formatted),
                "results": formatted,
                "has_more": has_more,
                "next_before_id": int(display[0]["id"]) if display else None,
                "marked_read": False,
            }

        inbox_baseline = int(self.store.meta("inbox_baseline", "0"))
        pool_limit = max(limit * 4, 100)
        snapshot = self.store.inbox_repo.fetch_incremental_window(
            persona, plat, skind, cid, limit, pool_limit, inbox_baseline
        )
        watermark = snapshot["watermark"]
        effective_baseline = snapshot["effective_baseline"]
        recent_external = snapshot["recent_external"]
        pool = snapshot["pool"]
        read_ids = snapshot["read_ids"]
        first_sight = watermark is None

        unread_in_window = [
            r
            for r in recent_external
            if int(r["id"]) > effective_baseline and int(r["id"]) not in read_ids
        ]

        caller_row = None
        caller_mid = self.host.message_id(event)
        current = self.host.current_conversation(event)
        if caller_mid and current == (plat, skind, cid):
            for row in pool:
                if str(row.get("message_id") or "") == caller_mid:
                    caller_row = row
                    break

        if unread_in_window:
            unread_id_set = {int(r["id"]) for r in unread_in_window}
            selected: dict[int, dict[str, Any]] = {}
            for row in reversed([r for r in unread_in_window if r.get("mentioned")]):
                if len(selected) >= limit:
                    break
                selected[int(row["id"])] = row
            if caller_row is not None and len(selected) < limit:
                selected.setdefault(int(caller_row["id"]), caller_row)
            first_unread_id = int(unread_in_window[0]["id"])
            for idx in range(len(pool) - 1, -1, -1):
                if len(selected) >= limit:
                    break
                row = pool[idx]
                if (
                    row.get("sender_id") == BOT_ACTIVITY_SENDER_ID
                    and int(row["id"]) >= first_unread_id
                ):
                    selected.setdefault(int(row["id"]), row)
                    if idx > 0 and len(selected) < limit:
                        prev_row = pool[idx - 1]
                        if prev_row.get("sender_id") != BOT_ACTIVITY_SENDER_ID:
                            selected.setdefault(int(prev_row["id"]), prev_row)
            for row in reversed(unread_in_window):
                if len(selected) >= limit:
                    break
                selected.setdefault(int(row["id"]), row)

            display = [selected[k] for k in sorted(selected)]
            shown_ids = {int(r["id"]) for r in display}
            shown_external_ids = [
                int(r["id"])
                for r in display
                if r.get("sender_id") != BOT_ACTIVITY_SENDER_ID
                and int(r["id"]) > inbox_baseline
            ]
            if shown_external_ids:
                self.mark(persona, shown_external_ids)

            max_shown_ext = max(shown_external_ids, default=int(display[-1]["id"]))
            new_wm = max(watermark or 0, max_shown_ext)
            self.store.inbox_repo.set_watermark(
                persona, plat, skind, cid, new_wm, time.time()
            )
            unshown_in_window = [
                r for r in unread_in_window if int(r["id"]) not in shown_ids
            ]
            has_more = self.store.inbox_repo.has_older_message(
                plat, skind, cid, int(display[0]["id"])
            )
            formatted = [self._format_row(r, unread_id_set) for r in display]
            payload: dict[str, Any] = {
                "count": len(formatted),
                "results": formatted,
                "has_more": has_more,
                "next_before_id": int(display[0]["id"]),
                "marked_read": bool(shown_external_ids),
                "unshown_unread_count": len(unshown_in_window),
                "unshown_before_id": (
                    int(unshown_in_window[-1]["id"]) + 1 if unshown_in_window else None
                ),
            }
            if unshown_in_window:
                payload["note"] = (
                    f"另有 {len(unshown_in_window)} 条更早的新消息这一屏摆不下——"
                    f"如需往前看请传 before_id={int(unshown_in_window[-1]['id']) + 1}"
                )
            return payload

        if first_sight:
            display = pool[-limit:]
            shown_external_ids = [
                int(r["id"])
                for r in display
                if r.get("sender_id") != BOT_ACTIVITY_SENDER_ID
                and int(r["id"]) > inbox_baseline
            ]
            if shown_external_ids:
                self.mark(persona, shown_external_ids)
            init_wm = max(
                (
                    int(r["id"])
                    for r in pool
                    if r.get("sender_id") != BOT_ACTIVITY_SENDER_ID
                ),
                default=(int(pool[-1]["id"]) if pool else 0),
            )
            self.store.inbox_repo.set_watermark(
                persona, plat, skind, cid, init_wm, time.time()
            )
            has_more = len(pool) > len(display)
            formatted = [self._format_row(r, set()) for r in display]
            return {
                "count": len(formatted),
                "results": formatted,
                "has_more": has_more,
                "next_before_id": int(display[0]["id"]) if display else None,
                "marked_read": bool(shown_external_ids),
            }

        max_pool_ext = max(
            (
                int(r["id"])
                for r in pool
                if r.get("sender_id") != BOT_ACTIVITY_SENDER_ID
            ),
            default=0,
        )
        if max_pool_ext > (watermark or 0):
            self.store.inbox_repo.set_watermark(
                persona, plat, skind, cid, max_pool_ext, time.time()
            )
        latest_id = int(pool[-1]["id"]) if pool else 0
        return {
            "status": "no_new_messages",
            "note": "你已读过此会话，截至现在没有新消息",
            "count": 0,
            "results": [],
            "has_more": bool(latest_id),
            "next_before_id": latest_id + 1 if latest_id else None,
            "marked_read": False,
        }

    def _read_history_sync(
        self,
        event: Any,
        persona: str,
        conversation: str,
        norm_kind: str,
        before_id: int,
        limit: int,
    ) -> dict[str, Any]:
        target = self._resolve_target_session(event, conversation, norm_kind)
        plat = str(target["platform_id"])
        skind = str(target["kind"])
        cid = str(target["conversation_id"])

        page = self.store.inbox_repo.fetch_history_page(
            plat, skind, cid, before_id, limit
        )
        rows = page["rows"]
        newest_row = page["newest_row"]
        has_more = len(rows) > limit
        display = list(reversed(rows[:limit]))

        marked_read = False
        if self.plugin.resident_inbox_enabled and display and newest_row is not None:
            has_external = any(
                r.get("sender_id") != BOT_ACTIVITY_SENDER_ID for r in display
            )
            covers_latest = int(display[-1]["id"]) >= int(newest_row["id"])
            if has_external and covers_latest:
                inbox_baseline = int(self.store.meta("inbox_baseline", "0"))
                shown_external_ids = [
                    int(r["id"])
                    for r in display
                    if r.get("sender_id") != BOT_ACTIVITY_SENDER_ID
                    and int(r["id"]) > inbox_baseline
                ]
                if shown_external_ids:
                    watermark = self.store.inbox_repo.get_watermark(
                        persona, plat, skind, cid
                    )
                    self.mark(persona, shown_external_ids)
                    new_wm = max(watermark or 0, max(shown_external_ids))
                    self.store.inbox_repo.set_watermark(
                        persona, plat, skind, cid, new_wm, time.time()
                    )
                    marked_read = True

        formatted = [
            self._format_row(r, set(), suppress_mention_status=True) for r in display
        ]
        return {
            "count": len(formatted),
            "results": formatted,
            "has_more": has_more,
            "next_before_id": int(display[0]["id"]) if display else None,
            "marked_read": marked_read,
        }

    def _read_search_sync(
        self,
        event: Any,
        persona: str,
        conversation: str,
        norm_kind: str,
        before_id: int,
        after_ts: float | None,
        sender: str,
        keyword: str,
        limit: int,
    ) -> dict[str, Any]:
        if conversation:
            sessions = [self._resolve_target_session(event, conversation, norm_kind)]
        else:
            sessions = self.host.activity_sessions(norm_kind or "all")
            sessions = [s for s in sessions if self.allowed(s, event)]
        if not sessions:
            return {
                "count": 0,
                "results": [],
                "has_more": False,
                "next_before_id": None,
                "marked_read": False,
            }
        sessions = sessions[:ACTIVITY_QUERY_MAX_SESSIONS]
        where: list[str] = []
        args: list[Any] = []
        scopes = []
        for s in sessions:
            scopes.append("(m.platform_id=? AND m.kind=? AND m.conversation_id=?)")
            args.extend((s["platform_id"], s["kind"], s["conversation_id"]))
        where.append("(" + " OR ".join(scopes) + ")")
        if before_id > 0:
            where.append("m.id<?")
            args.append(before_id)
        if after_ts is not None:
            where.append("m.ts>=?")
            args.append(after_ts)
        if sender:
            where.append("(m.sender_id=? OR m.sender_name LIKE ? ESCAPE '\\')")
            args.extend((sender, "%" + _escape_like(sender) + "%"))
        if keyword:
            where.append("instr(m.text,?)>0")
            args.append(keyword)

        rows = self.store.rows(
            "SELECT m.* FROM activity_messages m WHERE "
            + " AND ".join(where)
            + " ORDER BY m.id DESC LIMIT ?",
            (*args, limit + 1),
        )
        has_more = len(rows) > limit
        display = list(reversed(rows[:limit]))
        unread_id_set = self._unread_ids_for_rows(persona, display)
        formatted = [self._format_row(r, unread_id_set) for r in display]
        return {
            "count": len(formatted),
            "results": formatted,
            "has_more": has_more,
            "next_before_id": int(display[0]["id"]) if display else None,
            "marked_read": False,
        }

    async def read(
        self,
        event,
        conversation="",
        kind="",
        before_id=0,
        after="",
        sender="",
        keyword="",
        only_unread=False,
        limit=20,
    ):
        conversation = str(conversation or "").strip()
        kind_raw = str(kind or "").strip().lower()
        if kind_raw not in ("", "all", "group", "private"):
            raise ValueError("kind 须为 all / group / private")
        norm_kind = "" if kind_raw in ("", "all") else kind_raw
        try:
            before_id = max(0, int(before_id or 0))
        except (TypeError, ValueError) as exc:
            raise ValueError("before_id 必须是整数") from exc
        after = str(after or "").strip()
        sender = str(sender or "").strip()
        keyword = str(keyword or "").strip()
        only_unread = bool(only_unread)
        try:
            limit = max(1, min(50, int(limit)))
        except (TypeError, ValueError):
            limit = 20

        # Directory mode
        if conversation == "" and before_id == 0 and not any((after, sender, keyword)):
            if only_unread and not self.plugin.resident_inbox_enabled:
                raise ValueError("未读信箱已关闭，请传 only_unread=False 查看会话")
            persona, _ = await self.service.persona.resolve(event)
            return await self.store.arun(
                self._read_directory_sync,
                event,
                persona,
                kind_raw,
                only_unread,
                limit,
            )

        persona, _ = await self.service.persona.resolve(event)

        # Search mode (or cross-session paging with before_id when conversation == "")
        if any((after, sender, keyword)) or conversation == "":
            after_ts, error = self.host.parse_activity_time(after)
            if error:
                raise ValueError(error)
            return await self.store.arun(
                self._read_search_sync,
                event,
                persona,
                conversation,
                norm_kind,
                before_id,
                after_ts,
                sender,
                keyword,
                limit,
            )

        # History paging mode
        if before_id > 0:
            return await self.store.arun(
                self._read_history_sync,
                event,
                persona,
                conversation,
                norm_kind,
                before_id,
                limit,
            )

        # Incremental read mode
        return await self.store.arun(
            self._read_incremental_sync,
            event,
            persona,
            conversation,
            norm_kind,
            limit,
        )
