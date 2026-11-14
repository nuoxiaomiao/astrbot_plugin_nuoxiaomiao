"""Regression coverage for delivery observation and shared reply scope."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from astrbot.core.message.components import Image, Plain, Reply
from astrbot.core.message.message_event_result import MessageChain, MessageEventResult, ResultContentType
from astrbot.core.provider.entities import ProviderRequest

from infra.constants import SEND_OBSERVER_EXTRA, SYNTHETIC_EVENT_EXTRA
from tests.fakes import FakeEvent, fetch_activity_rows, make_plugin


def llm_result(text):
    return MessageEventResult(chain=[Plain(text)], result_content_type=ResultContentType.LLM_RESULT)


class ReplyChangesTest(unittest.IsolatedAsyncioTestCase):
    def plugin(self, **config):
        plugin = make_plugin(debounce_enabled=False, **config)
        self.addAsyncCleanup(plugin.terminate)
        return plugin

    async def test_generation_alone_does_not_record_delivery(self):
        plugin = self.plugin()
        event = FakeEvent()
        await plugin.on_llm_response(event, SimpleNamespace(completion_text="尚未发出去的话"))
        self.assertEqual(fetch_activity_rows(plugin, sender_id="bot"), [])

    async def test_cleaned_text_is_the_delivered_and_recorded_text(self):
        plugin = self.plugin()
        event = FakeEvent()
        event._result = llm_result("[动作]实际回复")
        await plugin.observe_outgoing_result(event)
        event._result.chain[0].text = "实际回复"
        await event.send(event.get_result())
        await plugin.after_reply_sent(event)
        # Check event.sent existence
        self.assertTrue(len(event.sent) > 0)
        self.assertEqual(event.sent[0].chain[0].text, "实际回复")
        self.assertEqual(fetch_activity_rows(plugin, sender_id="bot")[0][5], "实际回复")

    async def test_failed_delivery_is_not_recorded(self):
        plugin = self.plugin()
        event = FakeEvent()
        event.send = AsyncMock(side_effect=RuntimeError("send failed"))
        plugin.send_observer.ensure_send_observer(event)
        with self.assertRaises(RuntimeError):
            await event.send(MessageChain([Plain("没发成功")]))
        self.assertEqual(fetch_activity_rows(plugin, sender_id="bot"), [])

    async def test_explicit_false_delivery_is_not_recorded(self):
        plugin = self.plugin()
        event = FakeEvent(message_str="输入")
        send = AsyncMock(return_value=False)
        event.send = send
        event._result = llm_result("第一句。第二句。")
        await plugin.observe_outgoing_result(event)
        await event.send(event.get_result())
        await plugin.after_reply_sent(event)
        send.assert_awaited_once()
        self.assertEqual(fetch_activity_rows(plugin, sender_id="bot"), [])

    async def test_preceding_split_keeps_tail_in_normal_pipeline(self):
        plugin = self.plugin()
        event = FakeEvent()
        event._result = llm_result("前段，尾段")
        await plugin.observe_outgoing_result(event)
        await event.send(MessageChain([Plain("前段")]))
        event._result.chain = [Plain("尾段")]
        await plugin.observe_outgoing_result(event)
        self.assertIsNotNone(event.get_result())
        await event.send(MessageChain(event.get_result().chain))
        await plugin.after_reply_sent(event)
        self.assertEqual([row[5] for row in fetch_activity_rows(plugin, sender_id="bot")], ["前段", "尾段"])

    async def test_failed_preceding_split_does_not_retake_tail(self):
        plugin = self.plugin()
        event = FakeEvent()
        event._result = llm_result("尾段")
        event.send = AsyncMock(side_effect=RuntimeError("first segment failed"))
        await plugin.observe_outgoing_result(event)
        with self.assertRaises(RuntimeError):
            await event.send(MessageChain([Plain("前段")]))
        await plugin.observe_outgoing_result(event)
        self.assertIsNotNone(event.get_result())

    async def test_synthetic_auto_reply_is_cancelled_and_real_reply_is_preserved(self):
        plugin = self.plugin()
        event = FakeEvent(message_id="synthetic-id")
        event.set_extra(SYNTHETIC_EVENT_EXTRA, True)
        event.set_extra("_outputpro_pending_reply", object())
        base_send = event.send

        async def tracker(message):
            if event.get_extra("_outputpro_pending_reply"):
                message.chain.insert(0, Reply(id="synthetic-id"))
            await base_send(message)

        event.send = tracker
        plugin.send_observer.ensure_send_observer(event)
        await event.send(MessageChain([Reply(id="real-message"), Plain("回复")]))
        self.assertIsNone(event.get_extra("_outputpro_pending_reply"))
        replies = [comp.id for comp in event.sent[0].chain if isinstance(comp, Reply)]
        self.assertEqual(replies, ["real-message"])

    async def test_record_scope_applies_to_regular_and_proactive_messages(self):
        plugin = self.plugin(activity_group_blacklist=["20001"])
        event = FakeEvent()
        plugin.send_observer.ensure_send_observer(event)
        await event.send(MessageChain([Plain("群里回复")]))
        plugin.proactive.record_proactive_activity({"target_kind": "group", "target_id": "20001", "target_name": "群"}, "主动回复")
        self.assertEqual(fetch_activity_rows(plugin, sender_id="bot"), [])

    async def test_partial_failure_only_records_successful_bubbles(self):
        plugin = self.plugin()
        event = FakeEvent()
        # Mock send to fail on second call
        async def mock_send(message):
            if "第二句话" in message.get_plain_text():
                raise RuntimeError("second segment failed")
            event.sent.append(message)
        event.send = mock_send
        event._result = llm_result("第一句话来了，第二句话也来。")
        await plugin.observe_outgoing_result(event)
        await event.send(MessageChain([Plain("第一句话来了")]))
        with self.assertRaises(RuntimeError):
            await event.send(MessageChain([Plain("第二句话也来")]))
        await plugin.after_reply_sent(event)
        rows = fetch_activity_rows(plugin, sender_id="bot")
        self.assertEqual(len(event.sent), 1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][5], "第一句话来了")

    async def test_observer_restores_send_and_does_not_duplicate_hooks(self):
        plugin = self.plugin()
        event = FakeEvent()
        original = event.send
        first = plugin.send_observer.ensure_send_observer(event)
        self.assertIs(first, plugin.send_observer.ensure_send_observer(event))
        await event.send(MessageChain([Plain("一次")]))
        await plugin.after_reply_sent(event)
        self.assertEqual(event.send, original)
        self.assertFalse(event.get_extra(SEND_OBSERVER_EXTRA)["active"])
        self.assertEqual(len(fetch_activity_rows(plugin, sender_id="bot")), 1)

    async def test_tool_status_is_not_recorded_and_does_not_disable_delivery(self):
        plugin = self.plugin()
        event = FakeEvent()
        await plugin.on_llm_request(event, ProviderRequest())
        await event.send(MessageChain([Plain("正在调用工具")], type="tool_call"))
        self.assertEqual(fetch_activity_rows(plugin, sender_id="bot"), [])
        event._result = llm_result("最终回复")
        await plugin.observe_outgoing_result(event)
        await event.send(event.get_result())
        await plugin.after_reply_sent(event)
        self.assertEqual(fetch_activity_rows(plugin, sender_id="bot")[0][5], "最终回复")

    async def test_pure_media_reply_records_cached_llm_text(self):
        # outputpro 的 T2I 会把长正文换成图片：发送链上没有 Plain，
        # 速览库要退回本轮 LLM 文本补记，不能当没说过。
        plugin = self.plugin()
        event = FakeEvent()
        event._result = llm_result("")
        await plugin.on_llm_response(
            event, SimpleNamespace(completion_text="这是一段被转成图片的长回复")
        )
        observed = event.get_extra(SEND_OBSERVER_EXTRA)
        self.assertIsInstance(observed, dict)
        await event.send(
            MessageChain([Image.fromFileSystem("/tmp/nxm-test-image.png")])
        )
        await plugin.after_reply_sent(event)
        rows = fetch_activity_rows(plugin, sender_id="bot")
        self.assertEqual(
            [row[5] for row in rows], ["这是一段被转成图片的长回复"]
        )

    async def test_native_nonfinite_values_have_safe_runtime_view(self):
        plugin = self.plugin(debounce_slow_idle_seconds=float("inf"), proactive_silent_wake_target_mean_minutes=float("nan"))
        self.assertEqual(plugin.debounce_slow_idle_seconds, 1.2)
        self.assertEqual(plugin.proactive_silent_wake_target_mean_minutes, 30)
        self.assertIn("debounce_slow_idle_seconds", plugin.cfg.errors)


if __name__ == "__main__":
    unittest.main()
