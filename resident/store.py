"""Additive SQLite storage for inbox receipts and resident-owned state only."""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = "5"
MOODS = ("平静", "开心", "低落", "疲惫", "烦躁", "专注", "期待")


def migrate(conn: sqlite3.Connection, path: str) -> None:
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name='resident_meta'"
    ).fetchone()
    if exists and conn.execute(
        "SELECT value FROM resident_meta WHERE key='schema'"
    ).fetchone() == (SCHEMA_VERSION,):
        return
    conn.commit()
    if exists or conn.execute("SELECT COUNT(*) FROM activity_messages").fetchone()[0]:
        backup = Path(path + ".bak-resident-v1")
        if not backup.exists():
            with sqlite3.connect(str(backup)) as target:
                conn.backup(target)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(activity_messages)")}
    with conn:
        for name, spec in {
            "message_id": "TEXT NOT NULL DEFAULT ''",
            "mentioned": "INTEGER NOT NULL DEFAULT 0",
            "truncated": "INTEGER NOT NULL DEFAULT 0",
        }.items():
            if name not in columns:
                conn.execute(f"ALTER TABLE activity_messages ADD COLUMN {name} {spec}")
        statements = [
            "CREATE TABLE IF NOT EXISTS resident_tool_requests (id INTEGER PRIMARY KEY AUTOINCREMENT,run_id INTEGER NOT NULL,call_id TEXT NOT NULL,tool TEXT NOT NULL,args TEXT NOT NULL,created REAL NOT NULL)",
            "CREATE TABLE IF NOT EXISTS resident_deferred (pending_id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL)",
            "CREATE TABLE IF NOT EXISTS resident_daily_usage (day TEXT PRIMARY KEY, input_other INTEGER NOT NULL DEFAULT 0, input_cached INTEGER NOT NULL DEFAULT 0, output INTEGER NOT NULL DEFAULT 0, unknown INTEGER NOT NULL DEFAULT 0)",
            "CREATE TABLE IF NOT EXISTS resident_sends (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER NOT NULL, umo TEXT NOT NULL, body TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'sending', detail TEXT NOT NULL DEFAULT '', started REAL NOT NULL, ended REAL)",
            "CREATE TABLE IF NOT EXISTS resident_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS resident_reads (persona TEXT NOT NULL, message_id INTEGER NOT NULL, PRIMARY KEY(persona,message_id))",
            "CREATE INDEX IF NOT EXISTS resident_reads_message ON resident_reads(message_id)",
            "CREATE INDEX IF NOT EXISTS resident_message_ref ON activity_messages(platform_id,kind,conversation_id,message_id)",
            "CREATE TABLE IF NOT EXISTS resident_moods (persona TEXT PRIMARY KEY, mood TEXT NOT NULL, intensity INTEGER NOT NULL, updated REAL NOT NULL)",
            "CREATE TABLE IF NOT EXISTS resident_focus (persona TEXT NOT NULL, umo TEXT NOT NULL, body TEXT NOT NULL, updated REAL NOT NULL, PRIMARY KEY(persona,umo))",
            "CREATE TABLE IF NOT EXISTS resident_styles (persona TEXT PRIMARY KEY, version INTEGER NOT NULL, body TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS resident_style_versions (persona TEXT NOT NULL, version INTEGER NOT NULL, body TEXT NOT NULL, created REAL NOT NULL, PRIMARY KEY(persona,version))",
            "CREATE TABLE IF NOT EXISTS resident_proposals (id INTEGER PRIMARY KEY AUTOINCREMENT, persona TEXT NOT NULL, body TEXT NOT NULL, reason TEXT NOT NULL, base_version INTEGER NOT NULL, base_hash TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'pending', created REAL NOT NULL)",
            "CREATE TABLE IF NOT EXISTS resident_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, umo TEXT NOT NULL, persona TEXT NOT NULL, day TEXT NOT NULL, started REAL NOT NULL, ended REAL, status TEXT NOT NULL DEFAULT 'pending', outcome TEXT NOT NULL DEFAULT '', charged INTEGER NOT NULL DEFAULT 0, input_other INTEGER NOT NULL DEFAULT 0, input_cached INTEGER NOT NULL DEFAULT 0, output INTEGER NOT NULL DEFAULT 0, usage_unknown INTEGER NOT NULL DEFAULT 0, summary TEXT NOT NULL DEFAULT '')",
            "CREATE INDEX IF NOT EXISTS resident_runs_day ON resident_runs(day,charged)",
            "CREATE TABLE IF NOT EXISTS resident_actions (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER NOT NULL, tool TEXT NOT NULL, args TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'running', result TEXT NOT NULL DEFAULT '', started REAL NOT NULL, ended REAL)",
            "CREATE TABLE IF NOT EXISTS resident_topic_segments (id INTEGER PRIMARY KEY AUTOINCREMENT, umo TEXT NOT NULL, start_msg_id INTEGER NOT NULL, end_msg_id INTEGER NOT NULL, start_ts REAL NOT NULL, end_ts REAL NOT NULL, summary TEXT NOT NULL)",
            "CREATE INDEX IF NOT EXISTS resident_topic_segments_umo ON resident_topic_segments(umo,end_msg_id)",
            "CREATE TABLE IF NOT EXISTS vision_kept_images (id INTEGER PRIMARY KEY AUTOINCREMENT, umo TEXT NOT NULL, platform_message_id TEXT NOT NULL, activity_message_id INTEGER NOT NULL, idx INTEGER NOT NULL DEFAULT 0, path TEXT NOT NULL, ts REAL NOT NULL)",
            "CREATE INDEX IF NOT EXISTS vision_kept_images_umo ON vision_kept_images(umo,platform_message_id)",
            "CREATE TABLE IF NOT EXISTS vision_caption_cache (key TEXT PRIMARY KEY, caption TEXT NOT NULL, ts REAL NOT NULL)",
        ]
        for statement in statements:
            conn.execute(statement)
        run_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(resident_runs)")
        }
        if "requests" not in run_columns:
            conn.execute(
                "ALTER TABLE resident_runs ADD COLUMN requests INTEGER NOT NULL DEFAULT 0"
            )
        conn.execute(
            "INSERT OR IGNORE INTO resident_daily_usage SELECT day,SUM(input_other),SUM(input_cached),SUM(output),MAX(usage_unknown) FROM resident_runs GROUP BY day"
        )
        baseline = conn.execute(
            "SELECT COALESCE(MAX(id),0) FROM activity_messages"
        ).fetchone()[0]
        conn.execute(
            "INSERT OR IGNORE INTO resident_meta VALUES ('inbox_baseline',?)",
            (str(baseline),),
        )
        conn.execute(
            "INSERT OR REPLACE INTO resident_meta VALUES ('schema',?)",
            (SCHEMA_VERSION,),
        )


