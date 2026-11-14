"""Proactive messaging, quiet hours, rate limits, history sync, and persistence tests."""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
import unittest

from astrbot.core.message.components import At

from infra.constants import ActivityEntry, DISPLAY_NAME
from tests.fakes import (
    FakeContext,
    FakeConversationManager,
    FakeEvent,
    FakeProvider,
    fetch_activity_rows,
    make_plugin,
    make_proactive_plugin,
)


class SendMessageTest(unittest.IsolatedAsyncioTestCase):
    """send_message 工具：即时投递、目标语境生成、边界与顺延。"""

    def _private(self, sender_id="10001"):
        return FakeEvent(
            is_private=True,
            session=f"aiocqhttp:FriendMessage:{sender_id}",
            group_id="",
            sender_id=sender_id,
            sender_name="主人",
            message_str="我先去忙了",
        )

    @staticmethod
    def _group_event(sender_id="10001", group_id="90000", group_name="处刑群"):
        return FakeEvent(
            sender_id=sender_id,
            sender_name="主人",
            group_id=group_id,
            group_name=group_name,
            session=f"aiocqhttp:GroupMessage:{group_id}",
            message_str="回头群里见",
        )

    async def test_denied_target_returns_admin_hint(self):
        context = FakeContext(admins=["10001"])
        plugin = make_proactive_plugin(
            context=context,
            proactive_allow_groups=["88888"],
            proactive_target_whitelist=["20002"],
        )

        data = json.loads(
            await plugin.send_message(
                self._group_event(), target="群:90000", intent="说一声"
            )
        )

        self.assertIn("error", data)
        self.assertIn("hint", data)
        self.assertIn("88888", data["hint"])
        self.assertIn("20002", data["hint"])
        await plugin.terminate()

    async def test_denied_target_hint_hidden_from_non_admin(self):
        context = FakeContext(admins=["10001"])
        plugin = make_proactive_plugin(
            context=context,
            proactive_allow_groups=["88888"],
            proactive_target_whitelist=["20002"],
        )

        data = json.loads(
            await plugin.send_message(
                self._group_event(sender_id="30003"),
                target="群:90000",
                intent="说一声",
            )
        )

        self.assertIn("error", data)
        self.assertNotIn("hint", data)
        await plugin.terminate()

    async def test_send_returns_actual_text(self):
        provider = FakeProvider(text="那边的我来了喵～")
        context = FakeContext(admins=["10001"], provider=provider)
        plugin = make_proactive_plugin(context=context)

        data = json.loads(
            await plugin.send_message(
                self._private(),
                intent="催楠酱回我晚饭吃什么",
            )
        )

        self.assertEqual(data["status"], "sent")
        self.assertEqual(data["text"], "那边的我来了喵～")
        self.assertEqual(len(context.sent), 1)
        umo, _chain = context.sent[0]
        self.assertEqual(umo, "aiocqhttp:FriendMessage:10001")
        conn = plugin.db.get_connection()
        rows = conn.execute(
            "SELECT status, note FROM proactive_pending"
        ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "fired")
        self.assertIn("晚饭", rows[0][1])
        await plugin.terminate()

    async def test_generation_uses_target_history_and_persona(self):
        provider = FakeProvider(text="接得上话喵")
        context = FakeContext(admins=["10001"], provider=provider, persona_prompt="目标人设")
        conv_mgr = FakeConversationManager()
        context.conversation_manager = conv_mgr
        umo = "aiocqhttp:FriendMessage:10001"
        cid = await conv_mgr.new_conversation(umo)
        conv_mgr.conversations[cid] = [
            {"role": "user", "content": "晚上想吃烤鱼吗"},
            {"role": "assistant", "content": "想吃的喵！"},
        ]
        plugin = make_proactive_plugin(context=context)

        await plugin.send_message(self._private(), intent="提醒他烤鱼的事")

        self.assertEqual(len(provider.calls), 1)
        call = provider.calls[0]
        self.assertEqual(call["system_prompt"], "目标人设")
        self.assertIn("晚上想吃烤鱼吗", call["prompt"])
        self.assertIn("提醒他烤鱼的事", call["prompt"])
        self.assertIn("一切以它为准", call["prompt"])
        self.assertIn("编造没发生的事", call["prompt"])
        await plugin.terminate()

    async def test_group_send_without_at_has_no_at(self):
        provider = FakeProvider(text="路过说一句喵")
        context = FakeContext(admins=["10001"], provider=provider)
        plugin = make_proactive_plugin(
            context=context, proactive_allow_groups=["90000"]
        )

        data = json.loads(
            await plugin.send_message(
                self._group_event(),
                target="群:90000",
                intent="来群里打个招呼",
            )
        )

        self.assertEqual(data["status"], "sent")
        _umo, chain = context.sent[0]
        self.assertTrue(all(type(comp).__name__ != "At" for comp in chain.chain))
        await plugin.terminate()

    async def test_group_send_with_at_resolves_member(self):
        provider = FakeProvider(text="就决定是你了喵")
        context = FakeContext(admins=["10001"], provider=provider)
        plugin = make_proactive_plugin(
            context=context, proactive_allow_groups=["90000"]
        )
        plugin.activity.record_activity(
            ActivityEntry(
                platform_id="aiocqhttp",
                ts=time.time(),
                sender_id="10002",
                sender_name="小初音",
                conversation_id="90000",
                conversation_name="处刑群",
                kind="group",
                text="群里聊天",
            )
        )

        data = json.loads(
            await plugin.send_message(
                self._group_event(),
                target="群:90000",
                intent="叫小初音来打牌",
                at="小初音",
            )
        )

        self.assertEqual(data["status"], "sent")
        _umo, chain = context.sent[0]
        at_comps = [comp for comp in chain.chain if type(comp).__name__ == "At"]
        self.assertEqual(len(at_comps), 1)
        self.assertEqual(str(at_comps[0].qq), "10002")
        await plugin.terminate()

    async def test_bare_group_id_target_accepted(self):
        provider = FakeProvider(text="喵")
        context = FakeContext(admins=["10001"], provider=provider)
        plugin = make_proactive_plugin(
            context=context, proactive_allow_groups=["20001"]
        )

        data = json.loads(
            await plugin.send_message(
                self._group_event(group_id="20001", group_name="猫娘群"),
                target="20001",
                intent="来这边看看",
            )
        )

        self.assertEqual(data["status"], "sent")
        _umo, _chain = context.sent[0]
        self.assertEqual(_umo, "aiocqhttp:GroupMessage:20001")
        await plugin.terminate()

    async def test_group_not_allowed_rejected(self):
        provider = FakeProvider(text="喵")
        context = FakeContext(admins=["10001"], provider=provider)
        plugin = make_proactive_plugin(context=context)

        data = json.loads(
            await plugin.send_message(
                self._group_event(), target="群:90000", intent="想去看看"
            )
        )

        self.assertIn("error", data)
        self.assertEqual(len(context.sent), 0)
        await plugin.terminate()

    async def test_private_outside_whitelist_rejected(self):
        provider = FakeProvider(text="喵")
        context = FakeContext(admins=["99999"], provider=provider)
        plugin = make_proactive_plugin(context=context)

        data = json.loads(
            await plugin.send_message(
                self._private(sender_id="10001"),
                target="私聊:10001",
                intent="想找他聊天",
            )
        )

        self.assertIn("error", data)
        self.assertEqual(len(context.sent), 0)
        await plugin.terminate()

    async def test_empty_intent_rejected(self):
        plugin = make_proactive_plugin(context=FakeContext(admins=["10001"]))

        data = json.loads(
            await plugin.send_message(self._private(), intent="")
        )

        self.assertIn("error", data)
        await plugin.terminate()

    async def test_quiet_hours_postponed(self):
        provider = FakeProvider(text="早上的话喵")
        context = FakeContext(admins=["10001"], provider=provider)
        plugin = make_proactive_plugin(
            context=context,
            proactive_quiet_hours_start="00:00",
            proactive_quiet_hours_end="23:58",
        )

        data = json.loads(
            await plugin.send_message(self._private(), intent="夜里的安排")
        )

        self.assertEqual(data["status"], "postponed")
        self.assertIn("fire_at", data)
        self.assertEqual(len(context.sent), 0)
        self.assertEqual(len(plugin.proactive._pending), 1)
        row = next(iter(plugin.proactive._pending.values()))
        self.assertIn("夜里的安排", row["note"])
        await plugin.terminate()

    async def test_no_postpone_when_quiet_disabled(self):
        provider = FakeProvider(text="马上到喵")
        context = FakeContext(admins=["10001"], provider=provider)
        plugin = make_proactive_plugin(context=context)

        data = json.loads(
            await plugin.send_message(self._private(), intent="现在就说")
        )

        self.assertEqual(data["status"], "sent")
        await plugin.terminate()

    async def test_daily_limit_exhausted(self):
        provider = FakeProvider(text="喵")
        context = FakeContext(admins=["10001"], provider=provider)
        plugin = make_proactive_plugin(
            context=context, proactive_daily_limit_per_person=1
        )

        first = json.loads(
            await plugin.send_message(self._private(), intent="第一次")
        )
        second = json.loads(
            await plugin.send_message(self._private(), intent="第二次")
        )

        self.assertEqual(first["status"], "sent")
        self.assertIn("error", second)
        self.assertEqual(len(context.sent), 1)
        await plugin.terminate()

    async def test_cooldown_blocks_immediate_repeat(self):
        provider = FakeProvider(text="喵")
        context = FakeContext(admins=["10001"], provider=provider)
        plugin = make_proactive_plugin(
            context=context, proactive_cooldown_minutes=30
        )

        first = json.loads(
            await plugin.send_message(self._private(), intent="第一次")
        )
        second = json.loads(
            await plugin.send_message(self._private(), intent="第二次")
        )

        self.assertEqual(first["status"], "sent")
        self.assertIn("冷却", str(second))
        await plugin.terminate()

    async def test_generation_failure_sends_nothing(self):
        provider = FakeProvider(raise_error=True)
        context = FakeContext(admins=["10001"], provider=provider)
        plugin = make_proactive_plugin(context=context)

        data = json.loads(
            await plugin.send_message(self._private(), intent="想说点什么")
        )

        self.assertIn("error", data)
        self.assertEqual(len(context.sent), 0)
        conn = plugin.db.get_connection()
        status = conn.execute("SELECT status FROM proactive_pending").fetchone()[0]
        self.assertEqual(status, "failed")
        await plugin.terminate()

    async def test_tool_disabled_returns_error(self):
        plugin = make_proactive_plugin(proactive_enabled=False)

        data = json.loads(
            await plugin.send_message(self._private(), intent="想说点什么")
        )

        self.assertIn("error", data)
        await plugin.terminate()

    async def test_postpone_respects_pending_cap(self):
        provider = FakeProvider(text="喵")
        context = FakeContext(admins=["10001"], provider=provider)
        plugin = make_proactive_plugin(
            context=context,
            proactive_quiet_hours_start="00:00",
            proactive_quiet_hours_end="23:58",
            proactive_max_pending_per_person=2,
        )
        event = self._private()
        first = json.loads(await plugin.send_message(event, intent="安排一"))
        second = json.loads(await plugin.send_message(event, intent="安排二"))
        third = json.loads(await plugin.send_message(event, intent="安排三"))

        self.assertEqual(first["status"], "postponed")
        self.assertEqual(second["status"], "postponed")
        self.assertIn("error", third)
        self.assertIn("排队", str(third))
        self.assertEqual(len(plugin.proactive._pending), 2)
        await plugin.terminate()


