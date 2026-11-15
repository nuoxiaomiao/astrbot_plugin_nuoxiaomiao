"""Inbound guard, poke handling, event span closing, and group scope tests."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest

from astrbot.core.message.components import Plain

from infra.constants import MergeSession
from infra.runtime import group_scope_allows
from pipeline.inbound_guard import InboundGuard, PILImage
from tests.fakes import FakeContext, FakeEvent, make_plugin


class GuardLogicTest(unittest.IsolatedAsyncioTestCase):
    async def test_blocks_on_third_consecutive_dialogue(self):
        plugin = make_plugin(merge_enabled=False)

        first = FakeEvent(message_str="小喵一")
        second = FakeEvent(message_str="小喵二")
        third = FakeEvent(message_str="小喵三")

        await plugin.on_message(first)
        await plugin.on_message(second)
        await plugin.on_message(third)

        self.assertFalse(first.stopped)
        self.assertFalse(second.stopped)
        self.assertTrue(third.stopped)
        self.assertTrue(third.call_llm)  # True = 禁止默认 LLM
        self.assertTrue(third.extras["nuoxiaomiao_blocked"])
        await plugin.terminate()

    async def test_sender_change_resets_counter(self):
        plugin = make_plugin(merge_enabled=False)

        await plugin.on_message(FakeEvent(sender_id="10001", message_str="a"))
        await plugin.on_message(FakeEvent(sender_id="10001", message_str="b"))
        await plugin.on_message(FakeEvent(sender_id="10002", message_str="c"))
        event = FakeEvent(sender_id="10001", message_str="d")
        await plugin.on_message(event)

        self.assertFalse(event.stopped)
        await plugin.terminate()

    async def test_whitelist_takes_precedence_over_blacklist(self):
        plugin = make_plugin(
            merge_enabled=False,
            trigger_rounds=2,
            user_whitelist=["10001"],
            user_blacklist=["10001"],
        )

        first = FakeEvent(sender_id="10001", message_str="a")
        second = FakeEvent(sender_id="10001", message_str="b")
        await plugin.on_message(first)
        await plugin.on_message(second)

        self.assertFalse(first.stopped)
        self.assertFalse(second.stopped)
        await plugin.terminate()

    async def test_blacklist_limits_checked_users_when_not_empty(self):
        plugin = make_plugin(
            merge_enabled=False,
            trigger_rounds=2,
            user_blacklist=["10002"],
        )

        unchecked_second = FakeEvent(sender_id="10001", message_str="b")
        await plugin.on_message(FakeEvent(sender_id="10001", message_str="a"))
        await plugin.on_message(unchecked_second)
        await plugin.on_message(FakeEvent(sender_id="10002", message_str="c"))
        checked_second = FakeEvent(sender_id="10002", message_str="d")
        await plugin.on_message(checked_second)

        self.assertFalse(unchecked_second.stopped)
        self.assertTrue(checked_second.stopped)
        await plugin.terminate()

    async def test_non_wake_group_message_is_ignored(self):
        plugin = make_plugin(merge_enabled=False, trigger_rounds=2)

        first = FakeEvent(is_trigger=False, message_str="闲聊")
        second = FakeEvent(is_trigger=False, message_str="闲聊2")
        await plugin.on_message(first)
        await plugin.on_message(second)

        self.assertFalse(second.stopped)
        await plugin.terminate()

    async def test_matched_command_bypasses_merge_and_guard(self):
        for filter_name in ("CommandFilter", "CommandGroupFilter"):
            with self.subTest(filter_name=filter_name):
                command_filter = type(filter_name, (), {})()
                command_handler = type(
                    "Handler",
                    (),
                    {"event_filters": [command_filter]},
                )()
                plugin = make_plugin(trigger_rounds=2)

                for _ in range(3):
                    event = FakeEvent(
                        message_str="help",
                        components=[Plain("/help")],
                        activated_handlers=[command_handler],
                    )
                    await plugin.on_message(event)
                    self.assertFalse(event.stopped)
                    self.assertFalse(event.call_llm)

                self.assertEqual(plugin.guard._states, {})
                self.assertEqual(plugin.merger._merges, {})
                await plugin.terminate()

    async def test_command_fallback_uses_raw_prefixed_text(self):
        plugin = make_plugin(context=FakeContext(["/"]), trigger_rounds=2)
        event = FakeEvent(message_str="help", components=[Plain("/help")])
        event.get_extra = None

        await plugin.on_message(event)

        self.assertFalse(event.stopped)
        self.assertEqual(plugin.guard._states, {})
        self.assertEqual(plugin.merger._merges, {})
        await plugin.terminate()

    async def test_pipeline_wake_does_not_make_plain_group_message_a_trigger(self):
        plugin = make_plugin(merge_idle_seconds=30, merge_max_total_seconds=60)

        event = FakeEvent(
            is_trigger=False,
            pipeline_wake=True,
            message_str="普通群聊",
        )
        await plugin.on_message(event)

        key = plugin.merger.merge_key(event.unified_msg_origin, "10001")
        self.assertNotIn(key, plugin.merger._merges)
        self.assertFalse(event.stopped)
        self.assertFalse(event.call_llm)
        await plugin.terminate()

    @unittest.skipIf(PILImage is None, "PIL is unavailable")
    async def test_static_meme_converts_to_gif_and_cleans_temp_file(self):
        plugin = make_plugin(meme_tags=["angry"], merge_enabled=False)

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            source = tmp_dir / "source.png"
            config_path = tmp_dir / "meme_manager_config.json"
            PILImage.new("RGB", (8, 8), (255, 0, 0)).save(source)
            config_path.write_text(
                json.dumps({"convert_static_to_gif": True}),
                encoding="utf-8",
            )

            plugin.meme._meme_manager_config_paths = lambda: [config_path]
            plugin.meme.select_meme_file = lambda: source

            event = FakeEvent()
            await plugin.meme.send_meme(event)

            image = event.sent[0].chain[0]
            sent_path = Path(image.path)
            self.assertEqual(sent_path.suffix, ".gif")
            self.assertFalse(sent_path.exists())
            self.assertTrue(source.exists())
        await plugin.terminate()


class PokeLogicTest(unittest.IsolatedAsyncioTestCase):
    class _DummyReq:
        pass

    class FakePokeEvent:
        def __init__(self, raw, sender_id="10001", self_id="bot", group_id="20001"):
            self.message_obj = type(
                "PokeMO", (), {"raw_message": raw, "message": []}
            )()
            self._sender_id = sender_id
            self._self_id = self_id
            self._group_id = group_id
            self.unified_msg_origin = "aiocqhttp:GroupMessage:20001"
            self.message_str = ""
            self.stopped = False
            self.call_llm = False
            self.extras = {}

        def get_sender_id(self):
            return self._sender_id

        def get_self_id(self):
            return self._self_id

        def get_group_id(self):
            return self._group_id

        def get_platform_id(self):
            return "aiocqhttp"

        def get_sender_name(self):
            return "戳戳怪"

        def set_extra(self, key, value):
            self.extras[key] = value

        def get_extra(self, key=None, default=None):
            if key is None:
                return self.extras
            return self.extras.get(key, default)

        def should_call_llm(self, value):
            self.call_llm = value

        def stop_event(self):
            self.stopped = True

        def request_llm(self, **kwargs):
            self.requested_prompt = kwargs.get("prompt")
            self.requested_conversation = kwargs.get("conversation")
            return PokeLogicTest._DummyReq()

    @staticmethod
    def _raw(group_id="20001", target_id="bot", user_id="10001"):
        raw = {
            "post_type": "notice",
            "notice_type": "notify",
            "sub_type": "poke",
            "self_id": "bot",
            "user_id": user_id,
            "target_id": target_id,
        }
        if group_id is not None:
            raw["group_id"] = group_id
        return raw

    def _make_plugin(self, **cfg):
        return make_plugin(context=FakeContext(), **cfg)

    async def test_poke_triggers_llm_and_skips_chat_logic(self):
        plugin = self._make_plugin()
        event = self.FakePokeEvent(self._raw())
        result = await plugin.on_message(event)

        self.assertIsInstance(result, self._DummyReq)
        self.assertFalse(event.stopped)
        self.assertFalse(event.call_llm)
        self.assertIn("戳戳怪", event.requested_prompt)
        self.assertIn("戳了你一下", event.requested_prompt)
        self.assertNotIn("nuoxiaomiao_buffered", event.extras)
        self.assertEqual(plugin.merger._merges, {})
        await plugin.terminate()

    async def test_group_poke_cooldown_drops_burst(self):
        plugin = self._make_plugin(poke_reply_cooldown_seconds=0.5)
        first = self.FakePokeEvent(self._raw(user_id="10001"))
        second = self.FakePokeEvent(self._raw(user_id="10002"))

        r1 = await plugin.on_message(first)
        r2 = await plugin.on_message(second)

        self.assertIsInstance(r1, self._DummyReq)
        self.assertIsNone(r2)
        self.assertTrue(second.stopped)
        await plugin.terminate()

    async def test_private_poke_has_no_cooldown(self):
        plugin = self._make_plugin(poke_reply_cooldown_seconds=0.5)
        first = self.FakePokeEvent(self._raw(group_id=None, user_id="10001"), group_id="")
        second = self.FakePokeEvent(self._raw(group_id=None, user_id="10002"), group_id="")

        r1 = await plugin.on_message(first)
        r2 = await plugin.on_message(second)

        self.assertIsInstance(r1, self._DummyReq)
        self.assertIsInstance(r2, self._DummyReq)
        self.assertFalse(second.stopped)
        await plugin.terminate()

    async def test_non_self_poke_is_ignored(self):
        plugin = self._make_plugin()
        event = self.FakePokeEvent(self._raw(target_id="someone_else"))

        result = await plugin.on_message(event)

        self.assertIsNone(result)
        self.assertFalse(event.stopped)
        self.assertFalse(event.call_llm)
        await plugin.terminate()

    async def test_on_llm_request_does_not_block_poke(self):
        plugin = self._make_plugin(merge_enabled=True)
        event = self.FakePokeEvent(self._raw())
        key = plugin.merger.merge_key(event.unified_msg_origin, event.get_sender_id())
        plugin.merger._merges[key] = MergeSession(
            session_key=event.unified_msg_origin,
            sender_id=event.get_sender_id(),
            parts=["小喵等待中"],
            start_ts=time.monotonic(),
            last_ts=time.monotonic(),
            origin_event=event,
        )

        await plugin.on_llm_request(event, object())

        self.assertFalse(event.stopped)
        await plugin.terminate()


class EventSpanCloseTest(unittest.IsolatedAsyncioTestCase):
    """被拦下的事件要补收尾 trace：观察面板按 span 重建会话，不收尾会挂着。"""

    @staticmethod
    def _fake_trace():
        calls = []

        def record(action, **fields):
            calls.append((action, fields))

        return SimpleNamespace(record=record), calls

    async def test_buffered_message_closes_span(self):
        plugin = make_plugin(
            debounce_enabled=True,
            merge_idle_seconds=1,
            merge_max_total_seconds=12,
        )
        trace, calls = self._fake_trace()
        event = FakeEvent(message_str="短句", is_trigger=True)
        event.trace = trace

        await plugin.on_message(event)
        self.assertTrue(event.stopped)
        self.assertTrue(calls)
        action, fields = calls[-1]
        self.assertEqual(action, "astr_agent_complete")
        self.assertIn("防抖合并", fields.get("resp", ""))
        await plugin.terminate()

    async def test_blocked_user_closes_span(self):
        plugin = make_plugin(blocked_users=["10001"])
        trace, calls = self._fake_trace()
        event = FakeEvent(message_str="你好", is_trigger=False)
        event.trace = trace

        await plugin.on_message(event)
        self.assertTrue(event.stopped)
        action, fields = calls[-1]
        self.assertEqual(action, "astr_agent_complete")
        self.assertIn("屏蔽名单", fields.get("resp", ""))
        await plugin.terminate()

    async def test_missing_trace_is_silent(self):
        InboundGuard.close_event_span(FakeEvent(message_str="x"), "说明")
        self.assertTrue(True)


class GroupScopeTest(unittest.IsolatedAsyncioTestCase):
    """群聊黑白名单：黑名单优先、白名单圈定、群号群名均可、私聊不受影响。"""

    def _plugin(self, **cfg):
        return make_plugin(**cfg)

    def test_scope_semantics(self):
        self.assertTrue(group_scope_allows("1", "", set(), set()))
        self.assertFalse(group_scope_allows("1", "", set(), {"1"}))
        self.assertFalse(group_scope_allows("2", "猫群", set(), {"猫群"}))
        self.assertFalse(group_scope_allows("3", "其它", {"1"}, set()))
        self.assertTrue(group_scope_allows("1", "猫群", {"1"}, set()))
        self.assertTrue(group_scope_allows("9", "猫群", {"猫群"}, set()))
        # 黑名单优先于白名单。
        self.assertFalse(group_scope_allows("1", "猫群", {"猫群"}, {"1"}))

    async def test_private_chat_unaffected(self):
        plugin = self._plugin(guard_group_blacklist=["10001"])
        event = FakeEvent(
            is_private=True,
            session="aiocqhttp:FriendMessage:10001",
            group_id="",
            sender_id="10001",
            message_str="在吗",
        )
        self.assertTrue(
            plugin.guard.event_group_allowed(
                event,
                plugin.guard_group_whitelist,
                plugin.guard_group_blacklist,
            )
        )
        await plugin.terminate()

    async def test_debounce_respects_group_lists(self):
        control = self._plugin()
        buffered = FakeEvent(message_str="短句")
        await control.on_message(buffered)
        self.assertTrue(buffered.stopped)
        await control.terminate()

        blacklisted = self._plugin(debounce_group_blacklist=["20001"])
        passed = FakeEvent(message_str="短句")
        await blacklisted.on_message(passed)
        self.assertFalse(passed.stopped)
        await blacklisted.terminate()

        whitelisted = self._plugin(debounce_group_whitelist=["99999"])
        outside = FakeEvent(message_str="短句")
        await whitelisted.on_message(outside)
        self.assertFalse(outside.stopped)
        await whitelisted.terminate()

    async def test_guard_scope_respects_group_lists(self):
        event = FakeEvent(message_str="x", is_trigger=True)
        blacklisted = self._plugin(guard_group_blacklist=["20001"])
        self.assertFalse(blacklisted.guard.guard_scope_applies(event))
        await blacklisted.terminate()
        control = self._plugin()
        self.assertTrue(control.guard.guard_scope_applies(event))
        await control.terminate()

    async def test_keyword_wake_respects_group_lists(self):
        control = self._plugin(
            keyword_wake_enabled=True, keyword_wake_list=["小喵"]
        )
        woken = FakeEvent(message_str="小喵你看这个", is_trigger=False)
        await control.on_message(woken)
        self.assertTrue(woken.is_at_or_wake_command)
        await control.terminate()

        blacklisted = self._plugin(
            keyword_wake_enabled=True,
            keyword_wake_list=["小喵"],
            keyword_wake_group_blacklist=["20001"],
        )
        ignored = FakeEvent(message_str="小喵你看这个", is_trigger=False)
        await blacklisted.on_message(ignored)
        self.assertFalse(ignored.is_at_or_wake_command)
        await blacklisted.terminate()

    async def test_activity_capture_respects_group_lists(self):
        plugin = self._plugin(activity_group_blacklist=["20001"])
        recorded = []
        plugin.activity.record_activity = lambda entry: recorded.append(entry)
        await plugin._capture_chat_activity(FakeEvent(message_str="记一下"))
        self.assertEqual(recorded, [])
        await plugin.terminate()

        control = self._plugin()
        recorded2 = []
        control.activity.record_activity = lambda entry: recorded2.append(entry)
        await control._capture_chat_activity(FakeEvent(message_str="记一下"))
        self.assertEqual(len(recorded2), 1)
        await control.terminate()

    async def test_poke_skips_blacklisted_group(self):
        plugin = self._plugin(poke_group_blacklist=["20001"])
        event = FakeEvent(message_str="", is_trigger=False)
        event.message_obj.raw_message = {
            "post_type": "notice",
            "notice_type": "notify",
            "sub_type": "poke",
            "user_id": "10001",
            "target_id": "bot",
            "group_id": "20001",
        }

        result = await plugin.guard.handle_poke_event(event)

        self.assertIsNone(result)
        self.assertFalse(event.stopped)
        await plugin.terminate()
