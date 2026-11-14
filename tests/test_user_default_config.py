"""Config directory migration and user-default snapshot tests."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from config_support import CURRENT_CONFIG_NAME, LEGACY_CONFIG_NAME
from scripts.migrate_config import migrate_config
from tests.fakes import make_plugin, make_user_default_plugin


class ConfigMigrationTest(unittest.TestCase):
    def test_migrates_legacy_config_when_current_is_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            legacy = config_dir / LEGACY_CONFIG_NAME
            legacy.write_text('{"window_seconds": 240}', encoding="utf-8")

            result = migrate_config(config_dir)

            current = config_dir / CURRENT_CONFIG_NAME
            self.assertTrue(current.is_file())
            self.assertEqual(current.read_text(encoding="utf-8"), legacy.read_text())
            self.assertIn("已迁移", result)

    def test_existing_current_config_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            current = config_dir / CURRENT_CONFIG_NAME
            legacy = config_dir / LEGACY_CONFIG_NAME
            current.write_text('{"window_seconds": 300}', encoding="utf-8")
            legacy.write_text('{"window_seconds": 240}', encoding="utf-8")

            result = migrate_config(config_dir)

            self.assertEqual(
                current.read_text(encoding="utf-8"),
                '{"window_seconds": 300}',
            )
            self.assertIn("未覆盖", result)


class UserDefaultConfigTest(unittest.IsolatedAsyncioTestCase):
    """「我的默认」快照：写入、读取、清除、缺文件回退。"""

    async def test_write_and_load_roundtrip(self):
        plugin = make_user_default_plugin()

        snapshot = plugin.config_svc.write_user_default({"enabled": False, "trigger_rounds": 5})
        loaded = plugin.config_svc.load_user_default()

        self.assertEqual(snapshot, {"enabled": False, "trigger_rounds": 5})
        self.assertEqual(loaded, {"enabled": False, "trigger_rounds": 5})
        await plugin.terminate()

    async def test_load_missing_file_returns_none(self):
        plugin = make_user_default_plugin()

        self.assertIsNone(plugin.config_svc.load_user_default())
        await plugin.terminate()

    async def test_load_broken_file_returns_none(self):
        plugin = make_user_default_plugin()
        path = plugin.config_svc.user_default_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{这不是合法json", encoding="utf-8")

        self.assertIsNone(plugin.config_svc.load_user_default())
        await plugin.terminate()

    async def test_clear_removes_snapshot(self):
        plugin = make_user_default_plugin()
        plugin.config_svc.write_user_default({"enabled": True})

        self.assertTrue(plugin.config_svc.clear_user_default())
        self.assertIsNone(plugin.config_svc.load_user_default())
        self.assertFalse(plugin.config_svc.clear_user_default())
        await plugin.terminate()

    async def test_path_defaults_to_plugin_data_dir(self):
        plugin = make_plugin()

        path = plugin.config_svc.user_default_path()
        self.assertEqual(path.name, "user_default_config.json")
        self.assertIn("data", str(path))
        await plugin.terminate()
