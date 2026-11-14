"""LLM request/response hooks, brain-lock gating, capability reminders, and LMEM recall."""

from __future__ import annotations

import asyncio
import json
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
        BRAIN_LOCK_WAIT_SECONDS,
        CAPABILITY_HINT_ACTIVITY,
        CAPABILITY_HINT_PROACTIVE,
        CAPABILITY_REMINDER_HEADER,
        DISPLAY_NAME,
        LMEM_CONTEXT_FOOTER,
        LMEM_CONTEXT_HEADER,
        NO_BRAIN_EXTRA,
        SELF_WAKE_EVENT_EXTRA,
        SYNTHETIC_EVENT_EXTRA,
        UNTRUSTED_NOTE,
    )
    from ..infra.runtime import RateLimitedLogger, extract_text, matches_any_keyword
    from ..qq_official import is_qq_official, platform_capability_note
else:
    from infra.constants import (
        ACTIVITY_PREFETCH_MAX_SESSIONS,
        ACTIVITY_PREFETCH_PER_SESSION,
        BRAIN_LOCK_WAIT_SECONDS,
        CAPABILITY_HINT_ACTIVITY,
        CAPABILITY_HINT_PROACTIVE,
        CAPABILITY_REMINDER_HEADER,
        DISPLAY_NAME,
        LMEM_CONTEXT_FOOTER,
        LMEM_CONTEXT_HEADER,
        NO_BRAIN_EXTRA,
        SELF_WAKE_EVENT_EXTRA,
        SYNTHETIC_EVENT_EXTRA,
        UNTRUSTED_NOTE,
    )
    from infra.runtime import RateLimitedLogger, extract_text, matches_any_keyword
    from qq_official import is_qq_official, platform_capability_note


