"""Exercise the installed OutputPro pipeline with Nuoxiaomiao's send observer."""

from __future__ import annotations

import asyncio
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock

from astrbot.core.message.components import At, Nodes, Plain, Reply
from astrbot.core.message.message_event_result import MessageChain, MessageEventResult, ResultContentType

from main import SEND_OBSERVER_EXTRA, SYNTHETIC_EVENT_EXTRA
from test_plugin_logic import FakeContext, FakeEvent, fetch_activity_rows, make_plugin


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
# 外部插件集成测试：OutputPro 未安装时整个模块跳过，不阻断默认测试发现。
try:
    sys.path.insert(0, str(PLUGIN_ROOT.parent))
    from astrbot_plugin_outputpro.main import OutputPlugin
    from astrbot_plugin_outputpro.core.model import StateManager

    _EXTERNAL_IMPORTS_OK = True
except ImportError:
    _EXTERNAL_IMPORTS_OK = False


class OutputEvent(FakeEvent):
    def set_result(self, result):
        self._result = result

    def plain_result(self, text):
        return MessageEventResult().message(text)


def output_defaults():
    schema = json.loads((PLUGIN_ROOT.parent / "astrbot_plugin_outputpro" / "_conf_schema.json").read_text(encoding="utf-8-sig"))

    def parse(items):
        return {key: parse(spec["items"]) if spec["type"] == "object" else copy.deepcopy(spec.get("default"))
                for key, spec in items.items()}

    return parse(schema)


