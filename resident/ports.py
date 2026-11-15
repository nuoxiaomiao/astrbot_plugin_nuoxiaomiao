"""Host adapter and AstrBot core agent bridges for the resident package."""

from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import datetime
import inspect
import sys
from typing import Any

try:
    from astrbot.core.astr_main_agent import (
        MainAgentBuildConfig,
        _get_session_conv,
        build_main_agent,
    )
    from astrbot.core.cron.events import CronMessageEvent
    from astrbot.core.platform.message_session import MessageSession
    from astrbot.core.platform.message_type import MessageType
except Exception:  # pragma: no cover
    MainAgentBuildConfig = None  # type: ignore[assignment]
    _get_session_conv = None  # type: ignore[assignment]
    build_main_agent = None  # type: ignore[assignment]
    CronMessageEvent = None  # type: ignore[assignment]
    MessageSession = None  # type: ignore[assignment]
    MessageType = None  # type: ignore[assignment]

if __package__ and "." in __package__:
    from ..infra.constants import ACTIVITY_QUERY_MAX_SESSIONS, SELF_WAKE_EVENT_EXTRA
    from ..infra.database import ActivityDatabase
    from ..infra.providers import ProviderResolver
    from ..infra.runtime import (
        extract_components,
        group_scope_allows,
        message_id_of,
        now_in_tz,
        plugin_name_of_tool,
    )
else:
    from infra.constants import ACTIVITY_QUERY_MAX_SESSIONS, SELF_WAKE_EVENT_EXTRA
    from infra.database import ActivityDatabase
    from infra.providers import ProviderResolver
    from infra.runtime import (
        extract_components,
        group_scope_allows,
        message_id_of,
        now_in_tz,
        plugin_name_of_tool,
    )


@dataclass(frozen=True)
class AgentCoreHooks:
    cron_event_cls: Any
    message_session_cls: Any
    message_type_enum: Any
    build_main_agent: Any
    build_config_cls: Any
    get_session_conv: Any


def resolve_agent_core(plugin: Any = None) -> AgentCoreHooks:
    """Resolve AstrBot 4.29 autonomous agent APIs (supporting test monkeypatching)."""
    mod = sys.modules.get(plugin.__class__.__module__) if plugin is not None else None
    return AgentCoreHooks(
        cron_event_cls=getattr(plugin, "_cron_event_cls", None)
        or getattr(mod, "_CronMessageEvent", None)
        or CronMessageEvent,
        message_session_cls=getattr(plugin, "_message_session_cls", None)
        or getattr(mod, "_MessageSession", None)
        or MessageSession,
        message_type_enum=getattr(plugin, "_message_type_enum", None)
        or getattr(mod, "_MessageType", None)
        or MessageType,
        build_main_agent=getattr(plugin, "_build_main_agent_fn", None)
        or getattr(mod, "_build_main_agent", None)
        or build_main_agent,
        build_config_cls=getattr(plugin, "_main_agent_build_config_cls", None)
        or getattr(mod, "_MainAgentBuildConfig", None)
        or MainAgentBuildConfig,
        get_session_conv=getattr(plugin, "_get_session_conv_fn", None)
        or getattr(mod, "_get_session_conv", None)
        or _get_session_conv,
    )


def build_agent_config(context: Any, umo: str) -> Any:
    hooks = resolve_agent_core()
    if hooks.build_config_cls is None:
        return None
    try:
        cfg = context.get_config(umo=umo)
    except Exception:
        cfg = context.get_config()
    agent_cfg = (cfg.get("agent_runner", {}) or {}).get("config", {}) or {}
    misc = agent_cfg.get("misc", {}) or {}
    provider_settings = cfg.get("provider_settings", {}) or {}
    persona_config = agent_cfg.get("persona", {}) or {}
    from astrbot.core.config.agent_runner import resolve_context_compression_config

    return hooks.build_config_cls(
        tool_call_timeout=int(misc.get("tool_call_timeout", 120) or 120),
        tool_schema_mode=str(misc.get("tool_schema_mode", "full")),
        sanitize_context_by_modalities=bool(misc.get("sanitize_context_by_modalities", False)),
        kb_agentic_mode=bool(cfg.get("kb_agentic_mode", False)),
        **resolve_context_compression_config(agent_cfg.get("compression", {}) or {}),
        provider_settings={
            **provider_settings,
            "default_personality": persona_config.get("persona_id", "default"),
        },
        add_cron_tools=bool(
            (provider_settings.get("proactive_capability", {}) or {}).get("add_cron_tools", True)
        ),
        subagent_orchestrator=cfg.get("subagent_orchestrator", {}) or {},
        timezone=cfg.get("timezone"),
        llm_safety_mode=bool(persona_config.get("safety_mode", True)),
        safety_mode_strategy=str(persona_config.get("safety_mode_strategy", "system_prompt")),
        streaming_response=False,
        computer_use_runtime=str(provider_settings.get("computer_use_runtime", "none")),
        sandbox_cfg=provider_settings.get("sandbox", {}) or {},
    )


