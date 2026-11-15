"""IRMIA silent self-wake scheduler, fallback judge, and autonomous runner bridge."""

from __future__ import annotations

import asyncio
from datetime import datetime
import inspect
import json
import random
import time
from types import SimpleNamespace
from typing import Any, Callable

from astrbot.api import logger

if __package__ and "." in __package__:
    from ..infra.constants import (
        BOT_ACTIVITY_SENDER_ID,
        DISPLAY_NAME,
        SELF_WAKE_EVENT_EXTRA,
        SELF_WAKE_FALLBACK_LOOKBACK_MINUTES,
        SELF_WAKE_FALLBACK_MAX_MESSAGES,
        SELF_WAKE_OUTCOME_ERROR,
        SELF_WAKE_OUTCOME_QUIET,
        SELF_WAKE_OUTCOME_SKIPPED,
        SELF_WAKE_OUTCOME_SPOKE,
        SELF_WAKE_UMO,
        SILENT_WAKE_ALPHA_MAX,
        SILENT_WAKE_ALPHA_MIN,
        SILENT_WAKE_ALPHA_SEARCH_STEPS,
        SILENT_WAKE_TICK_SECONDS,
        SYNTHETIC_EVENT_EXTRA,
    )
    from ..infra.providers import ProviderResolver
    from ..infra.runtime import group_scope_allows, message_id_of, now_in_tz
    from ..qq_official import platform_id_of_inst
    from ..resident.autonomy_budget import CURRENT_RUN, BudgetExceeded
    from ..resident.ports import build_agent_config as _build_agent_cfg