class ContextService:
    """Coordinate waiting/request/response hooks, reminders, prefetch, and LMEM recall."""

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

    async def on_waiting_llm_request(self, event: Any) -> None:
        p = self.plugin
        if not p.enabled or not p.shared_memory_enabled:
            return
        if self.guard.is_poke_event(event) or self.guard.is_typing_notice(event):
            return
        if event.get_extra(SELF_WAKE_EVENT_EXTRA):
            return
        resident = self._resident_getter()
        await resident.master.maybe_auto_bind(event)
        if not p.shared_memory_enabled:
            return
        held = False
        try:
            held = await asyncio.wait_for(
                resident.master.acquire_brain(),
                timeout=BRAIN_LOCK_WAIT_SECONDS,
            )
        except asyncio.TimeoutError:
            event.set_extra(NO_BRAIN_EXTRA, True)
            self._rate_log.warning(
                "brain_lock_timeout",
                "[%s] 等待主脑锁超过 %.0f 秒，本轮不绑定共享记忆",
                DISPLAY_NAME,
                BRAIN_LOCK_WAIT_SECONDS,
            )
            return
        except asyncio.CancelledError:
            resident.master.release_brain()
            raise
        except Exception:
            resident.master.release_brain()
            return
        if held:
            task = asyncio.current_task()
            if task is not None:
                task.add_done_callback(lambda _t: resident.master.release_brain())

    async def on_llm_request(self, event: Any, req: ProviderRequest) -> None:
        p = self.plugin
        if self.guard.is_poke_event(event) or not p.enabled:
            return
        resident = self._resident_getter()
        if event.get_extra(SELF_WAKE_EVENT_EXTRA):
            resident.integration.restrict(event, req)
            self.maybe_override_tool_call_prompt(req)
            await resident.persona.inject(event, req)
            return

        await resident.master.bind_master(event, req)

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

        if p.mention_notice_enabled and getattr(event, "is_at_or_wake_command", False):
            try:
                persona, _ = await resident.persona.resolve(event)
                exclude_ids = (
                    event.get_extra("nuoxiaomiao_inbox_request_ids")
                    or event.get_extra("nuoxiaomiao_inbox_ids")
                    or []
                )
                notice_text, shown_ids = await resident.inbox.notice(event, persona, exclude_ids=exclude_ids)
                if notice_text:
                    self.append_temp_context(req, notice_text)
                if shown_ids:
                    event.set_extra("nuoxiaomiao_inbox_shown_ids", shown_ids)
            except Exception as exc:
                self._rate_log.warning("notice_inject", "[%s] 注入点名通知失败: %s", DISPLAY_NAME, exc)

        umo = str(event.unified_msg_origin or "")
        if umo:
            await resident.topic.maybe_summarize(umo)
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
            if {"list_conversations", "read_conversation_messages"} <= names:
                parts.append(f"- {CAPABILITY_HINT_ACTIVITY}")
            elif "list_conversations" in names:
                parts.append("- 用 list_conversations 查看其它会话列表；本轮未挂载读取消息的工具。")
            elif "read_conversation_messages" in names:
                parts.append("- 用 read_conversation_messages 按会话、发言人、时间和关键词查看消息，不要凭印象编造。")
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
        custom = str(getattr(p, "capability_hint_text", "") or "").strip()
        parts: list[str] = []
        if custom:
            parts.append(custom)
        else:
            menu = self.capability_menu(tool_names)
            if menu:
                parts.append(menu)
        if platform_note:
            parts.append(platform_note)
        if has_astrhe and p.proactive_enabled and "send_message" in (tool_names or set()):
            parts.append("工具分工：到点做某事、承诺提醒这类计划性安排用 astrhe_schedule；对允许的会话说一句话用 send_message。")
        if show_pendings and p.proactive_enabled and (p.proactive_hint_enabled or custom):
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
                    "不要传空字符串或 <openid> 占位。"
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
            if p.lmem_gateway_enabled and matches_any_keyword(text, p.lmem_gateway_keywords):
                block = await self.recall_from_lmem(event, text)
                if block:
                    self.append_temp_context(req, block)
                    logger.info("[%s] 已附带长期记忆参考: session=%s", DISPLAY_NAME, str(event.unified_msg_origin))

            if (
                p.activity_buffer_enabled
                and self.activity.activity_permission_ok(event)
                and not getattr(req, "_nuoxiaomiao_chat_context_added", False)
            ):
                block = ""
                if p.capability_context_digest:
                    current = self.activity.current_conversation(event)
                    digest = self.proactive.other_sessions_digest(
                        {current} if current else set(), require_proactive_context=False
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
            require_proactive_context=False,
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

    async def recall_from_lmem(self, event: Any, query: str) -> str:
        tool = self._find_lmem_tool()
        if tool is not None:
            if not callable(getattr(tool, "call", None)):
                return ""
            recall = self._recall_with_lmem_tool(event, query, tool=tool)
        else:
            recall = self._recall_with_lmem_engine(event, query)
        try:
            return await asyncio.wait_for(recall, timeout=self.plugin.lmem_gateway_timeout_seconds)
        except Exception as exc:
            logger.debug("[%s] 长期记忆检索失败或超时: %s", DISPLAY_NAME, exc)
            return ""

    def _find_lmem_tool(self) -> Any:
        try:
            tools = self.plugin.context.get_llm_tool_manager().func_list
        except Exception:
            return None
        for tool in tools or []:
            if getattr(tool, "name", "") == self.plugin.lmem_gateway_recall_tool_name:
                return tool
        return None

    def _build_agent_context(self, event: Any) -> Any:
        try:
            from astrbot.core.agent.run_context import ContextWrapper
            from astrbot.core.astr_agent_context import AstrAgentContext

            return ContextWrapper(context=AstrAgentContext(context=self.plugin.context, event=event))
        except Exception:
            holder = type("SimpleAgentContext", (), {})()
            holder.context = self.plugin.context
            holder.event = event
            try:
                from astrbot.core.agent.run_context import ContextWrapper

                return ContextWrapper(context=holder)
            except Exception:
                return type("SimpleContextWrapper", (), {"context": holder})()

    async def _recall_with_lmem_tool(self, event: Any, query: str, tool: Any = None) -> str:
        if tool is None:
            tool = self._find_lmem_tool()
        if tool is None or not callable(getattr(tool, "call", None)):
            return ""
        try:
            context_wrapper = self._build_agent_context(event)
            raw = await tool.call(context_wrapper, query=query, k=self.plugin.lmem_gateway_top_k)
        except Exception as exc:
            logger.debug("[%s] 调用长期记忆检索失败: %s", DISPLAY_NAME, exc)
            return ""
        return self._format_lmem_result(raw)

    async def _recall_with_lmem_engine(self, event: Any, query: str) -> str:
        try:
            star = self.plugin.context.get_registered_star(self.plugin.lmem_gateway_plugin_name)
        except Exception:
            star = None
        instance = getattr(star, "star_cls", None) if star is not None else None
        engine = getattr(getattr(instance, "initializer", None), "memory_engine", None)
        if engine is None or not callable(getattr(engine, "search_memories", None)):
            return ""
        try:
            memories = await engine.search_memories(
                query=query,
                k=self.plugin.lmem_gateway_top_k,
                session_id=str(event.unified_msg_origin),
                persona_id=None,
            )
        except Exception as exc:
            logger.debug("[%s] 直接检索长期记忆失败: %s", DISPLAY_NAME, exc)
            return ""
        lines = []
        for memory in memories or []:
            content = str(getattr(memory, "content", "") or "").strip()
            if content:
                lines.append(f"- {content}")
        return self._wrap_lmem_lines(lines)

    def _format_lmem_result(self, raw: Any) -> str:
        data: Any = raw
        if isinstance(raw, str):
            try:
                data = json.loads(raw)
            except Exception:
                data = raw
        items: list[Any] = []
        if isinstance(data, dict):
            for key in ("results", "记录", "memories"):
                if isinstance(data.get(key), list):
                    items = data[key]
                    break
        if items:
            lines = []
            for item in items:
                if isinstance(item, dict):
                    content = str(item.get("content") or item.get("内容") or item.get("text") or "").strip()
                    if not content:
                        continue
                    score = item.get("score")
                    if isinstance(score, (int, float)):
                        lines.append(f"- {content}（相关度 {float(score):.2f}）")
                    else:
                        lines.append(f"- {content}")
                else:
                    text = str(item).strip()
                    if text:
                        lines.append(f"- {text}")
            return self._wrap_lmem_lines(lines)
        if isinstance(data, str) and data.strip() and data.strip() != "null":
            return self._wrap_lmem_lines([data.strip()])
        return ""

    def _wrap_lmem_lines(self, lines: list[str]) -> str:
        if not lines:
            return ""
        body = "\n".join(lines)
        if len(body) > self.plugin.lmem_gateway_max_chars:
            body = body[: self.plugin.lmem_gateway_max_chars].rstrip() + "…"
        return f"{LMEM_CONTEXT_HEADER}\n{body}\n{LMEM_CONTEXT_FOOTER}"
