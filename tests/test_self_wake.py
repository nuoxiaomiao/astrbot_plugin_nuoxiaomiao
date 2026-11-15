"""Silent wake probability/budget, self-wake session identity, and autonomous turn telemetry tests."""

from __future__ import annotations

import json
import random
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from astrbot.api.provider import ProviderRequest
from astrbot.core.message.components import Plain
from astrbot.core.message.message_event_result import MessageChain

from infra.constants import ActivityEntry, SELF_WAKE_EVENT_EXTRA
from tests.fakes import FakeContext, FakeEvent, FakeProvider, make_proactive_plugin


class SilentWakeTest(unittest.IsolatedAsyncioTestCase):
    """静默自醒：概率随安静上升，floor/ceil 兜住两端，预算与复位语义正确。"""

    def _plugin(self, **cfg):
        return make_proactive_plugin(context=FakeContext(admins=["10001"]), **cfg)

    async def test_never_before_floor(self):
        plugin = self._plugin()

        for roll in (0.0, 0.5, 0.99):
            hit, probability = plugin.self_wake.should_trigger(4.9, roll)
            self.assertFalse(hit)
            self.assertEqual(probability, 0.0)
        await plugin.terminate()

    async def test_always_at_ceil(self):
        plugin = self._plugin()

        hit, probability = plugin.self_wake.should_trigger(60.0, 0.999999)
        self.assertTrue(hit)
        self.assertEqual(probability, 1.0)
        await plugin.terminate()

    async def test_probability_rises_with_quiet(self):
        plugin = self._plugin()
        _, p_low = plugin.self_wake.should_trigger(10.0, 1.0)
        _, p_mid = plugin.self_wake.should_trigger(30.0, 1.0)
        _, p_high = plugin.self_wake.should_trigger(50.0, 1.0)

        self.assertGreater(p_mid, p_low)
        self.assertGreater(p_high, p_mid)
        await plugin.terminate()

    async def test_alpha_solves_target_mean(self):
        plugin = self._plugin()
        floor = plugin.proactive_silent_wake_floor_minutes
        ceil = plugin.proactive_silent_wake_ceil_minutes
        target = plugin.proactive_silent_wake_target_mean_minutes
        rng = random.Random(42)
        times = []
        for _ in range(800):
            t = floor
            while t < ceil:
                hit, _probability = plugin.self_wake.should_trigger(t, rng.random())
                if hit:
                    break
                t += 1
            times.append(t)
        mean = sum(times) / len(times)

        self.assertGreaterEqual(mean, target * 0.8)
        self.assertLessEqual(mean, target * 1.2)
        await plugin.terminate()

    async def test_budget_exhaustion_and_daily_reset(self):
        plugin = self._plugin(proactive_silent_wake_daily_budget=1)

        self.assertTrue(plugin.self_wake.budget_left())
        day = time.strftime("%Y-%m-%d")
        rid = plugin.resident.store.new_run("test", "default", day)
        plugin.resident.store.charge(rid, day, 1)
        self.assertFalse(plugin.self_wake.budget_left())
        with plugin.resident.store.transaction() as db:
            db.execute("UPDATE resident_runs SET day='2000-01-01' WHERE id=?", (rid,))
        self.assertTrue(plugin.self_wake.budget_left())
        self.assertEqual(plugin.resident.store.budget(day)["used"], 0)
        await plugin.terminate()

    async def test_prompt_carries_silent_context(self):
        plugin = self._plugin()

        prompt = plugin.self_wake.build_heartbeat_prompt(silent_minutes=42)

        self.assertIn("静默自醒", prompt)
        self.assertIn("42", prompt)
        self.assertIn("want", prompt)
        await plugin.terminate()

    async def test_judge_uses_configured_provider_id(self):
        provider = FakeProvider(
            text=json.dumps(
                {"want": True, "target_kind": "private", "target": "10002", "note": "x"},
                ensure_ascii=False,
            )
        )
        context = FakeContext(admins=["10001"], provider=provider)
        plugin = make_proactive_plugin(
            context=context,
            proactive_target_whitelist=["10002"],
            proactive_judge_provider_id="my-model",
        )

        decision, judge_ok = await plugin.self_wake.ask_heartbeat_llm(silent_minutes=5)

        manager = plugin.context.provider_manager
        self.assertIn("my-model", manager.fetched_ids)
        self.assertTrue(judge_ok)
        self.assertTrue(decision and decision.get("want"))
        await plugin.terminate()

    async def test_judge_failure_returns_not_ok(self):
        provider = FakeProvider(raise_error=True)
        context = FakeContext(admins=["10001"], provider=provider)
        plugin = make_proactive_plugin(context=context)

        decision, judge_ok = await plugin.self_wake.ask_heartbeat_llm()

        self.assertIsNone(decision)
        self.assertFalse(judge_ok)
        await plugin.terminate()

    async def test_watcher_resets_quiet_timer(self):
        plugin = self._plugin()
        plugin.self_wake._last_human_ts = None

        await plugin._proactive_reply_watcher(
            FakeEvent(
                is_private=True,
                session="aiocqhttp:FriendMessage:10001",
                group_id="",
                sender_id="10001",
                sender_name="主人",
                message_str="我回来啦",
            )
        )

        self.assertIsNotNone(plugin.self_wake._last_human_ts)
        self.assertGreater(plugin.self_wake._last_human_ts, time.time() - 5)
        await plugin.terminate()

    async def test_baseline_falls_back_to_now_on_empty_db(self):
        plugin = self._plugin()
        plugin.self_wake._last_human_ts = None

        plugin.self_wake.init_baseline()

        self.assertIsNotNone(plugin.self_wake._last_human_ts)
        self.assertGreater(plugin.self_wake._last_human_ts, time.time() - 60)
        await plugin.terminate()

    async def test_ask_llm_without_provider_manager_is_failure(self):
        context = FakeContext(admins=["10001"])
        context.provider_manager = None
        plugin = make_proactive_plugin(context=context)

        decision, judge_ok = await plugin.self_wake.ask_heartbeat_llm()

        self.assertIsNone(decision)
        self.assertFalse(judge_ok)
        await plugin.terminate()


