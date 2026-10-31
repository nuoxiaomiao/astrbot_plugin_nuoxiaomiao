import json
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from astrbot.core.agent.tool import ToolSet
from astrbot.core.provider.entities import LLMResponse, ProviderRequest, TokenUsage

from main import ActivityEntry, SELF_WAKE_EVENT_EXTRA
from resident.autonomy import (
    Run,
    ScopedExecutor,
    BudgetProvider,
    BudgetExceeded,
)
from resident.store import Conflict, migrate
from test_plugin_logic import make_plugin, FakeContext, FakeEvent
from resident_fixtures import (
    ScriptedProvider,
    build_runner,
    conversation,
    tool,
    TestExecutor,
)


class ResidentTest(unittest.IsolatedAsyncioTestCase):
    def make(self, **cfg):
        p = make_plugin(context=FakeContext(admins=["10001"]), **cfg)
        self.addAsyncCleanup(p.terminate)
        return p

    def record(
        self,
        p,
        text="内容",
        mentioned=False,
        platform="aiocqhttp",
        kind="group",
        conv="20001",
        mid="",
        sender="9",
    ):
        return p._record_activity(
            ActivityEntry(
                ts=time.time(),
                sender_id=sender,
                sender_name="某人",
                conversation_id=conv,
                conversation_name=conv,
                kind=kind,
                text=text,
                platform_id=platform,
                message_id=mid,
                mentioned=mentioned,
            )
        )

    async def test_migration_preserves_old_rows_and_does_not_create_unread_backlog(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "old.db")
            db = sqlite3.connect(path)
            db.execute(
                "CREATE TABLE activity_messages(id INTEGER PRIMARY KEY AUTOINCREMENT,platform_id TEXT,kind TEXT,conversation_id TEXT,conversation_name TEXT,sender_id TEXT,sender_name TEXT,text TEXT,ts REAL)"
            )
            db.execute(
                "INSERT INTO activity_messages VALUES (1,'a','group','2','g','u','n','old',1)"
            )
            db.commit()
            migrate(db, path)
            migrate(db, path)
            self.assertEqual(
                db.execute("SELECT text FROM activity_messages").fetchone()[0], "old"
            )
            self.assertEqual(
                db.execute(
                    "SELECT value FROM resident_meta WHERE key='inbox_baseline'"
                ).fetchone()[0],
                "1",
            )
            self.assertTrue(Path(path + ".bak-resident-v1").exists())
            db.close()

    async def test_mentions_only_mark_the_returned_context(self):
        p = self.make()
        for i in range(25):
            self.record(p, str(i), mentioned=i == 20)
        event = FakeEvent()
        data = await p.resident.inbox.read(
            event, conversation="20001", mode="mentions", mark_read=True
        )
        self.assertEqual(data["count"], 11)
        remaining = await p.resident.inbox.read(
            event, conversation="20001", mode="unread"
        )
        self.assertEqual(remaining["count"], 14)
        self.assertNotIn("20", [r["text"] for r in remaining["results"]])

    async def test_history_pagination_never_marks_read(self):
        p = self.make()
        for i in range(12):
            self.record(p, str(i))
        event = FakeEvent()
        first = await p.resident.inbox.read(event, conversation="20001", limit=5)
        second = await p.resident.inbox.read(
            event, conversation="20001", limit=5, before_id=first["next_before_id"]
        )
        self.assertTrue(first["has_more"])
        self.assertFalse(
            {r["id"] for r in first["results"]} & {r["id"] for r in second["results"]}
        )
        unread = await p.resident.inbox.read(event, conversation="20001", mode="unread")
        self.assertEqual(unread["count"], 12)

    async def test_duplicate_platform_message_and_same_number_other_platform(self):
        p = self.make()
        a = self.record(p, mid="1")
        b = self.record(p, mid="1")
        self.assertEqual(a, b)
        self.record(p, platform="other", mid="1")
        result = await p.resident.inbox.read(
            FakeEvent(),
            conversation="other:GroupMessage:20001",
            mode="unread",
            mark_read=True,
        )
        self.assertEqual(result["count"], 1)
        remaining = await p.resident.inbox.read(
            FakeEvent(), conversation="aiocqhttp:GroupMessage:20001", mode="unread"
        )
        self.assertEqual(remaining["count"], 1)
        with self.assertRaises(ValueError):
            await p.resident.inbox.read(FakeEvent(), conversation="20001")

    async def test_privacy_and_changed_record_scope(self):
        p = self.make()
        self.record(p, kind="private", conv="30001")
        self.record(p)
        event = FakeEvent()
        event.set_extra(SELF_WAKE_EVENT_EXTRA, True)
        data = await p.resident.inbox.read(event, mode="unread")
        self.assertEqual([r["kind"] for r in data["results"]], ["group"])
        p.activity_group_blacklist = {"20001"}
        self.assertEqual(
            (await p.resident.inbox.read(event, mode="unread"))["count"], 0
        )

    async def test_successful_merged_request_marks_only_source_messages(self):
        p = self.make(debounce_enabled=False)
        self.record(p, mid="1")
        self.record(p, mid="2")
        self.record(p, mid="3")
        event = FakeEvent(message_id="synthetic")
        event.set_extra("nuoxiaomiao_merged_source_ids", ["1", "2"])
        req = ProviderRequest()
        await p.on_llm_request(event, req)
        await p.on_llm_response(event, LLMResponse(role="err"))
        self.assertEqual(
            (await p.resident.inbox.read(event, conversation="20001", mode="unread"))[
                "count"
            ],
            3,
        )
        await p.on_llm_response(
            event, LLMResponse(role="assistant", completion_text="好")
        )
        self.assertEqual(
            (await p.resident.inbox.read(event, conversation="20001", mode="unread"))[
                "count"
            ],
            1,
        )

    async def test_state_scope_and_style_approval_conflicts(self):
        p = self.make()
        s = p.resident.store
        s.update_state("a", "p:GroupMessage:1", "开心", 80, "甲的私事")
        self.assertEqual(s.state("a", "p:GroupMessage:2")["mood"]["mood"], "开心")
        self.assertEqual(s.state("a", "p:GroupMessage:2")["focus"], "")
        self.assertIsNone(s.state("b", "p:GroupMessage:1")["mood"])
        with self.assertRaises(ValueError):
            s.update_state("a", "x", "甲的私事", 50, "")
        proposal = s.propose("a", "更简短", "平常闲聊", "hash")
        self.assertEqual(s.style("a")["body"], "")
        with self.assertRaises(Conflict):
            s.change_style("a", "approve", 0, proposal["id"], base_hash="changed")
        s.change_style("a", "approve", 0, proposal["id"], base_hash="hash")
        with self.assertRaises(Conflict):
            s.change_style("a", "rollback", 0, version=0)
        s.change_style("a", "rollback", 1, version=0)
        self.assertEqual(s.style("a"), {"version": 2, "body": ""})

    async def test_state_injection_preserves_prompt_and_conversation_history(self):
        p = self.make()
        event = FakeEvent()
        p.resident.store.update_state(
            "default", event.unified_msg_origin, "专注", 50, "本群关注点"
        )
        req = ProviderRequest()
        req.system_prompt = "原始人设"
        req.contexts = [{"role": "user", "content": "旧消息"}]
        await p.resident.persona.inject(event, req)
        self.assertEqual(req.system_prompt, "原始人设")
        self.assertEqual(req.contexts, [{"role": "user", "content": "旧消息"}])
        self.assertIn("本群关注点", req.extra_user_content_parts[-1].text)

    async def test_tools_only_shrink_and_nested_dispatch_is_unavailable(self):
        p = self.make(resident_extra_tools=["safe_edit", "astrna_parallel_tool_use"])

        async def ok(event):
            return "ok"

        read = tool("read_conversation_messages", ok)
        edit = tool("safe_edit", ok)
        nested = tool("astrna_parallel_tool_use", ok)
        req = ProviderRequest()
        req.func_tool = ToolSet([read, edit, nested])
        p.resident.integration.restrict(FakeEvent(), req)
        self.assertEqual({t.name for t in req.func_tool.tools}, {read.name, edit.name})
        req.func_tool.remove_tool("safe_edit")
        p.resident.integration.restrict(FakeEvent(), req)
        self.assertEqual([t.name for t in req.func_tool.tools], [read.name])
        run = Run(
            p.resident.store.new_run("x", "default", "today"),
            FakeEvent(),
            "default",
            request=req,
        )
        executor = ScopedExecutor(TestExecutor(), p.resident.autonomy, run)
        outputs = [
            r
            async for r in executor.execute(
                edit, SimpleNamespace(context=SimpleNamespace(event=run.event))
            )
        ]
        self.assertTrue(outputs[0].isError)

    async def test_selection_migration_freezes_names_and_backup(self):
        p = self.make(proactive_silent_wake_tool_plugins=["dev"])
        p.resident.integration.catalog = lambda: [
            {
                "name": "edit",
                "plugin": "dev",
                "active": True,
                "selectable": True,
                "base": False,
            }
        ]
        p.resident.integration.migrate_selection()
        self.assertEqual(p.resident_extra_tools, {"edit"})
        p.resident.integration.catalog = lambda: [
            {
                "name": "new_tool",
                "plugin": "dev",
                "active": True,
                "selectable": True,
                "base": False,
            }
        ]
        p.resident.integration.migrate_selection()
        self.assertEqual(p.resident_extra_tools, {"edit"})
        self.assertTrue(
            (
                Path(p._activity_db_file()).parent / "config.before-resident-v1.json"
            ).exists()
        )

    async def test_failed_request_is_charged_and_survives_reopen(self):
        p = self.make(proactive_silent_wake_daily_budget=1)
        rid = p.resident.store.new_run(
            "x", "default", datetime.now().strftime("%Y-%m-%d")
        )
        run = Run(rid, FakeEvent(), "default")
        provider = BudgetProvider(
            ScriptedProvider([RuntimeError("失败")]), p.resident.autonomy, run
        )
        with self.assertRaises(RuntimeError):
            await provider.text_chat()
        p._close_activity_db()
        self.assertFalse(p._silent_wake_budget_left())
        p.resident.store.recover()
        row = p.resident.store.rows("SELECT * FROM resident_runs WHERE id=?", (rid,))[0]
        self.assertEqual(row["status"], "unknown")
        self.assertEqual(row["usage_unknown"], 1)

    async def test_usage_missing_blocks_next_request_with_limit(self):
        p = self.make(resident_run_token_limit=100)
        rid = p.resident.store.new_run(
            "x", "default", datetime.now().strftime("%Y-%m-%d")
        )
        run = Run(rid, FakeEvent(), "default")
        underlying = ScriptedProvider(
            [LLMResponse(role="assistant", completion_text="无用量")]
        )
        provider = BudgetProvider(underlying, p.resident.autonomy, run)
        await provider.text_chat()
        with self.assertRaises(BudgetExceeded):
            await provider.text_chat()
        self.assertEqual(len(underlying.calls), 1)

    async def test_budget_counts_uncached_only_and_no_double_charge(self):
        p = self.make(resident_run_token_limit=10)
        rid = p.resident.store.new_run(
            "x", "default", datetime.now().strftime("%Y-%m-%d")
        )
        run = Run(rid, FakeEvent(), "default")
        provider = BudgetProvider(
            ScriptedProvider(
                [
                    LLMResponse(
                        role="assistant",
                        usage=TokenUsage(input_other=8, input_cached=1000, output=3),
                    )
                ]
            ),
            p.resident.autonomy,
            run,
        )
        await provider.text_chat()
        with self.assertRaises(BudgetExceeded):
            await provider.text_chat()
        self.assertEqual(
            p.resident.store.budget(datetime.now().strftime("%Y-%m-%d"))["tokens"],
            1011,  # 8 input_other + 1000 input_cached + 3 output（审查 M13 计缓存）
        )
        self.assertEqual(
            p.resident.store.budget(datetime.now().strftime("%Y-%m-%d"))["used"], 1
        )

    async def test_recover_never_replays_unknown_tools(self):
        p = self.make()
        s = p.resident.store
        rid = s.new_run("x", "default", datetime.now().strftime("%Y-%m-%d"))
        s.charge(rid, datetime.now().strftime("%Y-%m-%d"), 5)
        aid = s.action(rid, "safe_edit", {"path": "demo"})
        s.recover()
        s.recover()
        self.assertEqual(
            s.rows("SELECT status FROM resident_actions WHERE id=?", (aid,))[0][
                "status"
            ],
            "unknown",
        )
        self.assertEqual(p.context.sent, [])

    async def test_real_runner_receipts_distinguish_failed_send_from_spoke(self):
        p = self.make(proactive_allow_groups=["20001"])
        self.record(p)
        provider = ScriptedProvider(
            [
                LLMResponse(
                    role="assistant",
                    tools_call_name=["send_message"],
                    tools_call_args=[{"intent": "你好"}],
                    tools_call_ids=["c1"],
                    usage=TokenUsage(input_other=10, output=2),
                ),
                LLMResponse(
                    role="assistant",
                    completion_text="结束",
                    usage=TokenUsage(input_other=20, output=3),
                ),
            ]
        )
        p._select_self_wake_provider = AsyncMock(return_value=provider)

        async def failed(event, intent):
            return json.dumps({"error": "目标不允许"})

        tools = ToolSet(
            [
                tool(
                    "send_message",
                    failed,
                    {"type": "object", "properties": {"intent": {"type": "string"}}},
                )
            ]
        )

        async def build(**kwargs):
            return await build_runner(**kwargs, tools=tools, hooks=p.on_llm_request)

        with (
            patch("main._get_session_conv", AsyncMock(return_value=conversation())),
            patch("main._build_main_agent", build),
        ):
            ok, outcome = await p._run_self_wake_turn(30, 0.5, 0.1)
        self.assertTrue(ok)
        self.assertEqual(outcome, "quiet")
        rows = p.resident.store.rows("SELECT * FROM resident_actions")
        self.assertEqual(rows[0]["status"], "failed")
        self.assertEqual(
            p.resident.store.budget(datetime.now().strftime("%Y-%m-%d"))["used"], 1
        )
        self.assertEqual(len(provider.calls), 2)
