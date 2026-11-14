"""Activity capture, SQLite persistence, read-only tools, bot reply recording, and tool owner resolution tests."""

from __future__ import annotations

import json
import time
from types import SimpleNamespace
import unittest

from astrbot.core.message.components import Plain
from astrbot.core.message.message_event_result import MessageChain

from infra.constants import ActivityEntry, DISPLAY_NAME
from infra.runtime import plugin_name_of_tool
from tests.fakes import (
    FakeContext,
    FakeEvent,
    fetch_activity_rows,
    make_activity_plugin,
)


class GroupActivityTest(unittest.IsolatedAsyncioTestCase):
    """会话速览：写进 SQLite，重开连接不丢，按天数清理。"""

    async def test_group_message_is_recorded(self):
        plugin = make_activity_plugin()
        await plugin.activity.capture_chat_activity(
            FakeEvent(message_str="大家好", group_name="猫娘群", sender_name="楠酱")
        )

        sessions = plugin.activity.activity_sessions()
        self.assertEqual(len(sessions), 1)
        session = sessions[0]
        self.assertEqual(session["kind"], "group")
        self.assertEqual(session["conversation_id"], "20001")
        self.assertEqual(session["name"], "猫娘群")
        self.assertEqual(session["messages"], 1)
        self.assertEqual(session["last_text"], "大家好")
        await plugin.terminate()

    async def test_private_message_is_recorded(self):
        plugin = make_activity_plugin()
        await plugin.activity.capture_chat_activity(
            FakeEvent(
                is_private=True,
                session="aiocqhttp:FriendMessage:10001",
                group_id="",
                sender_id="10001",
                sender_name="主人",
                message_str="私聊一句话",
            )
        )

        sessions = plugin.activity.activity_sessions("private")
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["conversation_id"], "10001")
        self.assertEqual(sessions[0]["name"], "主人")
        await plugin.terminate()

    async def test_private_recording_can_be_switched_off(self):
        plugin = make_activity_plugin(activity_record_private=False)
        await plugin.activity.capture_chat_activity(
            FakeEvent(
                is_private=True,
                session="aiocqhttp:FriendMessage:10001",
                group_id="",
                sender_id="10001",
                message_str="不记我",
            )
        )

        self.assertEqual(plugin.activity.activity_sessions(), [])
        await plugin.terminate()

    async def test_records_survive_reopen(self):
        plugin = make_activity_plugin()
        await plugin.activity.capture_chat_activity(FakeEvent(message_str="重启前的消息"))
        db_path = str(plugin.activity_db_file())
        await plugin.terminate()

        reopened = make_activity_plugin(activity_db_path=db_path)
        sessions = reopened.activity.activity_sessions()
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["last_text"], "重启前的消息")
        await reopened.terminate()

    async def test_old_messages_are_pruned(self):
        plugin = make_activity_plugin(activity_retention_days=1)
        plugin.activity.record_activity(
            ActivityEntry(
                ts=time.time() - 10 * 86400,
                sender_id="1",
                sender_name="老人",
                conversation_id="20001",
                conversation_name="旧群",
                kind="group",
                text="很久以前的消息",
            )
        )
        await plugin.activity.run_prune_once()

        self.assertEqual(plugin.activity.activity_sessions(), [])
        await plugin.terminate()

    async def test_long_text_is_truncated(self):
        plugin = make_activity_plugin(activity_buffer_max_text_chars=10)
        await plugin.activity.capture_chat_activity(FakeEvent(message_str="字" * 50))

        text = plugin.activity.activity_sessions()[0]["last_text"]
        self.assertEqual(len(text), 11)
        self.assertTrue(text.endswith("…"))
        await plugin.terminate()

    async def test_synthetic_and_self_messages_are_skipped(self):
        plugin = make_activity_plugin()
        await plugin.merger.mark_synthetic_id("mid-syn")
        await plugin.activity.capture_chat_activity(
            FakeEvent(message_str="合并后重新投递的消息", message_id="mid-syn")
        )
        await plugin.activity.capture_chat_activity(
            FakeEvent(message_str="机器人自己说的", sender_id="bot")
        )

        self.assertEqual(plugin.activity.activity_sessions(), [])
        await plugin.terminate()

    async def test_commands_are_skipped(self):
        plugin = make_activity_plugin(context=FakeContext(["/"]))
        await plugin.activity.capture_chat_activity(
            FakeEvent(message_str="help", components=[Plain("/help")])
        )

        self.assertEqual(plugin.activity.activity_sessions(), [])
        await plugin.terminate()

    async def test_capture_failure_never_raises(self):
        plugin = make_activity_plugin()

        def boom(entry):
            raise RuntimeError("故意出错")

        plugin.activity.record_activity = boom
        await plugin.activity.capture_chat_activity(FakeEvent(message_str="正常一句话"))
        await plugin.terminate()

    async def test_switch_off_records_nothing(self):
        plugin = make_activity_plugin(activity_buffer_enabled=False)
        await plugin.activity.capture_chat_activity(FakeEvent(message_str="大家好"))

        self.assertEqual(plugin.activity.activity_sessions(), [])
        await plugin.terminate()


