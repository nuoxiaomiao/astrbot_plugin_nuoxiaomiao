"""Capability reminders, activity prefetch, and long-term memory gateway tests."""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
import unittest

from infra.constants import ActivityEntry
from tests.fakes import FakeContext, FakeEvent, make_activity_plugin, make_plugin


class CapabilityHintTest(unittest.IsolatedAsyncioTestCase):
    """动态提醒只介绍本轮实际工具，聊天参考由统一入口按策略附带。"""

    class _Req:
        def __init__(self, with_tools=True):
            self.system_prompt = ""
            self.extra_user_content_parts = []
            self.func_tool = SimpleNamespace(tools=[
                SimpleNamespace(name=name) for name in (
                    "list_conversations", "read_conversation_messages", "send_message"
                )
            ]) if with_tools else None

    @staticmethod
    def _event(sender_id="10001"):
        return FakeEvent(
            is_private=True,
            session=f"aiocqhttp:FriendMessage:{sender_id}",
            group_id="",
            sender_id=sender_id,
            sender_name="主人",
            message_str="嗯嗯",
        )

    async def test_keyword_chat_gets_activity_prefetch(self):
        plugin = make_activity_plugin(context=FakeContext(admins=["10001"]))
        await plugin.activity.capture_chat_activity(
            FakeEvent(message_str="今晚谁在打游戏", group_name="猫娘群")
        )
        req = self._Req()

        await plugin.ctx_injector.maybe_inject_context(
            FakeEvent(
                is_private=True,
                session="aiocqhttp:FriendMessage:10001",
                group_id="",
                sender_id="10001",
                message_str="他们刚才在群里聊什么",
            ),
            req,
        )

        self.assertEqual(len(req.extra_user_content_parts), 1)
        part = req.extra_user_content_parts[0]
        self.assertIn("[别处聊天参考]", part.text)
        self.assertIn("今晚谁在打游戏", part.text)
        self.assertIn("猫娘群", part.text)
        await plugin.terminate()

    async def test_prefetch_off_gets_nothing(self):
        plugin = make_activity_plugin(
            context=FakeContext(admins=["10001"]), activity_prefetch_enabled=False
        )
        await plugin.activity.capture_chat_activity(
            FakeEvent(message_str="今晚谁在打游戏", group_name="猫娘群")
        )
        req = self._Req()

        await plugin.ctx_injector.maybe_inject_context(
            FakeEvent(
                is_private=True,
                session="aiocqhttp:FriendMessage:10001",
                group_id="",
                sender_id="10001",
                message_str="他们刚才在群里聊什么",
            ),
            req,
        )

        self.assertEqual(req.extra_user_content_parts, [])
        await plugin.terminate()

    async def test_prefetch_requires_admin(self):
        plugin = make_activity_plugin(context=FakeContext(admins=["99999"]))
        await plugin.activity.capture_chat_activity(
            FakeEvent(message_str="今晚谁在打游戏", group_name="猫娘群")
        )
        req = self._Req()

        await plugin.ctx_injector.maybe_inject_context(
            FakeEvent(
                is_private=True,
                session="aiocqhttp:FriendMessage:10001",
                group_id="",
                sender_id="10001",
                message_str="他们刚才在群里聊什么",
            ),
            req,
        )

        self.assertEqual(req.extra_user_content_parts, [])
        await plugin.terminate()

    async def test_only_dynamic_reminder_is_injected(self):
        plugin = make_activity_plugin(context=FakeContext(admins=["10001"]))
        req = self._Req()

        plugin.ctx_injector.maybe_add_capability_reminder(self._event(), req)

        self.assertEqual(req.system_prompt, "")
        self.assertEqual(len(req.extra_user_content_parts), 1)
        part = req.extra_user_content_parts[0]
        self.assertIn("[糯小喵能力提醒]", part.text)
        self.assertIn("傲娇可等不了太久", part.text)
        self.assertIn("翻到什么说什么", part.text)
        self.assertTrue(part._no_save)
        await plugin.terminate()

    async def test_capability_reminder_not_duplicated(self):
        plugin = make_activity_plugin(context=FakeContext(admins=["10001"]))
        req = self._Req()

        plugin.ctx_injector.maybe_add_capability_reminder(self._event(), req)
        plugin.ctx_injector.maybe_add_capability_reminder(self._event(), req)

        self.assertEqual(req.system_prompt.count("[糯小喵能力提示]"), 0)
        self.assertEqual(len(req.extra_user_content_parts), 1)
        await plugin.terminate()

    async def test_hint_and_reminder_skipped_without_tools(self):
        plugin = make_activity_plugin(context=FakeContext(admins=["10001"]))
        req = self._Req(with_tools=False)

        plugin.ctx_injector.maybe_add_capability_reminder(self._event(), req)

        self.assertEqual(req.system_prompt, "")
        self.assertEqual(req.extra_user_content_parts, [])
        await plugin.terminate()

    async def test_activity_hint_switch_off_excludes_activity_menu(self):
        plugin = make_activity_plugin(activity_hint_enabled=False)
        req = self._Req()

        plugin.ctx_injector.maybe_add_capability_reminder(self._event(), req)

        text = req.extra_user_content_parts[0].text if req.extra_user_content_parts else ""
        self.assertNotIn("翻到什么说什么", text)
        self.assertIn("傲娇可等不了太久", text)
        await plugin.terminate()

    async def test_proactive_hint_switch_off_excludes_proactive_menu(self):
        plugin = make_activity_plugin(proactive_hint_enabled=False)
        req = self._Req()

        plugin.ctx_injector.maybe_add_capability_reminder(self._event(), req)

        text = req.extra_user_content_parts[0].text if req.extra_user_content_parts else ""
        self.assertIn("翻到什么说什么", text)
        self.assertNotIn("傲娇可等不了太久", text)
        await plugin.terminate()

    async def test_custom_hint_goes_to_reminder_not_static(self):
        plugin = make_activity_plugin(
            context=FakeContext(admins=["10001"]),
            capability_hint_text="想看别处聊天就自己翻，想催人就自己定闹钟喵。",
        )
        req = self._Req()

        plugin.ctx_injector.maybe_add_capability_reminder(self._event(), req)

        self.assertEqual(req.system_prompt, "")
        self.assertEqual(len(req.extra_user_content_parts), 1)
        self.assertIn("想看别处聊天就自己翻", req.extra_user_content_parts[0].text)
        await plugin.terminate()

    async def test_custom_hint_ignores_feature_flags(self):
        plugin = make_activity_plugin(
            context=FakeContext(admins=["10001"]),
            capability_hint_text="自定义提示",
            activity_hint_enabled=False,
            proactive_hint_enabled=False,
        )
        req = self._Req()

        plugin.ctx_injector.maybe_add_capability_reminder(self._event(), req)

        self.assertIn("自定义提示", req.extra_user_content_parts[0].text)
        await plugin.terminate()

    async def test_blank_custom_hint_falls_back_to_builtin(self):
        plugin = make_activity_plugin(
            context=FakeContext(admins=["10001"]),
            capability_hint_text="   ",
        )
        req = self._Req()

        plugin.ctx_injector.maybe_add_capability_reminder(self._event(), req)

        self.assertEqual(req.system_prompt, "")
        self.assertIn("[糯小喵能力提醒]", req.extra_user_content_parts[0].text)
        await plugin.terminate()

    async def test_custom_reminder_skipped_without_tools(self):
        plugin = make_activity_plugin(
            context=FakeContext(admins=["10001"]),
            capability_hint_text="自定义提示",
        )
        req = self._Req(with_tools=False)

        plugin.ctx_injector.maybe_add_capability_reminder(self._event(), req)

        self.assertEqual(req.system_prompt, "")
        self.assertEqual(req.extra_user_content_parts, [])
        await plugin.terminate()

    async def _seed_pending_row(self, plugin, note="催他回我晚饭吃什么"):
        row = {
            "id": 0,
            "created_ts": time.time(),
            "fire_ts": time.time() + 25 * 60,
            "source_umo": "aiocqhttp:FriendMessage:10001",
            "platform_id": "aiocqhttp",
            "target_kind": "private",
            "target_id": "10001",
            "target_name": "主人",
            "target_umo": "aiocqhttp:FriendMessage:10001",
            "subject_id": "10001",
            "subject_name": "主人",
            "await_sender_id": "10001",
            "note": note,
            "status": "pending",
        }
        row["id"] = plugin.proactive.insert_row(row)
        await plugin.proactive.schedule_timer(row)

    async def test_reminder_lists_pending_proactive(self):
        plugin = make_activity_plugin(context=FakeContext(admins=["10001"]))
        await self._seed_pending_row(plugin)
        req = self._Req()

        plugin.ctx_injector.maybe_add_capability_reminder(self._event(), req)

        text = req.extra_user_content_parts[0].text
        self.assertIn("你还没发出", text)
        self.assertIn("催他回我晚饭吃什么", text)
        await plugin.terminate()

    async def test_reminder_hides_pendings_for_non_admin(self):
        plugin = make_activity_plugin(context=FakeContext(admins=["10001"]))
        await self._seed_pending_row(plugin)
        req = self._Req()

        plugin.ctx_injector.maybe_add_capability_reminder(self._event(sender_id="99999"), req)

        text = req.extra_user_content_parts[0].text
        self.assertIn("[糯小喵能力提醒]", text)
        self.assertNotIn("你还没发出", text)
        self.assertNotIn("催他回我晚饭吃什么", text)
        await plugin.terminate()

    async def test_reminder_astrhe_division_when_installed(self):
        plugin = make_activity_plugin(context=FakeContext(admins=["10001"]))

        with_astrhe = plugin.ctx_injector.capability_reminder(
            show_pendings=False, has_astrhe=True,
            tool_names={"astrhe_schedule", "send_message"},
        )
        without = plugin.ctx_injector.capability_reminder(
            show_pendings=False, has_astrhe=False, tool_names={"send_message"},
        )

        self.assertIn("astrhe_schedule", with_astrhe)
        self.assertIn("工具分工", with_astrhe)
        self.assertNotIn("astrhe_schedule", without)
        await plugin.terminate()

    async def test_reminder_detects_astrhe_tool_in_request(self):
        plugin = make_activity_plugin(context=FakeContext(admins=["10001"]))
        req = self._Req()
        req.func_tool = SimpleNamespace(tools=[
            SimpleNamespace(name="astrhe_schedule"), SimpleNamespace(name="send_message")
        ])

        plugin.ctx_injector.maybe_add_capability_reminder(self._event(), req)

        self.assertEqual(len(req.extra_user_content_parts), 1)
        self.assertIn("astrhe_schedule", req.extra_user_content_parts[0].text)
        await plugin.terminate()

    async def test_reminder_includes_other_sessions_digest(self):
        plugin = make_activity_plugin(
            context=FakeContext(admins=["10001"]),
            capability_context_digest=True,
        )
        plugin.activity.record_activity(
            ActivityEntry(
                platform_id="aiocqhttp",
                ts=time.time(),
                sender_id="10002",
                sender_name="小初音",
                conversation_id="70000",
                conversation_name="别的群",
                kind="group",
                text="群里聊到晚饭了",
            )
        )
        req = self._Req()

        plugin.ctx_injector.maybe_add_capability_reminder(self._event(), req)

        await plugin.ctx_injector.maybe_inject_context(self._event(), req)
        text = "\n".join(part.text for part in req.extra_user_content_parts)
        self.assertIn("别处近况", text)
        self.assertIn("群里聊到晚饭了", text)
        await plugin.terminate()

    async def test_reminder_digest_off_by_default(self):
        plugin = make_activity_plugin(context=FakeContext(admins=["10001"]))
        self.assertFalse(plugin.capability_context_digest)
        plugin.activity.record_activity(
            ActivityEntry(
                platform_id="aiocqhttp",
                ts=time.time(),
                sender_id="10002",
                sender_name="小初音",
                conversation_id="70000",
                conversation_name="别的群",
                kind="group",
                text="群里聊到晚饭了",
            )
        )
        req = self._Req()

        plugin.ctx_injector.maybe_add_capability_reminder(self._event(), req)

        await plugin.ctx_injector.maybe_inject_context(self._event(), req)
        text = "\n".join(part.text for part in req.extra_user_content_parts)
        self.assertNotIn("别处近况", text)
        self.assertNotIn("群里聊到晚饭了", text)
        await plugin.terminate()

    async def test_reminder_digest_can_be_switched_off(self):
        plugin = make_activity_plugin(
            context=FakeContext(admins=["10001"]),
            capability_context_digest=False,
        )
        plugin.activity.record_activity(
            ActivityEntry(
                platform_id="aiocqhttp",
                ts=time.time(),
                sender_id="10002",
                sender_name="小初音",
                conversation_id="70000",
                conversation_name="别的群",
                kind="group",
                text="群里聊到晚饭了",
            )
        )
        req = self._Req()

        plugin.ctx_injector.maybe_add_capability_reminder(self._event(), req)

        await plugin.ctx_injector.maybe_inject_context(self._event(), req)
        text = "\n".join(part.text for part in req.extra_user_content_parts)
        self.assertNotIn("别处近况", text)
        await plugin.terminate()

    async def test_digest_private_gate_in_reminder(self):
        plugin = make_activity_plugin(
            context=FakeContext(admins=["10001"]),
            capability_context_digest=True,
        )
        plugin.activity.record_activity(
            ActivityEntry(
                platform_id="aiocqhttp",
                ts=time.time(),
                sender_id="10002",
                sender_name="小初音",
                conversation_id="70000",
                conversation_name="别的群",
                kind="group",
                text="群里聊到晚饭了",
            )
        )
        plugin.activity.record_activity(
            ActivityEntry(
                platform_id="aiocqhttp",
                ts=time.time(),
                sender_id="10002",
                sender_name="小初音",
                conversation_id="10002",
                conversation_name="小初音",
                kind="private",
                text="私聊里的小秘密",
            )
        )
        req = self._Req()

        plugin.ctx_injector.maybe_add_capability_reminder(self._event(), req)

        await plugin.ctx_injector.maybe_inject_context(self._event(), req)
        text = "\n".join(part.text for part in req.extra_user_content_parts)
        self.assertIn("群里聊到晚饭了", text)
        self.assertNotIn("小秘密", text)
        plugin.proactive_context_other_private = True
        req2 = self._Req()
        plugin.ctx_injector.maybe_add_capability_reminder(self._event(), req2)
        await plugin.ctx_injector.maybe_inject_context(self._event(), req2)
        text2 = "\n".join(part.text for part in req2.extra_user_content_parts)
        self.assertIn("小秘密", text2)
        await plugin.terminate()


