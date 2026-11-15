"""LLM request/response hooks, capability reminders, self-wake context, and activity prefetch."""

from __future__ import annotations

import inspect
from typing import Any, Callable

from astrbot.api import logger
from astrbot.api.provider import ProviderRequest

try:
    from astrbot.core.astr_main_agent_resources import (
        TOOL_CALL_PROMPT as _DEFAULT_TOOL_CALL_PROMPT,
        TOOL_CALL_PROMPT_SKILLS_LIKE_MODE as _DEFAULT_TOOL_CALL_PROMPT_SKILLS_LIKE_MODE,
    )
except Exception:  # pragma: no cover
    _DEFAULT_TOOL_CALL_PROMPT = ""
    _DEFAULT_TOOL_CALL_PROMPT_SKILLS_LIKE_MODE = ""

if __package__ and "." in __package__:
    from ..infra.constants import (
        ACTIVITY_PREFETCH_MAX_SESSIONS,
        ACTIVITY_PREFETCH_PER_SESSION,
        CAPABILITY_HINT_ACTIVITY,
        CAPABILITY_HINT_PROACTIVE,
        CAPABILITY_REMINDER_HEADER,
        DISPLAY_NAME,
        SELF_WAKE_EVENT_EXTRA,
        SYNTHETIC_EVENT_EXTRA,
        UNTRUSTED_NOTE,
    )
    from ..infra.runtime import RateLimitedLogger, extract_text, matches_any_keyword, resolve_timezone
    from ..qq_official import is_qq_official, platform_capability_note
else:
    from infra.constants import (
        ACTIVITY_PREFETCH_MAX_SESSIONS,
        ACTIVITY_PREFETCH_PER_SESSION,
        CAPABILITY_HINT_ACTIVITY,
        CAPABILITY_HINT_PROACTIVE,
        CAPABILITY_REMINDER_HEADER,
        DISPLAY_NAME,
        SELF_WAKE_EVENT_EXTRA,
        SYNTHETIC_EVENT_EXTRA,
        UNTRUSTED_NOTE,
    )
    from infra.runtime import RateLimitedLogger, extract_text, matches_any_keyword, resolve_timezone
    from qq_official import is_qq_official, platform_capability_note


