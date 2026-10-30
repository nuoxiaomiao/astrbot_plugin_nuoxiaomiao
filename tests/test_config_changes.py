"""Configuration compatibility and dashboard saves, isolated from live files."""

from __future__ import annotations

import json
import math
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from quart import Quart

from config_support import (
    CURRENT_CONFIG_NAME,
    LEGACY_CONFIG_NAME,
    atomic_write_json,
    config_diagnostics,
    migrate_config_dir,
    normalize_config,
    preload_config_migration,
)
from main import NuoxiaomiaoPlugin


SCHEMA = json.loads((Path(__file__).resolve().parents[1] / "_conf_schema.json").read_text(encoding="utf-8-sig"))


class StoredConfig(dict):
    def __init__(self, path: Path, values: dict):
        super().__init__(values)
        self.path = path
        self.fail_save = False
        self.saves = 0

    def save_config(self, config):
        self.update(config)
        if self.fail_save:
            raise OSError("simulated write failure")
        atomic_write_json(self.path, dict(self))
        self.saves += 1


def backend_plugin(directory: Path, **config) -> NuoxiaomiaoPlugin:
    plugin = object.__new__(NuoxiaomiaoPlugin)
    values = {key: spec["default"] for key, spec in SCHEMA.items()}
    values.update(config)
    plugin.config = StoredConfig(directory / "config.json", values)
    plugin.user_default_config_path = str(directory / "user-default.json")
    plugin.context = SimpleNamespace(
        get_all_providers=lambda: [],
        get_llm_tool_manager=lambda: SimpleNamespace(get_full_tool_set=lambda: SimpleNamespace(tools=[])),
    )
    from resident.service import ResidentServices
    plugin.resident = ResidentServices(plugin)
    plugin.applies = 0

    def apply():
        plugin.applies += 1
        plugin._runtime_config = normalize_config(plugin.config, SCHEMA, runtime=True)[0]

    plugin._apply_config = apply
    return plugin