class SelfWakeIdentityTest(unittest.IsolatedAsyncioTestCase):
    """自醒轮次：事件身份要跟锚定会话一致，记账要能看见工具调用。"""

    def _plugin(self, **cfg):
        return make_proactive_plugin(
            context=FakeContext(admins=["10001"]), **cfg
        )

    async def test_pick_session_uses_dedicated_virtual_session(self):
        from infra.constants import SELF_WAKE_UMO

        plugin = self._plugin(proactive_allow_groups=["20001"])
        picked = await plugin.self_wake.pick_session()
        self.assertIsNotNone(picked)
        self.assertEqual(picked["platform_id"], "nuoxiaomiao")
        self.assertEqual(picked["umo"], SELF_WAKE_UMO)
        await plugin.terminate()

    async def test_on_llm_response_skips_self_wake_events(self):
        plugin = self._plugin()
        plugin.media.cleanup_expired = AsyncMock()
        wake_event = FakeEvent(message_str="", sender_name="Scheduler")
        wake_event.set_extra(SELF_WAKE_EVENT_EXTRA, True)
        chat_event = FakeEvent(message_str="", sender_name="测试用户")
        response = SimpleNamespace(completion_text="一句话", tools_call_name=None)

        await plugin.on_llm_response(wake_event, response)
        self.assertEqual(plugin.activity.activity_sessions(), [])

        await plugin.on_llm_response(chat_event, response)
        self.assertEqual(plugin.activity.activity_sessions(), [])
        plugin.send_observer.ensure_send_observer(chat_event)
        await chat_event.send(MessageChain([Plain(response.completion_text)]))
        self.assertEqual(len(plugin.activity.activity_sessions()), 1)
        await plugin.terminate()

    async def test_on_llm_request_applies_kept_toolset_for_wake_events(self):
        from tests.resident_fixtures import tool
        from astrbot.core.agent.tool import ToolSet

        plugin = self._plugin()
        allowed = tool("read_conversation_messages", AsyncMock())
        event = FakeEvent(message_str="醒来")
        event.set_extra(SELF_WAKE_EVENT_EXTRA, True)
        req = ProviderRequest()
        req.func_tool = ToolSet(tools=[allowed, tool("unselected", AsyncMock())])

        await plugin.on_llm_request(event, req)

        self.assertEqual(req.func_tool.tools, [allowed])
        self.assertFalse(event.stopped)
        await plugin.terminate()

    async def test_wake_turn_anchors_group_with_admin_identity_and_accounts_tools(self):
        plugin = self._plugin(proactive_allow_groups=["20001"])
        plugin.activity.record_activity(
            ActivityEntry(
                platform_id="aiocqhttp",
                ts=time.time() - 60,
                sender_id="9",
                sender_name="路人",
                conversation_id="20001",
                conversation_name="猫娘群",
                kind="group",
                text="大家好",
            )
        )
        wake_events = []

        from tests.resident_fixtures import ScriptedProvider, build_runner, conversation, tool
        from astrbot.core.agent.tool import ToolSet
        from astrbot.core.provider.entities import LLMResponse, TokenUsage

        provider = ScriptedProvider([
            LLMResponse(
                role="assistant",
                tools_call_name=["send_message"],
                tools_call_ids=["call-1"],
                tools_call_args=[{"intent": "找楠酱说句话"}],
                usage=TokenUsage(input_other=10, output=2),
            ),
            LLMResponse(
                role="assistant",
                completion_text="看完了",
                usage=TokenUsage(input_other=10, output=2),
            ),
        ])
        plugin.self_wake.select_provider = AsyncMock(return_value=provider)

        async def rejected(event, **kwargs):
            return '{"error":"目标不可用"}'

        async def fake_build_main_agent(**kwargs):
            wake_events.append(kwargs["event"])
            return await build_runner(**kwargs, tools=ToolSet(tools=[tool("send_message", rejected)]))

        async def fake_get_session_conv(event, context):
            return conversation()

        plugin.resident.autonomy.dispatch_request_hooks = AsyncMock(return_value=False)
        plugin._build_main_agent_fn = fake_build_main_agent
        plugin._get_session_conv_fn = fake_get_session_conv

        ok, outcome = await plugin.self_wake.run_turn(30.0, 0.5, 0.1)

        self.assertTrue(ok)
        self.assertEqual(outcome, "quiet")
        from infra.constants import SELF_WAKE_UMO

        event = wake_events[0]
        self.assertTrue(event.is_private_chat())
        self.assertEqual(event.unified_msg_origin, SELF_WAKE_UMO)
        self.assertEqual(event.get_sender_id(), "10001")
        conn = plugin.db.get_connection()
        run_row = conn.execute(
            "SELECT id, requests, outcome, meta FROM resident_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(run_row[1], 2)
        self.assertEqual(run_row[2], "quiet")
        meta = json.loads(run_row[3])
        self.assertEqual(meta["steps"], 2)
        self.assertTrue(any(tc["name"] == "send_message" for tc in meta["tool_calls"]))
        action_rows = conn.execute(
            "SELECT tool, status FROM resident_actions WHERE run_id = ?", (run_row[0],)
        ).fetchall()
        self.assertEqual(len(action_rows), 1)
        self.assertEqual(action_rows[0][0], "send_message")
        await plugin.terminate()


class SelfWakeTurnTest(unittest.IsolatedAsyncioTestCase):
    """静默自醒：醒来起一轮自主活动（可调工具、可发言），并把经过落库到 resident_runs。"""

    def _plugin(self, **cfg):
        return make_proactive_plugin(context=FakeContext(admins=["10001"]), **cfg)

    async def test_resident_runs_table_created(self):
        plugin = self._plugin()
        conn = plugin.db.get_connection()
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='resident_runs'"
        ).fetchall()
        self.assertEqual(len(rows), 1)
        await plugin.terminate()

    async def test_record_run_meta_persists(self):
        plugin = self._plugin()
        plugin.self_wake.record_run_meta(
            {
                "started_ts": 1000.0,
                "ended_ts": 1005.0,
                "quiet_minutes": 25.0,
                "probability": 0.05,
                "roll": 0.01,
                "steps": 3,
                "tool_calls": [{"name": "list_conversations"}],
                "said": True,
                "sent_to": "aiocqhttp:FriendMessage:10001",
                "said_text": "在忙吗",
                "thinking": "看了一眼，想问问",
                "outcome": "spoke",
            }
        )
        conn = plugin.db.get_connection()
        row = conn.execute(
            "SELECT requests, outcome, summary, meta "
            "FROM resident_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], 3)
        self.assertEqual(row[1], "spoke")
        self.assertIn("看了一眼", row[2])
        meta = json.loads(row[3])
        self.assertAlmostEqual(meta["quiet_minutes"], 25.0)
        self.assertEqual(meta["steps"], 3)
        self.assertEqual(meta["said_text"], "在忙吗")
        self.assertEqual(meta["sent_to"], "aiocqhttp:FriendMessage:10001")
        self.assertIn("list_conversations", json.dumps(meta["tool_calls"]))
        await plugin.terminate()

    async def test_prompt_does_not_ask_for_json(self):
        plugin = self._plugin()
        text = plugin.self_wake.self_wake_prompt(20.0)
        self.assertIn("不是真人提出的新要求", text)
        self.assertIn("安静结束", text)
        self.assertIn("send_message", text)
        self.assertNotIn("want", text)
        self.assertNotIn('"target_kind"', text)
        await plugin.terminate()

    async def test_no_admin_skips_without_budget(self):
        plugin = self._plugin(
            proactive_allow_groups=[],
            proactive_target_whitelist=[],
        )
        plugin.activity.admin_ids = lambda: set()
        day = time.strftime("%Y-%m-%d")
        before = plugin.resident.store.budget(day)["used"]
        ok, outcome = await plugin.self_wake.run_turn(30.0, 0.1, 0.01)
        self.assertTrue(ok)
        self.assertEqual(outcome, "skipped")
        plugin.self_wake.settle_turn(ok, outcome, time.time())
        self.assertEqual(plugin.resident.store.budget(day)["used"], before)
        await plugin.terminate()

    async def test_settle_self_wake_turn_semantics(self):
        plugin = self._plugin()
        day = time.strftime("%Y-%m-%d")

        plugin.self_wake._fail_until = 0.0
        plugin.self_wake._last_human_ts = 1000.0
        plugin.self_wake.settle_turn(False, "error", time.time())
        self.assertGreater(plugin.self_wake._fail_until, time.time() + 1700)
        self.assertEqual(plugin.resident.store.budget(day)["used"], 0)
        self.assertEqual(plugin.self_wake._last_human_ts, 1000.0)

        plugin.self_wake.settle_turn(True, "skipped", time.time())
        self.assertEqual(plugin.resident.store.budget(day)["used"], 0)
        self.assertEqual(plugin.self_wake._last_human_ts, 1000.0)

        used = plugin.resident.store.budget(day)["used"]
        plugin.self_wake.settle_turn(True, "quiet", time.time())
        self.assertEqual(plugin.resident.store.budget(day)["used"], used)
        await plugin.terminate()

    async def test_self_wake_skips_when_no_admins_configured(self):
        plugin = self._plugin(proactive_allow_groups=["90000"])
        plugin.activity.admin_ids = lambda: set()

        day = time.strftime("%Y-%m-%d")
        before = plugin.resident.store.budget(day)["used"]
        ok, outcome = await plugin.self_wake.run_turn(30.0, 0.1, 0.01)
        self.assertTrue(ok)
        self.assertEqual(outcome, "skipped")
        plugin.self_wake.settle_turn(ok, outcome, time.time())
        self.assertEqual(plugin.resident.store.budget(day)["used"], before)
        conn = plugin.db.get_connection()
        row = conn.execute(
            "SELECT outcome FROM resident_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(row[0], "skipped")
        await plugin.terminate()

    async def test_allow_tools_off_uses_fallback(self):
        plugin = self._plugin(
            proactive_silent_wake_allow_tools=False,
            proactive_allow_groups=["90000"],
        )
        called = {"fallback": 0}

        async def fake_fallback(row, quiet):
            called["fallback"] += 1
            row["outcome"] = "quiet"
            row["ended_ts"] = 1.0
            plugin.self_wake.record_run_meta(row)
            return True, "quiet"

        plugin.self_wake.run_fallback = fake_fallback
        plugin._get_session_conv_fn = AsyncMock(return_value=SimpleNamespace())
        ok, outcome = await plugin.self_wake.run_turn(30.0, 0.1, 0.01)
        self.assertTrue(ok)
        self.assertEqual(outcome, "quiet")
        self.assertEqual(called["fallback"], 1)
        await plugin.terminate()

    async def test_fallback_records_spoke_when_decision(self):
        plugin = self._plugin()

        async def fake_ask(silent_minutes=None):
            return (
                {"want": True, "target_kind": "private", "target": "10001",
                 "note": "想问问"},
                True,
            )

        delivered = {"n": 0, "result": {"status": "sent"}}

        async def fake_deliver(decision):
            delivered["n"] += 1
            return delivered["result"]

        plugin.self_wake.ask_heartbeat_llm = fake_ask
        plugin.self_wake.deliver_heartbeat_decision = fake_deliver
        row = {"started_ts": 1.0}
        ok, outcome = await plugin.self_wake.run_fallback(row, 25.0)
        self.assertTrue(ok)
        self.assertEqual(delivered["n"], 1)
        self.assertEqual(row["outcome"], "spoke")
        self.assertTrue(row["said"])

        delivered["result"] = {"status": "skipped", "error": "目标不在白名单"}
        row2 = {"started_ts": 2.0}
        ok2, outcome2 = await plugin.self_wake.run_fallback(row2, 25.0)
        self.assertTrue(ok2)
        self.assertEqual(outcome2, "skipped")
        self.assertEqual(row2["outcome"], "skipped")
        self.assertNotIn("said", row2)

        delivered["result"] = {"status": "failed", "error": "平台发送失败"}
        row3 = {"started_ts": 3.0}
        ok3, outcome3 = await plugin.self_wake.run_fallback(row3, 25.0)
        self.assertFalse(ok3)
        self.assertEqual(outcome3, "error")
        self.assertEqual(row3["outcome"], "error")
        await plugin.terminate()

    async def test_pick_session_returns_self_wake_umo_independent_of_allowlist(self):
        from infra.constants import SELF_WAKE_UMO

        plugin = self._plugin(proactive_allow_groups=["90000"])
        picked = await plugin.self_wake.pick_session()
        self.assertIsNotNone(picked)
        self.assertEqual(picked["umo"], SELF_WAKE_UMO)
        await plugin.terminate()

    async def test_config_defaults(self):
        plugin = self._plugin()
        self.assertEqual(plugin.proactive_silent_wake_max_steps, 8)
        self.assertTrue(plugin.proactive_silent_wake_allow_tools)
        await plugin.terminate()

    async def test_max_steps_clamped(self):
        plugin = self._plugin(proactive_silent_wake_max_steps=999)
        self.assertEqual(plugin.proactive_silent_wake_max_steps, 30)
        await plugin.terminate()
