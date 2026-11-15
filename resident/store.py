"""SQLite store facade composing focused resident domain repositories."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any

if __package__ and "." in __package__:
    from ..infra.database import (
        MOODS,
        SCHEMA_VERSION,
        ActivityDatabase,
        migrate_resident as migrate,
    )
    from .repos.autonomy_repo import AutonomyRepository, BudgetExceeded
    from .repos.inbox_repo import InboxRepository
    from .repos.persona_repo import Conflict, PersonaRepository
else:
    from infra.database import (
        MOODS,
        SCHEMA_VERSION,
        ActivityDatabase,
        migrate_resident as migrate,
    )
    from resident.repos.autonomy_repo import AutonomyRepository, BudgetExceeded
    from resident.repos.inbox_repo import InboxRepository
    from resident.repos.persona_repo import Conflict, PersonaRepository

__all__ = ["SCHEMA_VERSION", "MOODS", "Conflict", "BudgetExceeded", "Store", "migrate"]


class Store:
    """Facade exposing unified DB access plus domain repository methods."""

    def __init__(self, host: Any) -> None:
        if isinstance(host, ActivityDatabase):
            self.db = host
        elif hasattr(host, "database") and callable(host.database):
            self.db = host.database()
        elif hasattr(host, "db") and isinstance(host.db, ActivityDatabase):
            self.db = host.db
        else:
            self.db = ActivityDatabase(host._activity_db_file)
        self.persona_repo = PersonaRepository(self.db)
        self.autonomy_repo = AutonomyRepository(self.db)
        self.inbox_repo = InboxRepository(self.db)

    @contextmanager
    def transaction(self):
        with self.db.transaction() as conn:
            yield conn

    def rows(self, sql: str, args: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        return self.db.rows(sql, args)

    async def arows(self, sql: str, args: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        return await self.db.arows(sql, args)

    async def arun(self, fn, *args: Any, **kwargs: Any) -> Any:
        return await self.db.run(fn, *args, **kwargs)

    def meta(self, key: str, default: str = "") -> str:
        rows = self.db.rows("SELECT value FROM resident_meta WHERE key=?", (key,))
        return rows[0]["value"] if rows else default

    def set_meta(self, key: str, value: Any) -> None:
        with self.transaction() as db:
            db.execute("INSERT OR REPLACE INTO resident_meta VALUES (?,?)", (key, str(value)))

    def state(self, persona: str, umo: str) -> dict:
        return self.persona_repo.state(persona, umo)

    def update_state(
        self,
        persona: str,
        umo: str,
        mood: str | None = None,
        intensity: int | float | None = None,
        focus: str | None = None,
        *,
        clear_focus: bool = False,
    ) -> dict:
        return self.persona_repo.update_state(
            persona, umo, mood, intensity, focus, clear_focus=clear_focus
        )

    def new_run(self, umo: str, persona: str, day: str) -> int:
        return self.autonomy_repo.new_run(umo, persona, day)

    def charge(self, run_id: int, day: str, limit: int) -> None:
        self.autonomy_repo.charge(run_id, day, limit)

    def budget(self, day: str) -> dict:
        return self.autonomy_repo.budget(day)

    def usage(self, run_id: int, values=None, day: str | None = None) -> None:
        self.autonomy_repo.usage(run_id, values, day)

    def begin_send(self, run_id: int, umo: str, body: str) -> int:
        return self.autonomy_repo.begin_send(run_id, umo, body)

    def end_send(self, send_id: int, status: str, detail: str = "") -> None:
        self.autonomy_repo.end_send(send_id, status, detail)

    def finish(
        self,
        run_id: int,
        status: str,
        outcome: str,
        summary: str = "",
        preserve_ended: bool = False,
    ) -> None:
        self.autonomy_repo.finish(
            run_id, status, outcome, summary, preserve_ended=preserve_ended
        )

    def update_outcome(self, run_id: int, status: str, outcome: str, summary: str = "") -> None:
        self.autonomy_repo.update_outcome(run_id, status, outcome, summary)

    def tool_requests(self, run_id: int, response) -> None:
        self.autonomy_repo.tool_requests(run_id, response)

    def action(self, run_id: int, tool: str, args) -> int:
        return self.autonomy_repo.action(run_id, tool, args)

    def end_action(self, action_id: int, status: str, result) -> None:
        self.autonomy_repo.end_action(action_id, status, result)

    def recover(self) -> None:
        self.autonomy_repo.recover()

    def cleanup_runs(self, retention_days: int, cutoff_day: str = "") -> dict[str, int]:
        return self.autonomy_repo.cleanup_runs(retention_days, cutoff_day)
