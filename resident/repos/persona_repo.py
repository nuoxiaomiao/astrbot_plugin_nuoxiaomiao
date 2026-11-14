"""Repository for persona mood, per-session focus, and approved style versions."""

from __future__ import annotations

import difflib
import time

if __package__ and __package__.count(".") >= 2:
    from ...infra.database import MOODS, ActivityDatabase
else:
    from infra.database import MOODS, ActivityDatabase


class Conflict(ValueError):
    pass


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
        }

    def update_state(self, persona: str, umo: str, mood: str, intensity: int, focus: str) -> dict:
        if mood not in MOODS:
            raise ValueError("心情须选择：" + "、".join(MOODS))
        if isinstance(intensity, bool) or not isinstance(intensity, int) or not 0 <= intensity <= 100:
            raise ValueError("心情强度须为 0～100 的整数")
        if not isinstance(focus, str) or len(focus) > 2000:
            raise ValueError("当前会话关注点须为不超过 2000 字的文本")
        now = time.time()
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO resident_moods VALUES (?,?,?,?)",
                (persona, mood, intensity, now),
            )
            conn.execute(
                "INSERT OR REPLACE INTO resident_focus VALUES (?,?,?,?)",
                (persona, umo, focus, now),
            )
        return self.state(persona, umo)

    def style(self, persona: str) -> dict:
        rows = self.db.rows(
            "SELECT version,body FROM resident_styles WHERE persona=?", (persona,)
        )
        return rows[0] if rows else {"version": 0, "body": ""}

    def propose(self, persona: str, body: str, reason: str, base_hash: str) -> dict:
        if not isinstance(body, str) or not body.strip() or len(body) > 4000:
            raise ValueError("风格建议须为 1～4000 字")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
            raise ValueError("理由须为 1～2000 字")
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT version FROM resident_styles WHERE persona=?", (persona,)
            ).fetchone()
            version = row[0] if row else 0
            cursor = conn.execute(
                "INSERT INTO resident_proposals(persona,body,reason,base_version,base_hash,created) VALUES (?,?,?,?,?,?)",
                (persona, body, reason, version, base_hash, time.time()),
            )
            return {"id": cursor.lastrowid, "status": "pending", "base_version": version}

    def change_style(
        self,
        persona: str,
        action: str,
        expected_version: int,
        proposal_id: int = 0,
        version: int = 0,
        base_hash: str = "",
    ) -> dict:
        with self.db.transaction() as conn:
            current = conn.execute(
                "SELECT version,body FROM resident_styles WHERE persona=?", (persona,)
            ).fetchone() or (0, "")
            if current[0] != expected_version:
                raise Conflict("风格版本已变化，请刷新后查看差异")
            if action in ("approve", "reject"):
                proposal = conn.execute(
                    "SELECT body,base_version,status,base_hash FROM resident_proposals WHERE id=? AND persona=?",
                    (proposal_id, persona),
                ).fetchone()
                if not proposal or proposal[2] != "pending":
                    raise Conflict("提案不存在或已处理")
                if action == "reject":
                    conn.execute(
                        "UPDATE resident_proposals SET status='rejected' WHERE id=?",
                        (proposal_id,),
                    )
                    return {"version": current[0], "body": current[1]}
                if proposal[1] != current[0] or proposal[3] != base_hash:
                    raise Conflict("提案基准人设或风格已变化，请重新提案")
                body = proposal[0]
                conn.execute(
                    "UPDATE resident_proposals SET status='approved' WHERE id=?",
                    (proposal_id,),
                )
            elif action == "rollback":
                old = conn.execute(
                    "SELECT body FROM resident_style_versions WHERE persona=? AND version=?",
                    (persona, version),
                ).fetchone()
                if not old:
                    raise ValueError("找不到指定风格版本")
                body = old[0]
            else:
                raise ValueError("操作须为 approve / reject / rollback")
            conn.execute(
                "INSERT OR IGNORE INTO resident_style_versions VALUES (?,?,?,?)",
                (persona, current[0], current[1], time.time()),
            )
            conn.execute(
                "INSERT INTO resident_style_versions VALUES (?,?,?,?)",
                (persona, current[0] + 1, body, time.time()),
            )
            conn.execute(
                "INSERT OR REPLACE INTO resident_styles VALUES (?,?,?)",
                (persona, current[0] + 1, body),
            )
            return {"version": current[0] + 1, "body": body}

    def proposals_with_diff(self, persona: str, style_body: str) -> list[dict]:
        proposals = self.db.rows(
            "SELECT * FROM resident_proposals WHERE persona=? AND status='pending' ORDER BY id DESC LIMIT 20",
            (persona,),
        )
        for p_item in proposals:
            p_item["diff"] = "\n".join(
                difflib.unified_diff(
                    style_body.splitlines(),
                    p_item["body"].splitlines(),
                    fromfile="当前风格",
                    tofile="建议风格",
                    lineterm="",
                )
            )
        return proposals

    def versions(self, persona: str) -> list[dict]:
        return self.db.rows(
            "SELECT version,body,created FROM resident_style_versions WHERE persona=? ORDER BY version DESC LIMIT 50",
            (persona,),
        )