class Conflict(ValueError):
    pass


class Store:
    def __init__(self, plugin):
        self.plugin = plugin

    @contextmanager
    def transaction(self):
        conn = self.plugin._get_activity_db()
        with self.plugin._activity_db_lock, conn:
            yield conn

    def rows(self, sql, args=()):
        with self.transaction() as db:
            cursor = db.execute(sql, args)
            names = [col[0] for col in cursor.description]
            return [dict(zip(names, row)) for row in cursor.fetchall()]

    def meta(self, key, default=""):
        rows = self.rows("SELECT value FROM resident_meta WHERE key=?", (key,))
        return rows[0]["value"] if rows else default

    def set_meta(self, key, value):
        with self.transaction() as db:
            db.execute(
                "INSERT OR REPLACE INTO resident_meta VALUES (?,?)", (key, str(value))
            )

    def state(self, persona, umo):
        moods = self.rows(
            "SELECT mood,intensity,updated FROM resident_moods WHERE persona=?",
            (persona,),
        )
        focus = self.rows(
            "SELECT body,updated FROM resident_focus WHERE persona=? AND umo=?",
            (persona, umo),
        )
        return {
            "persona": persona,
            "umo": umo,
            "mood": moods[0] if moods else None,
            "focus": focus[0]["body"] if focus else "",
        }

    def update_state(self, persona, umo, mood, intensity, focus):
        if mood not in MOODS:
            raise ValueError("心情须选择：" + "、".join(MOODS))
        if (
            isinstance(intensity, bool)
            or not isinstance(intensity, int)
            or not 0 <= intensity <= 100
        ):
            raise ValueError("心情强度须为 0～100 的整数")
        if not isinstance(focus, str) or len(focus) > 2000:
            raise ValueError("当前会话关注点须为不超过 2000 字的文本")
        with self.transaction() as db:
            db.execute(
                "INSERT OR REPLACE INTO resident_moods VALUES (?,?,?,?)",
                (persona, mood, intensity, time.time()),
            )
            db.execute(
                "INSERT OR REPLACE INTO resident_focus VALUES (?,?,?,?)",
                (persona, umo, focus, time.time()),
            )
        return self.state(persona, umo)

    def style(self, persona):
        rows = self.rows(
            "SELECT version,body FROM resident_styles WHERE persona=?", (persona,)
        )
        return rows[0] if rows else {"version": 0, "body": ""}

    def propose(self, persona, body, reason, base_hash):
        if not isinstance(body, str) or not body.strip() or len(body) > 4000:
            raise ValueError("风格建议须为 1～4000 字")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
            raise ValueError("理由须为 1～2000 字")
        with self.transaction() as db:
            row = db.execute(
                "SELECT version FROM resident_styles WHERE persona=?", (persona,)
            ).fetchone()
            version = row[0] if row else 0
            cursor = db.execute(
                "INSERT INTO resident_proposals(persona,body,reason,base_version,base_hash,created) VALUES (?,?,?,?,?,?)",
                (persona, body, reason, version, base_hash, time.time()),
            )
            return {
                "id": cursor.lastrowid,
                "status": "pending",
                "base_version": version,
            }

    def change_style(
        self, persona, action, expected_version, proposal_id=0, version=0, base_hash=""
    ):
        with self.transaction() as db:
            current = db.execute(
                "SELECT version,body FROM resident_styles WHERE persona=?", (persona,)
            ).fetchone() or (0, "")
            if current[0] != expected_version:
                raise Conflict("风格版本已变化，请刷新后查看差异")
            if action in ("approve", "reject"):
                proposal = db.execute(
                    "SELECT body,base_version,status,base_hash FROM resident_proposals WHERE id=? AND persona=?",
                    (proposal_id, persona),
                ).fetchone()
                if not proposal or proposal[2] != "pending":
                    raise Conflict("提案不存在或已处理")
                if action == "reject":
                    db.execute(
                        "UPDATE resident_proposals SET status='rejected' WHERE id=?",
                        (proposal_id,),
                    )
                    return {"version": current[0], "body": current[1]}
                if proposal[1] != current[0] or proposal[3] != base_hash:
                    raise Conflict("提案基准人设或风格已变化，请重新提案")
                body = proposal[0]
                db.execute(
                    "UPDATE resident_proposals SET status='approved' WHERE id=?",
                    (proposal_id,),
                )
            elif action == "rollback":
                old = db.execute(
                    "SELECT body FROM resident_style_versions WHERE persona=? AND version=?",
                    (persona, version),
                ).fetchone()
                if not old:
                    raise ValueError("找不到指定风格版本")
                body = old[0]
            else:
                raise ValueError("操作须为 approve / reject / rollback")
            db.execute(
                "INSERT OR IGNORE INTO resident_style_versions VALUES (?,?,?,?)",
                (persona, current[0], current[1], time.time()),
            )
            db.execute(
                "INSERT INTO resident_style_versions VALUES (?,?,?,?)",
                (persona, current[0] + 1, body, time.time()),
            )
            db.execute(
                "INSERT OR REPLACE INTO resident_styles VALUES (?,?,?)",
                (persona, current[0] + 1, body),
            )
            return {"version": current[0] + 1, "body": body}

    def new_run(self, umo, persona, day):
        with self.transaction() as db:
            return db.execute(
                "INSERT INTO resident_runs(umo,persona,day,started) VALUES (?,?,?,?)",
                (umo, persona, day, time.time()),
            ).lastrowid

    def charge(self, run_id, day, limit):
        with self.transaction() as db:
            row = db.execute(
                "SELECT charged,day FROM resident_runs WHERE id=?", (run_id,)
            ).fetchone()
            if not row:
                raise ValueError("运行记录不存在")
            # 同一自然日不重复计次；跨天之后允许按新的一天再计一次（审查 H11）。
            if row[0] and row[1] == day:
                return
            used = db.execute(
                "SELECT COUNT(*) FROM resident_runs WHERE day=? AND charged=1", (day,)
            ).fetchone()[0]
            if used >= limit:
                raise ValueError("今天的自醒次数已用完")
            db.execute(
                "UPDATE resident_runs SET charged=1,status='running',day=? WHERE id=?",
                (day, run_id),
            )

    def budget(self, day):
        used = self.rows(
            "SELECT COUNT(*) AS used FROM resident_runs WHERE day=? AND charged=1",
            (day,),
        )[0]["used"]
        usage = self.rows(
            "SELECT input_other+input_cached+output AS tokens,unknown,input_cached FROM resident_daily_usage WHERE day=?",
            (day,),
        )
        return {
            "used": used,
            **(usage[0] if usage else {"tokens": 0, "unknown": 0, "input_cached": 0}),
        }

    def usage(self, run_id, values=None, day=None):
        with self.transaction() as db:
            if not day:
                day = db.execute(
                    "SELECT day FROM resident_runs WHERE id=?", (run_id,)
                ).fetchone()[0]
            db.execute(
                "INSERT OR IGNORE INTO resident_daily_usage(day) VALUES (?)", (day,)
            )
            if values is None:
                db.execute(
                    "UPDATE resident_runs SET usage_unknown=1 WHERE id=?", (run_id,)
                )
                db.execute(
                    "UPDATE resident_daily_usage SET unknown=1 WHERE day=?", (day,)
                )
            else:
                db.execute(
                    "UPDATE resident_runs SET input_other=input_other+?,input_cached=input_cached+?,output=output+? WHERE id=?",
                    (*values, run_id),
                )
                db.execute(
                    "UPDATE resident_daily_usage SET input_other=input_other+?,input_cached=input_cached+?,output=output+? WHERE day=?",
                    (*values, day),
                )

    def begin_send(self, run_id, umo, body):
        with self.transaction() as db:
            return db.execute(
                "INSERT INTO resident_sends(run_id,umo,body,started) VALUES (?,?,?,?)",
                (run_id, umo, body, time.time()),
            ).lastrowid

    def end_send(self, send_id, status, detail=""):
        with self.transaction() as db:
            db.execute(
                "UPDATE resident_sends SET status=?,detail=?,ended=? WHERE id=?",
                (status, str(detail)[:4000], time.time(), send_id),
            )

    def finish(self, run_id, status, outcome, summary=""):
        with self.transaction() as db:
            db.execute(
                "UPDATE resident_runs SET status=?,outcome=?,summary=?,ended=? WHERE id=?",
                (status, outcome, summary[:4000], time.time(), run_id),
            )

    def tool_requests(self, run_id, response):
        names = getattr(response, "tools_call_name", None) or []
        args = getattr(response, "tools_call_args", None) or []
        ids = getattr(response, "tools_call_ids", None) or []
        if not names:
            return
        with self.transaction() as db:
            for i, name in enumerate(names):
                db.execute(
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

    def action(self, run_id, tool, args):
        with self.transaction() as db:
            return db.execute(
                "INSERT INTO resident_actions(run_id,tool,args,started) VALUES (?,?,?,?)",
                (
                    run_id,
                    tool,
                    json.dumps(args, ensure_ascii=False, default=str)[:8000],
                    time.time(),
                ),
            ).lastrowid

    def end_action(self, action_id, status, result):
        with self.transaction() as db:
            db.execute(
                "UPDATE resident_actions SET status=?,result=?,ended=? WHERE id=?",
                (status, str(result)[:8000], time.time(), action_id),
            )

    def recover(self):
        with self.transaction() as db:
            db.execute(
                "UPDATE resident_sends SET status='unknown' WHERE status='sending'"
            )
            db.execute(
                "INSERT INTO resident_daily_usage(day,unknown) SELECT DISTINCT day,1 FROM resident_runs WHERE charged=1 AND status IN ('pending','preparing','running','paused','stopped') ON CONFLICT(day) DO UPDATE SET unknown=1"
            )
            db.execute(
                "UPDATE resident_actions SET status='unknown' WHERE status='running'"
            )
            db.execute(
                "UPDATE resident_runs SET status=CASE WHEN charged=1 THEN 'unknown' WHEN status='stopped' THEN 'interrupted' ELSE 'pending' END,outcome=CASE WHEN charged=1 THEN 'interrupted' ELSE '' END,usage_unknown=CASE WHEN charged=1 THEN 1 ELSE usage_unknown END,ended=CASE WHEN charged=1 THEN ? ELSE NULL END WHERE status IN ('pending','preparing','running','paused','stopped')",
                (time.time(),),
            )
            # Sending rows have crossed the external side-effect boundary.
            db.execute(
                "UPDATE proactive_pending SET status='unknown',done_ts=? WHERE status='sending'",
                (time.time(),),
            )

    # --- 话题概括（topic）辅助 ---

    def topic_state(self, umo):
        """返回会话的话题状态快照：topic / topic_to_id / topic_updated_at。"""
        topic = self.meta(f"topic:{umo}", "")
        try:
            topic_to_id = int(self.meta(f"topic_to_id:{umo}", "0"))
        except (TypeError, ValueError):
            topic_to_id = 0
        try:
            updated_at = float(self.meta(f"topic_updated_at:{umo}", "0"))
        except (TypeError, ValueError):
            updated_at = 0.0
        return {
            "topic": topic,
            "topic_to_id": topic_to_id,
            "topic_updated_at": updated_at,
        }

    def set_topic_state(self, umo, topic, topic_to_id, updated_ts):
        """落盘话题状态与已概括到的消息游标。"""
        with self.transaction() as db:
            db.execute(
                "INSERT OR REPLACE INTO resident_meta VALUES (?,?)",
                (f"topic:{umo}", str(topic or "")),
            )
            db.execute(
                "INSERT OR REPLACE INTO resident_meta VALUES (?,?)",
                (f"topic_to_id:{umo}", str(int(topic_to_id))),
            )
            db.execute(
                "INSERT OR REPLACE INTO resident_meta VALUES (?,?)",
                (f"topic_updated_at:{umo}", str(float(updated_ts))),
            )

    def set_topic_text(self, umo, topic, updated_ts):
        """只更新「在聊」文本（尾部不足一段时不推游标）。"""
        with self.transaction() as db:
            db.execute(
                "INSERT OR REPLACE INTO resident_meta VALUES (?,?)",
                (f"topic:{umo}", str(topic or "")),
            )
            db.execute(
                "INSERT OR REPLACE INTO resident_meta VALUES (?,?)",
                (f"topic_updated_at:{umo}", str(float(updated_ts))),
            )

    def append_topic_segment(
        self, umo, start_msg_id, end_msg_id, start_ts, end_ts, summary
    ):
        with self.transaction() as db:
            db.execute(
                "INSERT INTO resident_topic_segments(umo,start_msg_id,end_msg_id,start_ts,end_ts,summary) VALUES (?,?,?,?,?,?)",
                (umo, start_msg_id, end_msg_id, start_ts, end_ts, summary),
            )

    def topic_segments(self, umo, after_id=0, limit=10):
        rows = self.rows(
            "SELECT id,start_msg_id,end_msg_id,start_ts,end_ts,summary FROM resident_topic_segments WHERE umo=? AND end_msg_id>? ORDER BY end_msg_id DESC LIMIT ?",
            (umo, int(after_id), int(limit)),
        )
        return list(rows)

    def count_today_segments(self, umo, day_start_ts):
        row = self.rows(
            "SELECT COUNT(*) AS n FROM resident_topic_segments WHERE umo=? AND end_ts>=?",
            (umo, float(day_start_ts)),
        )
        return row[0]["n"] if row else 0

    # --- 群聊识图（vision）辅助 ---

    def vision_keep(self, umo, platform_message_id, activity_message_id, idx, path):
        with self.transaction() as db:
            db.execute(
                "INSERT INTO vision_kept_images(umo,platform_message_id,activity_message_id,idx,path,ts) VALUES (?,?,?,?,?,?)",
                (umo, platform_message_id, activity_message_id, int(idx), str(path), time.time()),
            )

    def vision_kept_for(self, umo, platform_message_id):
        return self.rows(
            "SELECT id,idx,path,ts FROM vision_kept_images WHERE umo=? AND platform_message_id=? ORDER BY idx",
            (umo, platform_message_id),
        )

    def vision_caption_get(self, key):
        rows = self.rows(
            "SELECT caption FROM vision_caption_cache WHERE key=?", (str(key),)
        )
        return rows[0]["caption"] if rows else ""

    def vision_caption_set(self, key, caption):
        with self.transaction() as db:
            db.execute(
                "INSERT OR REPLACE INTO vision_caption_cache(key,caption,ts) VALUES (?,?,?)",
                (str(key), str(caption or ""), time.time()),
            )

    def vision_stats(self):
        rows = self.rows(
            "SELECT (SELECT COUNT(*) FROM vision_kept_images) AS kept,(SELECT COUNT(*) FROM vision_caption_cache) AS descs"
        )
        return (rows[0]["kept"] if rows else 0, rows[0]["descs"] if rows else 0)

    def vision_cleanup(self, image_keep_days, ttl_days):
        """清理过期留底行与描述缓存，返回 (删行, 删图, 删描述) 计数。"""
        now = time.time()
        with self.transaction() as db:
            stale = db.execute(
                "SELECT path FROM vision_kept_images WHERE ts<?",
                (now - max(0, int(image_keep_days)) * 86400,),
            ).fetchall()
            removed_rows = db.execute(
                "DELETE FROM vision_kept_images WHERE ts<?",
                (now - max(0, int(image_keep_days)) * 86400,),
            ).rowcount
            removed_desc = db.execute(
                "DELETE FROM vision_caption_cache WHERE ts<?",
                (now - max(0, int(ttl_days)) * 86400,),
            ).rowcount
        removed_files = 0
        for (path,) in stale:
            try:
                Path(path).unlink(missing_ok=True)
                removed_files += 1
            except OSError:
                pass
        return (removed_rows, removed_files, removed_desc)

    def cleanup_runs(self, retention_days: int) -> dict[str, int]:
        """按保留天数清理自主运行审计日志（审查 C2/C3）。

        删除过期 resident_runs 及其关联的 actions / tool_requests / sends /
        deferred，以及过期的 topic_segments 与每日用量统计。这些全是日志型
        数据，删除不影响当前人设状态、未读信箱与识图留底。
        """
        if int(retention_days) <= 0:
            retention_days = 30
        now = time.time()
        cutoff = now - int(retention_days) * 86400
        removed = {
            "runs": 0,
            "actions": 0,
            "tool_requests": 0,
            "sends": 0,
            "deferred": 0,
            "topic_segments": 0,
            "daily_usage": 0,
        }
        with self.transaction() as db:
            old_run_ids = [
                row[0]
                for row in db.execute(
                    "SELECT id FROM resident_runs WHERE started < ?",
                    (cutoff,),
                ).fetchall()
            ]
            if old_run_ids:
                before = db.total_changes
                db.executemany(
                    "DELETE FROM resident_actions WHERE run_id=?",
                    ((rid,) for rid in old_run_ids),
                )
                db.executemany(
                    "DELETE FROM resident_tool_requests WHERE run_id=?",
                    ((rid,) for rid in old_run_ids),
                )
                db.executemany(
                    "DELETE FROM resident_sends WHERE run_id=?",
                    ((rid,) for rid in old_run_ids),
                )
                db.executemany(
                    "DELETE FROM resident_deferred WHERE run_id=?",
                    ((rid,) for rid in old_run_ids),
                )
                removed["actions"] = db.total_changes - before
                db.executemany(
                    "DELETE FROM resident_runs WHERE id=?",
                    ((rid,) for rid in old_run_ids),
                )
                removed["runs"] = len(old_run_ids)
            # 孤儿行兜底：父 run 已被删除（例如崩溃期间）的行一并清掉。
            removed["actions"] += db.execute(
                "DELETE FROM resident_actions WHERE run_id NOT IN (SELECT id FROM resident_runs)"
            ).rowcount
            removed["tool_requests"] += db.execute(
                "DELETE FROM resident_tool_requests WHERE run_id NOT IN (SELECT id FROM resident_runs)"
            ).rowcount
            removed["sends"] += db.execute(
                "DELETE FROM resident_sends WHERE run_id NOT IN (SELECT id FROM resident_runs)"
            ).rowcount
            removed["deferred"] += db.execute(
                "DELETE FROM resident_deferred WHERE run_id NOT IN (SELECT id FROM resident_runs)"
            ).rowcount
            removed["topic_segments"] = db.execute(
                "DELETE FROM resident_topic_segments WHERE start_ts<?",
                (cutoff,),
            ).rowcount
            removed["daily_usage"] = db.execute(
                "DELETE FROM resident_daily_usage WHERE day<?",
                (time.strftime("%Y-%m-%d", time.localtime(cutoff)),),
            ).rowcount
        return removed
