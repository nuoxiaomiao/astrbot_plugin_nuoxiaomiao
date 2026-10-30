"""Regression tests for context deduplication, model choice, and memory fallback."""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from astrbot.core.provider.entities import ProviderRequest

from main import NuoxiaomiaoPlugin


class ContextChangesTest(unittest.IsolatedAsyncioTestCase):
    def plugin(self, **overrides):
        # These tests do not need live AstrBot services or a persistent database.
        plugin = object.__new__(NuoxiaomiaoPlugin)
        values = {
            "activity_buffer_enabled": True,
            "activity_tool_enabled": True,
            "activity_hint_enabled": True,
            "proactive_enabled": True,
            "proactive_hint_enabled": True,
            "capability_hint_text": "",
            "capability_context_digest": False,
            "activity_prefetch_enabled": True,
            "activity_prefetch_keywords": ["群里聊"],
            "lmem_gateway_enabled": False,
            "lmem_gateway_keywords": ["还记得"],
            "lmem_gateway_timeout_seconds": 0.02,
            "lmem_gateway_top_k": 4,
            "lmem_gateway_max_chars": 600,
            "lmem_gateway_plugin_name": "LivingMemory",
            "proactive_judge_provider_id": "",
            "proactive_silent_wake_allow_tools": True,
            "proactive_silent_wake_max_steps": 8,
            "_warned_self_wake_no_admin": False,
        }
        values.update(overrides)
        for key, value in values.items():
            setattr(plugin, key, value)
        plugin._activity_permission_ok = Mock(return_value=True)
        plugin._pending_proactive_summary = Mock(return_value="")
        plugin._current_conversation = Mock(return_value=("test", "private", "1"))
        plugin._extract_text = lambda event: event.message_str
        return plugin

    @staticmethod
    def event(text="群里聊什么"):
        return SimpleNamespace(message_str=text, unified_msg_origin="test:FriendMessage:1")

    @staticmethod
    def request(*tool_names):
        request = ProviderRequest()
        request.func_tool = SimpleNamespace(
            tools=[SimpleNamespace(name=name) for name in tool_names]
        )
        return request

    def test_unavailable_tools_are_not_advertised(self):
        plugin = self.plugin()
        req = self.request("unrelated_tool")
        plugin._maybe_add_capability_hint(req)
        plugin._maybe_add_capability_reminder(self.event(), req)
        self.assertEqual(req.system_prompt, "")
        self.assertEqual(req.extra_user_content_parts, [])

    def test_partial_toolset_only_advertises_available_capabilities(self):
        plugin = self.plugin()
        req = self.request("read_conversation_messages")
        plugin._maybe_add_capability_reminder(self.event(), req)
        text = req.extra_user_content_parts[0].text
        self.assertIn("read_conversation_messages", text)
        self.assertNotIn("list_conversations", text)
        self.assertNotIn("send_message", text)
        plugin._maybe_add_capability_reminder(self.event(), req)
        self.assertEqual(len(req.extra_user_content_parts), 1)

    def test_activity_reminder_obeys_tool_permissions_instead_of_assuming_admin_only(self):
        plugin = self.plugin(activity_require_admin=False)
        req = self.request("list_conversations", "read_conversation_messages")
        plugin._maybe_add_capability_reminder(self.event(), req)
        text = req.extra_user_content_parts[0].text
        self.assertIn("按工具返回的权限结果处理", text)
        self.assertNotIn("只有主人/管理员", text)

    def test_inactive_tools_are_not_advertised_in_the_current_turn(self):
        plugin = self.plugin()
        req = self.request("list_conversations", "read_conversation_messages", "send_message")
        for tool in req.func_tool.tools:
            tool.active = False
        plugin._maybe_add_capability_reminder(self.event(), req)
        self.assertEqual(req.extra_user_content_parts, [])

    def test_custom_reminder_and_authorized_pending_summary_are_preserved(self):
        plugin = self.plugin(capability_hint_text="保留我的自定义提醒")
        plugin._pending_proactive_summary.return_value = "管理员的待发清单"
        req = self.request("send_message")
        plugin._maybe_add_capability_reminder(self.event(), req)
        text = req.extra_user_content_parts[0].text
        self.assertIn("保留我的自定义提醒", text)
        self.assertIn("管理员的待发清单", text)
        plugin._activity_permission_ok.return_value = False
        denied = self.request("send_message")
        plugin._maybe_add_capability_reminder(self.event(), denied)
        self.assertNotIn("管理员的待发清单", denied.extra_user_content_parts[0].text)

    async def test_digest_wins_over_keyword_prefetch_and_is_not_duplicated(self):
        plugin = self.plugin(capability_context_digest=True)
        plugin._other_sessions_digest = Mock(return_value="其它群最近的消息")
        plugin._build_activity_prefetch = Mock(return_value="重复的关键词参考")
        req = self.request("list_conversations", "read_conversation_messages")
        plugin._maybe_add_capability_reminder(self.event(), req)
        await plugin._maybe_inject_context(self.event(), req)
        await plugin._maybe_inject_context(self.event(), req)
        text = "\n".join(part.text for part in req.extra_user_content_parts)
        self.assertEqual(text.count("其它群最近的消息"), 1)
        self.assertNotIn("重复的关键词参考", text)
        plugin._build_activity_prefetch.assert_not_called()
        plugin._other_sessions_digest.assert_called_once()

    async def test_keyword_prefetch_only_runs_when_digest_is_off_and_keyword_matches(self):
        plugin = self.plugin()
        plugin._build_activity_prefetch = Mock(return_value="关键词参考")
        req = self.request()
        await plugin._maybe_inject_context(self.event("今天好累"), req)
        plugin._build_activity_prefetch.assert_not_called()
        await plugin._maybe_inject_context(self.event(), req)
        self.assertIn("关键词参考", req.extra_user_content_parts[0].text)

    async def test_digest_is_available_for_requests_without_plain_text(self):
        plugin = self.plugin(capability_context_digest=True)
        plugin._other_sessions_digest = Mock(return_value="图片回合也能参考的群聊")
        req = self.request()
        await plugin._maybe_inject_context(self.event(""), req)
        self.assertIn("图片回合也能参考的群聊", req.extra_user_content_parts[0].text)

    async def test_chat_reference_requires_permission_in_both_modes(self):
        for digest_enabled in (True, False):
            with self.subTest(digest_enabled=digest_enabled):
                plugin = self.plugin(capability_context_digest=digest_enabled)
                plugin._activity_permission_ok.return_value = False
                plugin._other_sessions_digest = Mock()
                plugin._build_activity_prefetch = Mock()
                req = self.request()
                await plugin._maybe_inject_context(self.event(), req)
                self.assertEqual(req.extra_user_content_parts, [])
                plugin._other_sessions_digest.assert_not_called()
                plugin._build_activity_prefetch.assert_not_called()

    def wake_plugin(self, configured="judge"):
        from test_plugin_logic import make_plugin
        from resident_fixtures import ScriptedProvider
        plugin = make_plugin(proactive_judge_provider_id=configured)
        self.addAsyncCleanup(plugin.terminate)
        provider = ScriptedProvider()
        provider.provider_config["id"] = "actual-judge"
        provider.meta = lambda: SimpleNamespace(id="actual-judge")
        provider.text_chat = AsyncMock(return_value=__import__("astrbot.core.provider.entities", fromlist=["LLMResponse"]).LLMResponse(role="assistant", completion_text='{"want": false}'))
        manager = SimpleNamespace(
            get_provider_by_id=AsyncMock(return_value=provider),
            get_using_provider_async=AsyncMock(return_value=provider),
        )
        plugin.context = SimpleNamespace(
            provider_manager=manager,
            get_llm_tool_manager=lambda: SimpleNamespace(func_list=[]),
            persona_manager=SimpleNamespace(
                get_default_persona_v3=AsyncMock(return_value=SimpleNamespace(system_prompt="人设"))
            ),
        )
        plugin._admin_ids = Mock(return_value={"1"})
        plugin._pick_self_wake_session = AsyncMock(
            return_value={"umo": "test:GroupMessage:2", "kind": "group", "id": "2"}
        )
        plugin._self_wake_prompt = Mock(return_value="自主活动")
        plugin._build_heartbeat_prompt = Mock(return_value="判断活动")
        plugin._build_self_wake_agent_config = Mock(
            return_value=SimpleNamespace(fallback_provider_ids=["unwanted-fallback"])
        )
        plugin._self_wake_toolset = Mock(return_value=None)
        plugin._record_self_wake_run = Mock()
        return plugin, provider, manager

    @staticmethod
    def wake_patches(build):
        from astrbot.core.cron.events import CronMessageEvent as WakeEvent
        from astrbot.core.platform.message_session import MessageSession
        return (
            patch("main._CronMessageEvent", WakeEvent),
            patch("main._MessageSession", MessageSession),
            patch("main._get_session_conv", AsyncMock(return_value=__import__("resident_fixtures").conversation())),
            patch("main._build_main_agent", build),
        )

    @staticmethod
    def quiet_build(provider):
        from resident_fixtures import build_runner
        return AsyncMock(side_effect=build_runner)

    async def test_autonomous_wake_uses_selected_model_and_records_real_id(self):
        plugin, provider, manager = self.wake_plugin()
        build = self.quiet_build(provider)
        p1, p2, p3, p4 = self.wake_patches(build)
        with p1, p2, p3, p4:
            ok, outcome = await plugin._run_self_wake_turn(30, 0.2, 0.1)
        self.assertTrue(ok)
        self.assertEqual(outcome, "quiet")
        manager.get_provider_by_id.assert_awaited_once_with("judge")
        manager.get_using_provider_async.assert_not_called()
        self.assertIs(build.call_args.kwargs["provider"], provider)
        self.assertEqual(build.call_args.kwargs["config"].fallback_provider_ids, [])
        self.assertEqual(plugin._record_self_wake_run.call_args.args[0]["provider_id"], "actual-judge")

    async def test_empty_selected_toolset_still_clears_tools_added_during_agent_build(self):
        plugin, provider, _ = self.wake_plugin()
        kept = SimpleNamespace(tools=[])
        plugin._self_wake_toolset.return_value = kept
        build = self.quiet_build(provider)
        from resident_fixtures import build_runner, tool
        from astrbot.core.agent.tool import ToolSet
        async def merge_persona_tools(**kwargs):
            return await build_runner(**kwargs, tools=ToolSet(tools=[tool("unselected_persona_tool", AsyncMock())]))

        build.side_effect = merge_persona_tools
        p1, p2, p3, p4 = self.wake_patches(build)
        with p1, p2, p3, p4:
            ok, outcome = await plugin._run_self_wake_turn(30, 0.2, 0.1)
        self.assertTrue(ok)
        self.assertEqual(outcome, "quiet")
        self.assertEqual(build.call_args.kwargs["req"].func_tool.tools, [])

    async def test_blank_wake_model_uses_global_default_without_session_preference(self):
        plugin, provider, manager = self.wake_plugin(configured="")
        build = self.quiet_build(provider)
        p1, p2, p3, p4 = self.wake_patches(build)
        with p1, p2, p3, p4:
            ok, _ = await plugin._run_self_wake_turn(30, 0.2, 0.1)
        self.assertTrue(ok)
        manager.get_provider_by_id.assert_not_called()
        self.assertEqual(manager.get_using_provider_async.call_args.kwargs, {})
        self.assertIs(build.call_args.kwargs["provider"], provider)

    async def test_missing_selected_model_does_not_fall_back(self):
        plugin, provider, manager = self.wake_plugin()
        manager.get_provider_by_id.return_value = None
        build = self.quiet_build(provider)
        p1, p2, p3, p4 = self.wake_patches(build)
        with p1, p2, p3, p4:
            ok, outcome = await plugin._run_self_wake_turn(30, 0.2, 0.1)
        self.assertFalse(ok)
        self.assertEqual(outcome, "error")
        build.assert_not_called()
        manager.get_using_provider_async.assert_not_called()

    async def test_judgment_fallback_records_the_same_selected_model(self):
        plugin, _, manager = self.wake_plugin()
        row = {"started_ts": 1}
        ok, outcome = await plugin._run_self_wake_fallback(row, 30)
        self.assertTrue(ok)
        self.assertEqual(outcome, "quiet")
        self.assertEqual(row["provider_id"], "actual-judge")
        manager.get_provider_by_id.assert_awaited_once_with("judge")

    async def test_judgment_missing_model_does_not_fall_back(self):
        plugin, _, manager = self.wake_plugin()
        manager.get_provider_by_id.return_value = None
        decision, ok = await plugin._ask_heartbeat_llm()
        self.assertFalse(ok)
        self.assertIsNone(decision)
        manager.get_using_provider_async.assert_not_called()

    async def test_empty_or_failed_memory_tool_never_calls_engine(self):
        for failure in (None, RuntimeError("tool denied")):
            with self.subTest(failure=failure):
                plugin = self.plugin()
                tool = SimpleNamespace(call=AsyncMock(return_value={"results": []}, side_effect=failure))
                plugin._find_lmem_tool = Mock(return_value=tool)
                plugin._build_agent_context = Mock(return_value=object())
                plugin._recall_with_lmem_engine = AsyncMock(return_value="must not be used")
                self.assertEqual(await plugin._recall_from_lmem(self.event(), "还记得吗"), "")
                plugin._recall_with_lmem_engine.assert_not_called()

    async def test_memory_tool_timeout_is_one_budget_and_never_calls_engine(self):
        plugin = self.plugin()
        cancelled = asyncio.Event()

        async def slow_tool(*args, **kwargs):
            try:
                await asyncio.sleep(1)
            finally:
                cancelled.set()

        plugin._find_lmem_tool = Mock(return_value=SimpleNamespace(call=slow_tool))
        plugin._build_agent_context = Mock(return_value=object())
        plugin._recall_with_lmem_engine = AsyncMock()
        self.assertEqual(await plugin._recall_from_lmem(self.event(), "还记得吗"), "")
        self.assertTrue(cancelled.is_set())
        plugin._recall_with_lmem_engine.assert_not_called()

    async def test_memory_engine_is_used_only_when_tool_is_missing(self):
        plugin = self.plugin()
        plugin._find_lmem_tool = Mock(return_value=None)
        plugin._recall_with_lmem_engine = AsyncMock(return_value="来自兼容引擎的记忆")
        self.assertEqual(
            await plugin._recall_from_lmem(self.event(), "还记得吗"),
            "来自兼容引擎的记忆",
        )
        plugin._recall_with_lmem_engine.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