class LmemGatewayTest(unittest.IsolatedAsyncioTestCase):
    """长期记忆按需检索：命中回忆意图才查，出错或超时都不影响回复。"""

    class _Req:
        def __init__(self):
            self.system_prompt = ""
            self.extra_user_content_parts = []

    class _FakeMemoryTool:
        name = "recall_long_term_memory"

        def __init__(self, payload=None, error=None, delay=0.0):
            self.payload = payload
            self.error = error
            self.delay = delay
            self.calls = []

        async def call(self, context, **kwargs):
            self.calls.append(kwargs)
            if self.delay:
                await asyncio.sleep(self.delay)
            if self.error:
                raise self.error
            return json.dumps(self.payload, ensure_ascii=False)

    def _context_with_tool(self, tool):
        context = FakeContext(admins=["10001"])
        context.get_llm_tool_manager = lambda: type("FTM", (), {"func_list": [tool]})()
        return context

    def _private(self, text):
        return FakeEvent(
            is_private=True,
            session="aiocqhttp:FriendMessage:10001",
            group_id="",
            message_str=text,
        )

    async def test_plain_chat_never_touches_memory(self):
        tool = self._FakeMemoryTool(payload={"results": []})
        plugin = make_plugin(context=self._context_with_tool(tool))
        req = self._Req()

        await plugin.ctx_injector.maybe_inject_context(self._private("抱抱，今天好累"), req)

        self.assertEqual(tool.calls, [])
        self.assertEqual(req.extra_user_content_parts, [])
        await plugin.terminate()

    async def test_recall_intent_injects_memory(self):
        tool = self._FakeMemoryTool(
            payload={"results": [{"content": "主人喜欢喝乌龙茶", "score": 0.92}]}
        )
        plugin = make_plugin(context=self._context_with_tool(tool))
        req = self._Req()

        await plugin.ctx_injector.maybe_inject_context(self._private("你还记得我喜欢喝什么吗"), req)

        self.assertEqual(len(tool.calls), 1)
        self.assertEqual(len(req.extra_user_content_parts), 1)
        self.assertIn("乌龙茶", req.extra_user_content_parts[0].text)
        await plugin.terminate()

    async def test_recall_failure_is_swallowed(self):
        tool = self._FakeMemoryTool(error=RuntimeError("记忆库炸了"))
        plugin = make_plugin(context=self._context_with_tool(tool))
        req = self._Req()

        await plugin.ctx_injector.maybe_inject_context(self._private("你还记得我的生日吗"), req)

        self.assertEqual(req.extra_user_content_parts, [])
        await plugin.terminate()

    async def test_recall_timeout_is_swallowed(self):
        tool = self._FakeMemoryTool(payload={"results": []}, delay=5.0)
        plugin = make_plugin(
            context=self._context_with_tool(tool), lmem_gateway_timeout_seconds=0.3
        )
        req = self._Req()

        await plugin.ctx_injector.maybe_inject_context(self._private("你还记得我的生日吗"), req)

        self.assertEqual(req.extra_user_content_parts, [])
        await plugin.terminate()

    async def test_missing_plugin_is_noop(self):
        plugin = make_plugin()
        req = self._Req()

        await plugin.ctx_injector.maybe_inject_context(self._private("你还记得我的生日吗"), req)

        self.assertEqual(req.extra_user_content_parts, [])
        await plugin.terminate()

    async def test_switch_off_never_queries(self):
        tool = self._FakeMemoryTool(
            payload={"results": [{"content": "主人喜欢喝乌龙茶", "score": 0.9}]}
        )
        plugin = make_plugin(
            context=self._context_with_tool(tool), lmem_gateway_enabled=False
        )
        req = self._Req()

        await plugin.ctx_injector.maybe_inject_context(self._private("你还记得我喜欢喝什么吗"), req)

        self.assertEqual(tool.calls, [])
        await plugin.terminate()
