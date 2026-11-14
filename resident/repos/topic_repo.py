"""Repository for per-session topic summary cursors and historical segments."""

from __future__ import annotations


if __package__ and __package__.count(".") >= 2:
    from ...infra.database import ActivityDatabase
else:
    from infra.database import ActivityDatabase


class TopicRepository:
    def __init__(self, db: ActivityDatabase) -> None:
        self.db = db

    def meta(self, key: str, default: str = "") -> str:
        rows = self.db.rows("SELECT value FROM resident_meta WHERE key=?", (key,))
        return rows[0]["value"] if rows else default

    def topic_state(self, umo: str) -> dict:
        topic = self.meta(f"topic:{umo}", "")
        try:
            topic_to_id = int(self.meta(f"topic_to_id:{umo}", "0"))
        except (TypeError, ValueError):
            topic_to_id = 0
        try:
            updated_at = float(self.meta(f"topic_updated_at:{umo}", "0"))
        except (TypeError, ValueError):
            updated_at = 0.0
        return {"topic": topic, "topic_to_id": topic_to_id, "topic_updated_at": updated_at}

    def set_topic_state(self, umo: str, topic: str, topic_to_id: int, updated_ts: float) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO resident_meta VALUES (?,?)",
                (f"topic:{umo}", str(topic or "")),
            )
            conn.execute(
                "INSERT OR REPLACE INTO resident_meta VALUES (?,?)",
                (f"topic_to_id:{umo}", str(int(topic_to_id or 0))),
            )
            conn.execute(
                "INSERT OR REPLACE INTO resident_meta VALUES (?,?)",
                (f"topic_updated_at:{umo}", str(float(updated_ts or 0))),
            )

    def append_topic_segment(
        self, umo: str, start_msg_id: int, end_msg_id: int, start_ts: float, end_ts: float, summary: str
    ) -> None:
        if not summary:
            return
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO resident_topic_segments(umo,start_msg_id,end_msg_id,start_ts,end_ts,summary) VALUES (?,?,?,?,?,?)",
                (umo, int(start_msg_id), int(end_msg_id), float(start_ts), float(end_ts), str(summary)),
            )

    def topic_segments(self, umo: str, limit: int = 3) -> list[dict]:
        return self.db.rows(
            "SELECT * FROM resident_topic_segments WHERE umo=? ORDER BY id DESC LIMIT ?",
            (umo, max(1, int(limit))),
        )

    def topic_segment_latest_end(self, umo: str) -> int:
        rows = self.db.rows(
            "SELECT end_msg_id FROM resident_topic_segments WHERE umo=? ORDER BY id DESC LIMIT 1",
            (umo,),
        )
        return int(rows[0]["end_msg_id"]) if rows else 0

    def prune_stale_topics(self, active_umos: set[str]) -> int:
        rows = self.db.rows("SELECT key FROM resident_meta WHERE key LIKE 'topic:%' OR key LIKE 'topic_to_id:%' OR key LIKE 'topic_updated_at:%'")
        stale_keys = []
        for row in rows:
            key = str(row["key"])
            umo = key.split(":", 1)[1] if ":" in key else ""
            if umo and umo not in active_umos:
                stale_keys.append((key,))
        if not stale_keys:
            return 0
        with self.db.transaction() as conn:
            cur = conn.executemany("DELETE FROM resident_meta WHERE key=?", stale_keys)
            return cur.rowcount if cur.rowcount >= 0 else len(stale_keys)