else:
    from infra.constants import (
        BOT_ACTIVITY_SENDER_ID,
        DISPLAY_NAME,
        SELF_WAKE_EVENT_EXTRA,
        SELF_WAKE_FALLBACK_LOOKBACK_MINUTES,
        SELF_WAKE_FALLBACK_MAX_MESSAGES,
        SELF_WAKE_OUTCOME_ERROR,
        SELF_WAKE_OUTCOME_QUIET,
        SELF_WAKE_OUTCOME_SKIPPED,
        SELF_WAKE_OUTCOME_SPOKE,
        SELF_WAKE_UMO,
        SILENT_WAKE_ALPHA_MAX,
        SILENT_WAKE_ALPHA_MIN,
        SILENT_WAKE_ALPHA_SEARCH_STEPS,
        SILENT_WAKE_TICK_SECONDS,
        SYNTHETIC_EVENT_EXTRA,
    )
    from infra.providers import ProviderResolver
    from infra.runtime import group_scope_allows, message_id_of, now_in_tz
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
        self._skip_until: float = 0.0
        self._last_provider_id: str = ""
        self._quiet_streak: int | None = None

    def invalidate_alpha(self) -> None:
        self._alpha_cache = None

    def quiet_streak(self) -> int:
        if self._quiet_streak is None:
            val = 0
            try:
                resident = self._resident_getter()
                store = getattr(resident, "store", None)
                if store is not None:
                    val = max(0, int(store.meta("self_wake_quiet_streak", "0") or 0))
            except Exception:
                val = 0
            self._quiet_streak = val
        return self._quiet_streak

    def set_quiet_streak(self, n: int) -> None:
        try:
            val = max(0, int(n))
        except (TypeError, ValueError):
            val = 0
        self._quiet_streak = val
        try:
            resident = self._resident_getter()
            store = getattr(resident, "store", None)
            if store is not None:
                store.set_meta("self_wake_quiet_streak", str(val))
        except Exception:
            pass

    def mark_human_activity(self, ts: float | None = None) -> None:
        self._last_human_ts = time.time() if ts is None else ts
        self.set_quiet_streak(0)

    def should_reset_quiet_timer(self, event: Any, *, keyword_woken: bool = False) -> bool:
        if not getattr(self.plugin, "enabled", True):
            return False
        guard = getattr(self.plugin, "guard", None) or getattr(self.activity, "guard", None)
        if guard is not None:
            if guard.is_poke_event(event) or guard.is_typing_notice(event):
                return False
        get_extra = getattr(event, "get_extra", None)
        if callable(get_extra):
            if get_extra(SYNTHETIC_EVENT_EXTRA) or get_extra(SELF_WAKE_EVENT_EXTRA):
                return False
        merger = getattr(self.plugin, "merger", None)
        if merger is not None and callable(getattr(merger, "is_synthetic_id", None)):
            mid = message_id_of(event)
            if mid and merger.is_synthetic_id(mid):
                return False
        get_sender = getattr(event, "get_sender_id", None)
        get_self = getattr(event, "get_self_id", None)
        sender_id = str((get_sender() if callable(get_sender) else "") or "").strip()
        self_id = str((get_self() if callable(get_self) else "") or "").strip()
        if not sender_id or (self_id and sender_id == self_id):
            return False
        blocked = getattr(self.plugin, "blocked_users", set()) or set()
        if sender_id in blocked:
            return False
        if guard is not None and guard.is_command_event(event):
            return False
        try:
            is_private = bool(event.is_private_chat())
        except Exception:
            is_private = False
        if is_private:
            return bool(
                getattr(self.plugin, "activity_record_private", False)
                or self.targets.person_allowed(sender_id)
            )
        get_group = getattr(event, "get_group_id", None)
        gid = str((get_group() if callable(get_group) else "") or "").strip()
        if gid:
            # 对齐 IRMIA：普通未 @ 机器人的群聊水群只进信箱，不重置心跳安静计时；
            # 只有真正触发/点名机器人的消息才视作与她互动并复位计时。
            is_direct_call = bool(keyword_woken)
            if not is_direct_call and guard is not None and callable(getattr(guard, "is_dialogue_trigger", None)):
                try:
                    is_direct_call = bool(guard.is_dialogue_trigger(event))
                except Exception:
                    is_direct_call = False
            if not is_direct_call and hasattr(self.activity, "mentioned_by_real_at"):
                try:
                    is_direct_call = bool(self.activity.mentioned_by_real_at(event))
                except Exception:
                    is_direct_call = False
            if not is_direct_call:
                return False
            group = getattr(getattr(event, "message_obj", None), "group", None)
            gname = str(getattr(group, "group_name", "") or "").strip()
            if not gname and hasattr(self.activity, "conversation_display_name"):
                try:
                    gname = str(self.activity.conversation_display_name(event, "group") or "").strip()
                except Exception:
                    gname = ""
            if guard is not None:
                group_ok = guard.event_group_allowed(
                    event,
                    self.plugin.activity_group_whitelist,
                    self.plugin.activity_group_blacklist,
                )
            else:
                get_plat = getattr(event, "get_platform_id", None)
                plat = str((get_plat() if callable(get_plat) else "") or "").strip()
                group_ok = group_scope_allows(
                    gid,
                    gname,
                    self.plugin.activity_group_whitelist,
                    self.plugin.activity_group_blacklist,
                    plat,
                )
            return bool(group_ok or self.targets.group_allowed(gid, gname))
        return True

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
        limit = int(getattr(self.plugin, "proactive_silent_wake_daily_budget", 0) or 0)
        if limit <= 0:
            return True
        day = now_in_tz(self.plugin.context).strftime("%Y-%m-%d")
        resident = self._resident_getter()
        budget = resident.store.budget(day)
        return budget["used"] < limit

    def init_baseline(self) -> None:
        try:
            with self.activity.db.read_conn() as conn:
                row = conn.execute(
                    "SELECT MAX(ts) FROM activity_messages "
                    "WHERE sender_id = ? OR kind = 'private' OR mentioned = 1",
                    (BOT_ACTIVITY_SENDER_ID,),
                ).fetchone()
                wake_ts = 0.0
                try:
                    run_row = conn.execute(
                        "SELECT MAX(COALESCE(ended, started)) FROM resident_runs "
                        "WHERE outcome IN (?, ?)",
                        (SELF_WAKE_OUTCOME_QUIET, SELF_WAKE_OUTCOME_SPOKE),
                    ).fetchone()
                    wake_ts = float(run_row[0] or 0) if run_row else 0.0
                except Exception:
                    wake_ts = 0.0
            ts = max(float(row[0] or 0) if row else 0.0, wake_ts)
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
                if not self.budget_left():
                    continue
                if now < self._fail_until or now < self._skip_until:
                    continue
                if not self.activity.admin_ids():
                    continue
                if self._last_human_ts is None:
                    self.init_baseline()
                quiet = (now - (self._last_human_ts or now)) / 60.0
                roll_val = self.roll()
                hit, probability = self.should_trigger(quiet, roll_val)
                if not hit:
                    continue
                logger.info(
                    "[%s] 自醒触发：距上次直接互动/自醒 %.0f 分钟（当拍概率 %.3f，随机 %.3f），起来看一眼",
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
                logger.debug("[%s] 自醒出错: %s", DISPLAY_NAME, exc)

    def settle_turn(self, ok: bool, outcome: str, now: float) -> None:
        if not ok:
            self._fail_until = now + 1800
            return
        if outcome == SELF_WAKE_OUTCOME_SKIPPED:
            self._skip_until = now + 300
            return
        self._last_human_ts = time.time()
        if outcome == SELF_WAKE_OUTCOME_QUIET:
            self.set_quiet_streak(self.quiet_streak() + 1)
        elif outcome in (SELF_WAKE_OUTCOME_SPOKE, "postponed"):
            self.set_quiet_streak(0)

    async def run_turn(self, quiet_minutes: float, probability: float, roll_val: float) -> tuple[bool, str]:
        return await self._resident_getter().autonomy.run(quiet_minutes, probability, roll_val)

    def build_agent_config(self, event: Any) -> Any:
        return _build_agent_cfg(self.plugin.context, str(getattr(event, "unified_msg_origin", "") or ""))

    @staticmethod
    def self_wake_prompt(quiet_minutes: float, context: Any = None) -> str:
        cur = now_in_tz(context) if context is not None else datetime.now()
        lines = [
            f"现在是 {cur:%Y-%m-%d %H:%M}。距离上次直接互动或自醒已过去约 {int(quiet_minutes)} 分钟，你醒来了。",
            "这是一轮独立自主活动，不是真人提出的新要求；没有值得做的事时直接安静结束（不调工具、不发言）完全正常，不必为了完成步骤强行动作，也不要重复已完成的动作。",
            "如需查看跨会话消息，使用 read_conversation_messages：不传 conversation 可列出可用会话与概况，传入 conversation 可读取上次查看之后的新增未读，传入 before_id 可往前翻页。只依据你实际看到的消息判断，不要凭印象脑补。",
            "如需对外说话，send_message 是唯一向允许目标发送消息的途径，且必须明确指定 target（群:...、私聊:... 或完整 umo）；本轮直接输出的回复文本不会发送给任何聊天对象。",
            "你也可以按需通过 nuoxiaomiao_update_state 维护当前心情与关注点。",
        ]
        return "\n".join(lines)

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
                if kind == "private":
                    row2 = conn.execute(
                        "SELECT platform_id FROM activity_messages WHERE sender_id = ? AND platform_id != '' ORDER BY id DESC LIMIT 1",
                        (conversation_id,),
                    ).fetchone()
                    if row2 and str(row2[0] or "").strip():
                        return str(row2[0]).strip()
        except Exception:
            pass
        insts = self.activity.platform_insts()
        if len(insts) == 1:
            only_id = platform_id_of_inst(insts[0]).strip()
            if only_id:
                return only_id
        return ""

    async def _session_unread_counts(
        self, resident: Any, pool: list[dict[str, Any]]
    ) -> dict[tuple[str, str, str], dict[str, Any]]:
        counts_map: dict[tuple[str, str, str], dict[str, Any]] = {}
        if resident is None or not pool:
            return counts_map
        inbox = getattr(resident, "inbox", None)
        if inbox is None:
            return counts_map
        persona = "default"
        try:
            persona_svc = getattr(resident, "persona", None)
            if persona_svc is not None and callable(getattr(persona_svc, "resolve", None)):
                top = pool[0]
                umo = top.get("umo") or (
                    f"{top['platform_id']}:{'GroupMessage' if top['kind'] == 'group' else 'FriendMessage'}:{top['conversation_id']}"
                )
                if callable(getattr(resident, "web_event", None)):
                    resolved_p, _ = await persona_svc.resolve(resident.web_event(umo))
                    if resolved_p and not str(resolved_p).startswith("@session:"):
                        persona = str(resolved_p)
        except Exception:
            persona = "default"

        batch_fn = getattr(inbox, "batch_counts", None)
        used_batch = False
        if callable(batch_fn):
            try:
                res = batch_fn(persona, pool)
                if inspect.isawaitable(res):
                    res = await res
                if isinstance(res, dict):
                    matched = 0
                    for s in pool:
                        key = (s["platform_id"], s["kind"], s["conversation_id"])
                        umo = s.get("umo") or (
                            f"{s['platform_id']}:{'GroupMessage' if s['kind'] == 'group' else 'FriendMessage'}:{s['conversation_id']}"
                        )
                        entry = res.get(key) if key in res else res.get(umo)
                        if isinstance(entry, dict):
                            counts_map[key] = entry
                            matched += 1
                    if len(res) == 0 or matched > 0:
                        used_batch = True
            except Exception:
                counts_map = {}

        if not used_batch and callable(getattr(inbox, "counts", None)):
            for s in pool:
                key = (s["platform_id"], s["kind"], s["conversation_id"])
                try:
                    c = inbox.counts(persona, s)
                    if inspect.isawaitable(c):
                        c = await c
                    if isinstance(c, dict):
                        counts_map[key] = c
                except Exception:
                    pass
        return counts_map

    async def pick_session(self) -> dict[str, Any] | None:
        return {
            "kind": "private",
            "id": "self_wake",
            "platform_id": "nuoxiaomiao",
            "umo": SELF_WAKE_UMO,
        }

    async def select_provider(self) -> Any:
        resolver = ProviderResolver(self.plugin.context)
        judge_id = str(getattr(self.plugin, "proactive_judge_provider_id", "") or "").strip()
        provider = await (resolver.by_id(judge_id) if judge_id else resolver.session_default())
        if provider is None:
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
            sessions = self.activity.activity_sessions(
                "all" if self.plugin.proactive_context_other_private else "group"
            )
            resident = None
            try:
                resident = self._resident_getter()
            except Exception:
                resident = None
            inbox = getattr(resident, "inbox", None) if resident is not None else None
            filtered: list[dict[str, Any]] = []
            for session in sessions:
                if session.get("kind") == "private" and not self.plugin.proactive_context_other_private:
                    continue
                if inbox is not None and callable(getattr(inbox, "allowed", None)):
                    if not inbox.allowed(session):
                        continue
                else:
                    if session.get("kind") == "group":
                        if not group_scope_allows(
                            session["conversation_id"],
                            session.get("name", ""),
                            self.plugin.activity_group_whitelist,
                            self.plugin.activity_group_blacklist,
                            session["platform_id"],
                        ):
                            continue
                    elif not self.plugin.activity_record_private:
                        continue
                filtered.append(session)
            if not filtered:
                return []
            return self.activity.query_activity_messages(
                sessions=filtered,
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
            persona_mgr = getattr(self.plugin.context, "persona_manager", None)
            if persona_mgr is not None:
                p_obj = await persona_mgr.get_default_persona_v3()
                if isinstance(p_obj, dict):
                    persona_prompt = str(p_obj.get("prompt") or p_obj.get("system_prompt") or "")
                elif p_obj is not None:
                    persona_prompt = str(
                        getattr(p_obj, "prompt", "") or getattr(p_obj, "system_prompt", "") or ""
                    )
        except Exception:
            persona_prompt = ""
        prompt = self.build_heartbeat_prompt(silent_minutes)
        resident = None
        try:
            resident = self._resident_getter()
        except Exception:
            resident = None
        if resident is not None and getattr(resident, "persona", None) is not None:
            try:
                run = CURRENT_RUN.get()
                state_event = getattr(run, "event", None) if run is not None else None
                if state_event is None:
                    picked = await self.pick_session()
                    if picked and picked.get("umo") and callable(getattr(resident, "web_event", None)):
                        state_event = resident.web_event(picked["umo"])
                    else:
                        state_event = SimpleNamespace(
                            unified_msg_origin="",
                            get_platform_name=lambda: "",
                            get_platform_id=lambda: "",
                        )
                from astrbot.api.provider import ProviderRequest

                overlay_req = ProviderRequest(prompt="")
                await resident.persona.inject(state_event, overlay_req)
                state_parts = [
                    str(getattr(part, "text", "") or "").strip()
                    for part in (getattr(overlay_req, "extra_user_content_parts", None) or [])
                    if str(getattr(part, "text", "") or "").strip()
                ]
                state_ref = "\n".join(state_parts)
                if not state_ref and str(getattr(overlay_req, "system_prompt", "") or "").strip():
                    state_ref = str(overlay_req.system_prompt).strip()
                if state_ref:
                    prompt = f"{state_ref}\n\n{prompt}"
            except Exception:
                pass
        judge_id = str(getattr(self.plugin, "proactive_judge_provider_id", "") or "").strip()
        provider_source = f"配置指定:{judge_id}" if judge_id else "全局默认"
        try:
            provider = await self.select_provider()
            self._last_provider_id = ProviderResolver.provider_id_of(provider)
        except Exception as exc:
            logger.warning("[%s] 巡检判断取模型失败（%s）: %s", DISPLAY_NAME, provider_source, exc)
            return None, False
        try:
            active = (
                resident.autonomy.wrap_provider(provider)
                if (resident is not None and CURRENT_RUN.get() is not None)
                else provider
            )
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
            return {"status": "skipped", "error": "决策里没有可识别的 target_kind（group/private）"}
        target = str(decision.get("target") or "").strip()
        note = str(decision.get("note") or "").strip()
        if not target or not note:
            return {"status": "skipped", "error": "决策缺少 target 或 note"}

        target_spec = target if ":" in target and target.count(":") >= 2 else (
            f"群:{target}" if kind == "group" else f"私聊:{target}"
        )
        dummy_event = SimpleNamespace(
            unified_msg_origin=SELF_WAKE_UMO,
            get_platform_id=lambda: "",
            get_platform_name=lambda: "",
            get_group_id=lambda: "",
            get_sender_id=lambda: "",
            get_sender_name=lambda: "",
            is_private_chat=lambda: True,
        )
        resolved = self.targets.resolve_target(dummy_event, target_spec)
        if "error" in resolved:
            return {"status": "skipped", "error": resolved["error"]}
        if not str(resolved.get("platform_id") or "").strip():
            return {"status": "skipped", "error": f"查不到 {target} 所在的平台"}

        intent = f"（你自己在空闲时想到的）{note}"
        schedule_fn = getattr(self.proactive, "schedule_or_deliver", None)
        if callable(schedule_fn):
            return await schedule_fn(
                resolved,
                intent,
                "",
                "",
                source_umo=SELF_WAKE_UMO,
            )
        return await self.proactive.deliver_message(
            resolved, intent, "", "", source_umo=SELF_WAKE_UMO
        )

    async def run_fallback(self, row: dict[str, Any], quiet_minutes: float) -> tuple[bool, str]:
        self._last_provider_id = ""
        decision, judge_ok = await self.ask_heartbeat_llm(silent_minutes=quiet_minutes)
        row["provider_id"] = self._last_provider_id
        row["ended_ts"] = time.time()
        if not judge_ok:
            row["outcome"] = SELF_WAKE_OUTCOME_ERROR
            if CURRENT_RUN.get() is None:
                self.record_run_meta(row)
            return False, SELF_WAKE_OUTCOME_ERROR
        ok = True
        outcome = SELF_WAKE_OUTCOME_QUIET
        if decision:
            result = await self.deliver_heartbeat_decision(decision) or {}
            status = str(result.get("status") or "").strip().lower()
            if status in ("sent", "postponed"):
                row["said"] = True
                row["sent_to"] = str(decision.get("target") or "")
                outcome = SELF_WAKE_OUTCOME_SPOKE
            elif status == "skipped":
                outcome = SELF_WAKE_OUTCOME_SKIPPED
            else:
                outcome = SELF_WAKE_OUTCOME_ERROR
                ok = False
        row["outcome"] = outcome
        if CURRENT_RUN.get() is None:
            self.record_run_meta(row)
        return ok, outcome