class GroupActivityToolTest(unittest.IsolatedAsyncioTestCase):
    """只读工具：主人/管理员在任何会话都能查，其他人拿不到。"""

    def _seed(self, plugin):
        now = time.time()
        plugin.activity.record_activity(
            ActivityEntry(
                platform_id="aiocqhttp",
                ts=now - 600,
                sender_id="1",
                sender_name="群友甲",
                conversation_id="777",
                conversation_name="猫娘群",
                kind="group",
                text="今晚谁在打游戏",
            )
        )
        plugin.activity.record_activity(
            ActivityEntry(
                platform_id="aiocqhttp",
                ts=now - 300,
                sender_id="2",
                sender_name="群友乙",
                conversation_id="778",
                conversation_name="技术群",
                kind="group",
                text="这个 bug 修好了",
            )
        )
        plugin.activity.record_activity(
            ActivityEntry(
                platform_id="aiocqhttp",
                ts=now - 60,
                sender_id="3",
                sender_name="网友丙",
                conversation_id="10001",
                conversation_name="主人",
                kind="private",
                text="私聊里说的事",
            )
        )

    def _private(self, sender_id="10001"):
        return FakeEvent(
            is_private=True,
            session=f"aiocqhttp:FriendMessage:{sender_id}",
            group_id="",
            sender_id=sender_id,
            message_str="查一下别的会话",
        )

    def _group(self, sender_id="10001", group_id="90000"):
        return FakeEvent(
            sender_id=sender_id,
            group_id=group_id,
            session=f"aiocqhttp:GroupMessage:{group_id}",
            message_str="查一下别的会话",
        )

    async def test_admin_private_chat_lists_other_conversations(self):
        plugin = make_activity_plugin(context=FakeContext(admins=["10001"]))
        self._seed(plugin)

        data = json.loads(await plugin.list_conversations(self._private()))

        self.assertEqual(data["count"], 2)
        names = {c["name"] for c in data["conversations"]}
        self.assertEqual(names, {"猫娘群", "技术群"})
        await plugin.terminate()

    async def test_admin_group_chat_also_works(self):
        plugin = make_activity_plugin(context=FakeContext(admins=["10001"]))
        self._seed(plugin)

        data = json.loads(await plugin.list_conversations(self._group()))

        self.assertEqual(data["count"], 3)
        await plugin.terminate()

    async def test_list_kind_filter(self):
        plugin = make_activity_plugin(context=FakeContext(admins=["10001"]))
        self._seed(plugin)

        data = json.loads(
            await plugin.list_conversations(self._group(), kind="private")
        )

        self.assertEqual(data["count"], 1)
        self.assertEqual(data["conversations"][0]["name"], "主人")
        await plugin.terminate()

    async def test_read_by_conversation_name(self):
        plugin = make_activity_plugin(context=FakeContext(admins=["10001"]))
        self._seed(plugin)

        data = json.loads(
            await plugin.read_conversation_messages(
                self._private(), conversation="猫娘群"
            )
        )

        self.assertEqual(data["count"], 1)
        self.assertEqual(data["results"][0]["text"], "今晚谁在打游戏")
        self.assertEqual(data["results"][0]["sender"], "群友甲")
        self.assertEqual(data["results"][0]["conversation"], "猫娘群")
        await plugin.terminate()

    async def test_read_keyword_filter(self):
        plugin = make_activity_plugin(context=FakeContext(admins=["10001"]))
        self._seed(plugin)

        data = json.loads(
            await plugin.read_conversation_messages(self._private(), keyword="bug")
        )

        self.assertEqual(data["count"], 1)
        self.assertEqual(data["results"][0]["conversation"], "技术群")
        await plugin.terminate()

    async def test_read_sender_filter(self):
        plugin = make_activity_plugin(context=FakeContext(admins=["10001"]))
        self._seed(plugin)

        data = json.loads(
            await plugin.read_conversation_messages(
                self._private(), sender="群友甲"
            )
        )

        self.assertEqual(data["count"], 1)
        self.assertEqual(data["results"][0]["text"], "今晚谁在打游戏")
        await plugin.terminate()

    async def test_read_after_time_filter(self):
        plugin = make_activity_plugin(context=FakeContext(admins=["10001"]))
        self._seed(plugin)

        data = json.loads(
            await plugin.read_conversation_messages(
                self._private(), after="6分钟前"
            )
        )

        self.assertEqual(data["count"], 2)
        texts = [r["text"] for r in data["results"]]
        self.assertEqual(texts, ["这个 bug 修好了", "私聊里说的事"])
        await plugin.terminate()

    async def test_read_combined_filters(self):
        plugin = make_activity_plugin(context=FakeContext(admins=["10001"]))
        self._seed(plugin)

        data = json.loads(
            await plugin.read_conversation_messages(
                self._private(), conversation="技术群", sender="群友乙"
            )
        )

        self.assertEqual(data["count"], 1)
        self.assertEqual(data["results"][0]["text"], "这个 bug 修好了")
        await plugin.terminate()

    async def test_read_without_filter_returns_recent(self):
        plugin = make_activity_plugin(context=FakeContext(admins=["10001"]))
        self._seed(plugin)

        data = json.loads(
            await plugin.read_conversation_messages(self._private())
        )

        self.assertNotIn("error", data)
        self.assertEqual(data["count"], 2)
        self.assertIn("note", data)
        self.assertEqual(
            {item["conversation"] for item in data["results"]},
            {"猫娘群", "技术群"},
        )
        await plugin.terminate()

    async def test_read_without_filter_still_requires_admin(self):
        plugin = make_activity_plugin(context=FakeContext(admins=["99999"]))
        self._seed(plugin)

        data = json.loads(
            await plugin.read_conversation_messages(self._private())
        )

        self.assertIn("error", data)
        await plugin.terminate()

    async def test_read_unknown_conversation(self):
        plugin = make_activity_plugin(context=FakeContext(admins=["10001"]))
        self._seed(plugin)

        data = json.loads(
            await plugin.read_conversation_messages(
                self._private(), conversation="不存在的群"
            )
        )

        self.assertIn("error", data)
        await plugin.terminate()

    async def test_read_bad_time_gets_error(self):
        plugin = make_activity_plugin(context=FakeContext(admins=["10001"]))
        self._seed(plugin)

        data = json.loads(
            await plugin.read_conversation_messages(
                self._private(), after="不是时间"
            )
        )

        self.assertIn("error", data)
        await plugin.terminate()

    async def test_non_admin_is_denied(self):
        plugin = make_activity_plugin(context=FakeContext(admins=["99999"]))
        self._seed(plugin)

        listed = json.loads(await plugin.list_conversations(self._private()))
        read = json.loads(
            await plugin.read_conversation_messages(
                self._private(), conversation="猫娘群"
            )
        )

        self.assertIn("error", listed)
        self.assertIn("error", read)
        await plugin.terminate()

    async def test_require_admin_off_opens_to_everyone(self):
        plugin = make_activity_plugin(
            context=FakeContext(admins=[]), activity_require_admin=False
        )
        self._seed(plugin)

        data = json.loads(await plugin.list_conversations(self._private()))

        self.assertEqual(data["count"], 2)
        await plugin.terminate()

    async def test_switch_off_returns_error(self):
        plugin = make_activity_plugin(
            context=FakeContext(admins=["10001"]), activity_tool_enabled=False
        )
        self._seed(plugin)

        data = json.loads(
            await plugin.read_conversation_messages(
                self._private(), conversation="猫娘群"
            )
        )

        self.assertIn("error", data)
        await plugin.terminate()


