"""Targeted regression tests covering all P0/P1 refactoring fixes."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import time
import unittest
from unittest.mock import AsyncMock, patch

from astrbot.core.agent.tool import ToolSet
from astrbot.core.message.components import At, Image, Plain
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.provider.entities import LLMResponse, TokenUsage
from quart import Quart

from infra.database import ActivityDatabase, SCHEMA_VERSION
from infra.media import is_safe_download_url, looks_like_image
import qq_official
from tests.fakes import FakeContext, FakeEvent, make_plugin, make_proactive_plugin
from resident.autonomy_budget import Run, RunStopped
from resident.autonomy_sandbox import ScopedExecutor
from tests.resident_fixtures import ScriptedProvider, build_runner, conversation, tool


class _QQOfficialEvent(FakeEvent):
    def get_platform_name(self):
        return "qq_official"

    def get_platform_id(self):
        return "qq_official"


class BrainLockAndSendOrderRegressionTest(unittest.IsolatedAsyncioTestCase):
    async def test_qq_official_one_shot_does_not_deadlock_under_astrna(self):
        round_ctxvar = __import__("contextvars").ContextVar("_CURRENT_SEND_ROUND", default=None)

        class FakeAstrNaModule:
            _installed = True

            def __init__(self):
                self._locks = {}

            def get_group_send_lock(self, key):
                return self._locks.setdefault(key, asyncio.Lock())

        mod = FakeAstrNaModule()
        sys.modules[FakeAstrNaModule.__module__]._CURRENT_SEND_ROUND = round_ctxvar
        star_cls = SimpleNamespace(runtime=SimpleNamespace(group_sender_concurrency=mod))
        ctx = FakeContext()
        ctx.get_registered_star = lambda name: SimpleNamespace(star_cls=star_cls) if name == "astrbot_plugin_AstrNa" else None
        plugin = make_plugin(context=ctx)
        plugin.send_order.refresh()

        GroupMessage = type("GroupMessage", (), {})
        event = _QQOfficialEvent(message_str="hi")
        event.message_obj.raw_message = GroupMessage()

        send_calls = []

        async def astrna_send(msg):
            send_calls.append(msg)
            self.assertTrue(mod.get_group_send_lock(event.unified_msg_origin).locked())
            self.assertTrue(plugin.send_order._is_task_holding(event.unified_msg_origin, asyncio.current_task()))

            async def check_child():
                return plugin.send_order._is_task_holding(event.unified_msg_origin, asyncio.current_task())

            self.assertFalse(await asyncio.create_task(check_child()))
            async with plugin.send_order.one_shot(event.unified_msg_origin, event=event):
                return True

        event.send = astrna_send
        chain = MessageChain([Plain("hello")])

        async def run():
            async with plugin.send_order.one_shot(event.unified_msg_origin, event=event):
                return await plugin.send_observer.qq_official_send(event, chain)

        ok = await asyncio.wait_for(run(), timeout=1.0)
        self.assertTrue(ok)
        self.assertEqual(len(send_calls), 1)
        self.assertFalse(mod.get_group_send_lock(event.unified_msg_origin).locked())

        # Also when event.send carries AstrNa's patch marker, one_shot yields to AstrNa instead of double-locking.
        patched_calls = []

        async def patched_send(msg):
            patched_calls.append(msg)
            async with mod.get_group_send_lock(event.unified_msg_origin):
                return True

        patched_send._astrna_group_sender_concurrency_send_patch = True
        event.send = patched_send

        async def run_patched():
            async with plugin.send_order.one_shot(event.unified_msg_origin, event=event):
                return await plugin.send_observer.qq_official_send(event, chain)

        ok2 = await asyncio.wait_for(run_patched(), timeout=1.0)
        self.assertTrue(ok2)
        self.assertEqual(len(patched_calls), 1)
        self.assertFalse(mod.get_group_send_lock(event.unified_msg_origin).locked())
        await plugin.terminate()


class DatabaseConcurrencyRegressionTest(unittest.TestCase):
    def test_concurrent_database_initialization_and_backup_closure(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "concurrent.db"
            db = ActivityDatabase(lambda: db_path)
            errors = []
            conns = []

            def worker():
                try:
                    conns.append(db.get_connection())
                except BaseException as exc:
                    errors.append(exc)

            threads = [threading.Thread(target=worker) for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            self.assertEqual(errors, [])
            self.assertEqual(len({id(c) for c in conns}), 1)
            db.close()

            # Legacy DB triggers migrate_resident backup and closes backup connection cleanly.
            legacy_path = Path(tmp) / "legacy.db"
            raw = sqlite3.connect(legacy_path)
            raw.execute(
                "CREATE TABLE activity_messages (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "platform_id TEXT NOT NULL DEFAULT '', kind TEXT NOT NULL DEFAULT 'group', "
                "conversation_id TEXT NOT NULL DEFAULT '', conversation_name TEXT NOT NULL DEFAULT '', "
                "sender_id TEXT NOT NULL DEFAULT '', sender_name TEXT NOT NULL DEFAULT '', "
                "text TEXT NOT NULL DEFAULT '', ts REAL NOT NULL DEFAULT 0)"
            )
            raw.execute("CREATE TABLE resident_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            raw.execute("INSERT INTO resident_meta(key, value) VALUES ('schema', '1')")
            raw.commit()
            raw.close()

            db2 = ActivityDatabase(lambda: legacy_path)
            conn2 = db2.get_connection()
            ver = conn2.execute("SELECT value FROM resident_meta WHERE key='schema'").fetchone()[0]
            self.assertEqual(ver, SCHEMA_VERSION)
            backups = list(Path(tmp).glob("legacy.db.bak-*"))
            self.assertEqual(len(backups), 1)
            # Verify backup connection was properly closed (writable/openable without lock issues).
            chk = sqlite3.connect(backups[0])
            self.assertEqual(chk.execute("SELECT value FROM resident_meta WHERE key='schema'").fetchone()[0], "1")
            chk.close()
            db2.close()


class MergeAndMediaRegressionTest(unittest.IsolatedAsyncioTestCase):
    async def test_collect_merge_epoch_toctou_and_interleaved_and_inbox_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin = make_plugin(
                merge_localize_images=True,
                merge_localize_images_dir=tmp,
                merge_idle_seconds=30,
                merge_max_total_seconds=60,
                context=FakeContext(["/"]),
            )
            src = Path(tmp).parent / "source_toctou.png"
            src.write_bytes(b"\x89PNG\r\n\x1a\n testdata")

            entered = asyncio.Event()
            proceed = asyncio.Event()
            orig_download = plugin.media.download_and_persist

            async def slow_download(ref, **kwargs):
                entered.set()
                await proceed.wait()
                return await orig_download(ref, **kwargs)

            plugin.media.download_and_persist = slow_download

            e1 = FakeEvent(message_str="", components=[Image(file=str(src))], message_id="m1")
            t1 = asyncio.create_task(plugin.on_message(e1))
            await entered.wait()

            key = plugin.merger.merge_key(e1.unified_msg_origin, "10001")
            self.assertFalse(await plugin.merger.has_active_merge(key))

            proceed.set()
            await t1
            self.assertIn(key, plugin.merger._merges)
            ms = plugin.merger._merges[key]
            self.assertEqual(ms.epoch, 1)
            self.assertTrue(plugin.media._tracked.get((key, 1)))

            # Verify interleaved Plain + Image + Plain preserves order and nuoxiaomiao_inbox_ids is copied.
            plugin.media.download_and_persist = orig_download
            ms.parts = ["one", "", "two"]
            ms.component_parts = [
                [Plain("one"), Image(file="https://e.test/1.png")],
                [Image(file="https://e.test/2.png")],
                [Plain("two")],
            ]
            inbox_list = [101, 102]
            ms.origin_event.set_extra("nuoxiaomiao_inbox_ids", inbox_list)

            committed_events = []
            inst = plugin.context.platform_manager.platform_insts[0]
            inst.create_event = lambda abm: FakeEvent(message_str=abm.message_str, components=abm.message)
            inst.commit_event = lambda ev: committed_events.append(ev)

            async def fake_create_message(**kwargs):
                return SimpleNamespace(message_id="syn-1", message_str=kwargs["message_str"], message=kwargs["message"])

            with patch("astrbot.core.star.star_tools.StarTools.create_message", new=fake_create_message):
                await plugin.merger.send_merged_event(ms, "one\ntwo")

            self.assertEqual(len(committed_events), 1)
            syn = committed_events[0]
            self.assertEqual([type(c).__name__ for c in syn.get_messages()], ["Plain", "Plain", "Image", "Image", "Plain"])
            copied_inbox = syn.get_extra("nuoxiaomiao_inbox_ids")
            self.assertEqual(copied_inbox, [101, 102])
            self.assertIsNot(copied_inbox, inbox_list)
            src_ids, epoch = await plugin.merger.consume_synthetic_id("syn-1")
            self.assertEqual(epoch, 1)
            self.assertEqual(src_ids, {"m1"})
            await plugin.terminate()

    def test_ssrf_and_magic_bytes_rejection(self):
        for bad in (
            "http://localhost/a.png",
            "http://127.0.0.1/a.png",
            "http://10.0.0.5/a.png",
            "http://192.168.1.1/a.png",
            "http://[::1]/a.png",
            "http://[::ffff:127.0.0.1]/a.png",
            "file:///etc/passwd",
        ):
            self.assertFalse(is_safe_download_url(bad), f"Expected SSRF block for {bad}")
        self.assertTrue(is_safe_download_url("https://example.com/a.png"))

        with tempfile.TemporaryDirectory() as tmp:
            html_file = Path(tmp) / "fake.png"
            html_file.write_bytes(b"<html>not an image</html>")
            self.assertFalse(looks_like_image(str(html_file)))
            png_file = Path(tmp) / "real.png"
            png_file.write_bytes(b"\x89PNG\r\n\x1a\n real")
            self.assertTrue(looks_like_image(str(png_file)))


class RemovalAndMigrationRegressionTest(unittest.IsolatedAsyncioTestCase):
    async def test_v7_db_migration_drops_removed_tables_and_preserves_kept_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "activity.db"
            raw = sqlite3.connect(db_path)
            raw.executescript(
                """
                CREATE TABLE activity_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    platform_id TEXT NOT NULL DEFAULT '',
                    kind TEXT NOT NULL DEFAULT 'group',
                    conversation_id TEXT NOT NULL DEFAULT '',
                    conversation_name TEXT NOT NULL DEFAULT '',
                    sender_id TEXT NOT NULL DEFAULT '',
                    sender_name TEXT NOT NULL DEFAULT '',
                    text TEXT NOT NULL DEFAULT '',
                    ts REAL NOT NULL DEFAULT 0,
                    message_id TEXT NOT NULL DEFAULT '',
                    mentioned INTEGER NOT NULL DEFAULT 0,
                    truncated INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE proactive_pending (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_ts REAL NOT NULL DEFAULT 0,
                    fire_ts REAL NOT NULL DEFAULT 0,
                    done_ts REAL NOT NULL DEFAULT 0,
                    source_umo TEXT NOT NULL DEFAULT '',
                    platform_id TEXT NOT NULL DEFAULT '',
                    target_kind TEXT NOT NULL DEFAULT '',
                    target_id TEXT NOT NULL DEFAULT '',
                    target_name TEXT NOT NULL DEFAULT '',
                    target_umo TEXT NOT NULL DEFAULT '',
                    subject_id TEXT NOT NULL DEFAULT '',
                    subject_name TEXT NOT NULL DEFAULT '',
                    await_sender_id TEXT NOT NULL DEFAULT '',
                    note TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending'
                );
                CREATE TABLE resident_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE resident_reads (persona TEXT NOT NULL, message_id INTEGER NOT NULL, PRIMARY KEY(persona,message_id));
                CREATE TABLE resident_moods (persona TEXT PRIMARY KEY, mood TEXT NOT NULL, intensity INTEGER NOT NULL, updated REAL NOT NULL);
                CREATE TABLE resident_focus (persona TEXT NOT NULL, umo TEXT NOT NULL, body TEXT NOT NULL, updated REAL NOT NULL, PRIMARY KEY(persona,umo));
                CREATE TABLE resident_styles (persona TEXT PRIMARY KEY, version INTEGER NOT NULL, body TEXT NOT NULL);
                CREATE TABLE resident_style_versions (persona TEXT NOT NULL, version INTEGER NOT NULL, body TEXT NOT NULL, created REAL NOT NULL, PRIMARY KEY(persona,version));
                CREATE TABLE resident_proposals (id INTEGER PRIMARY KEY AUTOINCREMENT, persona TEXT NOT NULL, body TEXT NOT NULL, reason TEXT NOT NULL, base_version INTEGER NOT NULL, base_hash TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'pending', created REAL NOT NULL);
                CREATE TABLE resident_topic_segments (id INTEGER PRIMARY KEY AUTOINCREMENT, umo TEXT NOT NULL, start_msg_id INTEGER NOT NULL, end_msg_id INTEGER NOT NULL, start_ts REAL NOT NULL, end_ts REAL NOT NULL, summary TEXT NOT NULL);
                CREATE TABLE vision_kept_images (id INTEGER PRIMARY KEY AUTOINCREMENT, umo TEXT NOT NULL, platform_message_id TEXT NOT NULL, activity_message_id INTEGER NOT NULL, idx INTEGER NOT NULL DEFAULT 0, path TEXT NOT NULL, ts REAL NOT NULL);
                CREATE TABLE vision_caption_cache (key TEXT PRIMARY KEY, caption TEXT NOT NULL, ts REAL NOT NULL);

                INSERT INTO activity_messages(id, platform_id, kind, conversation_id, text, ts) VALUES (1, 'aiocqhttp', 'group', '20001', '保留聊天', 100.0);
                INSERT INTO proactive_pending(id, target_kind, target_id, note, status) VALUES (1, 'private', '10001', '保留主动', 'pending');
                INSERT INTO resident_reads VALUES ('default', 1);
                INSERT INTO resident_moods VALUES ('default', '开心', 70, 100.0);
                INSERT INTO resident_focus VALUES ('default', 'aiocqhttp:GroupMessage:20001', '保留关注', 100.0);
                INSERT INTO resident_styles VALUES ('default', 1, '旧风格');
                INSERT INTO resident_meta VALUES ('schema', '6'), ('inbox_baseline', '1'), ('master_auto_umo', 'aiocqhttp:FriendMessage:1'), ('topic:u', '旧话题'), ('topic_to_id:u', '1'), ('topic_updated_at:u', '100');
                """
            )
            raw.commit()
            raw.close()

            db = ActivityDatabase(lambda: db_path)
            conn = db.get_connection()
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            for dropped in (
                "resident_styles",
                "resident_style_versions",
                "resident_proposals",
                "resident_topic_segments",
                "vision_kept_images",
                "vision_caption_cache",
            ):
                self.assertNotIn(dropped, tables)
            for kept in ("activity_messages", "proactive_pending", "resident_reads", "resident_moods", "resident_focus", "resident_runs"):
                self.assertIn(kept, tables)
            self.assertEqual(conn.execute("SELECT value FROM resident_meta WHERE key='schema'").fetchone()[0], SCHEMA_VERSION)
            self.assertEqual(conn.execute("SELECT value FROM resident_meta WHERE key='inbox_baseline'").fetchone()[0], "1")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM resident_meta WHERE key IN ('master_auto_umo','topic:u','topic_to_id:u','topic_updated_at:u')").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT text FROM activity_messages WHERE id=1").fetchone()[0], "保留聊天")
            self.assertEqual(conn.execute("SELECT mood FROM resident_moods WHERE persona='default'").fetchone()[0], "开心")
            self.assertEqual(conn.execute("SELECT body FROM resident_focus WHERE persona='default'").fetchone()[0], "保留关注")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM resident_reads").fetchone()[0], 1)
            backups = list(Path(tmp).glob("activity.db.bak-resident-*"))
            self.assertEqual(len(backups), 1)
            db.close()

    async def test_config_cleanup_strips_removed_keys_and_cleans_legacy_vision_dir(self):
        import json
        from config_support import CURRENT_CONFIG_NAME, migrate_config_dir

        with tempfile.TemporaryDirectory() as tmp:
            cfg_dir = Path(tmp) / "config"
            cfg_dir.mkdir(parents=True)
            cfg_file = cfg_dir / CURRENT_CONFIG_NAME
            cfg_file.write_text(
                json.dumps(
                    {
                        "enabled": True,
                        "resident_inbox_enabled": False,
                        "resident_state_enabled": True,
                        "force_wake_prefix": "旧前缀",
                        "capability_hint_text": "旧提示",
                        "lmem_gateway_enabled": True,
                        "shared_memory_enabled": True,
                        "unread_inbox_enabled": True,
                        "mention_notice_enabled": True,
                        "topic_summary_enabled": True,
                        "vision_enabled": True,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            udef = Path(tmp) / "user_default_config.json"
            udef.write_text(json.dumps({"enabled": True, "vision_enabled": True, "lmem_gateway_top_k": 5}), encoding="utf-8")

            msg = migrate_config_dir(cfg_dir, udef)
            self.assertIn("已迁移", msg)
            saved = json.loads(cfg_file.read_text(encoding="utf-8"))
            self.assertEqual(saved, {"enabled": True, "resident_inbox_enabled": False, "resident_state_enabled": True})
            saved_udef = json.loads(udef.read_text(encoding="utf-8"))
            self.assertEqual(saved_udef, {"enabled": True})
            self.assertEqual(len(list(cfg_dir.glob("*.bak-alias-*"))), 1)

        plugin = make_plugin(context=FakeContext(admins=["10001"]))
        legacy_dir = plugin.plugin_data_dir() / "vision_kept"
        legacy_dir.mkdir(parents=True, exist_ok=True)
        (legacy_dir / "old.jpg").write_bytes(b"old")
        await plugin.initialize()
        self.assertFalse(legacy_dir.exists())
        self.assertFalse(hasattr(plugin, "read_image"))
        self.assertFalse(hasattr(plugin, "nuoxiaomiao_propose_style"))
        res = json.loads(await plugin.nuoxiaomiao_update_state(FakeEvent(), "开心", 88, "测试关注点"))
        self.assertEqual(res["mood"]["mood"], "开心")
        self.assertEqual(res["focus"], "测试关注点")
        await plugin.terminate()


class AutonomyAndIntegrationRegressionTest(unittest.IsolatedAsyncioTestCase):
    async def test_delivery_locks_refcount_and_owner_allows_and_preparing_cleanup(self):
        plugin = make_proactive_plugin(
            context=FakeContext(admins=["10001"]),
            proactive_allow_groups=["20001"],
            proactive_silent_wake_daily_budget=1,
        )
        autonomy = plugin.resident.autonomy
        delivery = autonomy._delivery

        # Seed run + deferred mapping so deliver_pending serializes via ref-counted lock.
        day = time.strftime("%Y-%m-%d")
        run_id = plugin.resident.store.new_run("aiocqhttp:GroupMessage:20001", "default", day)
        plugin.resident.store.finish(run_id, "succeeded", "quiet", "ok")
        with plugin.resident.store.transaction() as db:
            db.execute("INSERT INTO resident_deferred(pending_id, run_id) VALUES (99, ?)", (run_id,))

        events = []
        entered = asyncio.Event()
        release = asyncio.Event()

        async def slow_cb(row):
            events.append(("start", row["id"]))
            entered.set()
            await release.wait()
            events.append(("end", row["id"]))
            return {"status": "sent"}

        row = {"id": 99, "target_umo": "aiocqhttp:GroupMessage:20001", "note": "hi"}
        t1 = asyncio.create_task(delivery.deliver_pending(row, slow_cb))
        await entered.wait()
        t2 = asyncio.create_task(delivery.deliver_pending(row, AsyncMock(return_value={"status": "sent"})))
        await asyncio.sleep(0.02)
        self.assertFalse(t2.done())
        release.set()
        await asyncio.gather(t1, t2)
        self.assertEqual(events, [("start", 99), ("end", 99)])
        self.assertNotIn(99, delivery._locks)

        # owner_allows checks resolved plugin_name_of_tool even when star_map has no direct module key.
        tool_obj = SimpleNamespace(name="other_tool", handler_module_path="data.plugins.other_plugin.sub")
        ev = FakeEvent()
        ev.plugins_name = ["only_this_plugin"]
        self.assertFalse(plugin.resident.integration.owner_allows(ev, tool_obj))
        ev.plugins_name = ["other_plugin"]
        self.assertTrue(plugin.resident.integration.owner_allows(ev, tool_obj))

        # BudgetExhausted sets run status to 'limited'.
        used_rid = plugin.resident.store.new_run("aiocqhttp:GroupMessage:20001", "default", day)
        plugin.resident.store.charge(used_rid, day, 1)
        provider = ScriptedProvider([LLMResponse(role="assistant", completion_text="hi", usage=TokenUsage(input_other=1, output=1))])
        plugin.self_wake.select_provider = AsyncMock(return_value=provider)
        plugin.resident.autonomy.dispatch_request_hooks = AsyncMock(return_value=False)

        async def fake_build(**kwargs):
            return await build_runner(**kwargs, tools=ToolSet())

        plugin._build_main_agent_fn = fake_build
        plugin._get_session_conv_fn = AsyncMock(return_value=conversation())
        ok, outcome = await plugin.self_wake.run_turn(30.0, 0.5, 0.1)
        self.assertTrue(ok)
        self.assertEqual(outcome, "quiet")
        last_status = plugin.db.get_connection().execute(
            "SELECT status FROM resident_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
        self.assertEqual(last_status, "limited")

        # Pending run that fails while 'preparing' (before Run object exists) cleans up to 'interrupted'.
        pid = plugin.resident.store.new_run("aiocqhttp:GroupMessage:20001", "default", day)
        with patch.object(plugin.resident.autonomy, "checkpoint", new=AsyncMock(side_effect=RuntimeError("boom"))):
            ok2, outcome2 = await plugin.self_wake.run_turn(30.0, 0.5, 0.1)
        self.assertFalse(ok2)
        self.assertEqual(outcome2, "error")
        prep_status = plugin.db.get_connection().execute(
            "SELECT status FROM resident_runs WHERE id=?", (pid,)
        ).fetchone()[0]
        self.assertEqual(prep_status, "interrupted")

        # ScopedExecutor marks RunStopped before side effect as 'rejected'.
        stop_rid = plugin.resident.store.new_run("aiocqhttp:GroupMessage:20001", "default", day)
        run_obj = Run(stop_rid, FakeEvent(), "default")
        run_obj.effective = {"send_message"}
        req = SimpleNamespace(func_tool=ToolSet(tools=[tool("send_message", AsyncMock())]))
        run_obj.request = req
        run_obj.runner = SimpleNamespace(_skill_like_raw_tool_set=req.func_tool)

        class _PreStopOriginal:
            async def execute(self, t, rc, **kwargs):
                raise RunStopped("stopped before side effect")
                yield  # pragma: no cover

        executor = ScopedExecutor(_PreStopOriginal(), plugin.resident.autonomy, run_obj)
        with self.assertRaises(RunStopped):
            async for _ in executor.execute(req.func_tool.tools[0], None, intent="hi"):
                pass
        act_status = plugin.db.get_connection().execute(
            "SELECT status FROM resident_actions WHERE run_id=? ORDER BY id DESC LIMIT 1",
            (stop_rid,),
        ).fetchone()[0]
        self.assertEqual(act_status, "rejected")
        await plugin.terminate()


class QQOfficialAndProactiveRegressionTest(unittest.IsolatedAsyncioTestCase):
    async def test_qq_official_outbound_ack_isolation_and_split_order(self):
        captured = []

        class FakeAdapter:
            def __init__(self):
                self._session_last_message_id = {}

            def remember_session_message_id(self, sid, mid):
                self._session_last_message_id[sid] = mid

        adapter = FakeAdapter()
        ctx = FakeContext(admins=["10001"])
        ctx.platform_manager.platform_insts = [
            SimpleNamespace(
                meta=lambda: SimpleNamespace(id="qq_official", name="qq_official"),
                remember_session_message_id=adapter.remember_session_message_id,
            )
        ]
        plugin = make_proactive_plugin(context=ctx)
        inst = ctx.platform_manager.platform_insts[0]

        async def send_msg(umo, chain):
            async def other_task():
                inst.remember_session_message_id("TARGET_OPENID", "inbound-msg-id")

            t = asyncio.create_task(other_task())
            await t
            inst.remember_session_message_id("TARGET_OPENID", "outbound-msg-id")
            captured.append(True)
            return True

        ctx.send_message = send_msg
        ok, unknown, err = await plugin.proactive.qq_official_send_by_session(
            inst, "qq_official:GroupMessage:TARGET_OPENID", MessageChain([Plain("hi")]), target_id="TARGET_OPENID"
        )
        self.assertTrue(ok)
        self.assertFalse(unknown)
        self.assertEqual(err, "")
        await plugin.terminate()

        # prepare_outbound_chains preserves At order and splits long Plain even alongside Image.
        chain = MessageChain([Image(file="https://e.test/a.png"), At(qq="OPENID_1"), Plain("A" * 20), Plain("B" * 20)])
        chains, _info = qq_official.prepare_outbound_chains(chain, text_limit=25, content_mode=True)
        self.assertGreaterEqual(len(chains), 2)
        self.assertEqual([type(c).__name__ for c in chains[0].chain], ["Image", "At", "Plain"])
        self.assertEqual(len(chains[0].chain[2].text), 25)
        self.assertEqual(chains[1].chain[0].text, "B" * 15)

    async def test_proactive_fail_closed_on_db_error_and_non_numeric_group_id(self):
        plugin = make_proactive_plugin(
            context=FakeContext(admins=["10001"]),
            proactive_allow_groups=["ABCDEF123456"],
            proactive_daily_limit_per_person=5,
            proactive_cooldown_minutes=10,
        )
        gid, _gname = plugin.targets.match_allowed_group("ABCDEF123456")
        self.assertEqual(gid, "ABCDEF123456")

        row = {"target_kind": "private", "target_id": "10001"}
        with patch.object(plugin.activity.db, "read_conn", side_effect=RuntimeError("db down")):
            self.assertFalse(plugin.proactive.daily_ok(row, time.time()))
            self.assertFalse(plugin.proactive.cooldown_ok(row, time.time()))
        await plugin.terminate()


class WebApiSecurityRegressionTest(unittest.IsolatedAsyncioTestCase):
    async def test_web_apis_fail_closed_401_and_reject_malformed_json_400(self):
        plugin = make_proactive_plugin(context=FakeContext(admins=["10001"]))
        app = Quart(__name__)

        @app.route("/config", methods=["GET", "POST"])
        async def _cfg():
            from quart import request

            if request.method == "GET":
                return await plugin.config_svc.webapi_get_config()
            return await plugin.config_svc.webapi_save_config()

        @app.route("/resident", methods=["GET", "POST"])
        async def _res():
            from quart import request

            if request.method == "GET":
                return await plugin.resident.get()
            return await plugin.resident.mutate()

        client = app.test_client()
        self.assertEqual((await client.get("/config")).status_code, 401)
        self.assertEqual((await client.post("/config", data="{}")).status_code, 401)
        self.assertEqual((await client.get("/resident")).status_code, 401)
        self.assertEqual((await client.post("/resident", data="{}")).status_code, 401)

        with patch.object(plugin.config_svc, "webapi_username", return_value="admin"):
            bad_cfg = await client.post("/config", data="{bad json", headers={"Content-Type": "application/json"})
            self.assertEqual(bad_cfg.status_code, 400)
            bad_res = await client.post("/resident", data="{bad json", headers={"Content-Type": "application/json"})
            self.assertEqual(bad_res.status_code, 200)
            payload = await bad_res.get_json()
            self.assertIn("operation_error", payload["data"])
        await plugin.terminate()


class SelfWakeAndInboxRefactorRegressionTest(unittest.IsolatedAsyncioTestCase):
    async def test_v7_to_v8_migration_creates_watermarks_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "v7.db"
            raw = sqlite3.connect(db_path)
            raw.executescript(
                """
                CREATE TABLE activity_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    platform_id TEXT NOT NULL DEFAULT '',
                    kind TEXT NOT NULL DEFAULT 'group',
                    conversation_id TEXT NOT NULL DEFAULT '',
                    conversation_name TEXT NOT NULL DEFAULT '',
                    sender_id TEXT NOT NULL DEFAULT '',
                    sender_name TEXT NOT NULL DEFAULT '',
                    text TEXT NOT NULL DEFAULT '',
                    ts REAL NOT NULL DEFAULT 0,
                    message_id TEXT NOT NULL DEFAULT '',
                    mentioned INTEGER NOT NULL DEFAULT 0,
                    truncated INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE proactive_pending (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_ts REAL NOT NULL DEFAULT 0,
                    fire_ts REAL NOT NULL DEFAULT 0,
                    done_ts REAL NOT NULL DEFAULT 0,
                    source_umo TEXT NOT NULL DEFAULT '',
                    platform_id TEXT NOT NULL DEFAULT '',
                    target_kind TEXT NOT NULL DEFAULT '',
                    target_id TEXT NOT NULL DEFAULT '',
                    target_name TEXT NOT NULL DEFAULT '',
                    target_umo TEXT NOT NULL DEFAULT '',
                    subject_id TEXT NOT NULL DEFAULT '',
                    subject_name TEXT NOT NULL DEFAULT '',
                    await_sender_id TEXT NOT NULL DEFAULT '',
                    note TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending'
                );
                CREATE TABLE resident_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                INSERT INTO resident_meta VALUES ('schema', '7');
                """
            )
            raw.commit()
            raw.close()
            db = ActivityDatabase(lambda: db_path)
            conn = db.get_connection()
            self.assertEqual(
                conn.execute("SELECT value FROM resident_meta WHERE key='schema'").fetchone()[0],
                SCHEMA_VERSION,
            )
            tables = {
                row[0]
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            }
            self.assertIn("resident_channel_watermarks", tables)
            db.close()

    async def test_media_placeholders_and_natural_wake_prefix_capture(self):
        from astrbot.core.message.components import File

        plugin = make_plugin(context=FakeContext(wake_prefixes=["小喵", "/"], admins=["10001"]))
        self.addAsyncCleanup(plugin.terminate)

        await plugin.activity.capture_chat_activity(
            FakeEvent(message_str="", components=[Image(file="https://e.test/1.png")], message_id="img1")
        )
        await plugin.activity.capture_chat_activity(
            FakeEvent(
                message_str="",
                components=[File(name="report.pdf", url="https://e.test/report.pdf")],
                message_id="file1",
            )
        )
        await plugin.activity.capture_chat_activity(
            FakeEvent(
                message_str="小喵你觉得呢",
                components=[Plain("小喵你觉得呢")],
                message_id="wake1",
                is_trigger=False,
            )
        )
        rows = plugin.resident.store.rows("SELECT text, mentioned FROM activity_messages ORDER BY id")
        self.assertEqual([r["text"] for r in rows], ["[图片]", "[文件: report.pdf]", "小喵你觉得呢"])
        self.assertEqual(rows[2]["mentioned"], 1)

    async def test_quiet_timer_precision_quiet_streak_and_pick_session_unread_priority(self):
        plugin = make_proactive_plugin(
            context=FakeContext(admins=["10001"]),
            blocked_users=["999"],
            activity_group_blacklist=["88888"],
            proactive_allow_groups=["20001", "20002"],
        )
        self.addAsyncCleanup(plugin.terminate)

        plugin.self_wake._last_human_ts = 1000.0
        self.assertFalse(plugin.self_wake.should_reset_quiet_timer(FakeEvent(sender_id="999", message_str="blocked")))
        self.assertFalse(
            plugin.self_wake.should_reset_quiet_timer(
                FakeEvent(group_id="88888", session="aiocqhttp:GroupMessage:88888", message_str="blacklisted")
            )
        )
        # Plain un-@'d group chatter must NOT reset the heartbeat quiet timer (IRMIA semantics)
        self.assertFalse(
            plugin.self_wake.should_reset_quiet_timer(
                FakeEvent(group_id="20001", session="aiocqhttp:GroupMessage:20001", is_trigger=False, message_str="hi")
            )
        )
        # Direct @/wake trigger in monitored group DOES reset the quiet timer
        self.assertTrue(
            plugin.self_wake.should_reset_quiet_timer(
                FakeEvent(group_id="20001", session="aiocqhttp:GroupMessage:20001", is_trigger=True, message_str="小喵 hi")
            )
        )

        from astrbot.core.provider.entities import ProviderRequest
        from infra.constants import SELF_WAKE_EVENT_EXTRA, SELF_WAKE_UMO

        # Consecutive quiet wake reminder on beat 2 & 5 only (injected in wake/context.py)
        async def _wake_ctx_text():
            ev = FakeEvent(session=SELF_WAKE_UMO, is_private=True, group_id="")
            ev.set_extra(SELF_WAKE_EVENT_EXTRA, True)
            req = ProviderRequest(prompt=plugin.self_wake.self_wake_prompt(30.0))
            await plugin.ctx_injector.maybe_add_self_wake_context(ev, req)
            return "\n".join(getattr(p, "text", "") for p in req.extra_user_content_parts)

        plugin.self_wake.settle_turn(True, "quiet", time.time())
        self.assertEqual(plugin.self_wake.quiet_streak(), 1)
        self.assertNotIn("连续第", await _wake_ctx_text())
        plugin.self_wake.settle_turn(True, "quiet", time.time())
        self.assertEqual(plugin.self_wake.quiet_streak(), 2)
        self.assertIn("连续第 2 次安静醒来", await _wake_ctx_text())
        for _ in range(3):
            plugin.self_wake.settle_turn(True, "quiet", time.time())
        self.assertEqual(plugin.self_wake.quiet_streak(), 5)
        self.assertIn("连续第 5 次安静醒来", await _wake_ctx_text())
        plugin.self_wake.mark_human_activity(time.time())
        self.assertEqual(plugin.self_wake.quiet_streak(), 0)

        picked = await plugin.self_wake.pick_session()
        self.assertIsNotNone(picked)
        self.assertEqual(picked["umo"], SELF_WAKE_UMO)

    async def test_self_wake_context_sanitizes_tool_prompt_and_injects_overview(self):
        from astrbot.core.provider.entities import ProviderRequest
        from infra.constants import ActivityEntry, SELF_WAKE_EVENT_EXTRA
        from wake.context import _DEFAULT_TOOL_CALL_PROMPT

        plugin = make_proactive_plugin(
            context=FakeContext(admins=["10001"]),
            proactive_allow_groups=["20001"],
        )
        self.addAsyncCleanup(plugin.terminate)
        plugin.activity.record_activity(
            ActivityEntry(
                platform_id="aiocqhttp",
                ts=time.time() - 30,
                sender_id="1",
                sender_name="群友",
                conversation_id="20001",
                conversation_name="测试群",
                kind="group",
                text="有新话题",
                mentioned=True,
            )
        )
        ev = FakeEvent(message_str="醒来")
        ev.set_extra(SELF_WAKE_EVENT_EXTRA, True)
        req = ProviderRequest(prompt=plugin.self_wake.self_wake_prompt(20.0))
        req.system_prompt = f"基础人设\n{_DEFAULT_TOOL_CALL_PROMPT}"
        req.func_tool = ToolSet([tool("read_conversation_messages", AsyncMock()), tool("send_message", AsyncMock())])

        await plugin.on_llm_request(ev, req)
        self.assertNotIn(_DEFAULT_TOOL_CALL_PROMPT, req.system_prompt)
        self.assertNotIn("等本轮工具任务完成后统一回复", req.system_prompt)
        injected = "\n".join(getattr(p, "text", "") for p in req.extra_user_content_parts)
        self.assertIn("未读概况", injected)
        self.assertIn("允许的主动目标", injected)
        self.assertIn("本轮实际挂载工具", injected)
        self.assertIn("read_conversation_messages", injected)

    async def test_cross_platform_umo_and_target_rewrite_uses_activity_and_keeps_steps(self):
        from astrbot.core.provider.entities import ProviderRequest
        from infra.constants import ActivityEntry
        from resident.autonomy_budget import CURRENT_RUN

        plugin = make_proactive_plugin(
            context=FakeContext(admins=["10001"]),
            proactive_allow_groups=["other:GroupMessage:777888"],
        )
        self.addAsyncCleanup(plugin.terminate)
        plugin.activity.record_activity(
            ActivityEntry(
                platform_id="other",
                ts=time.time() - 20,
                sender_id="9",
                sender_name="群友X",
                conversation_id="777888",
                conversation_name="跨平台群",
                kind="group",
                text="今晚打副本吗",
            )
        )
        ev = FakeEvent(session="aiocqhttp:FriendMessage:10001", is_private=True, group_id="")
        resolved = plugin.targets.resolve_target(ev, "other:GroupMessage:777888")
        self.assertEqual(resolved["platform_id"], "other")
        self.assertEqual(resolved["umo"], "other:GroupMessage:777888")

        provider = ScriptedProvider([LLMResponse(role="assistant", completion_text="走起呀", usage=TokenUsage(input_other=5, output=3))])
        plugin.context.provider_manager.get_using_provider = lambda umo="": provider
        rid = plugin.resident.store.new_run("aiocqhttp:FriendMessage:10001", "default", time.strftime("%Y-%m-%d"))
        run = Run(rid, ev, "default", request=ProviderRequest(), steps=8)
        token = CURRENT_RUN.set(run)
        try:
            text = await plugin.proactive.generate_in_target_context(resolved, "响应副本邀请")
        finally:
            CURRENT_RUN.reset(token)
        self.assertEqual(text, "走起呀")
        self.assertEqual(run.steps, 8)
        self.assertIn("今晚打副本吗", provider.calls[0]["prompt"])

    async def test_partial_update_state_and_review_unknown_run(self):
        from astrbot.core.provider.entities import ProviderRequest
        from quart import Quart

        plugin = make_proactive_plugin(context=FakeContext(admins=["10001"]))
        self.addAsyncCleanup(plugin.terminate)
        ev = FakeEvent()

        # Partial state update preserves focus when only mood changes, and preserves mood when only focus changes
        res1 = json.loads(await plugin.nuoxiaomiao_update_state(ev, mood="开心", intensity=80, focus="先看技术群"))
        self.assertEqual(res1["mood"]["mood"], "开心")
        self.assertEqual(res1["focus"], "先看技术群")
        res2 = json.loads(await plugin.nuoxiaomiao_update_state(ev, mood="专注"))
        self.assertEqual(res2["mood"]["mood"], "专注")
        self.assertEqual(res2["mood"]["intensity"], 80)
        self.assertEqual(res2["focus"], "先看技术群")
        res3 = json.loads(await plugin.nuoxiaomiao_update_state(ev, focus="新的关注点"))
        self.assertEqual(res3["mood"]["mood"], "专注")
        self.assertEqual(res3["focus"], "新的关注点")
        res4 = json.loads(await plugin.nuoxiaomiao_update_state(ev, clear_focus=True))
        self.assertEqual(res4["focus"], "")

        # Idempotent inject + relative time
        req = ProviderRequest()
        await plugin.resident.persona.inject(ev, req)
        await plugin.resident.persona.inject(ev, req)
        self.assertEqual(len(req.extra_user_content_parts), 1)
        self.assertIn("刚刚", req.extra_user_content_parts[0].text)

        # review_run clears unknown run without child actions/sends
        rid = plugin.resident.store.new_run(ev.unified_msg_origin, "default", time.strftime("%Y-%m-%d"))
        with plugin.resident.store.transaction() as db:
            db.execute("UPDATE resident_runs SET status='unknown' WHERE id=?", (rid,))
        snap = await plugin.resident.snapshot(ev.unified_msg_origin)
        self.assertEqual([r["id"] for r in snap["unknown_runs"]], [rid])

        app = Quart(__name__)

        @app.route("/resident", methods=["POST"])
        async def _res():
            return await plugin.resident.mutate()

        client = app.test_client()
        with patch.object(plugin.config_svc, "webapi_username", return_value="admin"):
            resp = await client.post(
                "/resident",
                json={"action": "review_run", "run_id": rid, "conclusion": "interrupted", "note": "人工核销"},
            )
        data = (await resp.get_json())["data"]
        self.assertEqual(data["unknown_runs"], [])
        row = plugin.resident.store.rows("SELECT status, summary FROM resident_runs WHERE id=?", (rid,))[0]
        self.assertEqual(row["status"], "interrupted")
        self.assertIn("人工核销", row["summary"])


    async def test_irmia_alignment_no_quiet_hours_block_no_body_leak_and_isolated_history(self):
        from astrbot.core.provider.entities import ProviderRequest
        from infra.constants import ActivityEntry, SELF_WAKE_EVENT_EXTRA

        plugin = make_proactive_plugin(
            context=FakeContext(admins=["10001"]),
            proactive_allow_groups=["20001"],
            proactive_quiet_hours_start="00:00",
            proactive_quiet_hours_end="23:59",
        )
        self.addAsyncCleanup(plugin.terminate)

        # Un-@'d group message does not pull init_baseline forward over older bot/direct interaction
        now = time.time()
        plugin.activity.record_activity(
            ActivityEntry(
                platform_id="aiocqhttp",
                ts=now - 3600,
                sender_id="10001",
                sender_name="主人",
                conversation_id="10001",
                conversation_name="主人",
                kind="private",
                text="早安",
            )
        )
        plugin.activity.record_activity(
            ActivityEntry(
                platform_id="aiocqhttp",
                ts=now - 30,
                sender_id="9",
                sender_name="群友",
                conversation_id="20001",
                conversation_name="摸鱼群",
                kind="group",
                text="秘密水群正文不应提前泄露",
                mentioned=False,
            )
        )
        plugin.self_wake._last_human_ts = None
        plugin.self_wake.init_baseline()
        self.assertLess(plugin.self_wake._last_human_ts, now - 1800)

        # Self-wake loop must still run during quiet hours (only send_message defers)
        ran = asyncio.Event()

        async def fake_run_turn(quiet, prob, roll):
            ran.set()
            return True, "quiet"

        with (
            patch.object(plugin.self_wake, "should_trigger", return_value=(True, 1.0)),
            patch.object(plugin.self_wake, "run_turn", side_effect=fake_run_turn),
            patch("wake.self_wake.SILENT_WAKE_TICK_SECONDS", 0.01),
        ):
            task = asyncio.create_task(plugin.self_wake._loop())
            await asyncio.wait_for(ran.wait(), timeout=1.0)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        # Unread overview in self-wake context must NOT leak message body ('秘密水群正文不应提前泄露')
        ev = FakeEvent(message_str="醒来")
        ev.set_extra(SELF_WAKE_EVENT_EXTRA, True)
        req = ProviderRequest(prompt=plugin.self_wake.self_wake_prompt(60.0))
        req.func_tool = ToolSet([tool("read_conversation_messages", AsyncMock())])
        await plugin.on_llm_request(ev, req)
        injected = "\n".join(getattr(p, "text", "") for p in req.extra_user_content_parts)
        self.assertIn("摸鱼群", injected)
        self.assertIn("未读 1 条", injected)
        self.assertNotIn("秘密水群正文不应提前泄露", injected)

        # Autonomy isolates anchored group conv.history while keeping persona_id
        conv_with_history = SimpleNamespace(
            cid="c1",
            persona_id="custom_p",
            history=json.dumps([{"role": "user", "content": "群里之前聊的旧话"}], ensure_ascii=False),
            token_usage=100,
        )
        captured_reqs = []
        provider = ScriptedProvider([LLMResponse(role="assistant", completion_text="安静结束", usage=TokenUsage(input_other=5, output=2))])
        plugin.self_wake.select_provider = AsyncMock(return_value=provider)
        plugin.resident.autonomy.dispatch_request_hooks = AsyncMock(return_value=False)

        async def fake_build(**kwargs):
            conv_obj = kwargs["req"].conversation
            captured_reqs.append((conv_obj.persona_id, conv_obj.history, conv_obj.token_usage))
            return await build_runner(**kwargs, tools=ToolSet())

        with (
            patch("resident.ports.build_main_agent", fake_build),
            patch("resident.ports._get_session_conv", AsyncMock(return_value=conv_with_history)),
        ):
            ok, outcome = await plugin.self_wake.run_turn(30.0, 0.5, 0.1)
        self.assertEqual((ok, outcome), (True, "quiet"))
        self.assertEqual(captured_reqs[0], ("custom_p", "[]", 0))
        self.assertEqual(conv_with_history.token_usage, 100)
        self.assertIn("群里之前聊的旧话", conv_with_history.history)


    async def test_unlimited_daily_budget_normal_send_fallback_defer_and_keyword_reset(self):
        from astrbot.core.provider.entities import ProviderRequest
        from infra.constants import ActivityEntry, SELF_WAKE_UMO

        # 1. daily_budget=0 means unlimited wakes (charged=1 past 20 runs)
        plugin = make_proactive_plugin(
            context=FakeContext(admins=["10001"]),
            proactive_silent_wake_daily_budget=0,
            proactive_allow_groups=["20001"],
            proactive_quiet_hours_start="00:00",
            proactive_quiet_hours_end="23:59",
            keyword_wake_enabled=True,
            keyword_wake_list=["召唤小喵"],
        )
        self.addAsyncCleanup(plugin.terminate)
        day = time.strftime("%Y-%m-%d")
        for _ in range(20):
            rid = plugin.resident.store.new_run(SELF_WAKE_UMO, "default", day)
            plugin.resident.store.charge(rid, day, 0)
        self.assertTrue(plugin.self_wake.budget_left())

        # 2. Turning off self-wake must NOT break normal-chat send_message
        plugin.proactive_silent_wake_enabled = False
        normal_ev = FakeEvent(is_private=True, session="aiocqhttp:FriendMessage:10001", group_id="", sender_id="10001")
        out = json.loads(await plugin.send_message(normal_ev, target="群:20001", intent="早上好"))
        self.assertEqual(out["status"], "postponed")

        # 3. Fallback mode during quiet hours postpones via schedule_or_deliver instead of sending overnight
        plugin.proactive_silent_wake_enabled = True
        plugin.activity.record_activity(
            ActivityEntry(
                platform_id="aiocqhttp",
                ts=time.time() - 10,
                sender_id="1",
                sender_name="路人",
                conversation_id="20001",
                conversation_name="讨论:第一期:A",
                kind="group",
                text="冒号群消息",
            )
        )
        fb = await plugin.self_wake.deliver_heartbeat_decision(
            {"target_kind": "group", "target": "20001", "note": "明早打招呼"}
        )
        self.assertEqual(fb["status"], "postponed")

        # 4. Self-wake rejects empty/current target on SELF_WAKE_UMO
        wake_ev = FakeEvent(session=SELF_WAKE_UMO, is_private=True, group_id="", sender_id="10001")
        wake_ev._platform_id = "nuoxiaomiao"
        wake_ev.get_platform_id = lambda: "nuoxiaomiao"
        self.assertIn("error", plugin.targets.resolve_target(wake_ev, ""))

        # 5. Group name containing colons still resolves via Inbox.read name lookup
        colon_res = await plugin.resident.inbox.read(normal_ev, conversation="讨论:第一期:A")
        self.assertEqual(colon_res["count"], 1)
        self.assertEqual(colon_res["results"][0]["text"], "冒号群消息")

        # 6. keyword_wake in on_message resets quiet timer; poke & proactive_context_other_sessions=False behave properly
        plugin.self_wake._last_human_ts = 100.0
        kw_ev = FakeEvent(is_trigger=False, message_str="召唤小喵出来玩", group_id="20001")
        await plugin.on_message(kw_ev)
        self.assertGreater(plugin.self_wake._last_human_ts, 1000.0)

        plugin.proactive_context_other_sessions = False
        req = ProviderRequest()
        await plugin.ctx_injector.maybe_inject_context(
            FakeEvent(is_private=True, session="aiocqhttp:FriendMessage:10001", group_id="", sender_id="10001", message_str="群里聊什么"),
            req,
        )
        self.assertEqual(req.extra_user_content_parts, [])
