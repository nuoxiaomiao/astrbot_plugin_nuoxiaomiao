"""Resolve real enabled capabilities. Never construct another plugin's tools."""

from __future__ import annotations

BASE_TOOLS = frozenset(
    {
        "list_conversations",
        "read_conversation_messages",
        "send_message",
        "recall_long_term_memory",
        "read_memory_evidence",
        "astrhe_read_plan",
        "astrhe_todo_update",
        "nuoxiaomiao_update_state",
        "nuoxiaomiao_propose_style",
    }
)
CAPABILITIES = (
    (
        "memory",
        "长期记忆",
        "LivingMemory",
        ("recall_long_term_memory", "memorize_long_term_memory"),
        "astrbot_plugin_livingmemory",
    ),
    (
        "relationships",
        "关系资料",
        "夜璃关系本",
        ("query_relationship", "update_relationship"),
        "astrbot_plugin_yeli_relationship",
    ),
    (
        "plans",
        "计划、待办与日记",
        "AstrHe",
        ("astrhe_read_plan", "astrhe_todo_update"),
        "astrbot_plugin_astrhe",
    ),
    (
        "schedule",
        "定时任务",
        "AstrHe / AstrBot",
        ("astrhe_schedule", "future_task"),
        "astrbot_plugin_astrhe",
    ),
    (
        "development",
        "开发工具",
        "Irmia DevKit / AstrBot",
        ("safe_read", "safe_edit", "shell_exec", "astrbot_execute_shell"),
        "astrbot_plugin_irmia_devkit",
    ),
    (
        "search",
        "互联网搜索",
        "AnySearch / AstrBot",
        ("anysearch_search", "web_search"),
        "astrbot_plugin_anysearch",
    ),
)
# These dispatch other tools through their own executor. They require a future
# scope-propagation adapter; a name alone cannot grant all their child tools.
DISPATCH_NAMES = frozenset(
    {"astrna_parallel_tool_use", "task", "mcp", "use_tool", "execute_tool"}
)


def is_dispatcher(tool):
    name = str(getattr(tool, "name", ""))
    return (
        name in DISPATCH_NAMES
        or name.startswith("transfer_to_")
        or any(c.__name__ == "HandoffTool" for c in type(tool).__mro__)
        or bool(getattr(tool, "is_background_task", False))
    )


def active_stars(context):
    try:
        return [
            s
            for s in context.get_all_stars()
            if getattr(s, "activated", False)
            and getattr(s, "star_cls", None)
            and getattr(s.star_cls, "enabled", True)
        ]
    except (AttributeError, TypeError):
        return []


