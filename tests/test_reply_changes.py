"""Regression coverage for delivery observation and shared reply scope."""
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from astrbot.core.message.components import Plain, Reply
from astrbot.core.message.message_event_result import MessageChain, MessageEventResult, ResultContentType
from astrbot.core.provider.entities import ProviderRequest

from main import SEND_OBSERVER_EXTRA, SYNTHETIC_EVENT_EXTRA
from test_plugin_logic import FakeEvent, make_plugin, fetch_activity_rows


def llm_result(text):
    return MessageEventResult(chain=[Plain(text)], result_content_type=ResultContentType.LLM_RESULT)


class ReplyChangesTest(unittest.IsolatedAsyncioTestCase):
    def plugin(self, **config):
        plugin = make_plugin(debounce_enabled=False, human_reply_enabled=True, **config)
        plugin._human_typing_wait = AsyncMock(return_value=True)
        self.addAsyncCleanup(plugin.terminate)
        return plugin

    async def finish_typing(self, plugin):
        tasks = [state.task for states in plugin._human_reply_tasks.values() for state in states]
        if tasks:
            await asyncio.gather(*tasks)

    async def test_excluded_group_cannot_register_or_interrupt_model(self):
        plugin = self.plugin(human_reply_group_blacklist=["20001"])
        old = FakeEvent(message_str="原来的问题")
        await plugin.on_llm_request(old, ProviderRequest())
        self.assertEqual(plugin._turn_inflight, {})
        key = plugin._merge_key(old.unified_msg_origin, old.get_sender_id())
        plugin._turn_inflight[key] = old  # A registration from before a scope change.
        await plugin.on_message(FakeEvent(message_str="新的问题"))
        self.assertFalse(old.stopped)

    async def test_webchat_and_streaming_do_not_interrupt_models(self):
        for webchat in (True, False):
            with self.subTest(webchat=webchat):
                plugin = self.plugin()
                event = FakeEvent()
                if webchat:
                    event.get_platform_name = lambda: "webchat"
                else:
                    plugin.context.get_config = lambda *a, **k: {"provider_settings": {"streaming_response": True}}
                await plugin.on_llm_request(event, ProviderRequest())
                self.assertEqual(plugin._turn_inflight, {})
                self.assertFalse(plugin._human_reply_scope_applies(event))

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
        event._result.chain[0].text = "实际回复"  # OutputPro's earlier cleaning stage.
        await plugin.on_decorating_result(event)
        await self.finish_typing(plugin)
        self.assertEqual(event.sent[0].chain[0].text, "实际回复")
        self.assertEqual(fetch_activity_rows(plugin, sender_id="bot")[0][5], "实际回复")

    async def test_failed_delivery_is_not_recorded(self):
        plugin = self.plugin()
        event = FakeEvent()
        event.send = AsyncMock(side_effect=RuntimeError("send failed"))
        plugin._ensure_send_observer(event)
        with self.assertRaises(RuntimeError):
            await event.send(MessageChain([Plain("没发成功")]))
        self.assertEqual(fetch_activity_rows(plugin, sender_id="bot"), [])

    async def test_explicit_false_delivery_is_not_treated_as_a_sent_first_bubble(self):
        plugin = self.plugin()
        event = FakeEvent(message_str="输入")
        send = AsyncMock(return_value=False)
        event.send = send
        event._result = llm_result("第一句。第二句。")
        await plugin.on_decorating_result(event)
        states = [state for tasks in plugin._human_reply_tasks.values() for state in tasks]
        await self.finish_typing(plugin)
        send.assert_awaited_once()
        self.assertEqual(states[0].sent_count, 0)
        self.assertEqual(fetch_activity_rows(plugin, sender_id="bot"), [])

    async def test_preceding_split_keeps_tail_in_normal_pipeline(self):
        plugin = self.plugin()
        event = FakeEvent()
        event._result = llm_result("前段，尾段")
        await plugin.observe_outgoing_result(event)
        await event.send(MessageChain([Plain("前段")]))
        event._result.chain = [Plain("尾段")]
        await plugin.on_decorating_result(event)
        self.assertEqual(plugin._human_reply_tasks, {})
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
        await plugin.on_decorating_result(event)
        self.assertEqual(plugin._human_reply_tasks, {})
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
        plugin._ensure_send_observer(event)
        await event.send(MessageChain([Reply(id="real-message"), Plain("回复")]))
        self.assertIsNone(event.get_extra("_outputpro_pending_reply"))
        replies = [comp.id for comp in event.sent[0].chain if isinstance(comp, Reply)]
        self.assertEqual(replies, ["real-message"])

    async def test_record_scope_applies_to_regular_and_proactive_messages(self):
        plugin = self.plugin(activity_group_blacklist=["20001"])
        event = FakeEvent()
        plugin._ensure_send_observer(event)
        await event.send(MessageChain([Plain("群里回复")]))
        plugin._record_proactive_activity({"target_kind": "group", "target_id": "20001", "target_name": "群"}, "主动回复")
        self.assertEqual(fetch_activity_rows(plugin, sender_id="bot"), [])

    async def test_interruption_before_first_send_does_not_record_text(self):
        plugin = self.plugin()
        plugin._human_typing_wait = AsyncMock(return_value=False)
        event = FakeEvent()
        event._result = llm_result("没发出的第一句，还有第二句")
        await plugin.on_llm_response(event, SimpleNamespace(completion_text=event._result.get_plain_text()))
        await plugin.on_decorating_result(event)
        await self.finish_typing(plugin)
        self.assertEqual(event.sent, [])
        self.assertEqual(fetch_activity_rows(plugin, sender_id="bot"), [])

    async def test_partial_failure_only_records_successful_bubbles(self):
        plugin = self.plugin()
        event = FakeEvent()
        base_send = event.send

        async def fail_second(message):
            if event.sent:
                raise RuntimeError("second failed")
            await base_send(message)

        event.send = fail_second
        event._result = llm_result("第一句话来了，第二句话也来")
        await plugin.on_decorating_result(event)
        await self.finish_typing(plugin)
        self.assertEqual(len(event.sent), 1)
        rows = fetch_activity_rows(plugin, sender_id="bot")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][5], event.sent[0].chain[0].text)
        self.assertNotIn("第二句话", rows[0][5])

    async def test_observer_restores_send_and_does_not_duplicate_hooks(self):
        plugin = self.plugin()
        event = FakeEvent()
        original = event.send
        first = plugin._ensure_send_observer(event)
        self.assertIs(first, plugin._ensure_send_observer(event))
        await event.send(MessageChain([Plain("一次")]))
        await plugin.after_reply_sent(event)
        self.assertEqual(event.send, original)
        self.assertFalse(event.get_extra(SEND_OBSERVER_EXTRA)["active"])
        self.assertEqual(len(fetch_activity_rows(plugin, sender_id="bot")), 1)

    async def test_tool_status_is_not_recorded_and_does_not_disable_human_reply(self):
        plugin = self.plugin()
        event = FakeEvent()
        await plugin.on_llm_request(event, ProviderRequest())
        await event.send(MessageChain([Plain("正在调用工具")], type="tool_call"))
        self.assertEqual(fetch_activity_rows(plugin, sender_id="bot"), [])
        event._result = llm_result("最终回复")
        await plugin.on_decorating_result(event)
        self.assertIsNone(event.get_result())
        await self.finish_typing(plugin)
        self.assertEqual(fetch_activity_rows(plugin, sender_id="bot")[0][5], "最终回复")

    async def test_native_nonfinite_values_have_safe_runtime_view(self):
        plugin = self.plugin(debounce_slow_idle_seconds=float("inf"), proactive_silent_wake_target_mean_minutes=float("nan"))
        self.assertEqual(plugin.debounce_slow_idle_seconds, 1.2)
        self.assertEqual(plugin.proactive_silent_wake_target_mean_minutes, 30)
        self.assertIn("debounce_slow_idle_seconds", plugin._runtime_config_errors)


if __name__ == "__main__":
    unittest.main()
