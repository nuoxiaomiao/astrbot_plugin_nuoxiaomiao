"""Unified SQLite schema, migration, and single-writer async gateway."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
import sqlite3
import threading
from typing import Any, Callable, Iterator, TypeVar

SCHEMA_VERSION = "8"
MOODS = ("平静", "开心", "低落", "疲惫", "烦躁", "专注", "期待")
T = TypeVar("T")


def _backup_before_migrate(conn: sqlite3.Connection, path: str, target_version: str) -> None:
    if not path or path == ":memory:":
        return
    exists_v1 = Path(path + ".bak-resident-v1")
    backup = exists_v1 if not exists_v1.exists() else Path(f"{path}.bak-resident-v{target_version}")
    if backup.exists():
        return
    target = sqlite3.connect(str(backup))
    try:
        conn.backup(target)
    finally:
        target.close()


def _ensure_base_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS activity_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            platform_id TEXT NOT NULL DEFAULT '',
            kind TEXT NOT NULL DEFAULT 'group',
            conversation_id TEXT NOT NULL DEFAULT '',
            conversation_name TEXT NOT NULL DEFAULT '',
            sender_id TEXT NOT NULL DEFAULT '',
            sender_name TEXT NOT NULL DEFAULT '',
            text TEXT NOT NULL DEFAULT '',
            ts REAL NOT NULL DEFAULT 0
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_activity_conv "
        "ON activity_messages(platform_id, kind, conversation_id, ts)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_activity_ts ON activity_messages(ts)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_activity_sender "
        "ON activity_messages(sender_id, ts)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS proactive_pending (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_ts REAL NOT NULL DEFAULT 0,
            fire_ts REAL NOT NULL DEFAULT 0,
            done_ts REAL NOT NULL DEFAULT 0,
            source_umo TEXT NOT NULL DEFAULT '',
            platform_id TEXT NOT NULL DEFAULT '',
            target_kind TEXT NOT NULL DEFAULT '',
            target_id TEXT NOT NULL DEFAULT '',
            target_name TEXT NOT NULL DEFAULT '',
            target_umo TEXT NOT NULL DEFAULT '',
            subject_id TEXT NOT NULL DEFAULT '',
            subject_name TEXT NOT NULL DEFAULT '',
            await_sender_id TEXT NOT NULL DEFAULT '',
            note TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending'
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_proactive_status "
        "ON proactive_pending(status, fire_ts)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_proactive_target "
        "ON proactive_pending(target_kind, target_id, status)"
    )


def migrate_resident(conn: sqlite3.Connection, path: str) -> None:
    """Idempotent schema initialization and additive migration for activity.db."""
    _ensure_base_tables(conn)
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name='resident_meta'"
    ).fetchone()
    if exists and conn.execute(
        "SELECT value FROM resident_meta WHERE key='schema'"
    ).fetchone() == (SCHEMA_VERSION,):
        return
    conn.commit()
    if exists or conn.execute("SELECT COUNT(*) FROM activity_messages").fetchone()[0]:
        _backup_before_migrate(conn, path, SCHEMA_VERSION)
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
            "CREATE TABLE IF NOT EXISTS resident_channel_watermarks (persona TEXT NOT NULL, platform_id TEXT NOT NULL, kind TEXT NOT NULL, conversation_id TEXT NOT NULL, last_read_id INTEGER NOT NULL DEFAULT 0, updated REAL NOT NULL DEFAULT 0, PRIMARY KEY(persona, platform_id, kind, conversation_id))",
            "CREATE INDEX IF NOT EXISTS resident_message_ref ON activity_messages(platform_id,kind,conversation_id,message_id)",
            "CREATE TABLE IF NOT EXISTS resident_moods (persona TEXT PRIMARY KEY, mood TEXT NOT NULL, intensity INTEGER NOT NULL, updated REAL NOT NULL)",
            "CREATE TABLE IF NOT EXISTS resident_focus (persona TEXT NOT NULL, umo TEXT NOT NULL, body TEXT NOT NULL, updated REAL NOT NULL, PRIMARY KEY(persona,umo))",
            "CREATE TABLE IF NOT EXISTS resident_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, umo TEXT NOT NULL, persona TEXT NOT NULL, day TEXT NOT NULL, started REAL NOT NULL, ended REAL, status TEXT NOT NULL DEFAULT 'pending', outcome TEXT NOT NULL DEFAULT '', charged INTEGER NOT NULL DEFAULT 0, input_other INTEGER NOT NULL DEFAULT 0, input_cached INTEGER NOT NULL DEFAULT 0, output INTEGER NOT NULL DEFAULT 0, usage_unknown INTEGER NOT NULL DEFAULT 0, summary TEXT NOT NULL DEFAULT '', requests INTEGER NOT NULL DEFAULT 0, meta TEXT NOT NULL DEFAULT '')",
            "CREATE INDEX IF NOT EXISTS resident_runs_day ON resident_runs(day,charged)",
            "CREATE TABLE IF NOT EXISTS resident_actions (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER NOT NULL, tool TEXT NOT NULL, args TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'running', result TEXT NOT NULL DEFAULT '', started REAL NOT NULL, ended REAL)",
        ]
        for statement in statements:
            conn.execute(statement)
        run_columns = {row[1] for row in conn.execute("PRAGMA table_info(resident_runs)")}
        if "requests" not in run_columns:
            conn.execute("ALTER TABLE resident_runs ADD COLUMN requests INTEGER NOT NULL DEFAULT 0")
        if "meta" not in run_columns:
            conn.execute("ALTER TABLE resident_runs ADD COLUMN meta TEXT NOT NULL DEFAULT ''")
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
        for drop_sql in (
            "DROP INDEX IF EXISTS resident_topic_segments_umo",
            "DROP INDEX IF EXISTS vision_kept_images_umo",
            "DROP TABLE IF EXISTS resident_styles",
            "DROP TABLE IF EXISTS resident_style_versions",
            "DROP TABLE IF EXISTS resident_proposals",
            "DROP TABLE IF EXISTS resident_topic_segments",
            "DROP TABLE IF EXISTS vision_kept_images",
            "DROP TABLE IF EXISTS vision_caption_cache",
        ):
            conn.execute(drop_sql)
        conn.execute(
            "DELETE FROM resident_meta WHERE key='master_auto_umo' OR key LIKE 'topic:%' OR key LIKE 'topic_to_id:%' OR key LIKE 'topic_updated_at:%'"
        )
        conn.execute(
            "INSERT OR REPLACE INTO resident_meta VALUES ('schema',?)",
            (SCHEMA_VERSION,),
        )


class ActivityDatabase:
    """Thread-safe SQLite connection manager with a single-worker async executor."""

    def __init__(self, path_resolver: Callable[[], Path]) -> None:
        self._path_resolver = path_resolver
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None
        self._open_path = ""
        self._executor: ThreadPoolExecutor | None = None

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    def _get_executor(self) -> ThreadPoolExecutor:
        with self._lock:
            if self._executor is None:
                self._executor = ThreadPoolExecutor(
                    max_workers=1,
                    thread_name_prefix="nuoxiaomiao-db",
                )
            return self._executor

    def _ensure_open_locked(self) -> sqlite3.Connection:
        target = str(self._path_resolver())
        if self._conn is not None and self._open_path == target:
            return self._conn
        if self._conn is not None:
            try:
                self._conn.commit()
                self._conn.close()
            except Exception:
                pass
            self._conn = None
            self._open_path = ""
        path = Path(target)
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(target, check_same_thread=False)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            migrate_resident(conn, target)
            conn.commit()
        except Exception:
            try:
                conn.close()
            except Exception:
                pass
            raise
        self._conn = conn
        self._open_path = target
        return conn

    def get_connection(self) -> sqlite3.Connection:
        with self._lock:
            return self._ensure_open_locked()

    @contextmanager
    def read_conn(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            yield self._ensure_open_locked()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            conn = self._ensure_open_locked()
            with conn:
                yield conn

    def rows(self, sql: str, args: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self.read_conn() as conn:
            cursor = conn.execute(sql, args)
            names = [col[0] for col in (cursor.description or ())]
            return [dict(zip(names, row)) for row in cursor.fetchall()]

    async def run(self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        loop = asyncio.get_running_loop()
        executor = self._get_executor()
        return await loop.run_in_executor(executor, lambda: fn(*args, **kwargs))

    async def arows(self, sql: str, args: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        return await self.run(self.rows, sql, args)

    async def awrite(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        def _exec() -> T:
            with self.transaction() as conn:
                return fn(conn)

        return await self.run(_exec)

    def close(self) -> None:
        executor: ThreadPoolExecutor | None = None
        with self._lock:
            executor = self._executor
            self._executor = None
        if executor is not None:
            executor.shutdown(wait=True)
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.commit()
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None
                self._open_path = ""

    async def aclose(self) -> None:
        executor: ThreadPoolExecutor | None = None
        with self._lock:
            executor = self._executor
            self._executor = None
        if executor is not None:
            await asyncio.to_thread(executor.shutdown, wait=True)
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.commit()
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None
                self._open_path = ""