class ProactiveFireTest(unittest.IsolatedAsyncioTestCase):
    """内部追击/顺延到点触发：目标语境生成、发送、@ 与边界（直接喂行）。"""

    def _row(self, plugin, target_kind="private", target_id="10001", **extra):
        row = {
            "id": 0,
            "created_ts": time.time() - 60,
            "fire_ts": time.time(),
            "source_umo": "aiocqhttp:FriendMessage:10001",
            "platform_id": "aiocqhttp",
            "target_kind": target_kind,
            "target_id": target_id,
            "target_name": "主人" if target_kind == "private" else "90000",
            "target_umo": (
                f"aiocqhttp:{'FriendMessage' if target_kind == 'private' else 'GroupMessage'}:{target_id}"
            ),
            "subject_id": extra.pop("subject_id", ""),
            "subject_name": extra.pop("subject_name", ""),
            "await_sender_id": "",
            "note": extra.pop("note", "你问了他：「去哪了？」他一直没回你"),
            "status": "pending",
        }
        row.update(extra)
        row["id"] = plugin.proactive.insert_row(row)
        return row

    async def test_timer_fires_private_message(self):
        provider = FakeProvider(text="人呢喵？")
        context = FakeContext(admins=["10001"], provider=provider)
        plugin = make_proactive_plugin(context=context)
        row = self._row(plugin)
        await plugin.proactive.schedule_timer({**row, "fire_ts": time.time() + 0.01})

        await asyncio.sleep(1.5)

        self.assertEqual(len(context.sent), 1)
        umo, _chain = context.sent[0]
        self.assertEqual(umo, "aiocqhttp:FriendMessage:10001")
        self.assertEqual(len(plugin.proactive._pending), 0)
        conn = plugin.db.get_connection()
        status = conn.execute("SELECT status FROM proactive_pending").fetchone()[0]
        self.assertEqual(status, "fired")
        await plugin.terminate()

    async def test_fire_uses_target_persona_and_note(self):
        provider = FakeProvider(text="喂，晚饭吃什么呢喵？")
        context = FakeContext(
            admins=["10001"], provider=provider, persona_prompt="目标会话人设"
        )
        plugin = make_proactive_plugin(context=context)
        row = self._row(plugin, note="催他回我晚饭吃什么")

        await plugin.proactive.fire_proactive(row)

        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(provider.calls[0]["system_prompt"], "目标会话人设")
        self.assertIn("催他回我晚饭吃什么", provider.calls[0]["prompt"])
        _umo, chain = context.sent[0]
        self.assertEqual(chain.get_plain_text(), "喂，晚饭吃什么呢喵？")
        await plugin.terminate()

    async def test_group_fire_ats_subject_when_specified(self):
        provider = FakeProvider(text="家人们，这位问了问题就跑！")
        context = FakeContext(admins=["10001"], provider=provider)
        plugin = make_proactive_plugin(
            context=context, proactive_allow_groups=["90000"]
        )
        row = self._row(
            plugin,
            target_kind="group",
            target_id="90000",
            subject_id="10001",
            subject_name="主人",
        )

        await plugin.proactive.fire_proactive(row)

        _umo, chain = context.sent[0]
        self.assertIsInstance(chain.chain[0], At)
        self.assertEqual(str(chain.chain[0].qq), "10001")
        await plugin.terminate()

    async def test_group_fire_without_subject_has_no_at(self):
        provider = FakeProvider(text="路过说一句喵")
        context = FakeContext(admins=["10001"], provider=provider)
        plugin = make_proactive_plugin(
            context=context, proactive_allow_groups=["90000"]
        )
        row = self._row(plugin, target_kind="group", target_id="90000")

        await plugin.proactive.fire_proactive(row)

        _umo, chain = context.sent[0]
        self.assertTrue(all(type(comp).__name__ != "At" for comp in chain.chain))
        await plugin.terminate()

    async def test_fire_group_not_allowed_skips(self):
        provider = FakeProvider(text="喵")
        context = FakeContext(admins=["10001"], provider=provider)
        plugin = make_proactive_plugin(context=context)
        row = self._row(plugin, target_kind="group", target_id="90000")

        await plugin.proactive.fire_proactive(row)

        self.assertEqual(len(context.sent), 0)
        conn = plugin.db.get_connection()
        status = conn.execute("SELECT status FROM proactive_pending").fetchone()[0]
        self.assertEqual(status, "skipped")
        await plugin.terminate()

    async def test_fire_private_outside_whitelist_skips(self):
        provider = FakeProvider(text="喵")
        context = FakeContext(admins=["99999"], provider=provider)
        plugin = make_proactive_plugin(context=context)
        row = self._row(plugin, target_kind="private", target_id="10001")

        await plugin.proactive.fire_proactive(row)

        self.assertEqual(len(context.sent), 0)
        conn = plugin.db.get_connection()
        status = conn.execute("SELECT status FROM proactive_pending").fetchone()[0]
        self.assertEqual(status, "skipped")
        await plugin.terminate()

    async def test_master_switch_off_skips_pending(self):
        provider = FakeProvider(text="喵")
        context = FakeContext(admins=["10001"], provider=provider)
        plugin = make_proactive_plugin(context=context, enabled=False)
        row = self._row(plugin)

        await plugin.proactive.fire_proactive(row)

        self.assertEqual(len(context.sent), 0)
        conn = plugin.db.get_connection()
        status = conn.execute("SELECT status FROM proactive_pending").fetchone()[0]
        self.assertEqual(status, "skipped")
        await plugin.terminate()

    async def test_fire_waits_for_target_lock(self):
        provider = FakeProvider(text="排队的话喵")
        context = FakeContext(admins=["10001"], provider=provider)
        plugin = make_proactive_plugin(context=context)
        row = self._row(plugin)
        lock = await plugin.proactive._get_target_lock("private", "10001")
        await lock.acquire()

        task = asyncio.ensure_future(plugin.proactive.fire_proactive(row))
        await asyncio.sleep(0.05)
        self.assertFalse(task.done())
        lock.release()
        await asyncio.wait_for(task, timeout=5)
        self.assertEqual(len(context.sent), 1)
        conn = plugin.db.get_connection()
        status = conn.execute("SELECT status FROM proactive_pending").fetchone()[0]
        self.assertEqual(status, "fired")
        await plugin.terminate()


