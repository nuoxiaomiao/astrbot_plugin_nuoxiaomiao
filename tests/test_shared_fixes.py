"""信箱数据层核心修复的回归测试（C1 / C2 / B5）。

- C1：@全体成员 不算「点名」，只认真实 At / 回复机器人 / 唤醒前缀；
- C2：当前这条唤醒消息不算未读；
- B5：速览写入立即提交，不被无关事务的异常回滚带走；
"""

import unittest

from astrbot.core.message.components import At, AtAll, Plain, Reply

from tests.fakes import FakeContext, FakeEvent, make_plugin, record_activity


class SharedFixesTest(unittest.IsolatedAsyncioTestCase):
    def make(self, **cfg):
        p = make_plugin(context=FakeContext(admins=["10001"]), **cfg)
        self.addAsyncCleanup(p.terminate)
        return p

    def record(self, p, text, sender="9", mid="", mentioned=False):
        return record_activity(p, text, sender=sender, mid=mid, mentioned=mentioned)

    def test_at_all_is_not_a_mention(self):
        p = self.make()
        event = FakeEvent(components=[AtAll(qq="all"), Plain("公告")])
        self.assertFalse(p.activity.mentioned_by_real_at(event))

    def test_real_at_to_bot_is_a_mention(self):
        p = self.make()
        event = FakeEvent(components=[At(qq="bot"), Plain("在吗")])
        event.is_at_or_wake_command = False
        self.assertTrue(p.activity.mentioned_by_real_at(event))

    def test_at_to_someone_else_is_not_a_mention(self):
        p = self.make()
        event = FakeEvent(components=[At(qq="10002"), Plain("问他")])
        event.is_at_or_wake_command = False
        self.assertFalse(p.activity.mentioned_by_real_at(event))

    def test_reply_to_bot_is_a_mention(self):
        p = self.make()
        event = FakeEvent(components=[Reply(id="m1", sender_id="bot"), Plain("接着说")])
        event.is_at_or_wake_command = False
        self.assertTrue(p.activity.mentioned_by_real_at(event))

    def test_wake_prefix_text_is_a_mention(self):
        p = self.make()
        event = FakeEvent(message_str="小喵你看这个", components=[Plain("小喵你看这个")])
        event.is_at_or_wake_command = False
        self.assertTrue(p.activity.mentioned_by_real_at(event))

    async def test_current_message_excluded_from_unread_counts(self):
        p = self.make(resident_inbox_enabled=True)
        self.record(p, "旧消息一")
        current = self.record(p, "当前这条")
        session = {
            "platform_id": "aiocqhttp",
            "kind": "group",
            "conversation_id": "20001",
            "name": "20001",
        }
        persona = "@session:aiocqhttp:GroupMessage:20001"
        counts = p.resident.inbox.counts(persona, session, exclude_ids=[current])
        self.assertEqual(counts["unread"], 1)

    async def test_record_survives_failed_store_transaction(self):
        p = self.make()
        self.record(p, "先记一条")
        try:
            with p.resident.store.transaction() as db:
                db.execute("SELECT 1")
                raise RuntimeError("模拟事务失败")
        except RuntimeError:
            pass
        rows = p.resident.store.rows(
            "SELECT text FROM activity_messages WHERE conversation_id='20001'"
        )
        self.assertEqual([row["text"] for row in rows], ["先记一条"])


if __name__ == "__main__":
    unittest.main()
