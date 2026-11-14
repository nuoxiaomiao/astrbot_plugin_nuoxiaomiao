"""IRMIA silent self-wake scheduler, fallback judge, and autonomous runner bridge."""

from __future__ import annotations

import asyncio
from datetime import datetime
import json
import random
import time
from typing import Any, Callable

from astrbot.api import logger

if __package__ and "." in __package__:
    from ..infra.constants import (
        BOT_ACTIVITY_SENDER_ID,
        DISPLAY_NAME,
        SELF_WAKE_FALLBACK_LOOKBACK_MINUTES,
        SELF_WAKE_FALLBACK_MAX_MESSAGES,
        SELF_WAKE_OUTCOME_ERROR,
        SELF_WAKE_OUTCOME_QUIET,
        SELF_WAKE_OUTCOME_SPOKE,
        SILENT_WAKE_ALPHA_MAX,
        SILENT_WAKE_ALPHA_MIN,
        SILENT_WAKE_ALPHA_SEARCH_STEPS,
        SILENT_WAKE_TICK_SECONDS,
    )
    from ..infra.providers import ProviderResolver
    from ..infra.runtime import now_in_tz
    from ..qq_official import platform_id_of_inst
    from ..resident.autonomy_budget import CURRENT_RUN, BudgetExceeded
    from ..resident.ports import build_agent_config as _build_agent_cfg
else:
    from infra.constants import (
        BOT_ACTIVITY_SENDER_ID,
        DISPLAY_NAME,
        SELF_WAKE_FALLBACK_LOOKBACK_MINUTES,
        SELF_WAKE_FALLBACK_MAX_MESSAGES,
        SELF_WAKE_OUTCOME_ERROR,
        SELF_WAKE_OUTCOME_QUIET,
        SELF_WAKE_OUTCOME_SPOKE,
        SILENT_WAKE_ALPHA_MAX,
        SILENT_WAKE_ALPHA_MIN,
        SILENT_WAKE_ALPHA_SEARCH_STEPS,
        SILENT_WAKE_TICK_SECONDS,
    )
    from infra.providers import ProviderResolver
    from infra.runtime import now_in_tz
    from qq_official import platform_id_of_inst
    from resident.autonomy_budget import CURRENT_RUN, BudgetExceeded
    from resident.ports import build_agent_config as _build_agent_cfg


