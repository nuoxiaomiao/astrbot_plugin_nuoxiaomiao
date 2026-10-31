"""Composition and authenticated AstrBot Plugin Page endpoints."""

from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace

from quart import jsonify, request

from .store import Store, Conflict, MOODS
from .inbox import Inbox
from .persona import Persona
from .integration import Integration
from .autonomy import Autonomy


class ResidentServices:
    def __init__(self, plugin):
        self.plugin = plugin
        self.store = Store(plugin)
        self.persona = Persona(self)
        self.inbox = Inbox(self)
        self.integration = Integration(self)
        self.autonomy = Autonomy(self)
        self.seen_personas = {}

    async def initialize(self):
        self.store.recover()
        self.integration.migrate_selection()

    def register(self):
        base = "/astrbot_plugin_nuoxiaomiao/resident"
        self.plugin.context.register_web_api(
            base, self.get, ["GET"], "糯小喵自主运行与状态"
        )
        self.plugin.context.register_web_api(
            base, self.mutate, ["POST"], "糯小喵状态、提案与运行控制"
        )

    def web_event(self, umo):
        parts = str(umo).split(":", 2)
        if (
            len(parts) != 3
            or parts[1] not in ("GroupMessage", "FriendMessage")
            or not parts[2]
        ):
            raise ValueError("请选择完整会话 ID")
        extras = {}
        return SimpleNamespace(
            get_extra=lambda key, default=None: extras.get(key, default),
            set_extra=lambda key, value: extras.__setitem__(key, value),
            is_stopped=lambda: False,
            unified_msg_origin=umo,
            get_platform_name=lambda: parts[0],
            get_platform_id=lambda: parts[0],
        )

    async def snapshot(self, umo="", before=0):
        sessions = self.plugin._activity_sessions("all")
        for s in sessions:
            s["umo"] = (
                f"{s['platform_id']}:{'GroupMessage' if s['kind'] == 'group' else 'FriendMessage'}:{s['conversation_id']}"
            )
        if not umo and sessions:
            umo = sessions[0]["umo"]
        persona = ""
        base_hash = ""
        state = None
        style = {"version": 0, "body": ""}
        proposals = []
        versions = []
        if umo:
            persona, base_hash = await self.persona.resolve(self.web_event(umo))
            state = (
                None
                if persona.startswith("@session:")
                else self.store.state(persona, umo)
            )
            style = self.store.style(persona)
            proposals = self.store.rows(
                "SELECT * FROM resident_proposals WHERE persona=? ORDER BY id DESC LIMIT 50",
                (persona,),
            )
            from difflib import unified_diff

            for proposal in proposals:
                proposal["diff"] = "\n".join(
                    unified_diff(
                        style["body"].splitlines(),
                        proposal["body"].splitlines(),
                        fromfile="当前风格",
                        tofile="建议风格",
                        lineterm="",
                    )
                )
            versions = self.store.rows(
                "SELECT version,body,created FROM resident_style_versions WHERE persona=? ORDER BY version DESC LIMIT 50",
                (persona,),
            )
            for s in sessions:
                s.update(self.inbox.counts(persona, s))
        runs = self.store.rows(
            "SELECT * FROM resident_runs WHERE (?=0 OR id<?) ORDER BY id DESC LIMIT 50",
            (before, before),
        )
        for run in runs:
            run["tool_requests"] = self.store.rows(
                "SELECT * FROM resident_tool_requests WHERE run_id=? ORDER BY id",
                (run["id"],),
            )
            run["actions"] = self.store.rows(
                "SELECT * FROM resident_actions WHERE run_id=? ORDER BY id",
                (run["id"],),
            )
            run["sends"] = self.store.rows(
                "SELECT * FROM resident_sends WHERE run_id=? ORDER BY id", (run["id"],)
            )
            run["deferred"] = self.store.rows(
                "SELECT p.id,p.status,p.target_umo,p.fire_ts,p.note FROM proactive_pending p JOIN resident_deferred d ON d.pending_id=p.id WHERE d.run_id=? ORDER BY p.id",
                (run["id"],),
            )
            if run["id"] in self.autonomy.runs:
                run["status"] = self.autonomy.runs[run["id"]].status
        return {
            "umo": umo,
            "persona": persona,
            "base_hash": base_hash,
            "state": state,
            "style": style,
            "proposals": proposals,
            "versions": versions,
            "moods": list(MOODS),
            "runs": runs,
            "next_before": runs[-1]["id"] if len(runs) == 50 else None,
            "sessions": sessions,
            "capabilities": self.integration.capabilities(umo),
            "tools": self.integration.catalog(),
            "budget": self.store.budget(datetime.now().strftime("%Y-%m-%d")),
            "migration": self.store.meta("tools_migration"),
            "unknown_sends": self.store.rows(
                "SELECT id,target_umo,note FROM proactive_pending WHERE status='unknown' ORDER BY id DESC LIMIT 50"
            ),
        }

    async def get(self):
        try:
            data = await self.snapshot(
                str(request.args.get("umo", "")),
                max(0, int(request.args.get("before", 0))),
            )
            return jsonify({"status": "ok", "data": data})
        except (ValueError, TypeError) as exc:
            return jsonify({"status": "ok", "data": {"operation_error": str(exc)}})

    async def mutate(self):
        try:
            body = await request.get_json()
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
                if not note or len(note) > 2000:
                    raise ValueError("请填写实际核实依据，最多 2000 字")
                with self.store.transaction() as db:
                    updated = db.execute(
                        "UPDATE resident_actions SET status=?,result=result || ?,ended=? WHERE id=? AND status='unknown'",
                        (
                            conclusion,
                            "\n管理员核实：" + note,
                            datetime.now().timestamp(),
                            int(body.get("action_id", 0)),
                        ),
                    )
                    if updated.rowcount != 1:
                        raise Conflict("该动作不存在或已核实")
            elif action == "review_resident_send":
                conclusion = body.get("conclusion")
                note = str(body.get("note", "")).strip()
                if conclusion not in ("sent", "failed") or not note or len(note) > 2000:
                    raise ValueError("请填写核实结果与依据")
                with self.store.transaction() as db:
                    updated = db.execute(
                        "UPDATE resident_sends SET status=?,detail=?,ended=? WHERE id=? AND status='unknown'",
                        (
                            conclusion,
                            note,
                            datetime.now().timestamp(),
                            int(body.get("send_id", 0)),
                        ),
                    )
                    if updated.rowcount != 1:
                        raise Conflict("发送不存在或已经核实")
            elif action == "review_send":
                conclusion = body.get("conclusion")
                if conclusion not in ("fired", "failed"):
                    raise ValueError("核实结果须为 fired / failed")
                with self.store.transaction() as db:
                    updated = db.execute(
                        "UPDATE proactive_pending SET status=? WHERE id=? AND status='unknown'",
                        (conclusion, int(body.get("send_id", 0))),
                    )
                    if updated.rowcount != 1:
                        raise Conflict("该发送记录不存在或已核实")
            else:
                persona, base_hash = await self.persona.resolve(self.web_event(umo))
                if persona.startswith("@session:"):
                    raise ValueError("当前会话没有可管理的人设")
                if action == "state":
                    self.store.update_state(
                        persona,
                        umo,
                        body.get("mood"),
                        body.get("intensity"),
                        body.get("focus", ""),
                    )
                elif action in ("approve", "reject", "rollback"):
                    if type(body.get("expected_version")) is not int:
                        raise ValueError("缺少当前风格版本，请刷新")
                    self.store.change_style(
                        persona,
                        action,
                        body["expected_version"],
                        int(body.get("proposal_id", 0)),
                        int(body.get("version", 0)),
                        base_hash,
                    )
                else:
                    raise ValueError("未知操作")
            if action in ("review", "review_resident_send"):
                with self.store.transaction() as db:
                    db.execute(
                        "UPDATE resident_runs SET status='interrupted',summary=summary || '；未知动作已由管理员核实' WHERE status='unknown' AND NOT EXISTS (SELECT 1 FROM resident_actions a WHERE a.run_id=resident_runs.id AND a.status='unknown') AND NOT EXISTS (SELECT 1 FROM resident_sends s WHERE s.run_id=resident_runs.id AND s.status='unknown') AND EXISTS (SELECT 1 FROM resident_actions a WHERE a.run_id=resident_runs.id)"
                    )
            return jsonify({"status": "ok", "data": await self.snapshot(umo)})
        except (ValueError, TypeError, KeyError) as exc:
            return jsonify({"status": "ok", "data": {"operation_error": str(exc)}})

    async def update_state(self, event, mood, intensity, focus):
        p = self.plugin
        if not p.enabled or not p.resident_state_enabled:
            return json.dumps({"error": "当前状态功能已关闭"}, ensure_ascii=False)
        persona, _ = await self.persona.resolve(event)
        if persona.startswith("@session:"):
            raise ValueError("当前会话没有可管理的人设")
        return json.dumps(
            self.store.update_state(
                persona, event.unified_msg_origin, mood, intensity, focus
            ),
            ensure_ascii=False,
        )

    async def propose_style(self, event, content, reason):
        if not self.plugin.enabled or not self.plugin.resident_state_enabled:
            return json.dumps({"error": "当前状态功能已关闭"}, ensure_ascii=False)
        persona, base_hash = await self.persona.resolve(event)
        if persona.startswith("@session:"):
            raise ValueError("当前会话没有可管理的人设")
        return json.dumps(
            self.store.propose(persona, content, reason, base_hash), ensure_ascii=False
        )