class ContextService:
    """Coordinate request/response hooks, capability reminders, self-wake context, and activity prefetch."""

    def __init__(
        self,
        plugin: Any,
        guard: Any,
        merger: Any,
        activity: Any,
        proactive: Any,
        send_observer: Any,
        media: Any,
        resident_getter: Callable[[], Any],
    ) -> None:
        self.plugin = plugin
        self.guard = guard
        self.merger = merger
        self.activity = activity
        self.proactive = proactive
        self.send_observer = send_observer
        self.media = media
        self._resident_getter = resident_getter
        self._rate_log = RateLimitedLogger(interval_seconds=60.0)

    async def on_llm_request(self, event: Any, req: ProviderRequest) -> None:
        p = self.plugin
        if self.guard.is_poke_event(event) or not p.enabled:
            return
        resident = self._resident_getter()
        if event.get_extra(SELF_WAKE_EVENT_EXTRA):
            resident.integration.restrict(event, req)
            self._sanitize_self_wake_tool_prompt(req)
            await resident.persona.inject(event, req)
            await self.maybe_add_self_wake_context(event, req)
            return

        if p.debounce_enabled and not event.get_extra(SYNTHETIC_EVENT_EXTRA):
            sender_id = str(event.get_sender_id() or "").strip()
            if sender_id:
                key = self.merger.merge_key(str(event.unified_msg_origin), sender_id)
                if await self.merger.has_active_merge(key):
                    event.stop_event()
                    logger.debug("[%s] on_llm_request blocked while merging: %s", DISPLAY_NAME, key)
                    return

        self.maybe_override_tool_call_prompt(req)
        self.send_observer.ensure_send_observer(event)
        self.maybe_add_capability_reminder(event, req)
        await self.maybe_inject_context(event, req)
        resident.inbox.request_ids(event)
        await resident.persona.inject(event, req)

    async def on_llm_response(self, event: Any, response: Any) -> None:
        if event.get_extra(SELF_WAKE_EVENT_EXTRA):
            return
        if self.plugin.enabled:
            await self._resident_getter().inbox.consumed(event, response)
        observed = self.send_observer.ensure_send_observer(event)
        if isinstance(observed, dict) and response is not None:
            if getattr(response, "role", "") != "err":
                completion_text = str(getattr(response, "completion_text", "") or "").strip()
                if completion_text:
                    observed["llm_text"] = completion_text
        if self.guard.is_poke_event(event):
            return
        session_key = str(getattr(event, "unified_msg_origin", "") or "")
        sender_id = str(event.get_sender_id() or "").strip()
        if session_key and sender_id:
            merge_key = self.merger.merge_key(session_key, sender_id)
            epoch = int(event.get_extra("nuoxiaomiao_merge_epoch") or 0)
            await self.media.cleanup_batch(merge_key, epoch)
        await self.media.cleanup_expired()

    async def maybe_add_self_wake_context(self, event: Any, req: ProviderRequest) -> None:
        if getattr(req, "_nuoxiaomiao_self_wake_context_added", False):
            return
        resident = None
        try:
            resident = self._resident_getter()
        except Exception:
            resident = None

        sections: list[str] = []

        # 1. Unread overview across allowed sessions
        try:
            if getattr(self.plugin, "activity_buffer_enabled", True):
                sessions = list(self.activity.activity_sessions("all") or [])
                inbox = getattr(resident, "inbox", None) if resident is not None else None
                if inbox is not None and callable(getattr(inbox, "allowed", None)):
                    allowed_sessions = [s for s in sessions if inbox.allowed(s, event)]
                else:
                    allowed_sessions = sessions

                persona = "default"
                if resident is not None and getattr(resident, "persona", None) is not None:
                    try:
                        resolved_p, _ = await resident.persona.resolve(event)
                        if resolved_p and not str(resolved_p).startswith("@session:"):
                            persona = str(resolved_p)
                    except Exception:
                        persona = "default"

                counts_map: dict[tuple[str, str, str], dict[str, Any]] = {}
                if inbox is not None and allowed_sessions:
                    batch_fn = getattr(inbox, "batch_counts", None)
                    used_batch = False
                    if callable(batch_fn):
                        try:
                            try:
                                res = batch_fn(persona, allowed_sessions, event)
                            except TypeError:
                                res = batch_fn(persona, allowed_sessions)
                            if inspect.isawaitable(res):
                                res = await res
                            if isinstance(res, dict):
                                matched = 0
                                for s in allowed_sessions:
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
                        for s in allowed_sessions:
                            key = (s["platform_id"], s["kind"], s["conversation_id"])
                            try:
                                try:
                                    c = inbox.counts(persona, s, event)
                                except TypeError:
                                    c = inbox.counts(persona, s)
                                if inspect.isawaitable(c):
                                    c = await c
                                if isinstance(c, dict):
                                    counts_map[key] = c
                            except Exception:
                                pass

                enriched: list[dict[str, Any]] = []
                for s in allowed_sessions:
                    key = (s["platform_id"], s["kind"], s["conversation_id"])
                    c = counts_map.get(key, {})
                    unread = int(c.get("unread", 0) or 0)
                    mentions = int(c.get("unread_mentions", 0) or 0)
                    umo = s.get("umo") or (
                        f"{s['platform_id']}:{'GroupMessage' if s['kind'] == 'group' else 'FriendMessage'}:{s['conversation_id']}"
                    )
                    if hasattr(self.activity, "display_conversation_name"):
                        name = self.activity.display_conversation_name(
                            s["platform_id"], s["kind"], s["conversation_id"], s.get("name", "")
                        )
                    else:
                        name = str(s.get("name") or s["conversation_id"])
                    last_ts = float(s.get("last_ts") or 0)
                    last_hhmm = ""
                    if last_ts > 0:
                        from datetime import datetime

                        tz = resolve_timezone(self.plugin.context)
                        dt = datetime.fromtimestamp(last_ts, tz=tz) if tz is not None else datetime.fromtimestamp(last_ts)
                        last_hhmm = dt.strftime("%m-%d %H:%M")
                    enriched.append(
                        {
                            "kind": s["kind"],
                            "umo": umo,
                            "name": name,
                            "unread": unread,
                            "unread_mentions": mentions,
                            "last_ts": last_ts,
                            "last_hhmm": last_hhmm,
                        }
                    )

                total_sessions = len(enriched)
                unread_items = [x for x in enriched if x["unread"] > 0 or x["unread_mentions"] > 0]
                unread_items.sort(
                    key=lambda x: (x["unread_mentions"] > 0, x["unread_mentions"], x["unread"], x["last_ts"]),
                    reverse=True,
                )
                if unread_items:
                    sections.append(
                        f"【未读概况】共 {total_sessions} 个可查看会话，其中 {len(unread_items)} 个有未读（需查看正文请调 read_conversation_messages）："
                    )
                    for item in unread_items[:8]:
                        kind_label = "群" if item["kind"] == "group" else "私聊"
                        when = f"｜最后活跃 {item['last_hhmm']}" if item["last_hhmm"] else ""
                        mention_note = f"，@提及 {item['unread_mentions']} 条" if item["unread_mentions"] else ""
                        sections.append(
                            f"- {kind_label}「{item['name']}」({item['umo']}){when}：未读 {item['unread']} 条{mention_note}"
                        )
                    if len(unread_items) > 8:
                        sections.append(
                            f"（另有 {len(unread_items) - 8} 个未读会话，可用 read_conversation_messages 不传 conversation 查看完整列表）"
                        )
                else:
                    sections.append(f"【未读概况】共 {total_sessions} 个可查看会话，当前暂无未读消息。")
        except Exception as exc:
            logger.debug("[%s] 生成自醒未读概况失败: %s", DISPLAY_NAME, exc)

        # 1.5 Recent self-wake timeline continuity (avoid hijacking a random group's chat history)
        store = getattr(resident, "store", None) if resident is not None else None
        if store is not None and callable(getattr(store, "rows", None)):
            try:
                cur_rid = int(event.get_extra("nuoxiaomiao_resident_run_id") or 0)
                recent_runs = store.rows(
                    "SELECT id, started, outcome, summary FROM resident_runs "
                    "WHERE id != ? AND outcome IN ('quiet', 'spoke', 'interrupted') "
                    "ORDER BY id DESC LIMIT 3",
                    (cur_rid,),
                )
                if recent_runs:
                    from datetime import datetime

                    tz = resolve_timezone(self.plugin.context)
                    run_lines = []
                    for rr in reversed(recent_runs):
                        ts_val = float(rr.get("started") or 0)
                        stamp = (
                            (datetime.fromtimestamp(ts_val, tz=tz) if tz is not None else datetime.fromtimestamp(ts_val)).strftime("%m-%d %H:%M")
                            if ts_val > 0
                            else "--:--"
                        )
                        summ = " ".join(str(rr.get("summary") or "").split())
                        if len(summ) > 60:
                            summ = summ[:60].rstrip() + "…"
                        outcome_label = {"quiet": "安静结束", "spoke": "已对外发言", "interrupted": "已中断"}.get(
                            str(rr.get("outcome") or ""), str(rr.get("outcome") or "")
                        )
                        tail = f"（{summ}）" if summ else ""
                        run_lines.append(f"- {stamp} {outcome_label}{tail}")
                    sections.append("【近期自醒记录】\n" + "\n".join(run_lines))
            except Exception:
                pass

        # 2. Allowed proactive targets
        targets = getattr(self.plugin, "targets", None) or getattr(self.proactive, "targets", None)
        if targets is not None and getattr(self.plugin, "proactive_enabled", True):
            try:
                hint_dict = targets.allowed_targets_hint(event) or {}
                hint = str(hint_dict.get("hint") or "").strip()
                if hint:
                    sections.append(f"【允许的主动目标】{hint}")
            except Exception:
                pass

        # 3. Pending deferred proactive queue
        if getattr(self.plugin, "proactive_enabled", True):
            try:
                pending = str(self.proactive.pending_summary() or "").strip()
                if pending:
                    sections.append(f"【主动消息待发与近期记录】\n{pending}")
            except Exception:
                pass

        # 4. QQ Official platform note
        if is_qq_official(event):
            sections.append(
                "【平台提示】QQ 官方平台需使用 openid（无法用数字 QQ），且非图文消息主动送达可能受限。"
            )

        # 5. Mounted tools actually present in req.func_tool.tools
        func_tool = getattr(req, "func_tool", None)
        tools = getattr(func_tool, "tools", None) if func_tool else None
        mounted = sorted(
            {
                str(getattr(tool, "name", "") or "").strip()
                for tool in (tools or [])
                if getattr(tool, "active", True) and str(getattr(tool, "name", "") or "").strip()
            }
        )
        if mounted:
            sections.append("【本轮实际挂载工具】" + "、".join(mounted))
        else:
            sections.append("【本轮实际挂载工具】无（当前无可用工具，可直接安静结束）")

        # 6. Consecutive quiet-wake reminder on beat 2 or 5
        self_wake = getattr(self.plugin, "self_wake", None)
        if self_wake is not None and callable(getattr(self_wake, "quiet_streak", None)):
            try:
                streak = int(self_wake.quiet_streak() or 0)
                prompt_text = str(getattr(req, "prompt", "") or "")
                if streak in (2, 5) and f"连续第 {streak} 次安静醒来" not in prompt_text:
                    sections.append(
                        f"（提示：这是连续第 {streak} 次安静醒来，可顺便检查一下当前心情、会话关注点或待办；没事继续安静结束即可。）"
                    )
            except Exception:
                pass

        if sections:
            self.append_temp_context(req, "[糯小喵自醒上下文]\n" + "\n".join(sections))
            req._nuoxiaomiao_self_wake_context_added = True

    @staticmethod
    def _sanitize_self_wake_tool_prompt(req: ProviderRequest) -> None:
        sys_prompt = str(getattr(req, "system_prompt", "") or "")
        if not sys_prompt:
            return
        if _DEFAULT_TOOL_CALL_PROMPT and _DEFAULT_TOOL_CALL_PROMPT in sys_prompt:
            req.system_prompt = sys_prompt.replace(
                _DEFAULT_TOOL_CALL_PROMPT,
                "Follow the tool schema exactly and do not invent parameters. This is an autonomous wake turn: do not narrate tool calls to a user; if no external action or message is needed, finish quietly.",
                1,
            )
            return
        if _DEFAULT_TOOL_CALL_PROMPT_SKILLS_LIKE_MODE and _DEFAULT_TOOL_CALL_PROMPT_SKILLS_LIKE_MODE in sys_prompt:
            req.system_prompt = sys_prompt.replace(
                _DEFAULT_TOOL_CALL_PROMPT_SKILLS_LIKE_MODE,
                "Tool schemas may be provided in two stages: first only name and description, then full parameter schema on request. Do not guess arguments before seeing the schema. This is an autonomous wake turn: do not explain tool calls or summarize tool results to a user; if no external action or message is needed, finish quietly.",
                1,
            )

    def maybe_override_tool_call_prompt(self, req: ProviderRequest) -> None:
        p = self.plugin
        if not p.tool_call_prompt_enabled or not p.tool_call_prompt:
            return
        if not (req.func_tool and req.func_tool.tools):
            return
        for orig in (_DEFAULT_TOOL_CALL_PROMPT, _DEFAULT_TOOL_CALL_PROMPT_SKILLS_LIKE_MODE):
            if orig and orig in req.system_prompt:
                req.system_prompt = req.system_prompt.replace(orig, p.tool_call_prompt, 1)
                return
        req.system_prompt += f"\n{p.tool_call_prompt}\n"

    def capability_menu(self, tool_names: set[str] | None = None) -> str:
        p = self.plugin
        names = tool_names or set()
        parts: list[str] = []
        if p.activity_buffer_enabled and p.activity_tool_enabled and p.activity_hint_enabled:
            if "read_conversation_messages" in names:
                parts.append(f"- {CAPABILITY_HINT_ACTIVITY}")
        if p.proactive_enabled and p.proactive_hint_enabled and "send_message" in names:
            parts.append(f"- {CAPABILITY_HINT_PROACTIVE}")
        return "\n".join(parts)

    def capability_reminder(
        self,
        show_pendings: bool = True,
        has_astrhe: bool = False,
        tool_names: set[str] | None = None,
        platform_note: str = "",
    ) -> str:
        p = self.plugin
        parts: list[str] = []
        menu = self.capability_menu(tool_names)
        if menu:
            parts.append(menu)
        if platform_note:
            parts.append(platform_note)
        if has_astrhe and p.proactive_enabled and "send_message" in (tool_names or set()):
            parts.append("工具分工：到点做某事、承诺提醒这类计划性安排用 astrhe_schedule；对允许的会话说一句话用 send_message。")
        if show_pendings and p.proactive_enabled and p.proactive_hint_enabled:
            pending = self.proactive.pending_summary()
            if pending:
                parts.append(pending)
        if not parts:
            return ""
        return f"{CAPABILITY_REMINDER_HEADER}\n" + "\n".join(parts)

    @staticmethod
    def qq_official_current_target_hint(event: Any) -> str:
        try:
            group_id = str(event.get_group_id() or "").strip()
            platform_id = str(event.get_platform_id() or "").strip()
            if group_id:
                return (
                    f"当前会话是群，群 openid={group_id}"
                    f"（完整 umo={platform_id}:GroupMessage:{group_id}）；"
                    f'主动消息 target 直接传「当前」即可，也可以传 「群:{group_id}」。'
                    '不要传空字符串、<群openid> 占位或群号。示例：send_message(target="当前", intent="测试主动消息", at="")。'
                )
            sender_id = str(event.get_sender_id() or "").strip()
            if sender_id:
                return (
                    f"当前会话是私聊，用户 openid={sender_id}"
                    f"（完整 umo={platform_id}:FriendMessage:{sender_id}）；"
                    f'主动消息 target 直接传「当前」即可，也可以传 「私聊:{sender_id}」。'
                    '示例：send_message(target="当前", intent="测试主动消息", at="")。'
                )
        except Exception:
            pass
        return "主动消息 target 传「当前」代表当前会话。"

    def maybe_add_capability_reminder(self, event: Any, req: ProviderRequest) -> None:
        func_tool = getattr(req, "func_tool", None)
        tools = getattr(func_tool, "tools", None) if func_tool else None
        if not tools or getattr(req, "_nuoxiaomiao_capability_reminder_added", False):
            return
        try:
            show_pendings = self.activity.activity_permission_ok(event)
        except Exception:
            show_pendings = False
        names = {str(getattr(tool, "name", "") or "") for tool in tools if getattr(tool, "active", True)}
        platform_note = ""
        if is_qq_official(event) and getattr(self.plugin, "qq_official_enabled", True):
            platform_note = (
                "平台边界："
                + platform_capability_note()
                + "\n主动消息测试："
                + self.qq_official_current_target_hint(event)
            )
        text = self.capability_reminder(
            show_pendings=show_pendings,
            has_astrhe="astrhe_schedule" in names,
            tool_names=names,
            platform_note=platform_note,
        )
        if text:
            self.append_temp_context(req, text)
            req._nuoxiaomiao_capability_reminder_added = True

    async def maybe_inject_context(self, event: Any, req: ProviderRequest) -> None:
        p = self.plugin
        try:
            text = extract_text(event, strip_at_markup=bool(getattr(p, "qq_official_strip_at_markup", True)))
            if (
                p.activity_buffer_enabled
                and self.activity.activity_permission_ok(event)
                and not getattr(req, "_nuoxiaomiao_chat_context_added", False)
            ):
                block = ""
                if p.capability_context_digest:
                    current = self.activity.current_conversation(event)
                    digest = self.proactive.other_sessions_digest(
                        {current} if current else set(), require_proactive_context=True
                    )
                    if digest:
                        block = (
                            "【别处近况】（背景参考，别原样搬运，"
                            "更不许把别处私聊的内容拿到这里来）：\n"
                            + digest
                            + "\n"
                            + UNTRUSTED_NOTE
                        )
                elif p.activity_prefetch_enabled and matches_any_keyword(text, p.activity_prefetch_keywords):
                    block = self.build_activity_prefetch(event)
                if block:
                    self.append_temp_context(req, block)
                    req._nuoxiaomiao_chat_context_added = True
                    logger.info("[%s] 已附带别处聊天参考: session=%s", DISPLAY_NAME, str(event.unified_msg_origin))
        except Exception as exc:
            logger.warning("[%s] 附带参考内容失败: %s", DISPLAY_NAME, exc)

    def build_activity_prefetch(self, event: Any) -> str:
        current = self.activity.current_conversation(event)
        digest = self.proactive.other_sessions_digest(
            {current} if current else set(),
            max_sessions=ACTIVITY_PREFETCH_MAX_SESSIONS,
            per_session=ACTIVITY_PREFETCH_PER_SESSION,
            require_proactive_context=True,
        )
        if not digest:
            return ""
        return (
            "[别处聊天参考] 下面是速览库里其它会话最近的消息，"
            "回答时以它为准，不要凭印象编造；没提到的细节可以说没看到：\n"
            + digest
            + "\n"
            + UNTRUSTED_NOTE
        )

    @staticmethod
    def append_temp_context(req: ProviderRequest, text: str) -> None:
        parts = getattr(req, "extra_user_content_parts", None)
        if isinstance(parts, list):
            try:
                from astrbot.core.agent.message import TextPart

                parts.append(TextPart(text=text).mark_as_temp())
                return
            except Exception:
                pass
        try:
            req.system_prompt = f"{req.system_prompt or ''}\n{text}\n"
        except Exception:
            pass
