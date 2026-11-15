"""Repository for autonomous runs, budget charges, tool receipts, sends, and cleanup."""

from __future__ import annotations

import json
import sqlite3
import time

if __package__ and __package__.count(".") >= 2:
    from ...infra.database import ActivityDatabase
else:
    from infra.database import ActivityDatabase


class BudgetExceeded(RuntimeError):
    pass


def _delete_children(db: sqlite3.Connection, table: str, run_ids: list[int]) -> int:
    cursor = db.executemany(f"DELETE FROM {table} WHERE run_id=?", ((rid,) for rid in run_ids))
    return cursor.rowcount if cursor.rowcount >= 0 else len(run_ids)


class AutonomyRepository:
    def __init__(self, db: ActivityDatabase) -> None:
        self.db = db

    def new_run(self, umo: str, persona: str, day: str) -> int:
        with self.db.transaction() as conn:
            return int(
                conn.execute(
                    "INSERT INTO resident_runs(umo,persona,day,started) VALUES (?,?,?,?)",
                    (umo, persona, day, time.time()),
                ).lastrowid
            )

    def charge(self, run_id: int, day: str, limit: int) -> None:
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT charged,day FROM resident_runs WHERE id=?", (run_id,)
            ).fetchone()
            if not row:
                raise ValueError("运行记录不存在")
            if row[0]:
                return
            limit_val = int(limit)
            if limit_val > 0:
                used = conn.execute(
                    "SELECT COUNT(*) FROM resident_runs WHERE day=? AND charged=1", (day,)
                ).fetchone()[0]
                if used >= limit_val:
                    raise BudgetExceeded("今天的自醒次数已用完")
            conn.execute(
                "UPDATE resident_runs SET charged=1,status='running',day=? WHERE id=?",
                (day, run_id),
            )

    def budget(self, day: str) -> dict:
        used = self.db.rows(
            "SELECT COUNT(*) AS used FROM resident_runs WHERE day=? AND charged=1",
            (day,),
        )[0]["used"]
        usage = self.db.rows(
            "SELECT input_other+input_cached+output AS tokens,input_other+output AS uncached_tokens,unknown,input_cached FROM resident_daily_usage WHERE day=?",
            (day,),
        )
        return {
            "used": used,
            **(usage[0] if usage else {"tokens": 0, "uncached_tokens": 0, "unknown": 0, "input_cached": 0}),
        }

    def usage(self, run_id: int, values=None, day: str | None = None) -> None:
        with self.db.transaction() as conn:
            if not day:
                day = conn.execute(
                    "SELECT day FROM resident_runs WHERE id=?", (run_id,)
                ).fetchone()[0]
            conn.execute("INSERT OR IGNORE INTO resident_daily_usage(day) VALUES (?)", (day,))
            if values is None:
                conn.execute("UPDATE resident_runs SET usage_unknown=1 WHERE id=?", (run_id,))
                conn.execute("UPDATE resident_daily_usage SET unknown=1 WHERE day=?", (day,))
            else:
                conn.execute(
                    "UPDATE resident_runs SET input_other=input_other+?,input_cached=input_cached+?,output=output+? WHERE id=?",
                    (*values, run_id),
                )
                conn.execute(
                    "UPDATE resident_daily_usage SET input_other=input_other+?,input_cached=input_cached+?,output=output+? WHERE day=?",
                    (*values, day),
                )

    def begin_send(self, run_id: int, umo: str, body: str) -> int:
        with self.db.transaction() as conn:
            return int(
                conn.execute(
                    "INSERT INTO resident_sends(run_id,umo,body,started) VALUES (?,?,?,?)",
                    (run_id, umo, body, time.time()),
                ).lastrowid
            )

    def end_send(self, send_id: int, status: str, detail: str = "") -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE resident_sends SET status=?,detail=?,ended=? WHERE id=?",
                (status, str(detail)[:4000], time.time(), send_id),
            )

    def finish(
        self,
        run_id: int,
        status: str,
        outcome: str,
        summary: str = "",
        preserve_ended: bool = False,
    ) -> None:
        now = time.time()
        with self.db.transaction() as conn:
            if preserve_ended:
                conn.execute(
                    "UPDATE resident_runs SET status=?,outcome=?,summary=?,ended=COALESCE(ended, ?) WHERE id=?",
                    (status, outcome, summary[:4000], now, run_id),
                )
            else:
                conn.execute(
                    "UPDATE resident_runs SET status=?,outcome=?,summary=?,ended=? WHERE id=?",
                    (status, outcome, summary[:4000], now, run_id),
                )

    def update_outcome(self, run_id: int, status: str, outcome: str, summary: str = "") -> None:
        self.finish(run_id, status, outcome, summary, preserve_ended=True)

    def tool_requests(self, run_id: int, response) -> None:
        names = getattr(response, "tools_call_name", None) or []
        args = getattr(response, "tools_call_args", None) or []
        ids = getattr(response, "tools_call_ids", None) or []
        if not names:
            return
        with self.db.transaction() as conn:
            for i, name in enumerate(names):
                conn.execute(
                    "INSERT INTO resident_tool_requests(run_id,call_id,tool,args,created) VALUES (?,?,?,?,?)",
                    (
                        run_id,
                        str(ids[i] if i < len(ids) else ""),
                        str(name),
                        json.dumps(
                            args[i] if i < len(args) else {},
                            ensure_ascii=False,
                            default=str,
                        )[:8000],
                        time.time(),
                    ),
                )

    def action(self, run_id: int, tool: str, args) -> int:
        with self.db.transaction() as conn:
            return int(
                conn.execute(
                    "INSERT INTO resident_actions(run_id,tool,args,started) VALUES (?,?,?,?)",
                    (
                        run_id,
                        tool,
                        json.dumps(args, ensure_ascii=False, default=str)[:8000],
                        time.time(),
                    ),
                ).lastrowid
            )

    def end_action(self, action_id: int, status: str, result) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE resident_actions SET status=?,result=?,ended=? WHERE id=?",
                (status, str(result)[:8000], time.time(), action_id),
            )

    def recover(self) -> None:
        with self.db.transaction() as conn:
            conn.execute("UPDATE resident_sends SET status='unknown' WHERE status='sending'")
            conn.execute(
                "INSERT INTO resident_daily_usage(day,unknown) SELECT DISTINCT day,1 FROM resident_runs WHERE charged=1 AND status IN ('pending','preparing','running','paused','stopped') ON CONFLICT(day) DO UPDATE SET unknown=1"
            )
            conn.execute("UPDATE resident_actions SET status='unknown' WHERE status='running'")
            conn.execute(
                "UPDATE resident_runs SET status=CASE WHEN charged=1 THEN 'unknown' WHEN status='stopped' THEN 'interrupted' ELSE 'pending' END,outcome=CASE WHEN charged=1 THEN 'interrupted' ELSE '' END,usage_unknown=CASE WHEN charged=1 THEN 1 ELSE usage_unknown END,ended=CASE WHEN charged=1 THEN ? ELSE NULL END WHERE status IN ('pending','preparing','running','paused','stopped')",
                (time.time(),),
            )
            conn.execute(
                "UPDATE proactive_pending SET status='unknown',done_ts=? WHERE status='sending'",
                (time.time(),),
            )

    def cleanup_runs(self, retention_days: int, cutoff_day: str = "") -> dict[str, int]:
        if int(retention_days) <= 0:
            retention_days = 30
        now = time.time()
        cutoff = now - int(retention_days) * 86400
        usage_cutoff_day = str(cutoff_day or "").strip() or time.strftime("%Y-%m-%d", time.localtime(cutoff))
        removed = {
            "runs": 0,
            "actions": 0,
            "tool_requests": 0,
            "sends": 0,
            "deferred": 0,
            "daily_usage": 0,
        }
        with self.db.transaction() as conn:
            old_run_ids = [
                row[0]
                for row in conn.execute(
                    "SELECT id FROM resident_runs WHERE started<? "
                    "AND status NOT IN "
                    "('pending','preparing','running','paused','stopped','sending') "
                    "AND NOT EXISTS ("
                    "SELECT 1 FROM resident_deferred d "
                    "JOIN proactive_pending p ON p.id=d.pending_id "
                    "WHERE d.run_id=resident_runs.id "
                    "AND p.status IN ('pending','sending'))",
                    (cutoff,),
                ).fetchall()
            ]
            if old_run_ids:
                removed["actions"] = _delete_children(conn, "resident_actions", old_run_ids)
                removed["tool_requests"] = _delete_children(conn, "resident_tool_requests", old_run_ids)
                removed["sends"] = _delete_children(conn, "resident_sends", old_run_ids)
                cursor = conn.executemany(
                    "DELETE FROM resident_runs WHERE id=?", ((rid,) for rid in old_run_ids)
                )
                removed["runs"] = cursor.rowcount if cursor.rowcount >= 0 else len(old_run_ids)
            removed["actions"] += conn.execute(
                "DELETE FROM resident_actions WHERE run_id NOT IN (SELECT id FROM resident_runs)"
            ).rowcount
            removed["tool_requests"] += conn.execute(
                "DELETE FROM resident_tool_requests WHERE run_id NOT IN (SELECT id FROM resident_runs)"
            ).rowcount
            removed["sends"] += conn.execute(
                "DELETE FROM resident_sends WHERE run_id NOT IN (SELECT id FROM resident_runs)"
            ).rowcount
            removed["deferred"] += conn.execute(
                "DELETE FROM resident_deferred WHERE run_id NOT IN "
                "(SELECT id FROM resident_runs) AND pending_id NOT IN "
                "(SELECT id FROM proactive_pending WHERE status IN ('pending','sending'))"
            ).rowcount
            removed["daily_usage"] = conn.execute(
                "DELETE FROM resident_daily_usage WHERE day<?",
                (usage_cutoff_day,),
            ).rowcount
        return removed
