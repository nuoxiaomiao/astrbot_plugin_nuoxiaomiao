"""A small mutable state and approved style overlay, not another persona engine."""

from __future__ import annotations
import hashlib
import json


def value(obj, name, default=None):
    return (
        obj.get(name, default) if isinstance(obj, dict) else getattr(obj, name, default)
    )


class Persona:
    def __init__(self, service):
        self.service = service

    async def resolve(self, event, req=None):
        context = self.service.plugin.context
        manager = context.persona_manager
        conversation = getattr(req, "conversation", None) if req else None
        if conversation is None:
            try:
                cm = context.conversation_manager
                cid = await cm.get_curr_conversation_id(event.unified_msg_origin)
                if cid:
                    conversation = await cm.get_conversation(
                        event.unified_msg_origin, cid
                    )
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
            selected = (
                value(conversation, "persona_id") or value(persona, "name") or "default"
            )
        if persona is None:
            return "@session:" + event.unified_msg_origin, ""
        base = str(value(persona, "prompt", value(persona, "system_prompt", "")) or "")
        return str(selected or value(persona, "name") or "default"), hashlib.sha256(
            base.encode()
        ).hexdigest()

    async def inject(self, event, req):
        if not self.service.plugin.resident_state_enabled:
            return
        persona, _ = await self.resolve(event, req)
        if persona.startswith("@session:"):
            return
        self.service.seen_personas[event.unified_msg_origin] = persona
        state = self.service.store.state(persona, event.unified_msg_origin)
        style = self.service.store.style(persona)
        # Temporary data belongs at the tail; leave the stable persona prefix alone.
        if state["mood"] or state["focus"] or style["body"]:
            payload = {"心情": state["mood"], "本会话关注点": state["focus"]}
            if style["body"]:
                payload["管理员已批准的风格补充"] = style["body"]
            self.service.plugin._append_temp_context(
                req,
                "[糯小喵当前状态参考]\n"
                + json.dumps(payload, ensure_ascii=False)
                + "\n[当前状态参考结束]",
            )
