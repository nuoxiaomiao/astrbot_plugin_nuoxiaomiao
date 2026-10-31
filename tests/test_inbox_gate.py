import time
import unittest

from astrbot.core.message.components import Image, Plain

from main import ActivityEntry, SELF_WAKE_EVENT_EXTRA, SYNTHETIC_EVENT_EXTRA
from test_plugin_logic import make_plugin, FakeContext, FakeEvent
from test_vision import image_file


class InboxGateTest(unittest.IsolatedAsyncioTestCase):
    def make(self, **cfg):
        p = make_plugin(context=FakeContext(admins=["10001"]), **cfg)
        self.addAsyncCleanup(p.terminate)
        return p

    def record(self, p, text, sender="9", mentioned=False, mid="", kind="group"):
        return p._record_activity(
            ActivityEntry(
                ts=time.time(),
                sender_id=sender,
                sender_name="某人",
                conversation_id="20001",
                conversation_name="20001",
                kind=kind,
                text=text,
                platform_id="aiocqhttp",
                message_id=mid or f"mid-{time.time_ns()}",
                mentioned=mentioned,
            )
        )

    # ------------------------------------------------------------------
    # silence_candidate 分流判定
    # ------------------------------------------------------------------

    def test_disabled_never_silences(self):
        p = self.make(unread_inbox_enabled=False)
        event = FakeEvent()
        self.assertFalse(p.resident.inbox.silence_candidate(event))

    def test_plain_group_message_is_silenced(self):
        p = self.make(unread_inbox_enabled=True)
        event = FakeEvent(is_trigger=False)
        self.assertTrue(p.resident.inbox.silence_candidate(event))

    def test_at_wake_never_silenced(self):
        p = self.make(unread_inbox_enabled=True)
        self.assertFalse(
            p.resident.inbox.silence_candidate(FakeEvent(is_trigger=True))
        )

    def test_keyword_woken_never_silenced(self):
        p = self.make(unread_inbox_enabled=True)
        self.assertFalse(
            p.resident.inbox.silence_candidate(FakeEvent(), keyword_woken=True)
        )

    def test_private_chat_never_silenced(self):
        p = self.make(unread_inbox_enabled=True)
        event = FakeEvent(session="aiocqhttp:FriendMessage:9", is_private=True)
        self.assertFalse(p.resident.inbox.silence_candidate(event))

    def test_command_never_silenced(self):
        command_filter = type("CommandFilter", (), {})()
        command_handler = type(
            "Handler", (), {"event_filters": [command_filter]}
        )()
        p = self.make(unread_inbox_enabled=True)
        event = FakeEvent(
            message_str="/help",
            components=[Plain("/help")],
            activated_handlers=[command_handler],
        )
        self.assertFalse(p.resident.inbox.silence_candidate(event))

    def test_synthetic_and_self_wake_never_silenced(self):
        p = self.make(unread_inbox_enabled=True)
        synthetic = FakeEvent()
        synthetic.set_extra(SYNTHETIC_EVENT_EXTRA, True)
        self.assertFalse(p.resident.inbox.silence_candidate(synthetic))
        self_wake = FakeEvent()
        self_wake.set_extra(SELF_WAKE_EVENT_EXTRA, True)
        self.assertFalse(p.resident.inbox.silence_candidate(self_wake))

    def test_group_whitelist_limits_scope(self):
        p = self.make(
            unread_inbox_enabled=True,
            inbox_group_whitelist=["30001"],
        )
        # 不在白名单里的群不进入信箱分流。
        self.assertFalse(
            p.resident.inbox.silence_candidate(FakeEvent(group_id="20001", is_trigger=False))
        )
        # 白名单内的群正常分流。
        self.assertTrue(
            p.resident.inbox.silence_candidate(FakeEvent(group_id="30001", is_trigger=False))
        )

    def test_group_blacklist_excludes_scope(self):
        p = self.make(
            unread_inbox_enabled=True,
            inbox_group_blacklist=["20001"],
        )
        self.assertFalse(
            p.resident.inbox.silence_candidate(FakeEvent(group_id="20001", is_trigger=False))
        )
        self.assertTrue(
            p.resident.inbox.silence_candidate(FakeEvent(group_id="30001", is_trigger=False))
        )

    # ------------------------------------------------------------------
    # mark_channel / notice / read 话题注入
    # ------------------------------------------------------------------

    async def test_mark_channel_clears_whole_channel(self):
        p = self.make(resident_inbox_enabled=True)
        for i in range(10):
            self.record(p, str(i))
        event = FakeEvent(is_trigger=False)
        persona, _ = await p.resident.persona.resolve(event)
        data = await p.resident.inbox.read(event, conversation="20001", mode="unread")
        self.assertEqual(data["count"], 10)
        p.resident.inbox.mark_channel(persona, ("aiocqhttp", "group", "20001"))
        remaining = await p.resident.inbox.read(
            event, conversation="20001", mode="unread"
        )
        self.assertEqual(remaining["count"], 0)

    async def test_notice_includes_unread_and_topic(self):
        p = self.make(
            resident_inbox_enabled=True,
            topic_summary_enabled=True,
        )
        p.resident.store.set_topic_state(
            "aiocqhttp:GroupMessage:20001", "大家在聊周末去哪", 1, time.time()
        )
        for i in range(6):
            self.record(p, f"内容{i}", mentioned=i == 3)
        persona = "@session:aiocqhttp:GroupMessage:20001"
        text = await p.resident.inbox.notice(FakeEvent(is_trigger=False), persona)
        self.assertIn("未读 6 条", text)
        self.assertIn("近期话题：大家在聊周末去哪", text)
        self.assertIn("（@你）", text)

    async def test_notice_respects_limit(self):
        p = self.make(resident_inbox_enabled=True, mention_notice_limit=2)
        for i in range(10):
            self.record(p, f"内容{i}")
        persona = "@session:aiocqhttp:GroupMessage:20001"
        text = await p.resident.inbox.notice(FakeEvent(is_trigger=False), persona, limit=2)
        self.assertEqual(text.count("- "), 2)

    async def test_read_injects_topic_for_single_session(self):
        p = self.make(resident_inbox_enabled=True, topic_summary_enabled=True)
        p.resident.store.set_topic_state(
            "aiocqhttp:GroupMessage:20001", "在聊什么", 1, time.time()
        )
        self.record(p, "内容")
        data = await p.resident.inbox.read(
            FakeEvent(is_trigger=False), conversation="aiocqhttp:GroupMessage:20001"
        )
        self.assertEqual(data["topic"], "在聊什么")

    # ------------------------------------------------------------------
    # on_message 分流 + 速览采集联动
    # ------------------------------------------------------------------

    async def test_on_message_diverts_plain_group_message(self):
        p = self.make(unread_inbox_enabled=True)
        event = FakeEvent(is_trigger=False, message_str="随便聊聊")
        await p.on_message(event)
        self.assertTrue(event.stopped)
        self.assertFalse(event.call_llm)

    async def test_on_message_never_diverts_wake(self):
        p = self.make(unread_inbox_enabled=True, merge_enabled=False)
        event = FakeEvent(is_trigger=True, message_str="小喵在吗")
        await p.on_message(event)
        self.assertFalse(event.stopped)

    async def test_diverted_message_still_captured(self):
        p = self.make(unread_inbox_enabled=True, activity_buffer_enabled=True)
        event = FakeEvent(is_trigger=False, message_str="随便聊聊", message_id="mid-x")
        await p.on_message(event)
        self.assertTrue(event.stopped)
        await p._capture_chat_activity(event)
        sessions = p._activity_sessions("group")
        self.assertTrue(any(s["messages"] for s in sessions))

    async def test_image_only_message_captured_when_vision_enabled(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            img = image_file(tmp)
            p = self.make(vision_enabled=True, activity_buffer_enabled=True)
            event = FakeEvent(
                is_trigger=False,
                message_str="",
                components=[Image(file=img)],
                message_id="mid-img",
            )
            await p._capture_chat_activity(event)
            sessions = p._activity_sessions("group")
            self.assertTrue(any(s["messages"] for s in sessions))

    async def test_image_only_message_not_captured_when_vision_disabled(self):
        p = self.make(vision_enabled=False, activity_buffer_enabled=True)
        event = FakeEvent(
            is_trigger=False,
            message_str="",
            components=[Image(file="/nonexistent/x.png")],
            message_id="mid-img",
        )
        await p._capture_chat_activity(event)
        sessions = p._activity_sessions("group")
        self.assertFalse(any(s["messages"] for s in sessions))


if __name__ == "__main__":
    unittest.main()
