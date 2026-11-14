"""Tests for runtime QQ official SplitStep enablement in outputpro_split."""

from __future__ import annotations

import copy
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from astrbot.core.message.components import Plain
from astrbot.core.message.message_event_result import (
    MessageEventResult,
    ResultContentType,
)

import outputpro_split
from tests.fakes import FakeContext, fetch_activity_rows, make_plugin
from tests.test_qq_official import QQOfficialFakeEvent

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
try:
    if str(PLUGIN_ROOT.parent) not in sys.path:
        sys.path.insert(0, str(PLUGIN_ROOT.parent))
    from astrbot_plugin_outputpro.core.model import StateManager
    from astrbot_plugin_outputpro.core.step.split import SplitStep
    from astrbot_plugin_outputpro.main import OutputPlugin
    from tests.test_outputpro_integration import output_defaults

    _EXTERNAL_IMPORTS_OK = True
except ImportError:
    SplitStep = None  # type: ignore[assignment]
    OutputPlugin = None  # type: ignore[assignment]
    StateManager = None  # type: ignore[assignment]
    output_defaults = None  # type: ignore[assignment]
    _EXTERNAL_IMPORTS_OK = False


class QQOfficialOutputEvent(QQOfficialFakeEvent):
    def set_result(self, result):
        self._result = result

    def plain_result(self, text):
        return MessageEventResult().message(text)


