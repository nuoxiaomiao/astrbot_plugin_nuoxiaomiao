"""Repository for persona-scoped mood and session focus state."""

from __future__ import annotations

import time

if __package__ and __package__.count(".") >= 2:
    from ...infra.database import MOODS, ActivityDatabase
else:
    from infra.database import MOODS, ActivityDatabase


class Conflict(ValueError):
    pass


def _validate_intensity(intensity: int | float) -> int:
    if (
        isinstance(intensity, bool)
        or not isinstance(intensity, (int, float))
        or not float(intensity).is_integer()
        or not 0 <= int(intensity) <= 100
    ):
        raise ValueError("心情强度须为 0～100 的整数")
    return int(intensity)


class PersonaRepository:
    def __init__(self, db: ActivityDatabase) -> None:
        self.db = db

    def state(self, persona: str, umo: str) -> dict:
        moods = self.db.rows(
            "SELECT mood,intensity,updated FROM resident_moods WHERE persona=?",
            (persona,),
        )
        focus = self.db.rows(
            "SELECT body,updated FROM resident_focus WHERE persona=? AND umo=?",
            (persona, umo),
        )
        return {
            "persona": persona,
            "umo": umo,
            "mood": moods[0] if moods else None,
            "focus": focus[0]["body"] if focus else "",
            "focus_updated": focus[0]["updated"] if focus else None,
        }

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
        current = self.state(persona, umo)
        mood_val = str(mood).strip() if mood is not None else ""
        write_mood: tuple[str, int] | None = None
        if mood_val:
            if mood_val not in MOODS:
                raise ValueError("心情须选择：" + "、".join(MOODS))
            if intensity is None:
                intensity_val = int(current["mood"]["intensity"]) if current["mood"] else 50
            else:
                intensity_val = _validate_intensity(intensity)
            write_mood = (mood_val, intensity_val)
        elif intensity is not None:
            if not current["mood"]:
                raise ValueError("首次设置心情强度须同时选择心情：" + "、".join(MOODS))
            write_mood = (current["mood"]["mood"], _validate_intensity(intensity))

        write_focus: str | None = None
        if clear_focus:
            write_focus = ""
        elif focus is not None:
            if not isinstance(focus, str) or len(focus) > 2000:
                raise ValueError("当前会话关注点须为不超过 2000 字的文本")
            write_focus = focus

        if write_mood is None and write_focus is None:
            raise ValueError("请至少更新心情、强度或关注点之一")

        now = time.time()
        with self.db.transaction() as conn:
            if write_mood is not None:
                conn.execute(
                    "INSERT OR REPLACE INTO resident_moods VALUES (?,?,?,?)",
                    (persona, write_mood[0], write_mood[1], now),
                )
            if write_focus is not None:
                conn.execute(
                    "INSERT OR REPLACE INTO resident_focus VALUES (?,?,?,?)",
                    (persona, umo, write_focus, now),
                )
        return self.state(persona, umo)