@unittest.skipUnless(
    _EXTERNAL_IMPORTS_OK, "需要已安装的 astrbot_plugin_outputpro 才能运行集成测试"
)
class OutputPipelineIntegrationTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="nxm-output-integration-")
        self.addCleanup(self.directory.cleanup)
        StateManager._groups.clear()
        self.context = FakeContext()
        self.plugin = make_plugin(
            context=self.context,
            activity_db_path=str(Path(self.directory.name) / "activity.db"),
            merge_localize_images=False,
        )
        self.plugin._record_bot_reply = Mock(wraps=self.plugin._record_bot_reply)
        self.addAsyncCleanup(self.plugin.terminate)

    def output(self, steps, overrides=None):
        config = output_defaults()
        config["pipeline"]["steps"] = steps
        config["pipeline"]["llm_steps"] = steps
        config["clean"].update(bracket=True, emoji=True, text_threshold=2000)
        config["split"].update(char_list=["。"], tail_punc=[], show_typing=False,
                               per_char_delay=0, delay_scope_str="0~0", max_length=1000, max_count=5)
        config["reply"].update(threshold=1, include_at=False)
        config["summary"]["quotes_files"] = []
        for section, changes in (overrides or {}).items():
            config[section].update(changes)
        return OutputPlugin(self.context, config)

    @staticmethod
    def event(text="第一段。第二段。第三段。"):
        event = OutputEvent(message_str="输入", message_id="original-source")
        event._result = MessageEventResult(chain=[Plain(text)], result_content_type=ResultContentType.LLM_RESULT)
        return event

    async def run_hooks(self, output, event):
        await output.on_message(event)
        # Using observe_outgoing_result to trigger observer and qq_official_force_content_mode to clean/prepare content
        await self.plugin.observe_outgoing_result(event)
        await self.plugin.qq_official_force_content_mode(event)
        await output.on_decorating_result(event)
        await self.plugin.qq_official_force_content_mode_late(event)

    def recorded_texts(self):
        return [row[5] for row in fetch_activity_rows(self.plugin, sender_id="bot")]

    async def test_cleaned_text_is_delivered_and_recorded(self):
        event = self.event("[动作]你好🙂。这是第二句。")
        output = self.output(["clean"])
        await self.run_hooks(output, event)
        await event.send(event.get_result())
        await self.plugin.after_reply_sent(event)
        self.assertEqual(len(event.sent), 1)
        body = event.sent[0].get_plain_text()
        self.assertNotIn("动作", body)
        self.assertNotIn("🙂", body)
        recorded = self.recorded_texts()
        for text in recorded:
            self.assertNotIn("动作", text)
            self.assertNotIn("🙂", text)

    async def test_outputpro_split_keeps_its_final_segment(self):
        event = self.event()
        output = self.output(["clean", "split"])
        await self.run_hooks(output, event)
        self.assertEqual(len(event.sent), 2)
        self.assertEqual(event.get_result().get_plain_text().rstrip("。"), "第三段")
        self.assertEqual(event.get_extra(SEND_OBSERVER_EXTRA)["attempted"], 2)
        await event.send(event.get_result())
        self.assertEqual(self.plugin._record_bot_reply.call_count, 3)
        await self.plugin.after_reply_sent(event)
        self.assertFalse(event.get_extra(SEND_OBSERVER_EXTRA)["active"])

    async def test_real_reply_pending_is_honored_by_nested_tracker(self):
        event = self.event("你好。")
        output = self.output(["reply"])
        await output.on_message(event)
        await StateManager.get_group(event.get_group_id()).record_outgoing_message()
        await self.plugin.observe_outgoing_result(event)
        await output.on_decorating_result(event)
        await self.plugin.observe_outgoing_result(event)
        await event.send(MessageChain().message("你好。"))
        self.assertTrue(any(isinstance(item, Reply) and item.id == "original-source" for item in event.sent[0].chain))
        self.assertEqual(self.plugin._record_bot_reply.call_args.args[1], "你好。")
        self.plugin._finish_send_observer(event)

    async def test_synthetic_uuid_is_never_used_for_automatic_reply(self):
        event = self.event()
        event.set_extra(SYNTHETIC_EVENT_EXTRA, True)
        output = self.output(["reply", "split"])
        await output.on_message(event)
        await StateManager.get_group(event.get_group_id()).record_outgoing_message()
        await self.plugin.observe_outgoing_result(event)
        await output.on_decorating_result(event)
        await self.plugin.observe_outgoing_result(event)
        await event.send(event.get_result())
        self.assertTrue(event.sent)
        self.assertFalse(any(isinstance(item, Reply) and item.id == event.message_obj.message_id
                             for message in event.sent for item in message.chain))
        await self.plugin.after_reply_sent(event)

    async def test_tool_status_does_not_prevent_final_split_or_enter_records(self):
        event = self.event("最终回复。")
        self.plugin._ensure_send_observer(event)
        await event.send(MessageChain(type="tool_call").message("工具执行状态"))
        event._result = MessageEventResult(
            chain=[Plain("第一段。第二段。")],
            result_content_type=ResultContentType.LLM_RESULT,
        )
        await self.run_hooks(self.output(["split"]), event)
        await event.send(event.get_result())
        await self.plugin.after_reply_sent(event)
        self.assertEqual(self.recorded_texts(), ["第一段。", "第二段。"])

    async def test_cleaning_away_the_entire_body_does_not_record_or_start_typing(self):
        event = self.event("[只有动作]🙂")
        await self.run_hooks(self.output(["clean"]), event)
        self.assertEqual(event.get_result().get_plain_text(), "")
        self.assertEqual(event.sent, [])
        self.assertEqual(self.recorded_texts(), [])
        self.plugin._finish_send_observer(event)

    async def test_outputpro_blocked_body_is_neither_sent_nor_recorded(self):
        event = self.event("这里出现禁止内容，后面的正文也不应发出。")
        output = self.output(["block", "clean", "split"], overrides={
            "block": {"timeout": 0, "block_reread": False, "block_words": ["禁止内容"]},
        })
        await self.run_hooks(output, event)
        self.assertEqual(event.get_result().get_plain_text(), "")
        self.assertEqual(event.sent, [])
        self.assertEqual(event.get_extra(SEND_OBSERVER_EXTRA)["attempted"], 0)
        self.assertEqual(self.recorded_texts(), [])
        self.plugin._finish_send_observer(event)

    async def test_real_at_conversion_survives_cleaning_and_split_delivery(self):
        event = self.event("[at:10002][动作]这是需要艾特发出的实际正文。")
        output = self.output(["at", "clean", "split"], overrides={
            "at": {"at_str": False, "at_prob": 1},
        })
        await self.run_hooks(output, event)
        if event.get_result() is not None:
            await event.send(event.get_result())
        await self.plugin.after_reply_sent(event)

        ats = [component for message in event.sent for component in message.chain if isinstance(component, At)]
        self.assertEqual([str(component.qq) for component in ats], ["10002"])
        body = "\n".join(component.text for message in event.sent for component in message.chain if isinstance(component, Plain))
        self.assertIn("这是需要艾特发出的实际正文", body)
        self.assertEqual("\n".join(self.recorded_texts()), body.strip())

    async def test_out_of_band_one_shot_waits_across_outputpro_split_delay(self):
        output = self.output(["split"], overrides={"split": {"per_char_delay": 0.03, "min_delay": 0.05, "max_delay": 0.2}})
        event = self.event("第一段。第二段。")
        order = []

        orig_send = event.send
        async def tracking_send(message):
            order.append("split")
            await orig_send(message)
        event.send = tracking_send

        async def out_of_band():
            # Wait for first split segment
            await asyncio.sleep(0.02)
            async with self.plugin._send_order.one_shot(event.unified_msg_origin):
                order.append("one-shot")

        task = asyncio.create_task(out_of_band())
        await self.run_hooks(output, event)
        await event.send(event.get_result())
        await self.plugin.after_reply_sent(event)
        await task

        self.assertEqual(order, ["split", "split", "one-shot"])

    async def test_successful_forward_records_cleaned_nodes_only_after_delivery(self):
        event = self.event("[动作]第一条转发消息。第二条转发消息。🙂")
        output = self.output(["clean", "forward"], overrides={
            "forward": {"threshold": 1, "node_name": "转发展示名", "node_max_length": 8, "node_hard_limit": 12},
        })
        await self.run_hooks(output, event)
        self.assertEqual(event.sent, [])
        self.assertEqual(self.recorded_texts(), [])
        nodes = event.get_result().chain[0]
        self.assertIsInstance(nodes, Nodes)
        self.assertEqual(len(nodes.nodes), 2)
        await event.send(event.get_result())
        self.assertEqual(len(event.sent), 1)
        self.assertEqual(self.recorded_texts(), ["第一条转发消息。\n第二条转发消息"])
        await self.plugin.after_reply_sent(event)

    async def test_failed_forward_does_not_record_its_nested_body(self):
        event = self.event("转发发送失败，这句话不应记进已发记录。")
        event.send = AsyncMock(side_effect=RuntimeError("forward platform failure"))
        output = self.output(["forward"], overrides={
            "forward": {"threshold": 1, "node_name": "机器人", "node_max_length": 8, "node_hard_limit": 12},
        })
        await self.run_hooks(output, event)
        self.assertIsInstance(event.get_result().chain[0], Nodes)
        with self.assertRaisesRegex(RuntimeError, "forward platform failure"):
            await event.send(event.get_result())
        self.assertEqual(event.get_extra(SEND_OBSERVER_EXTRA)["attempted"], 1)
        self.assertEqual(self.recorded_texts(), [])
        self.plugin._finish_send_observer(event)

    async def test_all_preceding_split_failures_leave_tail_without_human_retake(self):
        event = self.event()
        event.send = AsyncMock(side_effect=RuntimeError("split platform failure"))
        await self.run_hooks(self.output(["split"]), event)
        self.assertEqual(event.get_result().get_plain_text(), "第三段。")
        self.assertEqual(self.recorded_texts(), [])
        with self.assertRaisesRegex(RuntimeError, "split platform failure"):
            await event.send(event.get_result())
        self.assertEqual(self.recorded_texts(), [])
        self.plugin._finish_send_observer(event)

    async def test_partial_split_failure_records_only_delivered_first_and_tail(self):
        event = self.event()
        original_send = event.send
        attempts = []

        async def fail_second(message):
            attempts.append(message.get_plain_text())
            if len(attempts) == 2:
                raise RuntimeError("second segment platform failure")
            return await original_send(message)

        event.send = fail_second
        await self.run_hooks(self.output(["split"]), event)
        self.assertEqual(attempts, ["第一段。", "第二段。"])
        self.assertEqual(self.recorded_texts(), ["第一段。"])
        await event.send(event.get_result())
        self.assertEqual(attempts, ["第一段。", "第二段。", "第三段。"])
        self.assertEqual(self.recorded_texts(), ["第一段。", "第三段。"])
        await self.plugin.after_reply_sent(event)


if __name__ == "__main__":
    unittest.main()