@unittest.skipUnless(
    _EXTERNAL_IMPORTS_OK, "需要已安装的 astrbot_plugin_outputpro 才能运行测试"
)
class OutputproSplitQQOfficialTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._orig_support_platforms = copy.copy(SplitStep.support_platforms)
        self.addCleanup(
            setattr, SplitStep, "support_platforms", self._orig_support_platforms
        )
        StateManager._groups.clear()
        self.directory = tempfile.TemporaryDirectory(prefix="nxm-outputpro-qq-")
        self.addCleanup(self.directory.cleanup)
        self.context = FakeContext()
        self.plugin = make_plugin(
            context=self.context,
            activity_db_path=str(Path(self.directory.name) / "activity.db"),
            merge_localize_images=False,
        )
        self.addAsyncCleanup(self.plugin.terminate)

    def make_output(self, steps=None):
        config = output_defaults()
        use_steps = list(steps or ["split"])
        config["pipeline"]["steps"] = use_steps
        config["pipeline"]["llm_steps"] = use_steps
        config["split"].update(
            char_list=["。"],
            tail_punc=[],
            show_typing=False,
            per_char_delay=0,
            delay_scope_str="0~0",
            max_length=1000,
            max_count=5,
        )
        config["summary"]["quotes_files"] = []
        return OutputPlugin(self.context, config)

    @staticmethod
    def make_qq_event(
        text="第一段。第二段。",
        platform_name="qq_official",
        platform_id="qq_official",
    ):
        event = QQOfficialOutputEvent(
            sender_id="OPENID_USER_1",
            session=f"{platform_id}:GroupMessage:OPENID_GROUP_1",
            group_id="OPENID_GROUP_1",
            message_str="你好",
            message_id="qq-msg-1",
            platform_name=platform_name,
            platform_id=platform_id,
            raw_message=type("GroupMessage", (), {})(),
        )
        event._result = MessageEventResult(
            chain=[Plain(text)],
            result_content_type=ResultContentType.LLM_RESULT,
        )
        return event

    async def test_baseline_unpatched_qq_official_does_not_split(self):
        self.assertFalse(outputpro_split.ADDED_PLATFORMS & SplitStep.support_platforms)
        output = self.make_output(["split"])
        event = self.make_qq_event("第一段。第二段。")

        await output.on_message(event)
        await output.on_decorating_result(event)

        self.assertEqual(event.sent, [])
        self.assertEqual(event.get_result().get_plain_text(), "第一段。第二段。")

    async def test_patched_qq_official_splits_and_records_all_segments(self):
        self.assertTrue(outputpro_split.enable_qq_official())
        output = self.make_output(["split"])
        event = self.make_qq_event("第一段。第二段。")

        await output.on_message(event)
        await self.plugin.observe_outgoing_result(event)
        await self.plugin.qq_official_force_content_mode(event)
        await output.on_decorating_result(event)
        await self.plugin.qq_official_force_content_mode_late(event)

        self.assertEqual([m.get_plain_text() for m in event.sent], ["第一段。"])
        self.assertEqual(event.get_result().get_plain_text(), "第二段。")

        await event.send(event.get_result())
        await self.plugin.after_reply_sent(event)

        self.assertEqual(
            [m.get_plain_text() for m in event.sent],
            ["第一段。", "第二段。"],
        )
        recorded = [row[5] for row in fetch_activity_rows(self.plugin, sender_id="bot")]
        self.assertEqual(recorded, ["第一段。", "第二段。"])

    def test_enable_qq_official_is_idempotent_and_logs_once(self):
        base = set(SplitStep.support_platforms)
        with patch.object(outputpro_split.logger, "info") as info_mock:
            first = outputpro_split.enable_qq_official()
            second = outputpro_split.enable_qq_official()
            third = outputpro_split.enable_qq_official()

        self.assertTrue(first)
        self.assertTrue(second)
        self.assertTrue(third)
        self.assertIsInstance(SplitStep.support_platforms, set)
        self.assertEqual(
            SplitStep.support_platforms,
            base | set(outputpro_split.ADDED_PLATFORMS),
        )
        info_mock.assert_called_once()

    async def test_qq_official_webhook_variant_splits(self):
        self.assertTrue(outputpro_split.enable_qq_official())
        output = self.make_output(["split"])
        event = self.make_qq_event(
            "第一段。第二段。",
            platform_name="qq_official_webhook",
            platform_id="qq_official_webhook",
        )

        await output.on_message(event)
        await output.on_decorating_result(event)

        self.assertEqual([m.get_plain_text() for m in event.sent], ["第一段。"])
        self.assertEqual(event.get_result().get_plain_text(), "第二段。")

    def test_degrades_gracefully_when_structure_changes_or_module_absent(self):
        # 1. support_platforms 结构改变（tuple）→ 返回 False 且不修改原值
        SplitStep.support_platforms = ("aiocqhttp",)
        self.assertFalse(outputpro_split.enable_qq_official())
        self.assertEqual(SplitStep.support_platforms, ("aiocqhttp",))

        # 2. 模块不存在 / 摘除 sys.modules → 返回 False
        blocked = {name: None for name in outputpro_split.MODULE_CANDIDATES}
        with patch.dict(sys.modules, blocked):
            self.assertFalse(outputpro_split.enable_qq_official())

        # 3. 模块存在但缺少 SplitStep → 返回 False
        with patch.dict(
            sys.modules,
            {outputpro_split.MODULE_CANDIDATES[0]: SimpleNamespace()},
        ):
            self.assertFalse(outputpro_split.enable_qq_official())

    async def test_plugin_entry_points_and_hot_reload_repatch(self):
        # 1. initialize() 自动启用
        await self.plugin.initialize()
        self.assertTrue(
            outputpro_split.ADDED_PLATFORMS <= set(SplitStep.support_platforms)
        )

        # 2. _prepare_qq_official_event() 兜底自动重打
        SplitStep.support_platforms = set(self._orig_support_platforms)
        event = self.make_qq_event()
        await self.plugin._prepare_qq_official_event(event)
        self.assertTrue(
            outputpro_split.ADDED_PLATFORMS <= set(SplitStep.support_platforms)
        )

        # 3. _ensure_send_observer() 兜底自动重打
        SplitStep.support_platforms = set(self._orig_support_platforms)
        event2 = self.make_qq_event()
        self.plugin.send_observer.ensure_send_observer(event2)
        self.assertTrue(
            outputpro_split.ADDED_PLATFORMS <= set(SplitStep.support_platforms)
        )

        # 4. 模拟 AstrBot 热重载重建 data.plugins.* 模块与类对象后可再次生效
        class ReloadedSplitStep:
            support_platforms = {"aiocqhttp", "telegram", "lark"}

        reloaded_mod = SimpleNamespace(SplitStep=ReloadedSplitStep)
        with patch.dict(
            sys.modules,
            {outputpro_split.MODULE_CANDIDATES[0]: reloaded_mod},
        ):
            self.assertTrue(outputpro_split.enable_qq_official())
            self.assertTrue(
                outputpro_split.ADDED_PLATFORMS <= ReloadedSplitStep.support_platforms
            )


if __name__ == "__main__":
    unittest.main()
