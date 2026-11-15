"""Regression tests for context deduplication, capability reminders, and model choice."""

import json
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from astrbot.core.provider.entities import ProviderRequest

from infra.constants import ActivityEntry
from tests.fakes import FakeContext, FakeEvent, make_activity_plugin, make_plugin
from tests.resident_fixtures import ScriptedProvider, build_runner, conversation, tool


class ContextChangesTest(unittest.IsolatedAsyncioTestCase):
    def plugin(self, **overrides):
        defaults = {
            "activity_buffer_enabled": True,
            "activity_tool_enabled": True,
            "activity_hint_enabled": True,
            "proactive_enabled": True,
            "proactive_hint_enabled": True,
            "capability_context_digest": False,
            "activity_prefetch_enabled": True,
            "activity_prefetch_keywords": ["群里聊"],
            "proactive_judge_provider_id": "",
            "proactive_silent_wake_allow_tools": True,
            "proactive_silent_wake_max_steps": 8,
        }
        defaults.update(overrides)
        p = make_plugin(context=FakeContext(admins=["1"]), **defaults)
        self.addAsyncCleanup(p.terminate)
        p.activity.activity_permission_ok = Mock(return_value=True)
        p.proactive.pending_summary = Mock(return_value="")
        p.activity.current_conversation = Mock(return_value=("test", "private", "1"))
        return p

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
        plugin.ctx_injector.maybe_add_capability_reminder(self.event(), req)
        self.assertEqual(req.system_prompt, "")
        self.assertEqual(req.extra_user_content_parts, [])

    def test_partial_toolset_only_advertises_available_capabilities(self):
        plugin = self.plugin()
        req = self.request("read_conversation_messages")
        plugin.ctx_injector.maybe_add_capability_reminder(self.event(), req)
        text = req.extra_user_content_parts[0].text
        self.assertIn("read_conversation_messages", text)
        self.assertNotIn("list_conversations", text)
        self.assertNotIn("send_message", text)
        plugin.ctx_injector.maybe_add_capability_reminder(self.event(), req)
        self.assertEqual(len(req.extra_user_content_parts), 1)

    def test_activity_reminder_obeys_tool_permissions_instead_of_assuming_admin_only(self):
        plugin = self.plugin(activity_require_admin=False)
        req = self.request("list_conversations", "read_conversation_messages")
        plugin.ctx_injector.maybe_add_capability_reminder(self.event(), req)
        text = req.extra_user_content_parts[0].text
        self.assertIn("按工具返回的权限结果处理", text)
        self.assertNotIn("只有主人/管理员", text)

    def test_inactive_tools_are_not_advertised_in_the_current_turn(self):
        plugin = self.plugin()
        req = self.request("list_conversations", "read_conversation_messages", "send_message")
        for t_item in req.func_tool.tools:
            t_item.active = False
        plugin.ctx_injector.maybe_add_capability_reminder(self.event(), req)
        self.assertEqual(req.extra_user_content_parts, [])

    def test_authorized_pending_summary_is_preserved(self):
        plugin = self.plugin()
        plugin.proactive.pending_summary.return_value = "管理员的待发清单"
        req = self.request("send_message")
        plugin.ctx_injector.maybe_add_capability_reminder(self.event(), req)
        text = req.extra_user_content_parts[0].text
        self.assertIn("管理员的待发清单", text)
        plugin.activity.activity_permission_ok.return_value = False
        denied = self.request("send_message")
        plugin.ctx_injector.maybe_add_capability_reminder(self.event(), denied)
        self.assertNotIn("管理员的待发清单", denied.extra_user_content_parts[0].text)

    async def test_digest_wins_over_keyword_prefetch_and_is_not_duplicated(self):
        plugin = self.plugin(capability_context_digest=True)
        plugin.proactive.other_sessions_digest = Mock(return_value="其它群最近的消息")
        plugin.ctx_injector.build_activity_prefetch = Mock(return_value="预取文本")
        req = self.request("list_conversations")
        await plugin.ctx_injector.maybe_inject_context(self.event("他们刚才在群里聊什么"), req)
        await plugin.ctx_injector.maybe_inject_context(self.event("他们刚才在群里聊什么"), req)
        self.assertEqual(len(req.extra_user_content_parts), 1)
        self.assertIn("别处近况", req.extra_user_content_parts[0].text)
        plugin.ctx_injector.build_activity_prefetch.assert_not_called()

    async def test_keyword_prefetch_only_runs_when_digest_is_off_and_keyword_matches(self):
        plugin = self.plugin(capability_context_digest=False, activity_prefetch_enabled=True)
        plugin.ctx_injector.build_activity_prefetch = Mock(return_value="预取结果")
        req_miss = self.request("list_conversations")
        await plugin.ctx_injector.maybe_inject_context(self.event("今天吃什么"), req_miss)
        self.assertEqual(req_miss.extra_user_content_parts, [])
        req_hit = self.request("list_conversations")
        await plugin.ctx_injector.maybe_inject_context(self.event("刚才群里聊什么"), req_hit)
        self.assertEqual(req_hit.extra_user_content_parts[0].text, "预取结果")

    async def test_digest_is_available_for_requests_without_plain_text(self):
        plugin = self.plugin(capability_context_digest=True)
        plugin.proactive.other_sessions_digest = Mock(return_value="图片触发时的近况")
        req = self.request("list_conversations")
        await plugin.ctx_injector.maybe_inject_context(self.event(""), req)
        self.assertIn("图片触发时的近况", req.extra_user_content_parts[0].text)

    async def test_chat_reference_requires_permission_in_both_modes(self):
        for digest in (True, False):
            plugin = self.plugin(capability_context_digest=digest, activity_prefetch_enabled=True)
            plugin.activity.activity_permission_ok.return_value = False
            plugin.proactive.other_sessions_digest = Mock(return_value="不能泄露")
            plugin.ctx_injector.build_activity_prefetch = Mock(return_value="不能泄露")
            req = self.request("list_conversations")
            await plugin.ctx_injector.maybe_inject_context(self.event("群里聊什么"), req)
            self.assertEqual(req.extra_user_content_parts, [])

    @staticmethod
    def _mock_pm(providers):
        class PM:
            def __init__(self):
                self.fetched = []

            async def get_provider_by_id(self, pid):
                self.fetched.append(pid)
                return providers.get(pid)

            def get_using_provider(self, *_args, **_kwargs):
                return providers.get("__session__")

            async def get_using_provider_async(self, *_args, **_kwargs):
                return providers.get("__global__")

        return PM()

    async def test_autonomous_wake_uses_selected_model_and_records_real_id(self):
        chosen = ScriptedProvider()
        chosen.provider_config["id"] = "chosen-model"
        chosen.meta = lambda: SimpleNamespace(id="chosen-model", type="test", model="chosen")
        plugin = self.plugin(
            proactive_judge_provider_id="chosen-model",
            proactive_allow_groups=["20001"],
            proactive_silent_wake_allow_tools=True,
        )
        pm = self._mock_pm({"chosen-model": chosen, "__global__": ScriptedProvider()})
        plugin.context.provider_manager = pm
        plugin.context.get_config = lambda **_kw: {"wake_prefix": ["/"], "admins_id": ["1"]}
        plugin.self_wake.build_agent_config = lambda _event: SimpleNamespace(tool_schema_mode="full")
        plugin.resident.autonomy.dispatch_request_hooks = AsyncMock(return_value=False)

        async def build(**kwargs):
            return await build_runner(**kwargs, tools=ToolSetLike())

        class ToolSetLike:
            tools = []

            def __bool__(self):
                return True

        with (
            patch("resident.ports.build_main_agent", build),
            patch("resident.ports._get_session_conv", AsyncMock(return_value=conversation())),
        ):
            ok, outcome = await plugin.self_wake.run_turn(30.0, 0.5, 0.1)
        self.assertEqual((ok, outcome), (True, "quiet"))
        self.assertEqual(pm.fetched, ["chosen-model"])
        row = plugin.resident.store.rows("SELECT meta FROM resident_runs ORDER BY id DESC LIMIT 1")[0]
        self.assertEqual(json.loads(row["meta"])["provider_id"], "chosen-model")

    async def test_empty_selected_toolset_still_clears_tools_added_during_agent_build(self):
        chosen = ScriptedProvider()
        plugin = self.plugin(
            proactive_allow_groups=["20001"],
            proactive_silent_wake_allow_tools=True,
        )
        plugin.context.provider_manager = self._mock_pm({"__global__": chosen})
        plugin.context.get_config = lambda **_kw: {"wake_prefix": ["/"], "admins_id": ["1"]}
        plugin.self_wake.build_agent_config = lambda _event: SimpleNamespace(tool_schema_mode="full")
        plugin.resident.autonomy.dispatch_request_hooks = AsyncMock(return_value=False)

        async def build(**kwargs):
            from astrbot.core.agent.tool import ToolSet

            return await build_runner(
                **kwargs,
                tools=ToolSet([tool("unselected_tool", AsyncMock(return_value="should not run"))]),
            )

        with (
            patch("resident.ports.build_main_agent", build),
            patch("resident.ports._get_session_conv", AsyncMock(return_value=conversation())),
        ):
            ok, outcome = await plugin.self_wake.run_turn(30.0, 0.5, 0.1)
        self.assertEqual((ok, outcome), (True, "quiet"))
        self.assertEqual(chosen.calls[0]["func_tool"].tools, [])

    async def test_blank_wake_model_uses_global_default_without_session_preference(self):
        global_provider = ScriptedProvider()
        session_provider = ScriptedProvider()
        plugin = self.plugin(proactive_judge_provider_id="")
        pm = self._mock_pm({"__global__": global_provider, "__session__": session_provider})
        plugin.context.provider_manager = pm
        selected = await plugin.self_wake.select_provider()
        self.assertIs(selected, global_provider)
        self.assertEqual(pm.fetched, [])

    async def test_missing_selected_model_does_not_fall_back(self):
        plugin = self.plugin(proactive_judge_provider_id="missing-model")
        plugin.context.provider_manager = self._mock_pm(
            {"__global__": ScriptedProvider(), "__session__": ScriptedProvider()}
        )
        with self.assertRaisesRegex(RuntimeError, "missing-model"):
            await plugin.self_wake.select_provider()

    async def test_judgment_fallback_records_the_same_selected_model(self):
        chosen = ScriptedProvider(
            [SimpleNamespace(completion_text=json.dumps({"want": False}))]
        )
        chosen.provider_config["id"] = "chosen-judge"
        plugin = self.plugin(proactive_judge_provider_id="chosen-judge")
        plugin.context.provider_manager = self._mock_pm({"chosen-judge": chosen})
        row = {"started_ts": 1.0}
        ok, outcome = await plugin.self_wake.run_fallback(row, 30.0)
        self.assertEqual((ok, outcome), (True, "quiet"))
        self.assertEqual(row["provider_id"], "chosen-judge")

    async def test_judgment_missing_model_does_not_fall_back(self):
        plugin = self.plugin(proactive_judge_provider_id="missing-judge")
        plugin.context.provider_manager = self._mock_pm({"__global__": ScriptedProvider()})
        decision, judge_ok = await plugin.self_wake.ask_heartbeat_llm(silent_minutes=15)
        self.assertIsNone(decision)
        self.assertFalse(judge_ok)


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


if __name__ == "__main__":
    unittest.main()