class ConfigCompatibilityTest(unittest.TestCase):
    def test_aliases_precede_schema_defaults_and_preserve_custom_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            path = directory / CURRENT_CONFIG_NAME
            original = {"merge_enabled": False, "poke_reply_debounce_seconds": 2.5,
                        "tool_call_prompt": " 自定义\n提示词保持原样。 "}
            payload = ("\ufeff" + json.dumps(original, ensure_ascii=False)).encode("utf-8")
            path.write_bytes(payload)

            preload_config_migration("data.plugins.astrbot_plugin_nuoxiaomiao.main", directory, directory)

            from astrbot.core.config.astrbot_config import AstrBotConfig

            loaded = AstrBotConfig(config_path=str(path), schema=SCHEMA)
            self.assertFalse(loaded["debounce_enabled"])
            self.assertEqual(loaded["poke_reply_cooldown_seconds"], 2.5)
            self.assertEqual(loaded["tool_call_prompt"], original["tool_call_prompt"])
            backups = list(directory.glob("*.bak-alias-*"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_bytes(), payload)
            before = path.read_bytes()
            migrate_config_dir(directory)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(len(list(directory.glob("*.bak-alias-*"))), 1)

    def test_explicit_new_keys_win_and_personal_default_migrates(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            current = directory / CURRENT_CONFIG_NAME
            current.write_text(json.dumps({"merge_enabled": True, "debounce_enabled": False,
                                           "poke_reply_debounce_seconds": 2.0, "poke_reply_cooldown_seconds": 0.5}))
            snapshot = directory / "default.json"
            snapshot.write_text('{"merge_enabled": false, "custom": "keep"}')
            migrate_config_dir(directory, snapshot)
            migrated = json.loads(current.read_text())
            self.assertEqual(migrated, {"debounce_enabled": False, "poke_reply_cooldown_seconds": 0.5})
            self.assertEqual(json.loads(snapshot.read_text()), {"debounce_enabled": False, "custom": "keep"})

    def test_test_import_namespace_never_migrates(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            legacy = directory / LEGACY_CONFIG_NAME
            legacy.write_text('{"merge_enabled": false}')
            self.assertIsNone(preload_config_migration("main", directory, directory))
            self.assertFalse((directory / CURRENT_CONFIG_NAME).exists())
            self.assertEqual(list(directory.iterdir()), [legacy])

    def test_failed_atomic_replace_preserves_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            path = directory / CURRENT_CONFIG_NAME
            path.write_text('{"merge_enabled": false}')
            original = path.read_bytes()
            with patch("config_support.os.replace", side_effect=OSError("replace failed")):
                with self.assertRaises(OSError):
                    migrate_config_dir(directory)
            self.assertEqual(path.read_bytes(), original)
            self.assertFalse(list(directory.glob(".*.tmp-*")))

    def test_runtime_preserves_historical_finite_values_but_reports_limits(self):
        raw = {"human_reply_chars_per_minute": 10_000_000, "window_seconds": 0}
        runtime, errors = normalize_config(raw, SCHEMA, runtime=True)
        self.assertEqual(runtime["human_reply_chars_per_minute"], 10_000_000)
        self.assertEqual(runtime["window_seconds"], 0)
        self.assertIn("human_reply_chars_per_minute", errors)
        self.assertIn("window_seconds", errors)
        self.assertEqual(raw, {"human_reply_chars_per_minute": 10_000_000, "window_seconds": 0})

    def test_nonfinite_values_never_reach_runtime(self):
        for bad in [float("nan"), float("inf"), "-Infinity", "NaN"]:
            runtime, errors = normalize_config({"debounce_slow_idle_seconds": bad}, SCHEMA, runtime=True)
            self.assertTrue(math.isfinite(runtime["debounce_slow_idle_seconds"]))
            self.assertIn("debounce_slow_idle_seconds", errors)
            self.assertTrue(config_diagnostics({"debounce_slow_idle_seconds": bad}, SCHEMA))

    def test_invalid_native_image_directory_uses_the_runtime_default(self):
        plugin = object.__new__(NuoxiaomiaoPlugin)
        plugin.config = {"merge_localize_images_dir": {"unexpected": "directory"}}
        plugin._runtime_config, errors = normalize_config(plugin.config, SCHEMA, runtime=True)
        self.assertIn("merge_localize_images_dir", errors)
        with patch.object(Path, "mkdir"):
            target = plugin._resolve_localize_images_dir()
        self.assertEqual(target, Path(__file__).resolve().parents[1] / "data" / "merged_images")


class ConfigApiTest(unittest.IsolatedAsyncioTestCase):
    async def call_api(self, plugin, method, body=None):
        app = Quart(__name__)
        async with app.test_request_context("/config", method="POST" if body is not None else "GET", json=body):
            result = await getattr(plugin, method)()
            response, status = result if isinstance(result, tuple) else (result, 200)
            return await response.get_json(), status

    async def test_partial_save_validates_merged_self_wake_relationship(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin = backend_plugin(Path(tmp), proactive_silent_wake_floor_minutes=5,
                                    proactive_silent_wake_target_mean_minutes=30,
                                    proactive_silent_wake_ceil_minutes=60)
            before = dict(plugin.config)
            data, status = await self.call_api(plugin, "_webapi_save_config", {"config": {"proactive_silent_wake_floor_minutes": 30}})
            self.assertEqual(status, 400)
            self.assertIn("proactive_silent_wake_target_mean_minutes", data["field_errors"])
            self.assertTrue(data["errors"])
            self.assertEqual(dict(plugin.config), before)
            self.assertEqual(plugin.applies, 0)

    async def test_invalid_range_time_and_fractional_integer_are_rejected(self):
        for key, value in [("human_reply_max_length", -1), ("trigger_rounds", 2.5),
                           ("proactive_quiet_hours_start", "24:00"), ("proactive_quiet_hours_end", "08:60"),
                           ("debounce_fast_idle_seconds", float("inf"))]:
            with self.subTest(key=key), tempfile.TemporaryDirectory() as tmp:
                plugin = backend_plugin(Path(tmp))
                data, status = await self.call_api(plugin, "_webapi_save_config", {"config": {key: value}})
                self.assertEqual(status, 400)
                self.assertIn(key, data["field_errors"])
                self.assertEqual(plugin.config.saves, 0)

    async def test_native_bridge_can_request_validation_details_without_saving(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin = backend_plugin(Path(tmp))
            before = dict(plugin.config)
            data, status = await self.call_api(plugin, "_webapi_save_config", {
                "config": {"trigger_rounds": -1}, "return_validation_errors": True})
            self.assertEqual(status, 200)
            self.assertEqual(data["status"], "ok")
            self.assertTrue(data["data"]["validation_error"])
            self.assertIn("trigger_rounds", data["data"]["field_errors"])
            self.assertIn("连刷触发轮数", data["data"]["message"])
            self.assertEqual(dict(plugin.config), before)
            self.assertEqual(plugin.config.saves, 0)
            self.assertEqual(plugin.applies, 0)

    async def test_save_returns_normalized_values_without_rewriting_custom_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin = backend_plugin(Path(tmp), internal_option="keep")
            custom = " 自定义\n提示词。 "
            data, status = await self.call_api(plugin, "_webapi_save_config", {"config": {
                "debounce_enabled": "false", "trigger_rounds": "4", "proactive_allow_groups": "group-a，group-b\ngroup-a",
                "tool_call_prompt": custom, "unknown_payload_key": "ignored"}})
            self.assertEqual(status, 200)
            config = data["data"]["config"]
            self.assertIs(config["debounce_enabled"], False)
            self.assertEqual(config["trigger_rounds"], 4)
            self.assertEqual(config["proactive_allow_groups"], ["group-a", "group-b"])
            self.assertEqual(config["tool_call_prompt"], custom)
            self.assertEqual(config["internal_option"], "keep")
            self.assertNotIn("unknown_payload_key", config)
            self.assertEqual(plugin.applies, 1)

    async def test_dedicated_and_native_saves_read_the_same_values_after_reload(self):
        from astrbot.core.config.astrbot_config import AstrBotConfig

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            plugin = backend_plugin(directory)
            updates = {
                "debounce_enabled": False,
                "tool_call_prompt": " 自定义提示词\n原样保留。 ",
                "blocked_users": ["10001", "10002"],
                "activity_db_path": str(directory / "custom" / "records.db"),
            }
            saved, status = await self.call_api(plugin, "_webapi_save_config", {"config": updates})
            self.assertEqual(status, 200)
            native = AstrBotConfig(config_path=str(plugin.config.path), schema=SCHEMA)
            for key in SCHEMA:
                self.assertEqual(native[key], saved["data"]["config"][key], key)
            native["poke_reply_enabled"] = False
            native["poke_reply_prompt"] = " {username}，你好。\n这是原生页保存的提示词。 "
            native.save_config()
            reloaded = AstrBotConfig(config_path=str(plugin.config.path), schema=SCHEMA)
            plugin.config = reloaded
            plugin._apply_config()
            dedicated, status = await self.call_api(plugin, "_webapi_get_config")
            self.assertEqual(status, 200)
            for key in SCHEMA:
                self.assertEqual(dedicated["data"]["config"][key], reloaded[key], key)
            for key, value in updates.items():
                self.assertEqual(reloaded[key], value, key)

    async def test_get_returns_safe_provider_catalog_timezone_and_diagnostics(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin = backend_plugin(Path(tmp), debounce_slow_idle_seconds=float("inf"))
            plugin.context.get_all_providers = lambda: [SimpleNamespace(
                meta=lambda: SimpleNamespace(id="chat-model", model="model-name"),
                provider_config={"api_key": "never-return-this"})]
            data, status = await self.call_api(plugin, "_webapi_get_config")
            self.assertEqual(status, 200)
            self.assertEqual(data["data"]["providers"], [{"id": "chat-model", "label": "chat-model · model-name"}])
            self.assertNotIn("never-return-this", json.dumps(data))
            self.assertTrue(data["data"]["timezone"])
            self.assertTrue(data["data"]["diagnostics"])
            self.assertEqual(data["data"]["config"]["debounce_slow_idle_seconds"], "Infinity")
            json.loads(json.dumps(data, allow_nan=False))
            self.assertTrue(math.isfinite(data["data"]["effective_config"]["debounce_slow_idle_seconds"]))
            self.assertEqual(plugin.config["debounce_slow_idle_seconds"], float("inf"))

    async def test_tool_catalog_uses_registered_owner_and_excludes_inactive_tools(self):
        from astrbot.core.agent.tool import FunctionTool, ToolSet
        from astrbot.core.provider.func_tool_manager import _PermissionGuardedTool

        def wrapped_tool(name, owner, active=True):
            tool = FunctionTool(name=name, description="catalog fixture", parameters={})
            tool.handler_module_path = owner
            tool.active = active
            return _PermissionGuardedTool(tool, SimpleNamespace())

        selected_module = "data.plugins.selected.main"
        disabled_module = "data.plugins.disabled.main"
        tools = ToolSet()
        for tool in [wrapped_tool("selected_enabled", selected_module),
                     wrapped_tool("selected_disabled", selected_module, False),
                     wrapped_tool("disabled_only", disabled_module, False),
                     wrapped_tool("builtin_enabled", None)]:
            tools.add_tool(tool)
        with tempfile.TemporaryDirectory() as tmp:
            plugin = backend_plugin(Path(tmp))
            plugin.context.get_llm_tool_manager = lambda: SimpleNamespace(get_full_tool_set=lambda: tools)
            owners = {selected_module: SimpleNamespace(name="registered_selected"),
                      disabled_module: SimpleNamespace(name="registered_disabled")}
            with patch("astrbot.core.star.star.star_map", owners):
                catalog = plugin._tool_plugin_catalog()
            self.assertEqual(catalog, [
                {"name": "registered_selected", "builtin": False,
                 "tools": ["selected_enabled"], "count": 1},
                {"name": "（内置工具）", "builtin": True,
                 "tools": ["builtin_enabled"], "count": 1},
            ])

    async def test_self_wake_whitelist_respects_disabled_selected_and_core_tools(self):
        from astrbot.core.agent.tool import FunctionTool, ToolSet

        selected_module = "data.plugins.selected.main"
        own_module = "data.plugins.astrbot_plugin_nuoxiaomiao.main"
        tools = ToolSet()
        for name, owner, active in [
            ("selected_enabled", selected_module, True),
            ("selected_disabled", selected_module, False),
            ("list_conversations", own_module, True),
            ("read_conversation_messages", own_module, True),
            ("send_message", own_module, False),
        ]:
            tool = FunctionTool(name=name, description="wake fixture", parameters={})
            tool.handler_module_path = owner
            tool.active = active
            tools.add_tool(tool)
        with tempfile.TemporaryDirectory() as tmp:
            plugin = backend_plugin(Path(tmp))
            plugin.resident_extra_tools = {"selected_enabled", "selected_disabled"}
            plugin.resident_state_enabled = plugin.activity_buffer_enabled = plugin.activity_tool_enabled = plugin.proactive_enabled = True
            plugin.context.get_llm_tool_manager = lambda: SimpleNamespace(get_full_tool_set=lambda: tools)
            with patch("astrbot.core.star.star.star_map", {
                selected_module: SimpleNamespace(name="registered_selected"),
                own_module: SimpleNamespace(name="astrbot_plugin_nuoxiaomiao"),
            }):
                kept = plugin.resident.integration.restrict(SimpleNamespace(unified_msg_origin="test:GroupMessage:1"), SimpleNamespace(func_tool=tools))
            self.assertEqual({tool.name for tool in kept.tools},
                             {"selected_enabled", "list_conversations", "read_conversation_messages"})

    async def test_default_write_failure_does_not_change_active_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin = backend_plugin(Path(tmp))
            before = dict(plugin.config)
            with patch("main.atomic_write_json", side_effect=OSError("snapshot failed")):
                data, status = await self.call_api(plugin, "_webapi_save_default", {"config": {"enabled": False}})
            self.assertEqual(status, 500)
            self.assertEqual(dict(plugin.config), before)
            self.assertEqual(plugin.config.saves, 0)
            self.assertEqual(plugin.applies, 0)

    async def test_active_write_failure_rolls_back_snapshot_and_memory(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin = backend_plugin(Path(tmp))
            snapshot = plugin._user_default_config_path()
            original = b'{"enabled": true, "label": "original"}'
            snapshot.write_bytes(original)
            before = dict(plugin.config)
            plugin.config.fail_save = True
            data, status = await self.call_api(plugin, "_webapi_save_default", {"config": {"enabled": False}})
            self.assertEqual(status, 500)
            self.assertEqual(snapshot.read_bytes(), original)
            self.assertEqual(dict(plugin.config), before)
            self.assertEqual(plugin.applies, 0)

    async def test_apply_failure_rolls_back_active_file_and_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin = backend_plugin(Path(tmp))
            before = dict(plugin.config)
            snapshot = plugin._user_default_config_path()
            original = b'{"enabled": true}'
            snapshot.write_bytes(original)
            with patch.object(plugin, "_apply_config", side_effect=[RuntimeError("apply failed"), None]):
                data, status = await self.call_api(plugin, "_webapi_save_default", {"config": {"enabled": False}})
            self.assertEqual(status, 500)
            self.assertEqual(dict(plugin.config), before)
            self.assertEqual(json.loads(plugin.config.path.read_text()), before)
            self.assertEqual(snapshot.read_bytes(), original)

    async def test_default_clear_failure_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin = backend_plugin(Path(tmp))
            with patch.object(plugin, "_clear_user_default_config", side_effect=OSError("clear failed")):
                data, status = await self.call_api(plugin, "_webapi_save_default", {"clear": True})
            self.assertEqual(status, 500)
            self.assertEqual(data["status"], "error")


if __name__ == "__main__":
    unittest.main()
