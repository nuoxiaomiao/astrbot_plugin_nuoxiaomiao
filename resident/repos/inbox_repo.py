"""Repository helpers for read receipts and inbox metadata."""

from __future__ import annotations

if __package__ and __package__.count(".") >= 2:
    from ...infra.database import ActivityDatabase
else:
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
