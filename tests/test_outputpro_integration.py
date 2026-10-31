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
            context=self.context, human_reply_enabled=True, debounce_enabled=False,
            human_reply_chars_per_minute=10_000_000,
            activity_db_path=str(Path(self.directory.name) / "activity.db"),
            merge_localize_images=False,
        )
        self.plugin._record_bot_reply = Mock(wraps=self.plugin._record_bot_reply)
        self.real_spawn_human_reply = self.plugin._spawn_human_reply
        self.plugin._spawn_human_reply = Mock()
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
        await self.plugin.observe_outgoing_result(event)
        await output.on_decorating_result(event)
        await self.plugin.on_decorating_result(event)

    def recorded_texts(self):
        return [row[5] for row in fetch_activity_rows(self.plugin, sender_id="bot")]

    async def test_cleaned_text_is_used_by_human_reply(self):
        event = self.event("[动作]你好🙂。这是第二句。")
        output = self.output(["clean"])
        await self.run_hooks(output, event)
        self.assertIsNone(event.get_result())
        arguments = self.plugin._spawn_human_reply.call_args.args
        self.assertNotIn("动作", "".join(arguments[2]))
        self.assertNotIn("🙂", "".join(arguments[2]))
        self.assertEqual(event.sent, [])
        self.plugin._finish_send_observer(event)

    async def test_outputpro_split_keeps_its_final_segment(self):
        event = self.event()
        output = self.output(["clean", "split"])
        await self.run_hooks(output, event)
        self.assertEqual(len(event.sent), 2)
        self.assertEqual(event.get_result().get_plain_text().rstrip("。"), "第三段")
        self.plugin._spawn_human_reply.assert_not_called()
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
        await self.plugin.on_decorating_result(event)
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
        await self.plugin.on_decorating_result(event)
        await event.send(event.get_result())
        self.assertTrue(event.sent)
        self.assertFalse(any(isinstance(item, Reply) and item.id == event.message_obj.message_id
                             for message in event.sent for item in message.chain))
        await self.plugin.after_reply_sent(event)

    async def test_tool_status_does_not_prevent_final_human_reply_or_enter_records(self):
        event = self.event("最终回复。")
        self.plugin._ensure_send_observer(event)
        await event.send(MessageChain(type="tool_call").message("工具执行状态"))
        event._result = MessageEventResult(chain=[Plain("最终回复。")], result_content_type=ResultContentType.LLM_RESULT)
        await self.plugin.on_decorating_result(event)
        self.plugin._spawn_human_reply.assert_called_once()
        self.plugin._record_bot_reply.assert_not_called()
        self.plugin._finish_send_observer(event)

    async def test_cleaning_away_the_entire_body_does_not_record_or_start_typing(self):
        event = self.event("[只有动作]🙂")
        await self.run_hooks(self.output(["clean"]), event)
        self.assertEqual(event.get_result().get_plain_text(), "")
        self.assertEqual(event.sent, [])
        self.assertEqual(self.recorded_texts(), [])
        self.plugin._spawn_human_reply.assert_not_called()
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
        self.plugin._spawn_human_reply.assert_not_called()
        self.plugin._finish_send_observer(event)

    async def test_real_at_conversion_survives_cleaning_and_human_delivery(self):
        event = self.event("[at:10002][动作]这是需要艾特发出的实际正文。")
        output = self.output(["at", "clean"], overrides={
            "at": {"at_str": False, "at_prob": 1},
        })
        self.plugin._spawn_human_reply = Mock(wraps=self.real_spawn_human_reply)
        self.plugin._human_typing_wait = AsyncMock(return_value=True)
        await self.run_hooks(output, event)
        tasks = [state.task for states in self.plugin._human_reply_tasks.values() for state in states]
        await asyncio.gather(*tasks)
        self.assertIsNone(event.get_result())
        self.assertTrue(event.sent)
        ats = [component for message in event.sent for component in message.chain if isinstance(component, At)]
        self.assertEqual([str(component.qq) for component in ats], ["10002"])
        first_body = "".join(component.text for component in event.sent[0].chain if isinstance(component, Plain))
        self.assertIn("这是需要艾特发出的实际正文", first_body)
        body = "\n".join(component.text for message in event.sent for component in message.chain if isinstance(component, Plain))
        self.assertIn("这是需要艾特发出的实际正文", body)
        self.assertNotIn("[at:", body)
        self.assertNotIn("动作", body)
        self.assertEqual("\n".join(self.recorded_texts()), body.strip())

    async def test_successful_forward_records_cleaned_nodes_only_after_delivery(self):
        event = self.event("[动作]第一条转发消息。第二条转发消息。🙂")
        output = self.output(["clean", "forward"], overrides={
            "forward": {"threshold": 1, "node_name": "转发展示名", "node_max_length": 8, "node_hard_limit": 12},
        })
        await self.run_hooks(output, event)
        self.plugin._spawn_human_reply.assert_not_called()
        self.assertEqual(event.sent, [])
        self.assertEqual(self.recorded_texts(), [])
        nodes = event.get_result().chain[0]
        self.assertIsInstance(nodes, Nodes)
        self.assertEqual(len(nodes.nodes), 2)
        self.assertEqual([node.name for node in nodes.nodes], ["转发展示名", "转发展示名"])
        await event.send(event.get_result())
        self.assertEqual(len(event.sent), 1)
        self.assertEqual(self.recorded_texts(), ["第一条转发消息。\n第二条转发消息"])
        self.assertNotIn("转发展示名", self.recorded_texts()[0])
        self.assertNotIn("动作", self.recorded_texts()[0])
        self.assertNotIn("🙂", self.recorded_texts()[0])
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
        self.assertEqual(event.get_extra(SEND_OBSERVER_EXTRA)["succeeded"], 0)
        self.assertEqual(self.recorded_texts(), [])
        self.plugin._spawn_human_reply.assert_not_called()
        self.plugin._finish_send_observer(event)

    async def test_all_preceding_split_failures_leave_tail_without_human_retake(self):
        event = self.event()
        event.send = AsyncMock(side_effect=RuntimeError("split platform failure"))
        await self.run_hooks(self.output(["split"]), event)
        self.assertEqual(event.get_extra(SEND_OBSERVER_EXTRA)["attempted"], 2)
        self.assertEqual(event.get_extra(SEND_OBSERVER_EXTRA)["succeeded"], 0)
        self.assertEqual(event.get_result().get_plain_text(), "第三段。")
        self.plugin._spawn_human_reply.assert_not_called()
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
        self.assertEqual(event.get_extra(SEND_OBSERVER_EXTRA)["attempted"], 2)
        self.plugin._spawn_human_reply.assert_not_called()
        await event.send(event.get_result())
        self.assertEqual(attempts, ["第一段。", "第二段。", "第三段。"])
        self.assertEqual(self.recorded_texts(), ["第一段。", "第三段。"])
        await self.plugin.after_reply_sent(event)


if __name__ == "__main__":
    unittest.main()