class ProactiveGuardTest(unittest.IsolatedAsyncioTestCase):
    """频率保险丝、静默时段、兜底文案与重启恢复。"""

    def _row(self, plugin, target_kind="private", target_id="10001", **extra):
        row = {
            "id": 0,
            "created_ts": time.time() - 60,
            "fire_ts": time.time(),
            "source_umo": "aiocqhttp:FriendMessage:10001",
            "platform_id": "aiocqhttp",
            "target_kind": target_kind,
            "target_id": target_id,
            "target_name": "主人",
            "target_umo": f"aiocqhttp:{'FriendMessage' if target_kind == 'private' else 'GroupMessage'}:{target_id}",
            "subject_id": "10001",
            "subject_name": "主人",
            "await_sender_id": "",
            "note": "测试",
            "status": "pending",
        }
        row.update(extra)
        row["id"] = plugin.proactive.insert_row(row)
        return row

    async def test_daily_limit_skips_send(self):
        context = FakeContext(admins=["10001"])
        plugin = make_proactive_plugin(
            context=context, proactive_daily_limit_per_person=2
        )
        row = self._row(plugin)
        now = time.time()
        conn = plugin.db.get_connection()
        for _ in range(2):
            conn.execute(
                "INSERT INTO proactive_pending (created_ts, fire_ts, done_ts,"
                " target_kind, target_id, target_umo, status)"
                " VALUES (?, ?, ?, 'private', '10001', 'x', 'fired')",
                (now, now, now),
            )
        conn.commit()

        await plugin.proactive.fire_proactive(row)

        self.assertEqual(len(context.sent), 0)
        status = conn.execute(
            "SELECT status FROM proactive_pending WHERE id = ?", (row["id"],)
        ).fetchone()[0]
        self.assertEqual(status, "skipped")
        await plugin.terminate()

    async def test_cooldown_skips_send(self):
        context = FakeContext(admins=["10001"])
        plugin = make_proactive_plugin(
            context=context, proactive_cooldown_minutes=30
        )
        row = self._row(plugin)
        conn = plugin.db.get_connection()
        conn.execute(
            "INSERT INTO proactive_pending (created_ts, fire_ts, done_ts,"
            " target_kind, target_id, target_umo, status)"
            " VALUES (?, ?, ?, 'private', '10001', 'x', 'fired')",
            (time.time() - 60, time.time() - 60, time.time() - 60),
        )
        conn.commit()

        await plugin.proactive.fire_proactive(row)

        self.assertEqual(len(context.sent), 0)
        await plugin.terminate()

    async def test_quiet_hours_postpone(self):
        context = FakeContext(admins=["10001"])
        plugin = make_proactive_plugin(
            context=context,
            proactive_quiet_hours_start="00:00",
            proactive_quiet_hours_end="23:58",
        )
        fire_ts = time.time() + 60

        adjusted = plugin.proactive.quiet_adjust(fire_ts)

        self.assertGreater(adjusted, fire_ts)
        await plugin.terminate()

    async def test_quiet_hours_disabled_returns_same(self):
        context = FakeContext(admins=["10001"])
        plugin = make_proactive_plugin(
            context=context,
            proactive_quiet_hours_start="08:00",
            proactive_quiet_hours_end="08:00",
        )
        fire_ts = time.time() + 60

        self.assertEqual(plugin.proactive.quiet_adjust(fire_ts), fire_ts)
        await plugin.terminate()

    async def test_llm_failure_sends_nothing(self):
        context = FakeContext(admins=["10001"], provider=FakeProvider(raise_error=True))
        plugin = make_proactive_plugin(context=context)
        row = self._row(plugin)

        await plugin.proactive.fire_proactive(row)

        self.assertEqual(len(context.sent), 0)
        conn = plugin.db.get_connection()
        status = conn.execute(
            "SELECT status FROM proactive_pending WHERE id = ?", (row["id"],)
        ).fetchone()[0]
        self.assertEqual(status, "failed")
        await plugin.terminate()

    async def test_persistence_restores_pending(self):
        context = FakeContext(admins=["10001"])
        plugin = make_proactive_plugin(context=context)
        db_path = str(plugin.activity_db_file())
        self._row(plugin, note="重启也要记得找我", fire_ts=time.time() + 1800)
        await plugin.terminate()

        reopened = make_proactive_plugin(
            context=FakeContext(admins=["10001"]), activity_db_path=db_path
        )
        await reopened.proactive.load_pending()

        self.assertEqual(len(reopened.proactive._pending), 1)
        restored = next(iter(reopened.proactive._pending.values()))
        self.assertEqual(restored["note"], "重启也要记得找我")
        await reopened.terminate()

    async def test_stale_pending_not_restored(self):
        context = FakeContext(admins=["10001"])
        plugin = make_proactive_plugin(context=context)
        self._row(plugin, fire_ts=time.time() - 3 * 86400)
        db_path = str(plugin.activity_db_file())
        await plugin.terminate()

        reopened = make_proactive_plugin(
            context=FakeContext(admins=["10001"]), activity_db_path=db_path
        )
        await reopened.proactive.load_pending()

        self.assertEqual(len(reopened.proactive._pending), 0)
        conn = reopened.db.get_connection()
        status = conn.execute("SELECT status FROM proactive_pending").fetchone()[0]
        self.assertEqual(status, "skipped")
        await reopened.terminate()


