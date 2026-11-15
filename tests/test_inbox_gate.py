"""Tests for resident inbox data layer (watermarks, unread counts, incremental read, mark_channel)."""

import json
import unittest

from tests.fakes import FakeContext, FakeEvent, make_plugin, record_activity


class InboxDataLayerTest(unittest.IsolatedAsyncioTestCase):
    def make(self, **cfg):
        p = make_plugin(context=FakeContext(admins=["10001"]), **cfg)
        self.addAsyncCleanup(p.terminate)
        return p

    def record(self, p, text, sender="9", mid="", mentioned=False):
        return record_activity(p, text, sender=sender, mid=mid, mentioned=mentioned)

    async def test_mark_channel_clears_whole_channel(self):
        p = self.make(resident_inbox_enabled=True)
        for i in range(10):
            self.record(p, str(i))
        event = FakeEvent(is_trigger=False)
        persona, _ = await p.resident.persona.resolve(event)
        session = {"platform_id": "aiocqhttp", "kind": "group", "conversation_id": "20001", "name": "20001"}
        self.assertEqual(p.resident.inbox.counts(persona, session, event)["unread"], 10)
        p.resident.inbox.mark_channel(persona, ("aiocqhttp", "group", "20001"))
        self.assertEqual(p.resident.inbox.counts(persona, session, event)["unread"], 0)
        remaining = await p.resident.inbox.read(event, conversation="20001")
        self.assertEqual(remaining["count"], 0)
        self.assertEqual(remaining["status"], "no_new_messages")

    async def test_list_and_incremental_read_clears_shown_unread_and_mentions(self):
        p = self.make(resident_inbox_enabled=True)
        self.record(p, "普通消息1", mid="m1", mentioned=False)
        self.record(p, "@你消息", mid="m2", mentioned=True)
        self.record(p, "普通消息2", mid="m3", mentioned=False)

        admin_private = FakeEvent(
            is_private=True,
            session="aiocqhttp:FriendMessage:10001",
            group_id="",
            sender_id="10001",
            message_str="看看未读",
        )
        listed = json.loads(await p.read_conversation_messages(admin_private))
        self.assertEqual(listed["conversations"][0]["unread"], 3)
        self.assertEqual(listed["conversations"][0]["unread_mentions"], 1)

        unread = json.loads(
            await p.read_conversation_messages(
                admin_private,
                conversation="aiocqhttp:GroupMessage:20001",
            )
        )
        self.assertEqual(unread["count"], 3)
        self.assertTrue(unread["marked_read"])
        self.assertEqual([r["text"] for r in unread["results"]], ["普通消息1", "@你消息", "普通消息2"])
        self.assertEqual(unread["results"][1]["mention_status"], "new")

        listed_after = json.loads(await p.read_conversation_messages(admin_private))
        self.assertEqual(listed_after["conversations"][0]["unread"], 0)
        self.assertEqual(listed_after["conversations"][0]["unread_mentions"], 0)

        again = json.loads(
            await p.read_conversation_messages(
                admin_private,
                conversation="aiocqhttp:GroupMessage:20001",
            )
        )
        self.assertEqual(again["count"], 0)
        self.assertEqual(again["status"], "no_new_messages")

    async def test_disabled_inbox_data_layer_returns_zero_unread(self):
        p = self.make(resident_inbox_enabled=False)
        self.record(p, "消息", mid="m1", mentioned=True)
        admin_private = FakeEvent(
            is_private=True,
            session="aiocqhttp:FriendMessage:10001",
            group_id="",
            sender_id="10001",
            message_str="看看未读",
        )
        listed = json.loads(await p.read_conversation_messages(admin_private))
        self.assertEqual(listed["conversations"][0]["unread"], 0)
        self.assertEqual(listed["conversations"][0]["unread_mentions"], 0)
        read_res = json.loads(
            await p.read_conversation_messages(admin_private, conversation="aiocqhttp:GroupMessage:20001")
        )
        self.assertEqual(read_res["count"], 1)
        self.assertFalse(read_res["marked_read"])


if __name__ == "__main__":
    unittest.main()
