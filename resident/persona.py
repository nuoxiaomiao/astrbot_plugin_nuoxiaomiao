"""A small mutable state and approved style overlay, not another persona engine."""

from __future__ import annotations

import hashlib
import time


def value(obj, name, default=None):
    return obj.get(name, default) if isinstance(obj, dict) else getattr(obj, name, default)


def _format_relative_time(ts: float | int | None, now: float | None = None) -> str:
    if ts is None:
        return ""
    try:
        ref = time.time() if now is None else now
        delta = max(0, int(ref - float(ts)))
    except (TypeError, ValueError):
        return ""
    if delta < 60:
        return "刚刚"
    if delta < 3600:
        return f"{delta // 60}分钟前"
    if delta < 86400:
        return f"{delta // 3600}小时前"
    return f"{delta // 86400}天前"


class Persona:
    def __init__(self, service):
        self.service = service

    async def resolve(self, event, req=None):
        context = self.service.host.context
        manager = context.persona_manager
        conversation = getattr(req, "conversation", None) if req else None
        if conversation is None:
            try:
                cm = context.conversation_manager
                cid = await cm.get_curr_conversation_id(event.unified_msg_origin)
                if cid:
                    conversation = await cm.get_conversation(event.unified_msg_origin, cid)
            except (AttributeError, TypeError):
                pass
        if callable(getattr(manager, "resolve_selected_persona", None)):
            selected, persona, _, _ = await manager.resolve_selected_persona(
                umo=event.unified_msg_origin,
                conversation_persona_id=value(conversation, "persona_id"),
                platform_name=event.get_platform_name(),
            )
        else:
            persona = await manager.get_default_persona_v3(umo=event.unified_msg_origin)
            selected = value(conversation, "persona_id") or value(persona, "name") or "default"
        if persona is None:
            return "@session:" + event.unified_msg_origin, ""
        base = str(value(persona, "prompt", value(persona, "system_prompt", "")) or "")
        return str(selected or value(persona, "name") or "default"), hashlib.sha256(
            base.encode()
        ).hexdigest()

    async def inject(self, event, req):
        if not self.service.plugin.resident_state_enabled:
            return
        if getattr(req, "_nuoxiaomiao_state_injected", False):
            return
        persona, _ = await self.resolve(event, req)
        if persona.startswith("@session:"):
            return
        state = self.service.store.state(persona, event.unified_msg_origin)
        mood = state.get("mood")
        focus = str(state.get("focus") or "").strip()
        if not mood and not focus:
            return
        now = time.time()
        lines = ["[糯小喵当前状态参考]"]
        if mood:
            mood_name = str(mood.get("mood") or "").strip()
            intensity = mood.get("intensity", 50)
            rel = _format_relative_time(mood.get("updated"), now)
            mood_line = f"- 心情：{mood_name}（强度 {intensity}/100"
            if rel:
                mood_line += f"，更新于{rel}"
            mood_line += "）"
            lines.append(mood_line)
        if focus:
            rel_focus = _format_relative_time(state.get("focus_updated"), now)
            focus_line = f"- 本会话关注点：{focus}"
            if rel_focus:
                focus_line += f"（更新于{rel_focus}）"
            lines.append(focus_line)
        lines.append("[当前状态参考结束]")
        self.service.host.append_temp_context(req, "\n".join(lines))
        req._nuoxiaomiao_state_injected = True