class ProactiveRecordTest(unittest.IsolatedAsyncioTestCase):
    """主动消息发出后的两处补记：目标会话历史 + 速览库 + 来源会话语境。"""

    def _private(self, sender_id="10001"):
        return FakeEvent(
            is_private=True,
            session=f"aiocqhttp:FriendMessage:{sender_id}",
            group_id="",
            sender_id=sender_id,
            sender_name="主人",
            message_str="我先去忙了",
        )

    def _group_event(self, sender_id="10001", group_id="90000", group_name="处刑群"):
        return FakeEvent(
            sender_id=sender_id,
            sender_name="主人",
            group_id=group_id,
            group_name=group_name,
            session=f"aiocqhttp:GroupMessage:{group_id}",
            message_str="回头群里见",
        )

    async def test_send_appends_to_target_history(self):
        provider = FakeProvider(text="来找我喵～")
        context = FakeContext(admins=["10001"], provider=provider)
        context.conversation_manager = FakeConversationManager()
        conv_mgr = context.conversation_manager
        umo = "aiocqhttp:FriendMessage:10001"
        cid = await conv_mgr.new_conversation(umo)
        conv_mgr.conversations[cid] = [
            {"role": "user", "content": "我先去忙了"}
        ]
        plugin = make_proactive_plugin(context=context)

        data = json.loads(
            await plugin.send_message(
                self._private(), intent="催他回我晚饭吃什么"
            )
        )
        self.assertEqual(data["status"], "sent")
        await asyncio.sleep(0.3)

        history = conv_mgr.conversations[cid]
        self.assertEqual(len(history), 2)
        appended = history[-1]
        self.assertEqual(appended["role"], "assistant")
        self.assertIsInstance(appended["content"], list)
        text = "".join(
            str(part.get("text") or "")
            for part in appended["content"]
            if isinstance(part, dict)
        )
        self.assertIn("催他回我晚饭吃什么", text)
        self.assertIn("来找我喵～", text)
        await plugin.terminate()

    async def test_send_skips_history_for_fresh_conversation(self):
        provider = FakeProvider(text="来找我喵～")
        context = FakeContext(admins=["10001"], provider=provider)
        context.conversation_manager = FakeConversationManager()
        plugin = make_proactive_plugin(context=context)

        await plugin.send_message(self._private(), intent="催他")
        await asyncio.sleep(0.3)

        conv_mgr = context.conversation_manager
        self.assertEqual(conv_mgr.sessions, {})
        rows = fetch_activity_rows(plugin, sender_id="bot")
        self.assertEqual(len(rows), 1)
        await plugin.terminate()

    async def test_send_records_bot_row_in_activity(self):
        provider = FakeProvider(text="来找我喵～")
        context = FakeContext(admins=["10001"], provider=provider)
        plugin = make_proactive_plugin(context=context)

        await plugin.send_message(self._private(), intent="催他")
        await asyncio.sleep(0.3)

        rows = fetch_activity_rows(plugin, sender_id="bot")
        self.assertEqual(len(rows), 1)
        kind, conversation_id, _name, sender_id, sender_name, text = rows[0]
        self.assertEqual((kind, conversation_id), ("private", "10001"))
        self.assertEqual(sender_name, DISPLAY_NAME)
        self.assertIn("来找我喵～", text)
        await plugin.terminate()

    async def test_group_send_records_at_prefix(self):
        provider = FakeProvider(text="来找我喵～")
        context = FakeContext(admins=["10001"], provider=provider)
        context.conversation_manager = FakeConversationManager()
        conv_mgr = context.conversation_manager
        group_umo = "aiocqhttp:GroupMessage:90000"
        cid = await conv_mgr.new_conversation(group_umo)
        conv_mgr.conversations[cid] = [
            {"role": "user", "content": "群里有人吗"}
        ]
        plugin = make_proactive_plugin(
            context=context,
            proactive_allow_groups=["90000"],
        )
        plugin.activity.record_activity(
            ActivityEntry(
                platform_id="aiocqhttp",
                ts=time.time(),
                sender_id="10001",
                sender_name="主人",
                conversation_id="90000",
                conversation_name="处刑群",
                kind="group",
                text="在群里说话",
            )
        )

        data = json.loads(
            await plugin.send_message(
                self._group_event(),
                target="群:90000",
                intent="公开处刑他",
                at="主人",
            )
        )
        self.assertEqual(data["status"], "sent")
        await asyncio.sleep(0.3)

        rows = fetch_activity_rows(plugin, sender_id="bot")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "group")
        self.assertTrue(rows[0][5].startswith("@主人"))
        history = conv_mgr.conversations[cid]
        self.assertEqual(len(history), 2)
        text = "".join(
            str(part.get("text") or "")
            for part in history[-1]["content"]
            if isinstance(part, dict)
        )
        self.assertIn("@主人", text)
        self.assertIn("来找我喵～", text)
        await plugin.terminate()