class BotReplyRecordTest(unittest.IsolatedAsyncioTestCase):
    """机器人自己的回复（含 poke 回复）也记进速览库。"""

    async def _send_reply(self, plugin, event, text):
        await plugin.on_llm_response(event, SimpleNamespace(completion_text=text))
        self.assertEqual(fetch_activity_rows(plugin, sender_id="bot"), [])
        await event.send(MessageChain([Plain(text)]))
        await plugin.after_reply_sent(event)

    def _group_event(self, message_str="小喵好"):
        return FakeEvent(message_str=message_str, group_id="20001", group_name="闲聊群")

    async def test_group_reply_recorded(self):
        plugin = make_activity_plugin()

        await self._send_reply(plugin, self._group_event(), "好耶喵～")

        rows = fetch_activity_rows(plugin, sender_id="bot")
        self.assertEqual(len(rows), 1)
        kind, conversation_id, _name, sender_id, sender_name, text = rows[0]
        self.assertEqual((kind, conversation_id), ("group", "20001"))
        self.assertEqual(sender_name, DISPLAY_NAME)
        self.assertEqual(text, "好耶喵～")
        await plugin.terminate()

    async def test_poke_reply_recorded(self):
        plugin = make_activity_plugin()
        event = self._group_event(message_str="")
        event.message_obj.raw_message = {
            "post_type": "notice",
            "notice_type": "notify",
            "sub_type": "poke",
        }

        await self._send_reply(plugin, event, "戳我干嘛喵！")

        rows = fetch_activity_rows(plugin, sender_id="bot")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][5], "戳我干嘛喵！")
        await plugin.terminate()

    async def test_private_reply_respects_private_switch(self):
        plugin = make_activity_plugin(activity_record_private=False)
        event = FakeEvent(
            is_private=True,
            session="aiocqhttp:FriendMessage:10001",
            group_id="",
            sender_id="10001",
            sender_name="主人",
            message_str="在吗",
        )

        await self._send_reply(plugin, event, "在的喵")

        self.assertEqual(fetch_activity_rows(plugin, sender_id="bot"), [])
        await plugin.terminate()

    async def test_reply_not_recorded_when_disabled(self):
        plugin = make_activity_plugin(activity_record_bot_replies=False)

        await self._send_reply(plugin, self._group_event(), "好耶")

        self.assertEqual(fetch_activity_rows(plugin, sender_id="bot"), [])
        await plugin.terminate()

    async def test_long_reply_truncated(self):
        plugin = make_activity_plugin(activity_buffer_max_text_chars=10)

        await self._send_reply(plugin, self._group_event(), "一二三四五六七八九十一二三四五六七八九十")

        rows = fetch_activity_rows(plugin, sender_id="bot")
        self.assertEqual(rows[0][5], "一二三四五六七八九十…")
        await plugin.terminate()


class PluginNameOfToolTest(unittest.IsolatedAsyncioTestCase):
    """工具归属插件的判定：多候选取最长匹配，兜底从模块名抠目录名。"""

    async def test_prefers_longest_matching_key(self):
        star_map = {
            "data.plugins.astrbot_plugin_a": SimpleNamespace(name="短前缀插件"),
            "data.plugins.astrbot_plugin_a.sub": SimpleNamespace(name="长前缀插件"),
        }
        tool = SimpleNamespace(
            handler_module_path="data.plugins.astrbot_plugin_a.sub.main"
        )

        self.assertEqual(
            plugin_name_of_tool(tool, star_map), "长前缀插件"
        )

    async def test_exact_hit_wins_without_ambiguity(self):
        star_map = {
            "data.plugins.astrbot_plugin_b.main": SimpleNamespace(name="乙插件"),
        }
        tool = SimpleNamespace(handler_module_path="data.plugins.astrbot_plugin_b.main")

        self.assertEqual(plugin_name_of_tool(tool, star_map), "乙插件")

    async def test_falls_back_to_directory_name(self):
        tool = SimpleNamespace(handler_module_path="data.plugins.some_unknown.main")

        self.assertEqual(plugin_name_of_tool(tool, {}), "some_unknown")
