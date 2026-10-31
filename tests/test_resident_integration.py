"""AstrBot 4.29.1 Runner and installed plugin public interfaces, without network."""

import asyncio
import sys
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from astrbot.core.agent.tool import ToolSet
from astrbot.core.provider.entities import LLMResponse, TokenUsage
from quart import Quart

from main import ActivityEntry
from resident.autonomy import CURRENT_RUN, BudgetProvider
from resident_fixtures import ScriptedProvider, build_runner, conversation
from test_plugin_logic import make_plugin, FakeContext, FakeEvent

# 外部插件集成测试：对应插件未安装时整个模块跳过，不阻断默认测试发现。
try:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from astrbot_plugin_astrhe.astrhe.tools.base import AstrHeFunctionTool
    from astrbot_plugin_astrhe.astrhe.tools.reader_tools import ReaderTools
    from astrbot_plugin_astrhe.astrhe.injector import Injector

    _EXTERNAL_IMPORTS_OK = True
except ImportError:
    _EXTERNAL_IMPORTS_OK = False


@unittest.skipUnless(
    _EXTERNAL_IMPORTS_OK, "需要已安装的 astrbot_plugin_astrhe 才能运行集成测试"
)
class ResidentIntegrationTest(unittest.IsolatedAsyncioTestCase):
    def make(self, **config):
        p = make_plugin(
            context=FakeContext(admins=["10001"]),
            proactive_allow_groups=["20001"],
            **config,
        )
        p._record_activity(
            ActivityEntry(
                platform_id="aiocqhttp",
                kind="group",
                conversation_id="20001",
                conversation_name="群",
                sender_id="9",
                sender_name="用户",
                ts=time.time(),
                text="新消息",
            )
        )
        self.addAsyncCleanup(p.terminate)
        return p

    async def wake(self, p, responses, tools, hooks=None, mode="full"):
        provider = ScriptedProvider(responses)
        p._select_self_wake_provider = AsyncMock(return_value=provider)
        p._build_self_wake_agent_config = lambda event: SimpleNamespace(
            tool_schema_mode=mode
        )
        if hooks is not None:
            p.resident.autonomy.dispatch_request_hooks = hooks

        async def build(**kwargs):
            return await build_runner(**kwargs, tools=tools, native_executor=True)

        with (
            patch("main._build_main_agent", build),
            patch("main._get_session_conv", AsyncMock(return_value=conversation())),
        ):
            outcome = await p._run_self_wake_turn(30, 0.5, 0.1)
        return outcome, provider

    @staticmethod
    def call(name, args=None):
        return LLMResponse(
            role="assistant",
            tools_call_name=[name],
            tools_call_args=[args or {}],
            tools_call_ids=["c1"],
            usage=TokenUsage(input_other=10, output=2),
        )

    @staticmethod
    def end():
        return LLMResponse(
            role="assistant",
            completion_text="结束",
            usage=TokenUsage(input_other=10, output=2),
        )

    async def test_native_executor_reads_existing_astrhe_plan_with_secrecy(self):
        p = self.make()
        plans = SimpleNamespace(
            get_latest_plan=AsyncMock(
                return_value={
                    "id": 1,
                    "plan_date": "2026-10-25",
                    "full_text": "私聊中的秘密",
                }
            ),
            get_sections=AsyncMock(
                return_value={"aiocqhttp:GroupMessage:20001": "本群今天读一本书"}
            ),
        )
        reader = ReaderTools(
            SimpleNamespace(plans=plans), p.context, lambda: timezone.utc, lambda: None
        )
        plan_tool = AstrHeFunctionTool(name="astrhe_read_plan", fn=reader._read_plan)
        outcome, provider = await self.wake(
            p,
            [self.call("astrhe_read_plan"), self.end()],
            ToolSet([plan_tool]),
            AsyncMock(return_value=False),
        )
        self.assertEqual(outcome, (True, "quiet"))
        receipt = p.resident.store.rows("SELECT * FROM resident_actions")[0]
        self.assertEqual(receipt["status"], "succeeded")
        self.assertIn("本群今天读一本书", receipt["result"])
        self.assertNotIn("私聊中的秘密", receipt["result"])
        self.assertEqual(len(provider.calls), 2)

    async def test_astrhe_scope_filter_runs_before_runner_reset_and_never_returns(self):
        p = self.make(resident_extra_tools=["future_task"])
        injector = Injector(
            SimpleNamespace(),
            lambda: timezone.utc,
            None,
            lambda: True,
            Mock(),
            access=SimpleNamespace(allows=AsyncMock(return_value=False)),
        )
        read = AstrHeFunctionTool(
            name="astrhe_read_plan", fn=AsyncMock(return_value="不能调用")
        )

        async def hook(event, req):
            await injector.on_llm_request(event, req)
            await p.on_llm_request(event, req)
            return False

        outcome, provider = await self.wake(
            p, [self.call("astrhe_read_plan"), self.end()], ToolSet([read]), hook
        )
        self.assertEqual(outcome, (True, "quiet"))
        self.assertEqual(provider.calls[0]["func_tool"].tools, [])
        read.fn.assert_not_called()

    async def test_skills_like_requery_preserves_parameter_schema_and_guard(self):
        p = self.make()
        update = AstrHeFunctionTool(
            name="nuoxiaomiao_update_state",
            parameters={
                "type": "object",
                "properties": {"mood": {"type": "string"}},
                "required": ["mood"],
            },
            fn=AsyncMock(return_value="已更新"),
        )
        result, provider = await self.wake(
            p,
            [
                self.call(update.name),
                self.call(update.name, {"mood": "平静"}),
                self.end(),
            ],
            ToolSet([update]),
            AsyncMock(return_value=False),
            mode="skills_like",
        )
        self.assertEqual(result, (True, "quiet"))
        update.fn.assert_awaited_once()
        self.assertIn(
            "mood",
            provider.calls[1]["func_tool"]
            .get_tool(update.name)
            .parameters["properties"],
        )
        self.assertEqual(
            p.resident.store.rows("SELECT requests FROM resident_runs")[0]["requests"],
            3,
        )

    async def test_actual_send_receipts_success_false_and_exception(self):
        for delivery, expected in [
            (True, "sent"),
            (False, "failed"),
            (RuntimeError("transport disconnected"), "unknown"),
        ]:
            with self.subTest(expected=expected):
                p = self.make()
                p._generate_in_target_context = AsyncMock(return_value="实际发送内容")
                p.context.send_message = AsyncMock(
                    side_effect=delivery if isinstance(delivery, Exception) else None,
                    return_value=delivery,
                )
                send = AstrHeFunctionTool(name="send_message", fn=p.send_message)
                outcome, _ = await self.wake(
                    p,
                    [self.call("send_message", {"intent": "问候"}), self.end()],
                    ToolSet([send]),
                    AsyncMock(return_value=False),
                )
                receipts = p.resident.store.rows("SELECT * FROM resident_sends")
                self.assertEqual(receipts[0]["status"], expected)
                self.assertEqual(outcome[1], "spoke" if expected == "sent" else "quiet")
                if expected == "unknown":
                    self.assertEqual(
                        p.resident.store.rows("SELECT status FROM resident_runs")[0][
                            "status"
                        ],
                        "unknown",
                    )

    async def test_pause_stop_during_model_prevents_next_tool_and_send(self):
        p = self.make()
        started, finish = asyncio.Event(), asyncio.Event()

        class Slow(ScriptedProvider):
            async def text_chat(self, **kwargs):
                started.set()
                await finish.wait()
                return await super().text_chat(**kwargs)

        provider = Slow([self.call("send_message", {"intent": "不可发送"})])
        send = AstrHeFunctionTool(name="send_message", fn=AsyncMock(return_value="bad"))
        p._select_self_wake_provider = AsyncMock(return_value=provider)
        p.resident.autonomy.dispatch_request_hooks = AsyncMock(return_value=False)

        async def build(**kwargs):
            return await build_runner(
                **kwargs, tools=ToolSet([send]), native_executor=True
            )

        with (
            patch("main._build_main_agent", build),
            patch("main._get_session_conv", AsyncMock(return_value=conversation())),
        ):
            task = asyncio.create_task(p._run_self_wake_turn(30, 0.5, 0.1))
            await asyncio.wait_for(started.wait(), 2)
            rid = next(iter(p.resident.autonomy.runs))
            await p.resident.autonomy.control(rid, "pause")
            finish.set()
            await asyncio.sleep(0.03)
            self.assertFalse(task.done())
            send.fn.assert_not_called()
            await p.resident.autonomy.control(rid, "stop")
            await asyncio.wait_for(task, 2)
            send.fn.assert_not_called()
            self.assertEqual(p.context.sent, [])

    async def test_uncharged_run_recovers_once_without_replaying_unknown_action(self):
        p = self.make()
        store = p.resident.store
        rid = store.new_run("aiocqhttp:GroupMessage:20001", "default", "2000-01-01")
        store.recover()
        self.assertEqual(
            store.rows("SELECT status FROM resident_runs")[0]["status"], "pending"
        )
        await self.wake(p, [self.end()], ToolSet(), AsyncMock(return_value=False))
        self.assertEqual(
            store.rows("SELECT id,charged FROM resident_runs"),
            [{"id": rid, "charged": 1}],
        )
        store.recover()
        self.assertEqual(
            store.rows("SELECT status FROM resident_runs")[0]["status"], "succeeded"
        )

    async def test_daily_usage_tracks_request_day_after_midnight(self):
        p = self.make()
        store = p.resident.store
        rid = store.new_run("x", "default", "2000-01-01")
        store.charge(rid, "2000-01-01", 15)
        store.usage(rid, (3, 100, 4), day="2000-01-02")
        self.assertEqual(store.budget("2000-01-01")["tokens"], 0)
        self.assertEqual(
            store.budget("2000-01-02")["tokens"], 107  # 3+100+4，含缓存（审查 M13）
        )
        self.assertEqual(store.budget("2000-01-02")["input_cached"], 100)

    async def test_deferred_send_keeps_budget_after_restart_without_context_leak(self):
        p = self.make(resident_run_token_limit=100)
        store = p.resident.store
        rid = store.new_run(
            "aiocqhttp:GroupMessage:20001",
            "default",
            datetime.now().strftime("%Y-%m-%d"),
        )
        store.charge(rid, datetime.now().strftime("%Y-%m-%d"), 15)
        store.usage(rid, (100, 0, 0))
        store.finish(rid, "succeeded", "quiet")
        with store.transaction() as db:
            db.execute("INSERT INTO resident_deferred VALUES (?,?)", (42, rid))
        attempts = []

        async def deliver(row):
            provider = BudgetProvider(
                ScriptedProvider(), p.resident.autonomy, CURRENT_RUN.get()
            )
            await provider.text_chat()
            attempts.append(True)

        await p.resident.autonomy.deliver_pending({"id": 42}, deliver)
        self.assertEqual(attempts, [])
        self.assertIsNone(CURRENT_RUN.get())
        self.assertEqual(
            store.rows("SELECT status FROM resident_runs")[0]["status"], "limited"
        )

    async def test_session_plugin_disable_stops_before_model(self):
        p = self.make()
        p.context.get_config = lambda **kwargs: {"plugin_set": ["another_plugin"]}
        provider = ScriptedProvider()
        p._select_self_wake_provider = AsyncMock(return_value=provider)
        self.assertEqual(await p._run_self_wake_turn(30, 0.5, 0.1), (True, "skipped"))
        p._select_self_wake_provider.assert_not_called()
        self.assertEqual(provider.calls, [])

    async def test_api_proposal_conflict_keeps_current_and_review_resolves_unknown(
        self,
    ):
        p = self.make()
        service = p.resident
        persona, base_hash = await service.persona.resolve(FakeEvent())
        proposal = service.store.propose(persona, "少用感叹号", "表达更自然", base_hash)
        app = Quart(__name__)

        async def post(body):
            async with app.test_request_context(
                "/resident",
                method="POST",
                json={"umo": FakeEvent().unified_msg_origin, **body},
            ):
                from quart import g
                g.username = "admin"  # 模拟平台认证（审查 P0-1）
                return (await (await service.mutate()).get_json())["data"]

        saved = await post(
            {"action": "approve", "proposal_id": proposal["id"], "expected_version": 0}
        )
        self.assertEqual(saved["style"]["version"], 1)
        conflict = await post(
            {"action": "rollback", "version": 0, "expected_version": 0}
        )
        self.assertIn("operation_error", conflict)
        self.assertEqual(service.store.style(persona)["body"], "少用感叹号")
        rolled = await post({"action": "rollback", "version": 0, "expected_version": 1})
        self.assertEqual(rolled["style"]["body"], "")

    async def test_host_dispatch_preserves_memory_and_astrna_request_hooks(self):
        from astrbot.core.star.star_handler import (
            StarHandlerMetadata,
            StarHandlerRegistry,
            EventType,
        )
        try:
            from astrbot_plugin_livingmemory.main import LivingMemoryPlugin
            from astrbot_plugin_AstrNa.main import AstrNa
        except ImportError:
            self.skipTest("需要已安装的 livingmemory / AstrNa 插件")

        p = self.make()
        called = []

        async def recall(event, req):
            called.append("memory")
            p._append_temp_context(req, "已有记忆检索结果")

        async def compress(event, req):
            called.append("astrna")
            self.assertEqual(req.system_prompt, "")
            req.contexts = []

        memory = SimpleNamespace(
            _ensure_plugin_ready=AsyncMock(return_value=(True, "")),
            event_handler=SimpleNamespace(handle_memory_recall=recall),
        )
        astrna = SimpleNamespace(runtime=SimpleNamespace(sanitize_request=compress))

        async def memory_hook(event, req):
            await LivingMemoryPlugin.handle_memory_recall(memory, event, req)

        async def astrna_hook(event, req):
            await AstrNa.sanitize_llm_context(astrna, event, req)

        registry = StarHandlerRegistry()
        owners = {}
        for name, handler, priority in [
            ("memory", memory_hook, 0),
            ("astrna", astrna_hook, 1000),
            ("nuoxiaomiao", p.on_llm_request, 100),
        ]:
            owners[name] = SimpleNamespace(
                name=name, display_name=name, activated=True, reserved=False
            )
            registry.append(
                StarHandlerMetadata(
                    event_type=EventType.OnLLMRequestEvent,
                    handler_full_name=name,
                    handler_name=name,
                    handler_module_path=name,
                    handler=handler,
                    event_filters=[],
                    extras_configs={"priority": priority},
                )
            )
        with (
            patch(
                "astrbot.core.pipeline.context_utils.star_handlers_registry", registry
            ),
            patch("astrbot.core.pipeline.context_utils.star_map", owners),
            patch("astrbot.core.star.star_handler.star_map", owners),
        ):
            outcome, provider = await self.wake(p, [self.end()], ToolSet())
        self.assertEqual(outcome, (True, "quiet"))
        self.assertEqual(called, ["astrna", "memory"])
        payload = str(provider.calls[0])
        self.assertIn("已有记忆检索结果", payload)

    async def test_private_wake_reads_target_session_instead_of_admin_sender(self):
        from astrbot.core.cron.events import CronMessageEvent
        from astrbot.core.platform.message_session import MessageSession

        p = self.make(proactive_target_whitelist=["30001"])
        event = CronMessageEvent(
            context=p.context,
            session=MessageSession.from_str("other:FriendMessage:30001"),
            message="醒来",
        )
        event.message_obj.sender.user_id = "10001"
        event.set_extra("nuoxiaomiao_self_wake", True)
        self.assertEqual(p._current_conversation(event), ("other", "private", "30001"))
        target = p._resolve_proactive_target(event, "")
        self.assertEqual(target["id"], "30001")
        self.assertEqual(target["umo"], "other:FriendMessage:30001")

    async def test_group_record_scope_can_select_one_platform_with_same_number(self):
        p = self.make(activity_group_whitelist=["other:GroupMessage:20001"])
        p._record_activity(
            ActivityEntry(
                platform_id="other",
                kind="group",
                conversation_id="20001",
                ts=time.time(),
                sender_id="9",
                text="另一平台",
                sender_name="用户",
                conversation_name="群",
            )
        )
        result = await p.resident.inbox.read(FakeEvent(), mode="unread")
        self.assertEqual([row["platform_id"] for row in result["results"]], ["other"])

    async def test_revoked_plugin_and_dynamic_addition_do_not_expand_current_run(self):
        from resident.autonomy import ScopedExecutor, Run
        from astrbot.core.provider.entities import ProviderRequest
        from resident_fixtures import TestExecutor, tool

        p = self.make(resident_extra_tools=["safe_edit"])
        edit = tool("safe_edit", AsyncMock(return_value="edited"))
        edit.handler_module_path = "loaded.plugin"
        enabled = SimpleNamespace(
            name="devkit",
            activated=True,
            reserved=False,
            star_cls=SimpleNamespace(enabled=True),
        )
        p.context.get_all_stars = lambda: [enabled]
        event = FakeEvent()
        req = ProviderRequest(func_tool=ToolSet([edit]))
        rid = p.resident.store.new_run(
            event.unified_msg_origin, "default", datetime.now().strftime("%Y-%m-%d")
        )
        run = Run(rid, event, "default", request=req)
        with patch("astrbot.core.star.star.star_map", {"loaded.plugin": enabled}):
            p.resident.autonomy.restrict_run(run)
            enabled.activated = False
            executor = ScopedExecutor(TestExecutor(), p.resident.autonomy, run)
            results = [
                result async for result in executor.execute(edit, SimpleNamespace())
            ]
        self.assertTrue(results[0].isError)
        edit.handler.assert_not_called()
        enabled.activated = True
        req.func_tool = ToolSet([edit])
        p.resident.autonomy.restrict_run(run)
        self.assertEqual(req.func_tool.tools, [])
