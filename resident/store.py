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
    from .repos.topic_repo import TopicRepository
    from .repos.vision_repo import VisionRepository
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
    from resident.repos.topic_repo import TopicRepository
    from resident.repos.vision_repo import VisionRepository

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
        self.topic_repo = TopicRepository(self.db)
        self.vision_repo = VisionRepository(self.db)
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
        return self.topic_repo.meta(key, default)

    def set_meta(self, key: str, value: Any) -> None:
        with self.transaction() as db:
            db.execute("INSERT OR REPLACE INTO resident_meta VALUES (?,?)", (key, str(value)))

    def state(self, persona: str, umo: str) -> dict:
        return self.persona_repo.state(persona, umo)

    def update_state(self, persona: str, umo: str, mood: str, intensity: int, focus: str) -> dict:
        return self.persona_repo.update_state(persona, umo, mood, intensity, focus)

    def style(self, persona: str) -> dict:
        return self.persona_repo.style(persona)

    def propose(self, persona: str, body: str, reason: str, base_hash: str) -> dict:
        return self.persona_repo.propose(persona, body, reason, base_hash)

    def change_style(
        self,
        persona: str,
        action: str,
        expected_version: int,
        proposal_id: int = 0,
        version: int = 0,
        base_hash: str = "",
    ) -> dict:
        return self.persona_repo.change_style(persona, action, expected_version, proposal_id, version, base_hash)

    def proposals_with_diff(self, persona: str, style_body: str) -> list[dict]:
        return self.persona_repo.proposals_with_diff(persona, style_body)

    def versions(self, persona: str) -> list[dict]:
        return self.persona_repo.versions(persona)

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

    def finish(self, run_id: int, status: str, outcome: str, summary: str = "") -> None:
        self.autonomy_repo.finish(run_id, status, outcome, summary)

    def tool_requests(self, run_id: int, response) -> None:
        self.autonomy_repo.tool_requests(run_id, response)

    def action(self, run_id: int, tool: str, args) -> int:
        return self.autonomy_repo.action(run_id, tool, args)

    def end_action(self, action_id: int, status: str, result) -> None:
        self.autonomy_repo.end_action(action_id, status, result)

    def recover(self) -> None:
        self.autonomy_repo.recover()

    def cleanup_runs(self, retention_days: int) -> dict[str, int]:
        return self.autonomy_repo.cleanup_runs(retention_days)

    def topic_state(self, umo: str) -> dict:
        return self.topic_repo.topic_state(umo)

    def set_topic_state(self, umo: str, topic: str, topic_to_id: int, updated_ts: float) -> None:
        self.topic_repo.set_topic_state(umo, topic, topic_to_id, updated_ts)

    def append_topic_segment(
        self, umo: str, start_msg_id: int, end_msg_id: int, start_ts: float, end_ts: float, summary: str
    ) -> None:
        self.topic_repo.append_topic_segment(umo, start_msg_id, end_msg_id, start_ts, end_ts, summary)

    def topic_segments(self, umo: str, limit: int = 3) -> list[dict]:
        return self.topic_repo.topic_segments(umo, limit)

    def topic_segment_latest_end(self, umo: str) -> int:
        return self.topic_repo.topic_segment_latest_end(umo)

    def vision_keep(
        self, umo: str, platform_message_id: str, activity_message_id: int, idx: int, path: str
    ) -> None:
        self.vision_repo.vision_keep(umo, platform_message_id, activity_message_id, idx, path)

    def vision_kept_for(self, umo: str, platform_message_id: str) -> list[dict]:
        return self.vision_repo.vision_kept_for(umo, platform_message_id)

    def vision_caption_get(self, key: str) -> str:
        return self.vision_repo.vision_caption_get(key)

    def vision_caption_set(self, key: str, caption: str) -> None:
        self.vision_repo.vision_caption_set(key, caption)

    def vision_cleanup(self, keep_days: int, ttl_days: int, *, max_kept_files: int = 5000) -> tuple[int, int, int]:
        return self.vision_repo.vision_cleanup(keep_days, ttl_days, max_kept_files=max_kept_files)

    def vision_stats(self) -> dict[str, int]:
        return self.vision_repo.vision_stats()