class PendingSummaryTest(unittest.IsolatedAsyncioTestCase):
    """未发/已发清单文案（内部追击可见性）。"""

    async def test_summary_lists_recently_fired(self):
        plugin = make_proactive_plugin(context=FakeContext(admins=["10001"]))
        conn = plugin.db.get_connection()
        conn.execute(
            "INSERT INTO proactive_pending (created_ts, fire_ts, done_ts,"
            " target_kind, target_id, target_name, status)"
            " VALUES (?, ?, ?, 'private', '10001', '主人', 'fired')",
            (time.time() - 100, time.time() - 100, time.time() - 50),
        )
        conn.commit()

        summary = plugin.proactive.pending_summary()

        self.assertIn("已经发出去", summary)
        self.assertIn("主人", summary)
        await plugin.terminate()


class ProactiveAutoFollowupRemovedTest(unittest.IsolatedAsyncioTestCase):
    """自动追击已移除：主动消息是"自己想说话"，不替用户追问。"""

    def _private(self, sender_id="10001"):
        return FakeEvent(
            is_private=True,
            session=f"aiocqhttp:FriendMessage:{sender_id}",
            group_id="",
            sender_id=sender_id,
            sender_name="主人",
            message_str="我先去忙了",
        )

    async def test_question_reply_registers_nothing(self):
        plugin = make_proactive_plugin(context=FakeContext(admins=["10001"]))

        await plugin.on_llm_response(
            self._private(), SimpleNamespace(completion_text="楠酱去哪了喵？")
        )

        self.assertEqual(len(plugin.proactive._pending), 0)
        self.assertFalse(hasattr(plugin, "_maybe_auto_followup"))
        await plugin.terminate()

    async def test_relaxed_frequency_defaults(self):
        plugin = make_plugin()

        self.assertEqual(plugin.proactive_daily_limit_per_person, 10)
        self.assertEqual(plugin.proactive_max_pending_per_person, 3)
        self.assertEqual(plugin.proactive_cooldown_seconds, 10 * 60)
        self.assertFalse(hasattr(plugin, "proactive_auto_followup_enabled"))
        await plugin.terminate()