class ResidentHost:
    """Narrow host interface connecting resident services with feature services."""

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin

    @property
    def context(self) -> Any:
        return self.plugin.context

    @property
    def raw_config(self) -> Any:
        return self.plugin.config

    @property
    def bg_tasks(self) -> set[Any]:
        return self.plugin._resident_bg_tasks

    def database(self) -> ActivityDatabase:
        return self.plugin.db

    def plugin_data_dir(self):
        return self.plugin.plugin_data_dir()

    def activity_db_file(self):
        return self.plugin.activity_db_file()

    def admin_ids(self) -> set[str]:
        return self.plugin.activity.admin_ids()

    @staticmethod
    def is_self_wake_event(event: Any) -> bool:
        try:
            return bool(event.get_extra(SELF_WAKE_EVENT_EXTRA))
        except Exception:
            return False

    def is_poke_event(self, event: Any) -> bool:
        return self.plugin.guard.is_poke_event(event)

    def is_typing_notice(self, event: Any) -> bool:
        return self.plugin.guard.is_typing_notice(event)

    def is_command_event(self, event: Any) -> bool:
        return self.plugin.guard.is_command_event(event)

    def is_synthetic_id(self, message_id: str) -> bool:
        merger = getattr(self.plugin, "merger", None)
        if merger is not None and callable(getattr(merger, "is_synthetic_id", None)):
            return bool(merger.is_synthetic_id(message_id))
        return False

    def should_reset_quiet_timer(self, event: Any, *, keyword_woken: bool = False) -> bool:
        return bool(self.plugin.self_wake.should_reset_quiet_timer(event, keyword_woken=keyword_woken))

    @staticmethod
    def message_id(event: Any) -> str:
        return message_id_of(event)

    @staticmethod
    def extract_components(event: Any) -> list[Any]:
        return extract_components(event)

    def current_conversation(self, event: Any):
        return self.plugin.activity.current_conversation(event)

    def activity_sessions(self, kind: str = "all") -> list[dict[str, Any]]:
        return self.plugin.activity.activity_sessions(kind=kind)

    def resolve_activity_conversations(self, name: str, kind: str = "all") -> list[dict[str, Any]]:
        return self.plugin.activity.resolve_activity_conversations(name, kind)

    def now_in_tz(self) -> datetime:
        return now_in_tz(getattr(self.plugin, "context", None))

    def today_in_tz(self) -> str:
        return self.now_in_tz().strftime("%Y-%m-%d")

    def display_conversation_name(
        self, platform_id: str, kind: str, conversation_id: str, name: str = ""
    ) -> str:
        return self.plugin.activity.display_conversation_name(platform_id, kind, conversation_id, name)

    def display_user_name(self, platform_id: str, user_id: str, name: str = "") -> str:
        return self.plugin.activity.display_user_name(platform_id, user_id, name)

    @staticmethod
    def group_scope_allows(
        gid: str, gname: str, whitelist: set[str], blacklist: set[str], platform_id: str = ""
    ) -> bool:
        return group_scope_allows(gid, gname, whitelist, blacklist, platform_id)

    def event_group_allowed(self, event: Any, whitelist: set[str], blacklist: set[str]) -> bool:
        return self.plugin.guard.event_group_allowed(event, whitelist, blacklist)

    def parse_activity_time(self, raw: str) -> tuple[float | None, str | None]:
        return self.plugin.activity.parse_activity_time(raw)

    def append_temp_context(self, req: Any, text: str) -> None:
        self.plugin.ctx_injector.append_temp_context(req, text)

    def webapi_username(self) -> str:
        return self.plugin.config_svc.webapi_username()

    def commit_web_config(self, config: dict[str, Any]) -> None:
        self.plugin.config_svc.commit_web_config(config)

    @staticmethod
    def plugin_name_of_tool(tool: Any, star_map: dict) -> str:
        return plugin_name_of_tool(tool, star_map)

    def proactive_group_allowed(self, group_id: str, group_name: str = "") -> bool:
        return self.plugin.targets.group_allowed(group_id, group_name)

    def proactive_person_allowed(self, user_id: str) -> bool:
        return self.plugin.targets.person_allowed(user_id)

    def update_proactive_row(self, row_id: int, **fields: Any) -> None:
        self.plugin.proactive.update_row(row_id, **fields)

    def cancel_proactive_timer(self, row_id: int) -> None:
        self.plugin.proactive.cancel_timer(row_id)

    async def pick_self_wake_session(self) -> dict[str, Any] | None:
        res = self.plugin.self_wake.pick_session()
        if inspect.isawaitable(res):
            return await res
        return res

    def self_wake_prompt(self, quiet_minutes: float) -> str:
        return self.plugin.self_wake.self_wake_prompt(quiet_minutes, self.plugin.context)

    def build_self_wake_agent_config(self, event: Any) -> Any:
        return self.plugin.self_wake.build_agent_config(event)

    async def select_self_wake_provider(self) -> Any:
        return await self.plugin.self_wake.select_provider()

    @staticmethod
    def provider_id_of(provider: Any) -> str:
        return ProviderResolver.provider_id_of(provider)

    def record_self_wake_run(self, row: dict[str, Any]) -> None:
        self.plugin.self_wake.record_run_meta(row)

    async def run_self_wake_fallback(self, row: dict[str, Any], quiet_minutes: float) -> tuple[bool, str]:
        return await self.plugin.self_wake.run_fallback(row, quiet_minutes)

    @property
    def max_query_sessions(self) -> int:
        return ACTIVITY_QUERY_MAX_SESSIONS


def sync_config_fields(target: Any, cfg: Any) -> None:
    for f in fields(cfg):
        if f.name in {"raw_runtime", "errors"}:
            continue
        setattr(target, f.name, getattr(cfg, f.name))
