"""Repository for kept message images and vision caption caches."""

from __future__ import annotations

from pathlib import Path
import time

if __package__ and __package__.count(".") >= 2:
    from ...infra.database import ActivityDatabase
else:
    from infra.database import ActivityDatabase


class VisionRepository:
    def __init__(self, db: ActivityDatabase) -> None:
        self.db = db

    def vision_keep(
        self, umo: str, platform_message_id: str, activity_message_id: int, idx: int, path: str
    ) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO vision_kept_images(umo,platform_message_id,activity_message_id,idx,path,ts) VALUES (?,?,?,?,?,?)",
                (umo, platform_message_id, int(activity_message_id or 0), int(idx), path, time.time()),
            )

    def vision_kept_for(self, umo: str, platform_message_id: str) -> list[dict]:
        return self.db.rows(
            "SELECT * FROM vision_kept_images WHERE umo=? AND platform_message_id=? ORDER BY idx, id",
            (umo, platform_message_id),
        )

    def vision_caption_get(self, key: str) -> str:
        rows = self.db.rows(
            "SELECT caption FROM vision_caption_cache WHERE key=?", (key,)
        )
        return str(rows[0]["caption"]) if rows else ""

    def vision_caption_set(self, key: str, caption: str) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO vision_caption_cache(key,caption,ts) VALUES (?,?,?)",
                (key, caption, time.time()),
            )

    def vision_cleanup(
        self,
        keep_days: int,
        ttl_days: int,
        *,
        max_kept_files: int = 5000,
    ) -> tuple[int, int, int]:
        now = time.time()
        expired_paths: list[str] = []
        deleted_ids: list[int] = []
        if int(keep_days) >= 0:
            rows = self.db.rows(
                "SELECT id,path FROM vision_kept_images WHERE ts<?",
                (now - int(keep_days) * 86400,),
            )
            for row in rows:
                deleted_ids.append(int(row["id"]))
                expired_paths.append(str(row["path"] or ""))
        if max_kept_files > 0:
            total = self.db.rows("SELECT COUNT(*) AS cnt FROM vision_kept_images")[0]["cnt"]
            excess = int(total) - len(deleted_ids) - max_kept_files
            if excess > 0:
                extra_rows = self.db.rows(
                    "SELECT id,path FROM vision_kept_images ORDER BY id ASC LIMIT ?",
                    (excess,),
                )
                for row in extra_rows:
                    rid = int(row["id"])
                    if rid not in deleted_ids:
                        deleted_ids.append(rid)
                        expired_paths.append(str(row["path"] or ""))

        removed_files = 0
        for p in expired_paths:
            if not p:
                continue
            try:
                target = Path(p)
                if target.is_file():
                    target.unlink()
                    removed_files += 1
            except OSError:
                continue

        removed_rows = 0
        with self.db.transaction() as conn:
            if deleted_ids:
                cursor = conn.executemany(
                    "DELETE FROM vision_kept_images WHERE id=?",
                    ((rid,) for rid in deleted_ids),
                )
                removed_rows = cursor.rowcount if cursor.rowcount >= 0 else len(deleted_ids)
            removed_desc = conn.execute(
                "DELETE FROM vision_caption_cache WHERE ts<?",
                (now - max(0, int(ttl_days)) * 86400,),
            ).rowcount
            # Prune cache entries whose backing kept file no longer exists on disk when keep_days expired.
            if removed_files:
                conn.execute(
                    "DELETE FROM vision_caption_cache WHERE ts<?",
                    (now - max(1, int(keep_days or 1)) * 86400,),
                )
        return (removed_rows, removed_files, removed_desc)

    def vision_stats(self) -> dict[str, int]:
        kept = self.db.rows("SELECT COUNT(*) AS cnt FROM vision_kept_images")[0]["cnt"]
        cached = self.db.rows("SELECT COUNT(*) AS cnt FROM vision_caption_cache")[0]["cnt"]
        return {"kept": int(kept), "cached": int(cached)}
