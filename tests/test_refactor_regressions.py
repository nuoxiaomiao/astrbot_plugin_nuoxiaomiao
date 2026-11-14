"""Targeted regression tests covering all P0/P1 refactoring fixes."""

from __future__ import annotations

import asyncio
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

from infra.constants import ActivityEntry
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
    async def test_cancelled_brain_waiter_does_not_release_active_holder(self):
        plugin = make_plugin(shared_memory_enabled=True, master_umo="aiocqhttp:FriendMessage:10001")
        master = plugin.resident.master
        acquired_holder = asyncio.Event()
        release_holder = asyncio.Event()

        async def holder():
            is_new = await master.acquire_brain()
            self.assertTrue(is_new)
            acquired_holder.set()
            await release_holder.wait()
            master.release_brain()

        holder_task = asyncio.create_task(holder())
        await acquired_holder.wait()

        waiter_task = asyncio.create_task(master.acquire_brain())
        await asyncio.sleep(0.02)
        waiter_task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter_task

        # Active holder's lock and ownership must remain intact.
        self.assertTrue(master._brain_lock.locked())
        self.assertIs(master._brain_owner, holder_task)
        # Non-owner calling release_brain() cannot steal/release the holder's lock.
        master.release_brain()
        self.assertTrue(master._brain_lock.locked())
        self.assertIs(master._brain_owner, holder_task)

        release_holder.set()
        await holder_task
        self.assertFalse(master._brain_lock.locked())
        self.assertIsNone(master._brain_owner)
        await plugin.terminate()

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


class VisionAndTopicRegressionTest(unittest.IsolatedAsyncioTestCase):
    async def test_vision_ordered_fallback_umo_cache_isolation_and_disabled_cleanup(self):
        good_provider = SimpleNamespace(
            meta=lambda: SimpleNamespace(id="good-v"),
            text_chat=AsyncMock(return_value=SimpleNamespace(completion_text="一只橘猫")),
        )
        seen_umo = []

        class OrderedPM:
            def get_using_provider(self, ptype, umo=None):
                seen_umo.append(umo)
                return good_provider

            async def get_provider_by_id(self, pid):
                if pid == "missing-v":
                    return None
                if pid == "good-v":
                    return good_provider
                return None

        ctx = FakeContext()
        ctx.provider_manager = OrderedPM()
        plugin = make_plugin(
            context=ctx,
            vision_enabled=True,
            vision_provider_ids=["missing-v", "good-v"],
        )
        with tempfile.TemporaryDirectory() as tmp:
            img = Path(tmp) / "cat.png"
            img.write_bytes(b"\x89PNG\r\n\x1a\n")
            plugin.activity.record_activity(
                ActivityEntry(
                    ts=time.time(),
                    sender_id="1",
                    sender_name="路人",
                    conversation_id="20001",
                    conversation_name="猫群",
                    kind="group",
                    text="[图片]",
                    platform_id="aiocqhttp",
                    message_id="msg-v1",
                )
            )
            act_id = plugin.db.get_connection().execute("SELECT id FROM activity_messages").fetchone()[0]
            plugin.resident.store.vision_keep("aiocqhttp:GroupMessage:20001", "msg-v1", act_id, 0, str(img))

            # Brief read uses brief cache key; QA uses distinct qa: cache key.
            brief_res = await plugin.resident.vision.read_image("aiocqhttp:GroupMessage:20001", "msg-v1", 0, "")
            self.assertEqual(brief_res, "一只橘猫")
            good_provider.text_chat.return_value = SimpleNamespace(completion_text="橘猫在睡觉")
            qa_res = await plugin.resident.vision.read_image("aiocqhttp:GroupMessage:20001", "msg-v1", 0, "它在干嘛")
            self.assertEqual(qa_res, "橘猫在睡觉")
            self.assertEqual(good_provider.text_chat.await_count, 2)
            # Re-reading brief returns cached brief without invoking provider again.
            brief_cached = await plugin.resident.vision.read_image("aiocqhttp:GroupMessage:20001", "msg-v1", 0, "")
            self.assertEqual(brief_cached, "一只橘猫")
            self.assertEqual(good_provider.text_chat.await_count, 2)

            # Fallback passes umo when ordered providers are exhausted.
            plugin.vision_provider_ids = []
            await plugin.resident.vision.describe_once(str(img), "", umo="aiocqhttp:GroupMessage:20001")
            self.assertIn("aiocqhttp:GroupMessage:20001", seen_umo)

            # cleanup() still prunes stale rows even when vision_enabled=False.
            plugin.vision_enabled = False
            with plugin.resident.store.transaction() as db:
                db.execute("UPDATE vision_kept_images SET ts = 1.0")
            plugin.resident.vision.cleanup()
            left = plugin.db.get_connection().execute("SELECT COUNT(*) FROM vision_kept_images").fetchone()[0]
            self.assertEqual(left, 0)
        await plugin.terminate()

    async def test_topic_pure_image_segment_advances_cursor(self):
        plugin = make_plugin(topic_summary_enabled=True, topic_min_unread=2, topic_segment_msg_count=2)
        umo = "aiocqhttp:GroupMessage:20001"
        for idx in range(2):
            plugin.activity.record_activity(
                ActivityEntry(
                    ts=time.time() + idx,
                    sender_id="1",
                    sender_name="路人",
                    conversation_id="20001",
                    conversation_name="猫群",
                    kind="group",
                    text="",
                    platform_id="aiocqhttp",
                    message_id=f"img-{idx}",
                )
            )
        max_id = plugin.db.get_connection().execute("SELECT MAX(id) FROM activity_messages").fetchone()[0]
        res = await plugin.resident.topic.summarize(umo)
        self.assertFalse(res)
        state = plugin.resident.store.topic_state(umo)
        self.assertEqual(state["topic_to_id"], max_id)
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
