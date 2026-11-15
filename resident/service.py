"""Composition root and single dashboard API for resident capabilities."""

from __future__ import annotations

from datetime import datetime
import json
import time
from types import SimpleNamespace

from quart import jsonify, request

if __package__ and "." in __package__:
    from ..infra.constants import PLUGIN_NAME
    from .autonomy import Autonomy
    from .inbox import Inbox
    from .integration import Integration
    from .persona import Persona
    from .ports import ResidentHost
    from .store import MOODS, Conflict, Store
else:
    from infra.constants import PLUGIN_NAME
    from resident.autonomy import Autonomy
    from resident.inbox import Inbox
    from resident.integration import Integration
    from resident.persona import Persona
    from resident.ports import ResidentHost
    from resident.store import MOODS, Conflict, Store


class ResidentServices:
    def __init__(self, plugin):
        self.plugin = plugin
        self.host = ResidentHost(plugin)
        self.store = Store(self.host)
        self.persona = Persona(self)
        self.inbox = Inbox(self)
        self.integration = Integration(self)
        self.autonomy = Autonomy(self)

    def register(self):
        self.host.context.register_web_api(f"/{PLUGIN_NAME}/resident", self.get, ["GET"], "读取糯小喵自主运行状态")
        self.host.context.register_web_api(f"/{PLUGIN_NAME}/resident", self.mutate, ["POST"], "操作糯小喵自主运行与人设状态")

    async def initialize(self):
        self.store.recover()
        self.integration.migrate_selection()

    def web_event(self, umo: str):
        parts = str(umo or "").split(":", 2)
        if len(parts) != 3:
            raise ValueError("需要完整的会话标识")
        kind = parts[1]
        if kind == "GroupMessage":
            is_private = False
        elif kind == "FriendMessage":
            is_private = True
        else:
            # 兼容扩展平台会话类型（如频道/临时会话），避免崩溃。
            is_private = "friend" in kind.lower() or "private" in kind.lower() or "c2c" in kind.lower() or "dm" in kind.lower()
        return SimpleNamespace(
            unified_msg_origin=umo,
            get_platform_name=lambda: parts[0],
            get_platform_id=lambda: parts[0],
            get_group_id=lambda: "" if is_private else parts[2],
            get_sender_id=lambda: parts[2] if is_private else "",
            is_private_chat=lambda: is_private,
            is_admin=lambda: False,
            get_extra=lambda _k, default=None: default,
            role="member",
        )

    @staticmethod
    def _batch_by_run_id(rows: list[dict], key: str = "run_id") -> dict[int, list[dict]]:
        grouped: dict[int, list[dict]] = {}
        for row in rows:
            grouped.setdefault(int(row[key]), []).append(row)
        return grouped

    async def snapshot(self, umo: str = "", before: int = 0) -> dict:
        sessions = self.host.activity_sessions("all")
        for s in sessions:
            s["label"] = self.host.display_conversation_name(
                s["platform_id"], s["kind"], s["conversation_id"], s.get("name", "")
            )
        if not umo and sessions:
            umo = sessions[0]["umo"]
        persona = ""
        state = {"persona": "", "umo": umo, "mood": None, "focus": "", "focus_updated": None}
        if umo:
            persona, _ = await self.persona.resolve(self.web_event(umo))
            state = self.store.state(persona, umo)
            batch_fn = getattr(self.inbox, "batch_counts", None)
            if callable(batch_fn):
                counts_res = batch_fn(persona, sessions)
                if isinstance(counts_res, dict):
                    for s in sessions:
                        key = (str(s["platform_id"]), str(s["kind"]), str(s["conversation_id"]))
                        val = counts_res.get(key)
                        if val is None and "umo" in s:
                            val = counts_res.get(s["umo"])
                        s.update(val if isinstance(val, dict) else {"unread": 0, "unread_mentions": 0})
                elif isinstance(counts_res, list) and len(counts_res) == len(sessions):
                    for s, val in zip(sessions, counts_res):
                        s.update(val if isinstance(val, dict) else {"unread": 0, "unread_mentions": 0})
                else:
                    for s in sessions:
                        s.update(self.inbox.counts(persona, s))
            else:
                for s in sessions:
                    s.update(self.inbox.counts(persona, s))
        runs = self.store.rows(
            "SELECT * FROM resident_runs WHERE (?=0 OR id<?) ORDER BY id DESC LIMIT 50",
            (before, before),
        )
        run_ids = [int(r["id"]) for r in runs]
        if run_ids:
            ph = ",".join("?" for _ in run_ids)
            tr_map = self._batch_by_run_id(
                self.store.rows(f"SELECT * FROM resident_tool_requests WHERE run_id IN ({ph}) ORDER BY id", tuple(run_ids))
            )
            act_map = self._batch_by_run_id(
                self.store.rows(f"SELECT * FROM resident_actions WHERE run_id IN ({ph}) ORDER BY id", tuple(run_ids))
            )
            send_map = self._batch_by_run_id(
                self.store.rows(f"SELECT * FROM resident_sends WHERE run_id IN ({ph}) ORDER BY id", tuple(run_ids))
            )
            def_map = self._batch_by_run_id(
                self.store.rows(
                    f"SELECT d.run_id, p.id,p.status,p.target_umo,p.fire_ts,p.note FROM proactive_pending p JOIN resident_deferred d ON d.pending_id=p.id WHERE d.run_id IN ({ph}) ORDER BY p.id",
                    tuple(run_ids),
                )
            )
        else:
            tr_map = act_map = send_map = def_map = {}

        for run in runs:
            rid = int(run["id"])
            run["tool_requests"] = tr_map.get(rid, [])
            run["actions"] = act_map.get(rid, [])
            run["sends"] = send_map.get(rid, [])
            run["deferred"] = def_map.get(rid, [])
            if rid in self.autonomy.runs:
                run["status"] = self.autonomy.runs[rid].status
        today_fn = getattr(self.host, "today_in_tz", None)
        today = today_fn() if callable(today_fn) else datetime.now().strftime("%Y-%m-%d")
        return {
            "umo": umo,
            "persona": persona,
            "state": state,
            "moods": list(MOODS),
            "runs": runs,
            "next_before": runs[-1]["id"] if len(runs) == 50 else None,
            "sessions": sessions,
            "capabilities": self.integration.capabilities(umo),
            "tools": self.integration.catalog(),
            "budget": self.store.budget(today),
            "migration": self.store.meta("tools_migration"),
            "unknown_sends": self.store.rows(
                "SELECT id,target_umo,note FROM proactive_pending WHERE status='unknown' ORDER BY id DESC LIMIT 50"
            ),
            "unknown_runs": self.store.rows(
                "SELECT id,umo,started,summary FROM resident_runs "
                "WHERE status='unknown' "
                "AND NOT EXISTS (SELECT 1 FROM resident_actions a WHERE a.run_id=resident_runs.id AND a.status='unknown') "
                "AND NOT EXISTS (SELECT 1 FROM resident_sends s WHERE s.run_id=resident_runs.id AND s.status='unknown') "
                "ORDER BY id DESC LIMIT 50"
            ),
        }

    def _auth_ok(self) -> bool:
        return bool(self.host.webapi_username())

    def _auth_denied(self):
        return jsonify({"status": "error", "message": "未登录或登录已过期"}), 401

    async def get(self):
        if not self._auth_ok():
            return self._auth_denied()
        try:
            data = await self.snapshot(
                str(request.args.get("umo", "")),
                max(0, int(request.args.get("before", 0))),
            )
            return jsonify({"status": "ok", "data": data})
        except (ValueError, TypeError) as exc:
            return jsonify({"status": "ok", "data": {"operation_error": str(exc)}})

    async def mutate(self):
        if not self._auth_ok():
            return self._auth_denied()
        try:
            raw_text = await request.get_data(as_text=True)
            body = await request.get_json(silent=True)
            if body is None and raw_text.strip():
                raise ValueError("请求体必须是合法 JSON")
            if not isinstance(body, dict):
                raise ValueError("请求必须是对象")
            action = body.get("action")
            umo = str(body.get("umo", ""))
            if action in ("pause", "resume", "stop"):
                await self.autonomy.control(int(body.get("run_id", 0)), action)
            elif action == "review":
                conclusion = body.get("conclusion")
                if conclusion not in ("succeeded", "failed"):
                    raise ValueError("核实结果须为 succeeded / failed")
                note = str(body.get("note", "")).strip()
                if len(note) > 2000:
                    raise ValueError("核实依据最多 2000 字")
                with self.store.transaction() as db:
                    updated = db.execute(
                        "UPDATE resident_actions SET status=?,result=result || ?,ended=? WHERE id=? AND status='unknown'",
                        (
                            conclusion,
                            "\n管理员核实：" + (note or "已人工确认"),
                            datetime.now().timestamp(),
                            int(body.get("action_id", 0)),
                        ),
                    )
                    if updated.rowcount != 1:
                        raise Conflict("该动作不存在或已核实")
            elif action == "review_resident_send":
                conclusion = body.get("conclusion")
                note = str(body.get("note", "")).strip()
                if conclusion not in ("sent", "failed") or len(note) > 2000:
                    raise ValueError("核实结果须为 sent / failed，依据最多 2000 字")
                with self.store.transaction() as db:
                    updated = db.execute(
                        "UPDATE resident_sends SET status=?,detail=?,ended=? WHERE id=? AND status='unknown'",
                        (conclusion, note or "管理员已人工核实", datetime.now().timestamp(), int(body.get("send_id", 0))),
                    )
                    if updated.rowcount != 1:
                        raise Conflict("发送不存在或已经核实")
            elif action == "review_send":
                conclusion = body.get("conclusion")
                if conclusion not in ("fired", "failed"):
                    raise ValueError("核实结果须为 fired / failed")
                with self.store.transaction() as db:
                    updated = db.execute(
                        "UPDATE proactive_pending SET status=?,done_ts=? WHERE id=? AND status='unknown'",
                        (conclusion, time.time(), int(body.get("send_id", 0))),
                    )
                    if updated.rowcount != 1:
                        raise Conflict("该发送记录不存在或已核实")
            elif action == "review_run":
                conclusion = body.get("conclusion")
                note = str(body.get("note", "")).strip()
                if conclusion not in ("interrupted", "failed", "succeeded") or len(note) > 2000:
                    raise ValueError("核实结果须为 interrupted / failed / succeeded，依据最多 2000 字")
                with self.store.transaction() as db:
                    updated = db.execute(
                        "UPDATE resident_runs SET status=?,summary=summary || ? WHERE id=? AND status='unknown'",
                        (
                            conclusion,
                            "\n管理员核实：" + (note or "已人工确认"),
                            int(body.get("run_id", 0)),
                        ),
                    )
                    if updated.rowcount != 1:
                        raise Conflict("该运行不存在或已核实")
            else:
                persona, _ = await self.persona.resolve(self.web_event(umo))
                if persona.startswith("@session:"):
                    raise ValueError("当前会话没有可管理的人设")
                if action == "state":
                    self.store.update_state(
                        persona,
                        umo,
                        body.get("mood"),
                        body.get("intensity"),
                        body.get("focus"),
                        clear_focus=bool(body.get("clear_focus", False)),
                    )
                else:
                    raise ValueError("未知操作")
            if action in ("review", "review_resident_send"):
                with self.store.transaction() as db:
                    db.execute(
                        "UPDATE resident_runs SET status='interrupted',summary=summary || '；未知动作已由管理员核实' "
                        "WHERE status='unknown' "
                        "AND NOT EXISTS (SELECT 1 FROM resident_actions a WHERE a.run_id=resident_runs.id AND a.status='unknown') "
                        "AND NOT EXISTS (SELECT 1 FROM resident_sends s WHERE s.run_id=resident_runs.id AND s.status='unknown') "
                        "AND (EXISTS (SELECT 1 FROM resident_actions a WHERE a.run_id=resident_runs.id) "
                        "OR EXISTS (SELECT 1 FROM resident_sends s WHERE s.run_id=resident_runs.id))"
                    )
            return jsonify({"status": "ok", "data": await self.snapshot(umo)})
        except (ValueError, TypeError, KeyError) as exc:
            return jsonify({"status": "ok", "data": {"operation_error": str(exc)}})

    async def update_state(
        self,
        event,
        mood: str = "",
        intensity: int | float | None = None,
        focus: str | None = None,
        clear_focus: bool = False,
    ) -> str:
        p = self.plugin
        if not p.enabled or not p.resident_state_enabled:
            return json.dumps({"error": "当前状态功能已关闭"}, ensure_ascii=False)
        persona, _ = await self.persona.resolve(event)
        if persona.startswith("@session:"):
            raise ValueError("当前会话没有可管理的人设")
        return json.dumps(
            self.store.update_state(
                persona,
                event.unified_msg_origin,
                mood,
                intensity,
                focus,
                clear_focus=bool(clear_focus),
            ),
            ensure_ascii=False,
        )