class Integration:
    def __init__(self, service):
        self.service = service
        self.last_effective = {}
        self.owners = {}

    def tools(self):
        try:
            return (
                self.service.plugin.context.get_llm_tool_manager()
                .get_full_tool_set()
                .tools
            )
        except AttributeError:
            return list(
                getattr(
                    self.service.plugin.context.get_llm_tool_manager(), "func_list", []
                )
                or []
            )

    def catalog(self):
        p = self.service.plugin
        try:
            from astrbot.core.star.star import star_map
        except ImportError:
            star_map = {}
        result = []
        stars = {s.name: s for s in active_stars(p.context)}
        can_check = callable(getattr(p.context, "get_all_stars", None))
        for tool in self.tools():
            name = str(getattr(tool, "name", ""))
            if not name:
                continue
            owner = p._plugin_name_of_tool(tool, star_map)
            loaded = (
                not can_check
                or owner in stars
                or owner in ("", "（内置工具）", "astrbot_plugin_nuoxiaomiao")
            )
            result.append(
                {
                    "name": name,
                    "plugin": p._plugin_name_of_tool(tool, star_map),
                    "active": bool(getattr(tool, "active", True)) and loaded,
                    "base": name in BASE_TOOLS,
                    "selectable": not is_dispatcher(tool),
                    "reason": "嵌套或后台执行尚无自主权限传递接口，保留在普通聊天使用"
                    if is_dispatcher(tool)
                    else "",
                }
            )
        return result

    def migrate_selection(self):
        p = self.service.plugin
        # A marker survives AstrBot inserting schema defaults before initialize.
        if p.config.get("resident_tools_migrated", False):
            return
        legacy = set(
            p._normalize_str_list(
                p.config.get("proactive_silent_wake_tool_plugins", [])
            )
        )
        selected = set(p._normalize_str_list(p.config.get("resident_extra_tools", [])))
        selected.update(
            t["name"]
            for t in self.catalog()
            if t["plugin"] in legacy
            and t["active"]
            and t["selectable"]
            and not t["base"]
        )
        from pathlib import Path
        from importlib import import_module

        support = import_module(
            (p.__class__.__module__.rsplit(".", 1)[0] + ".config_support")
            if "." in p.__class__.__module__
            else "config_support"
        )
        backup = Path(p._activity_db_file()).parent / "config.before-resident-v1.json"
        if not backup.exists():
            support.atomic_write_json(backup, dict(p.config))
        updated = dict(p.config)
        updated.update(
            resident_extra_tools=sorted(selected), resident_tools_migrated=True
        )
        p._commit_web_config(updated)
        self.service.store.set_meta(
            "tools_migration",
            "旧全工具模式已收窄为基础工具；原非空插件选择已冻结为具体工具。",
        )

    def allowed(self):
        p = self.service.plugin
        names = BASE_TOOLS | set(p.resident_extra_tools)
        if not p.resident_state_enabled:
            names -= {"nuoxiaomiao_update_state", "nuoxiaomiao_propose_style"}
        if not p.activity_buffer_enabled or not p.activity_tool_enabled:
            names -= {"list_conversations", "read_conversation_messages"}
        if not p.proactive_enabled:
            names -= {"send_message"}
        return names

    def owner_allows(self, event, tool):
        p = self.service.plugin
        try:
            from astrbot.core.star.star import star_map
        except ImportError:
            star_map = {}
        name = getattr(tool, "name", "")
        module = getattr(tool, "handler_module_path", None)
        owner = star_map.get(module)
        if owner is not None:
            self.owners[name] = owner.name
        owner_name = self.owners.get(name)
        if not owner_name:
            return True  # Native/MCP tools have already passed the host builder.
        if callable(getattr(p.context, "get_all_stars", None)):
            if owner_name not in {s.name for s in active_stars(p.context)}:
                return False
        selected = getattr(event, "plugins_name", None)
        try:
            configured = p.context.get_config(umo=event.unified_msg_origin).get(
                "plugin_set", ["*"]
            )
            if "*" not in configured:
                selected = (
                    configured if selected is None else set(selected) & set(configured)
                )
        except (AttributeError, TypeError):
            pass
        return (
            selected is None
            or owner_name in selected
            or bool(getattr(owner, "reserved", False))
        )

    def restrict(self, event, req):
        from astrbot.core.provider.func_tool_manager import ToolSet

        original = getattr(req, "func_tool", None)
        allowed = self.allowed()
        kept = ToolSet()
        for tool in getattr(original, "tools", []) or []:
            if (
                getattr(tool, "name", "") in allowed
                and getattr(tool, "active", True)
                and not is_dispatcher(tool)
                and self.owner_allows(event, tool)
            ):
                kept.add_tool(tool)
        req.func_tool = kept
        self.last_effective[event.unified_msg_origin] = {t.name for t in kept.tools}
        return kept

    def capabilities(self, umo=""):
        catalog = self.catalog()
        known = {t["name"]: t for t in catalog}
        effective = self.last_effective.get(umo)
        stars = {s.name: s for s in active_stars(self.service.plugin.context)}
        result = []
        for key, title, source, names, plugin in CAPABILITIES:
            present = [
                name for name in names if name in known and known[name]["active"]
            ]
            state = "ready" if present else "unavailable"
            reason = (
                "工具已注册；会话权限在请求时再核对"
                if present
                else "插件未加载、未启用或未注册对应工具"
            )
            if effective is not None:
                present = [name for name in present if name in effective]
                state = "ready" if present else "out_of_scope"
                reason = (
                    "最近一轮自醒中实际可用"
                    if present
                    else "本会话、自主工具选择或依赖权限未允许"
                )
            result.append(
                {
                    "id": key,
                    "title": title,
                    "source": source,
                    "state": state,
                    "reason": reason,
                    "tools": present,
                    "plugin": plugin,
                    "page": ("admin" if key == "relationships" else "dashboard")
                    if plugin in stars and key in ("memory", "plans", "relationships")
                    else "",
                    "url": f"/#/plugin-view/{plugin}/{'admin' if key == 'relationships' else 'dashboard'}"
                    if plugin in stars and key in ("memory", "plans", "relationships")
                    else "",
                }
            )
        for key, title, plugin in (
            ("flow", "暂停与恢复", "astrbot_plugin_MiaoFlow"),
            ("context", "长对话压缩", "astrbot_plugin_AstrNa"),
            ("output", "拟人发送", "astrbot_plugin_outputpro"),
        ):
            found = stars.get(plugin)
            ready = found is not None
            if key == "flow" and ready:
                ready = all(
                    callable(getattr(found.star_cls, name, None))
                    for name in ("checkpoint", "get_session_state", "register_listener")
                )
            result.append(
                {
                    "id": key,
                    "title": title,
                    "source": plugin,
                    "state": "ready" if ready else "unavailable",
                    "reason": "已加载；沿用该插件的会话范围与开关"
                    if ready
                    else "插件未启用或缺少公开联动接口",
                    "tools": [],
                    "plugin": plugin,
                    "page": "",
                    "url": "",
                }
            )
        return result
