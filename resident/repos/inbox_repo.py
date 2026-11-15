"""Repository helpers for read receipts, channel watermarks, and inbox metadata."""

from __future__ import annotations

from typing import Any

if __package__ and __package__.count(".") >= 2:
    from ...infra.constants import ACTIVITY_QUERY_MAX_SESSIONS, BOT_ACTIVITY_SENDER_ID
    from ...infra.database import ActivityDatabase
else:
    from infra.constants import ACTIVITY_QUERY_MAX_SESSIONS, BOT_ACTIVITY_SENDER_ID
    from infra.database import ActivityDatabase


class InboxRepository:
    def __init__(self, db: ActivityDatabase) -> None:
        self.db = db

    def mark_ids(self, persona: str, ids: list[int]) -> None:
        if not ids:
            return
        with self.db.transaction() as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO resident_reads(persona,message_id) SELECT ?,id FROM activity_messages WHERE id=?",
                ((persona, i) for i in ids),
            )

    def prune_orphan_reads(self) -> int:
        with self.db.transaction() as conn:
            return conn.execute(
                "DELETE FROM resident_reads WHERE message_id NOT IN (SELECT id FROM activity_messages)"
            ).rowcount

    def get_watermark(
        self, persona: str, platform_id: str, kind: str, conversation_id: str
    ) -> int | None:
        rows = self.db.rows(
            "SELECT last_read_id FROM resident_channel_watermarks "
            "WHERE persona=? AND platform_id=? AND kind=? AND conversation_id=?",
            (persona, platform_id, kind, conversation_id),
        )
        return int(rows[0]["last_read_id"]) if rows else None

    @staticmethod
    def _cursor_dicts(cursor) -> list[dict[str, Any]]:
        names = [col[0] for col in (cursor.description or ())]
        return [dict(zip(names, row)) for row in cursor.fetchall()]

    def set_watermark(
        self,
        persona: str,
        platform_id: str,
        kind: str,
        conversation_id: str,
        last_read_id: int,
        updated: float,
    ) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO resident_channel_watermarks "
                "(persona, platform_id, kind, conversation_id, last_read_id, updated) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(persona, platform_id, kind, conversation_id) DO UPDATE SET "
                "last_read_id = MAX(resident_channel_watermarks.last_read_id, excluded.last_read_id), "
                "updated = excluded.updated",
                (
                    persona,
                    platform_id,
                    kind,
                    conversation_id,
                    int(last_read_id),
                    float(updated),
                ),
            )

    def has_older_message(
        self, platform_id: str, kind: str, conversation_id: str, before_id: int
    ) -> bool:
        rows = self.db.rows(
            "SELECT 1 FROM activity_messages "
            "WHERE platform_id=? AND kind=? AND conversation_id=? AND id<? LIMIT 1",
            (platform_id, kind, conversation_id, int(before_id)),
        )
        return bool(rows)

    def fetch_incremental_window(
        self,
        persona: str,
        platform_id: str,
        kind: str,
        conversation_id: str,
        limit: int,
        pool_limit: int,
        inbox_baseline: int,
    ) -> dict[str, Any]:
        with self.db.read_conn() as conn:
            watermark = self.get_watermark(persona, platform_id, kind, conversation_id)
            effective_baseline = max(int(inbox_baseline), watermark or 0)
            recent_ext_desc = self._cursor_dicts(
                conn.execute(
                    "SELECT m.* FROM activity_messages m "
                    "WHERE m.platform_id=? AND m.kind=? AND m.conversation_id=? "
                    "AND m.sender_id!=? "
                    "ORDER BY m.id DESC LIMIT ?",
                    (
                        platform_id,
                        kind,
                        conversation_id,
                        BOT_ACTIVITY_SENDER_ID,
                        int(limit),
                    ),
                )
            )
            recent_external = list(reversed(recent_ext_desc))
            pool_desc = self._cursor_dicts(
                conn.execute(
                    "SELECT m.* FROM activity_messages m "
                    "WHERE m.platform_id=? AND m.kind=? AND m.conversation_id=? "
                    "ORDER BY m.id DESC LIMIT ?",
                    (platform_id, kind, conversation_id, int(pool_limit)),
                )
            )
            pool = list(reversed(pool_desc))
            candidate_ids = [
                int(r["id"])
                for r in recent_external
                if int(r["id"]) > effective_baseline
            ]
            read_ids: set[int] = set()
            if candidate_ids:
                placeholders = ",".join("?" for _ in candidate_ids)
                read_rows = conn.execute(
                    f"SELECT message_id FROM resident_reads WHERE persona=? AND message_id IN ({placeholders})",
                    (persona, *candidate_ids),
                ).fetchall()
                read_ids = {int(row[0]) for row in read_rows}
            return {
                "watermark": watermark,
                "effective_baseline": effective_baseline,
                "recent_external": recent_external,
                "pool": pool,
                "read_ids": read_ids,
            }

    def fetch_history_page(
        self,
        platform_id: str,
        kind: str,
        conversation_id: str,
        before_id: int,
        limit: int,
    ) -> dict[str, Any]:
        with self.db.read_conn() as conn:
            rows = self._cursor_dicts(
                conn.execute(
                    "SELECT m.* FROM activity_messages m "
                    "WHERE m.platform_id=? AND m.kind=? AND m.conversation_id=? AND m.id<? "
                    "ORDER BY m.id DESC LIMIT ?",
                    (platform_id, kind, conversation_id, int(before_id), int(limit) + 1),
                )
            )
            newest_rows = self._cursor_dicts(
                conn.execute(
                    "SELECT id, sender_id FROM activity_messages "
                    "WHERE platform_id=? AND kind=? AND conversation_id=? "
                    "ORDER BY id DESC LIMIT 1",
                    (platform_id, kind, conversation_id),
                )
            )
            return {
                "rows": rows,
                "newest_row": newest_rows[0] if newest_rows else None,
            }

    def batch_counts(
        self, persona: str, baseline: int, sessions: list[dict[str, Any]]
    ) -> dict[tuple[str, str, str], dict[str, int]]:
        result: dict[tuple[str, str, str], dict[str, int]] = {}
        keys: list[tuple[str, str, str]] = []
        seen: set[tuple[str, str, str]] = set()
        for session in sessions or ():
            key = (
                str(session.get("platform_id") or ""),
                str(session.get("kind") or ""),
                str(session.get("conversation_id") or ""),
            )
            result.setdefault(key, {"unread": 0, "unread_mentions": 0})
            if key not in seen:
                seen.add(key)
                keys.append(key)
        if not keys:
            return result
        chunk_size = max(1, int(ACTIVITY_QUERY_MAX_SESSIONS))
        base = int(baseline)
        for idx in range(0, len(keys), chunk_size):
            chunk = keys[idx : idx + chunk_size]
            clauses = " OR ".join(
                "(m.platform_id=? AND m.kind=? AND m.conversation_id=?)"
                for _ in chunk
            )
            params: list[Any] = [persona]
            for plat, skind, cid in chunk:
                params.extend((plat, skind, cid))
            params.extend((base, BOT_ACTIVITY_SENDER_ID, persona))
            rows = self.db.rows(
                f"""
                SELECT m.platform_id, m.kind, m.conversation_id,
                       COUNT(*) AS unread,
                       COALESCE(SUM(m.mentioned), 0) AS unread_mentions
                FROM activity_messages m
                LEFT JOIN resident_channel_watermarks w
                  ON w.persona = ?
                 AND w.platform_id = m.platform_id
                 AND w.kind = m.kind
                 AND w.conversation_id = m.conversation_id
                WHERE ({clauses})
                  AND m.id > MAX(?, COALESCE(w.last_read_id, 0))
                  AND m.sender_id != ?
                  AND NOT EXISTS (
                      SELECT 1 FROM resident_reads r
                      WHERE r.persona = ? AND r.message_id = m.id
                  )
                GROUP BY m.platform_id, m.kind, m.conversation_id
                """,
                tuple(params),
            )
            for row in rows:
                key = (
                    str(row["platform_id"]),
                    str(row["kind"]),
                    str(row["conversation_id"]),
                )
                result[key] = {
                    "unread": int(row["unread"] or 0),
                    "unread_mentions": int(row["unread_mentions"] or 0),
                }
        return result

    def mark_channel_watermark(
        self,
        persona: str,
        platform_id: str,
        kind: str,
        conversation_id: str,
        baseline: int,
        updated: float,
    ) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO resident_reads(persona, message_id) "
                "SELECT ?, id FROM activity_messages "
                "WHERE platform_id=? AND kind=? AND conversation_id=? AND id>? AND sender_id!=?",
                (
                    persona,
                    platform_id,
                    kind,
                    conversation_id,
                    int(baseline),
                    BOT_ACTIVITY_SENDER_ID,
                ),
            )
            row = conn.execute(
                "SELECT COALESCE(MAX(id), 0) FROM activity_messages "
                "WHERE platform_id=? AND kind=? AND conversation_id=?",
                (platform_id, kind, conversation_id),
            ).fetchone()
            max_id = int(row[0] if row else 0)
            conn.execute(
                "INSERT INTO resident_channel_watermarks "
                "(persona, platform_id, kind, conversation_id, last_read_id, updated) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(persona, platform_id, kind, conversation_id) DO UPDATE SET "
                "last_read_id = MAX(resident_channel_watermarks.last_read_id, excluded.last_read_id), "
                "updated = excluded.updated",
                (
                    persona,
                    platform_id,
                    kind,
                    conversation_id,
                    max_id,
                    float(updated),
                ),
            )