class SelfWakeService:
    """Manage IRMIA wake loop, session selection, fallback judge, and run settlement."""

    def __init__(
        self,
        plugin: Any,
        activity: Any,
        proactive: Any,
        targets: Any,
        resident_getter: Callable[[], Any],
    ) -> None:
        self.plugin = plugin
        self.activity = activity
        self.proactive = proactive
        self.targets = targets
        self._resident_getter = resident_getter
        self._alpha_cache: float | None = None
        self._task: asyncio.Task[None] | None = None
        self._last_human_ts: float | None = None
        self._fail_until: float = 0.0
        self._last_provider_id: str = ""

    def invalidate_alpha(self) -> None:
        self._alpha_cache = None

    def mark_human_activity(self, ts: float | None = None) -> None:
        self._last_human_ts = time.time() if ts is None else ts

    def solve_alpha(self) -> float:
        if self._alpha_cache is not None:
            return self._alpha_cache
        floor = self.plugin.proactive_silent_wake_floor_minutes
        ceil = self.plugin.proactive_silent_wake_ceil_minutes
        target = self.plugin.proactive_silent_wake_target_mean_minutes
        span = max(1e-9, ceil - floor)
        start = max(1, int(floor))
        end = max(start, int(ceil))

        def mean_minutes(alpha: float) -> float:
            survival = 1.0
            expect = 0.0
            for t in range(start, end):
                q = min(1.0, (t - floor) / span)
                p = q ** alpha
                expect += survival * p * t
                survival *= 1.0 - p
                if survival <= 1e-12:
                    break
            return expect + survival * ceil

        low, high = SILENT_WAKE_ALPHA_MIN, SILENT_WAKE_ALPHA_MAX
        for _ in range(SILENT_WAKE_ALPHA_SEARCH_STEPS):
            mid = (low + high) / 2
            if mean_minutes(mid) > target:
                high = mid
            else:
                low = mid
        self._alpha_cache = (low + high) / 2
        return self._alpha_cache

    @staticmethod
    def roll() -> float:
        return random.random()

    def should_trigger(self, quiet_minutes: float, roll_val: float) -> tuple[bool, float]:
        floor = self.plugin.proactive_silent_wake_floor_minutes
        ceil = self.plugin.proactive_silent_wake_ceil_minutes
        if quiet_minutes < floor:
            return False, 0.0
        q = min(1.0, (quiet_minutes - floor) / max(1e-9, ceil - floor))
        probability = q ** self.solve_alpha()
        return roll_val < probability, probability

    def budget_left(self) -> bool:
        day = now_in_tz(self.plugin.context).strftime("%Y-%m-%d")
        resident = self._resident_getter()
        budget = resident.store.budget(day)
        return budget["used"] < self.plugin.proactive_silent_wake_daily_budget

    def init_baseline(self) -> None:
        try:
            with self.activity.db.read_conn() as conn:
                row = conn.execute(
                    "SELECT MAX(ts) FROM activity_messages WHERE sender_id != ?",
                    (BOT_ACTIVITY_SENDER_ID,),
                ).fetchone()
            ts = float(row[0] or 0) if row else 0.0
            self._last_human_ts = ts if ts > 0 else time.time()
        except Exception:
            self._last_human_ts = time.time()

    def start(self) -> None:
        old = self._task
        if old is not None and not old.done():
            old.cancel()
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _loop(self) -> None:
        p = self.plugin
        while True:
            await asyncio.sleep(SILENT_WAKE_TICK_SECONDS)
            try:
                if not (p.enabled and p.proactive_enabled and p.proactive_silent_wake_enabled):
                    continue
                now = time.time()
                if self.proactive.quiet_adjust(now) > now + 1:
                    continue
                if not self.budget_left():
                    continue
                if now < self._fail_until:
                    continue
                if self._last_human_ts is None:
                    self.init_baseline()
                quiet = (now - (self._last_human_ts or now)) / 60.0
                roll_val = self.roll()
                hit, probability = self.should_trigger(quiet, roll_val)
                if not hit:
                    continue
                logger.info(
                    "[%s] 静默自醒：已安静 %.0f 分钟（当拍概率 %.3f，随机 %.3f），起来看一眼",
                    DISPLAY_NAME,
                    quiet,
                    probability,
                    roll_val,
                )
                ok, outcome = await self.run_turn(quiet, probability, roll_val)
                self.settle_turn(ok, outcome, now)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug("[%s] 静默自醒出错: %s", DISPLAY_NAME, exc)

    def settle_turn(self, ok: bool, outcome: str, now: float) -> None:
        if not ok:
            self._fail_until = now + 1800
            return
        self._last_human_ts = time.time()

    async def run_turn(self, quiet_minutes: float, probability: float, roll_val: float) -> tuple[bool, str]:
        return await self._resident_getter().autonomy.run(quiet_minutes, probability, roll_val)

    def build_agent_config(self, event: Any) -> Any:
        return _build_agent_cfg(self.plugin.context, str(getattr(event, "unified_msg_origin", "") or ""))

    @staticmethod
    def self_wake_prompt(quiet_minutes: float, context: Any = None) -> str:
        cur = now_in_tz(context) if context is not None else datetime.now()
        return (
            f"现在是 {cur:%Y-%m-%d %H:%M}。安静了约 {int(quiet_minutes)} 分钟，你醒来了。\n"
            "这是一轮自主活动，不是真人提出的新要求。先参考当前状态、已有计划与未读概况，再决定做什么。\n"
            "可用 list_conversations 查看未读数；用 read_conversation_messages 的 mode=unread 或 mentions、"
            "mark_read=true 阅读实际未读，before_id 可翻页。只处理你确实看到的消息。\n"
            "若本轮提供 astrhe_read_plan 和 astrhe_todo_update，沿用已有计划与待办；日记、复盘由 AstrHe 管理。"
            "记忆需要时用已有检索工具，关系资料沿用关系本。缺少工具时不要假装调用。\n"
            "你可以维护心情和本会话关注点、提出风格建议；想说话时调用 send_message，只向允许目标发送。\n"
            "没有值得做的事可以安静结束，不必为了完成步骤强行调用工具或发言。不要重复已完成的动作。"
        )

    def _last_human_activity_map(self) -> dict[tuple[str, str, str], float]:
        try:
            with self.activity.db.read_conn() as conn:
                rows = conn.execute(
                    "SELECT platform_id,kind,conversation_id,MAX(ts) FROM activity_messages "
                    "WHERE sender_id!=? GROUP BY platform_id,kind,conversation_id",
                    (BOT_ACTIVITY_SENDER_ID,),
                ).fetchall()
            return {(str(p), str(k), str(c)): float(ts or 0) for p, k, c, ts in rows}
        except Exception:
            return {}

    def activity_platform_of(self, kind: str, conversation_id: str) -> str:
        try:
            with self.activity.db.read_conn() as conn:
                row = conn.execute(
                    "SELECT platform_id FROM activity_messages WHERE kind = ? AND conversation_id = ? ORDER BY id DESC LIMIT 1",
                    (kind, conversation_id),
                ).fetchone()
            if row and str(row[0] or "").strip():
                return str(row[0]).strip()
        except Exception:
            pass
        insts = self.activity.platform_insts()
        if len(insts) == 1:
            only_id = platform_id_of_inst(insts[0]).strip()
            if only_id:
                return only_id
        return ""

    async def pick_session(self) -> dict[str, Any] | None:
        human_ts = self._last_human_activity_map()
        candidates: list[tuple[str, str, str]] = []
        for item in self.activity.activity_sessions("all"):
            kind, cid = item["kind"], item["conversation_id"]
            key = (item["platform_id"], kind, cid)
            allowed = (
                self.targets.group_allowed(cid, item.get("name", ""))
                if kind == "group"
                else self.targets.person_allowed(cid)
            )
            if allowed and human_ts.get(key, 0) > 0:
                candidates.append(key)
        candidates.sort(key=lambda k: human_ts[k], reverse=True)
        if not candidates:
            for kind, ids in (
                ("group", self.plugin.proactive_allow_groups),
                ("private", self.plugin.proactive_target_whitelist),
            ):
                for cid in sorted(ids):
                    platform = self.activity_platform_of(kind, cid)
                    if platform:
                        candidates.append((platform, kind, cid))
        for platform, kind, cid in candidates:
            return {
                "kind": kind,
                "id": cid,
                "platform_id": platform,
                "umo": f"{platform}:{'GroupMessage' if kind == 'group' else 'FriendMessage'}:{cid}",
            }
        return None

    async def select_provider(self) -> Any:
        resolver = ProviderResolver(self.plugin.context)
        judge_id = str(getattr(self.plugin, "proactive_judge_provider_id", "") or "").strip()
        if judge_id:
            provider = await resolver.by_id(judge_id)
        else:
            provider = await resolver.session_default()
        if provider is None or not callable(getattr(provider, "text_chat", None)):
            raise RuntimeError(f"找不到可用对话模型：{judge_id or '全局默认'}")
        return provider

    def record_run_meta(self, row: dict[str, Any]) -> None:
        """Persist self-wake telemetry onto unified resident_runs ledger."""
        try:
            run = CURRENT_RUN.get()
            meta = json.dumps(
                {
                    "quiet_minutes": float(row.get("quiet_minutes") or 0),
                    "probability": float(row.get("probability") or 0),
                    "roll": float(row.get("roll") or 0),
                    "provider_id": str(row.get("provider_id") or ""),
                    "steps": int(row.get("steps") or 0),
                    "tool_calls": row.get("tool_calls") or [],
                    "said_text": str(row.get("said_text") or ""),
                    "sent_to": str(row.get("sent_to") or ""),
                },
                ensure_ascii=False,
            )[:8000]
            with self.activity.db.transaction() as conn:
                if run is not None:
                    conn.execute("UPDATE resident_runs SET meta=? WHERE id=?", (meta, run.id))
                else:
                    started = float(row.get("started_ts") or time.time())
                    ended = float(row.get("ended_ts") or started)
                    outcome = str(row.get("outcome") or "skipped")
                    status = "skipped" if outcome == "skipped" else ("failed" if outcome == "error" else "succeeded")
                    day = now_in_tz(self.plugin.context).strftime("%Y-%m-%d")
                    conn.execute(
                        "INSERT INTO resident_runs(umo,persona,day,started,ended,status,outcome,summary,requests,meta) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (
                            "",
                            "default",
                            day,
                            started,
                            ended,
                            status,
                            outcome,
                            str(row.get("thinking") or "")[:4000],
                            int(row.get("steps") or 0),
                            meta,
                        ),
                    )
        except Exception as exc:
            logger.debug("[%s] 自醒记录写入失败: %s", DISPLAY_NAME, exc)

    def _heartbeat_activity_view(self) -> list[dict[str, Any]]:
        try:
            return self.activity.query_activity_messages(
                sessions=None,
                kind="" if self.plugin.proactive_context_other_private else "group",
                after_ts=time.time() - SELF_WAKE_FALLBACK_LOOKBACK_MINUTES * 60,
                sender="",
                keyword="",
                limit=SELF_WAKE_FALLBACK_MAX_MESSAGES,
            )
        except Exception:
            return []

    def _allowed_targets_desc(self) -> str:
        p = self.plugin
        parts = [
            "群：" + "、".join(sorted(p.proactive_allow_groups))
            if p.proactive_allow_groups
            else "群：（主人还没允许任何群）"
        ]
        people = set(p.proactive_target_whitelist) | self.activity.admin_ids()
        parts.append("私聊：" + "、".join(sorted(people)) if people else "私聊：（暂无）")
        return "\n".join(parts)

    def build_heartbeat_prompt(self, silent_minutes: float | None = None) -> str:
        now_dt = now_in_tz(self.plugin.context)
        lines = [f"现在是 {now_dt:%Y-%m-%d %H:%M}。"]
        if silent_minutes is not None:
            lines.append(
                f"【静默自醒】距离上一次有人说话已经安静了约 {int(silent_minutes)} 分钟。"
                "这不是谁在问你问题，只是你自己安静地醒来，想想有没有哪个人/哪个群你想主动打个招呼。"
            )
        view = self._heartbeat_activity_view()
        if view:
            summary = "\n".join(
                f"[{'群' if item['kind'] == 'group' else '私聊'}|{item['conversation']}] "
                f"{item['time'][5:16]} {item['sender']}: {item['text']}"
                for item in view
            )
            lines.append(
                f"【最近动态】（近 {int(SELF_WAKE_FALLBACK_LOOKBACK_MINUTES)} "
                f"分钟，从旧到新；其中「{DISPLAY_NAME}」就是你自己说过的话）\n{summary}"
            )
        else:
            lines.append(
                "【最近动态】（这段时间没有任何新消息——大家都在安静，"
                "你不知道任何人最近说过什么、约过什么）"
            )
        pending = self.proactive.pending_summary()
        if pending:
            lines.append(f"【你已有的主动消息安排】\n{pending}")
        lines.append(f"【允许的主动目标】\n{self._allowed_targets_desc()}")
        lines.append(
            "要求：\n"
            '- 大多数时候直接回 {"want": false}，别为了说话而说话。\n'
            "- 只看【最近动态】里有没有让你「自己想开口」的话头：有人聊到你/提起你、"
            "有话头值得接、有人情绪不对劲想安慰。动态为空时，"
            "除非你就是单纯想找谁说句惦记的话，否则一律回 want:false。\n"
            "- 不要替用户解决问题、不要追问、不要催促别人回话。"
            "这是你自己想说的一句话，不是对别人提问的回应；"
            "看到有人在问技术/具体问题时，除非你确实有话想说，否则回 want:false，"
            "让正常回复路径去答。\n"
            "**反编造铁律：note 里提到的任何对话、约定、事件，"
            "必须能在【最近动态】或【已有安排】里找到出处；"
            "找不到的一律不许写——宁可说一句单纯的「想你了/在忙什么」，"
            "也绝不声称有过什么约定。编造没发生的事是最严重的错误。**\n"
            "- 决定要发就只回一行 JSON（不要输出别的）：\n"
            '{"want": true, "target_kind": "group 或 private", '
            '"target": "群名/群号/群openid 或 QQ/openid", '
            '"note": "背景（只能来自上面的真实动态）；你想说什么、用什么语气"}\n'
            "- 别处会话的私聊内容不许原样搬到其它会话去说。\n"
            "- note 写清楚背景：那边的你会带着这条便签和目标会话的真实对话把话自然说出来；"
            "插件不会自动 @ 任何人。"
        )
        return "\n".join(lines)

    @staticmethod
    def parse_heartbeat_decision(raw: str) -> dict[str, Any] | None:
        text = str(raw or "").strip()
        if not text:
            return None
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            data = json.loads(text[start : end + 1])
        except Exception:
            return None
        if not isinstance(data, dict):
            return None
        want = data.get("want")
        if isinstance(want, str):
            want = want.strip().lower() in {"true", "yes", "1", "想要", "是"}
        if not want:
            return None
        return data

    async def ask_heartbeat_llm(self, silent_minutes: float | None = None) -> tuple[dict[str, Any] | None, bool]:
        self._last_provider_id = ""
        persona_prompt = ""
        try:
            persona = await self.plugin.context.persona_manager.get_default_persona_v3()
            persona_prompt = str(getattr(persona, "system_prompt", "") or "")
        except Exception:
            persona_prompt = ""
        prompt = self.build_heartbeat_prompt(silent_minutes)
        judge_id = str(getattr(self.plugin, "proactive_judge_provider_id", "") or "").strip()
        provider_source = f"配置指定:{judge_id}" if judge_id else "全局默认"
        try:
            provider = await self.select_provider()
            self._last_provider_id = ProviderResolver.provider_id_of(provider)
        except Exception as exc:
            logger.warning("[%s] 巡检判断取模型失败（%s）: %s", DISPLAY_NAME, provider_source, exc)
            return None, False
        try:
            active = self._resident_getter().autonomy.wrap_provider(provider) if CURRENT_RUN.get() is not None else provider
            resp = await asyncio.wait_for(
                active.text_chat(prompt=prompt, system_prompt=persona_prompt or None),
                timeout=120,
            )
        except BudgetExceeded:
            raise
        except Exception as exc:
            logger.warning("[%s] 巡检判断调用模型失败（%s）: %s", DISPLAY_NAME, provider_source, exc)
            return None, False
        raw = ProviderResolver.extract_text(resp)
        return self.parse_heartbeat_decision(raw), True

    async def deliver_heartbeat_decision(self, decision: dict[str, Any]) -> dict[str, Any]:
        kind = str(decision.get("target_kind") or "").strip().lower()
        if kind in {"群", "group"}:
            kind = "group"
        elif kind in {"私聊", "private"}:
            kind = "private"
        else:
            return {"error": "决策里没有可识别的 target_kind（group/private）"}
        target = str(decision.get("target") or "").strip()
        note = str(decision.get("note") or "").strip()
        if not target or not note:
            return {"error": "决策缺少 target 或 note"}
        if kind == "group":
            group_id, group_name = self.targets.match_allowed_group(target)
            if not group_id:
                return {"error": f"群 {target} 不在主人允许的名单里"}
            platform_id = self.activity_platform_of("group", group_id)
            if not platform_id:
                return {"error": f"查不到群 {group_id} 所在的平台"}
            resolved = {
                "platform_id": platform_id,
                "kind": "group",
                "id": group_id,
                "name": group_name or group_id,
                "umo": f"{platform_id}:GroupMessage:{group_id}",
                "desc": f"群「{group_name or group_id}」",
            }
        else:
            qq = ""
            name = ""
            if target.isdigit():
                qq = target
                name = self.targets.person_display_name(qq)
            else:
                qq, name = self.targets.lookup_person_qq(target)
                if not qq and (self.targets.person_display_name(target) or target in self.plugin.proactive_target_whitelist):
                    qq = target
                    name = self.targets.person_display_name(target) or target
            if not qq or not self.targets.person_allowed(qq):
                return {"error": f"人 {target} 不在主人允许的白名单里"}
            platform_id = self.activity_platform_of("private", qq)
            if not platform_id:
                return {"error": f"查不到 {qq} 所在的平台"}
            resolved = {
                "platform_id": platform_id,
                "kind": "private",
                "id": qq,
                "name": name or qq,
                "umo": f"{platform_id}:FriendMessage:{qq}",
                "desc": f"私聊「{name or qq}」",
            }
        return await self.proactive.deliver_message(
            resolved, f"（你自己在空闲时想到的）{note}", "", "", source_umo=str(resolved.get("umo") or "")
        )

    async def run_fallback(self, row: dict[str, Any], quiet_minutes: float) -> tuple[bool, str]:
        self._last_provider_id = ""
        decision, judge_ok = await self.ask_heartbeat_llm(silent_minutes=quiet_minutes)
        row["provider_id"] = self._last_provider_id
        row["ended_ts"] = time.time()
        if not judge_ok:
            if CURRENT_RUN.get() is None:
                self.record_run_meta(row)
            return False, SELF_WAKE_OUTCOME_ERROR
        outcome = SELF_WAKE_OUTCOME_QUIET
        if decision:
            result = await self.deliver_heartbeat_decision(decision) or {}
            if str(result.get("status") or "") == "sent":
                row["said"] = True
                row["sent_to"] = str(decision.get("target") or "")
                outcome = SELF_WAKE_OUTCOME_SPOKE
        row["outcome"] = outcome
        if CURRENT_RUN.get() is None:
            self.record_run_meta(row)
        return True, outcome
