"""共享记忆与信箱这一批修复的回归测试（C1 / C2 / B5 / 诊断 / 主脑锁）。

对应 2026-11 的审查清单：
- C1：@全体成员 不算「点名」，只认真实 At / 回复机器人 / 唤醒前缀；
- C2：当前这条唤醒消息不算未读；
- B5：速览写入立即提交，不被无关事务的异常回滚带走；
- D3：记录关闭时信箱/话题要在配置诊断里给出提示；
- 93：主脑锁等待超时降级、失败不泄漏。
"""

import time
import unittest

from astrbot.core.message.components import At, AtAll, Plain, Reply

from main import ActivityEntry, NO_BRAIN_EXTRA
from test_plugin_logic import make_plugin, FakeContext, FakeEvent


class SharedFixesTest(unittest.IsolatedAsyncioTestCase):
    def make(self, **cfg):
        p = make_plugin(context=FakeContext(admins=["10001"]), **cfg)
        self.addAsyncCleanup(p.terminate)
        return p

    def record(self, p, text, sender="9", mid="", mentioned=False):
        return p._record_activity(
            ActivityEntry(
                ts=time.time(),
                sender_id=sender,
                sender_name="某人",
                conversation_id="20001",
                conversation_name="20001",
                kind="group",
                text=text,
                platform_id="aiocqhttp",
                message_id=mid or f"mid-{time.time_ns()}",
                mentioned=mentioned,
            )
        )

    # ------------------------------------------------------------------
    # C1：mentioned 只认真实 @
    # ------------------------------------------------------------------

    def test_at_all_is_not_a_mention(self):
        p = self.make()
        event = FakeEvent(components=[AtAll(qq="all"), Plain("公告")])
        self.assertFalse(p._mentioned_by_real_at(event))

    def test_real_at_to_bot_is_a_mention(self):
        p = self.make()
        event = FakeEvent(components=[At(qq="bot"), Plain("在吗")])
        event.is_at_or_wake_command = False
        self.assertTrue(p._mentioned_by_real_at(event))

    def test_at_to_someone_else_is_not_a_mention(self):
        p = self.make()
        event = FakeEvent(components=[At(qq="10002"), Plain("问他")])
        event.is_at_or_wake_command = False
        self.assertFalse(p._mentioned_by_real_at(event))

    def test_reply_to_bot_is_a_mention(self):
        p = self.make()
        event = FakeEvent(components=[Reply(id="m1", sender_id="bot"), Plain("接着说")])
        event.is_at_or_wake_command = False
        self.assertTrue(p._mentioned_by_real_at(event))

    def test_wake_prefix_text_is_a_mention(self):
        p = self.make()
        event = FakeEvent(message_str="小喵你看这个", components=[Plain("小喵你看这个")])
        event.is_at_or_wake_command = False
        # 唤醒前缀是「小喵」，q=叫了名字也算叫机器人。
        self.assertTrue(p._mentioned_by_real_at(event))

    # ------------------------------------------------------------------
    # C2：当前这条不算未读
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # B5：速览写入立即提交，不被别的事务异常回滚
    # ------------------------------------------------------------------

    async def test_record_survives_failed_store_transaction(self):
        p = self.make()
        self.record(p, "先记一条")
        # 模拟别的功能在同一个连接上开事务后抛异常。
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

    # ------------------------------------------------------------------
    # D3：配置诊断
    # ------------------------------------------------------------------

    def test_diagnostics_flag_inbox_and_topic_without_recording(self):
        p = self.make(
            unread_inbox_enabled=True,
            topic_summary_enabled=True,
            activity_buffer_enabled=False,
        )
        codes = {item["code"] for item in p._configuration_diagnostics()}
        self.assertIn("inbox_requires_recording", codes)
        self.assertIn("topic_requires_recording", codes)

    def test_no_diagnostics_when_recording_on(self):
        p = self.make(
            unread_inbox_enabled=True,
            topic_summary_enabled=True,
            activity_buffer_enabled=True,
        )
        codes = {item["code"] for item in p._configuration_diagnostics()}
        self.assertNotIn("inbox_requires_recording", codes)
        self.assertNotIn("topic_requires_recording", codes)

    # ------------------------------------------------------------------
    # 93：主脑锁
    # ------------------------------------------------------------------

    async def test_bind_master_respects_timeout_flag(self):
        p = self.make(shared_memory_enabled=True, master_umo="aiocqhttp:FriendMessage:1")
        event = FakeEvent(session="aiocqhttp:FriendMessage:1", is_private=True)
        event.set_extra(NO_BRAIN_EXTRA, True)
        self.assertFalse(await p.resident.master.bind_master(event, ProviderRequestStub()))

    async def test_release_brain_is_safe_without_owner(self):
        p = self.make(shared_memory_enabled=True, master_umo="aiocqhttp:FriendMessage:1")
        # 没拿过锁也要能安全调用（异常路径兜底不会抛）。
        p.resident.master.release_brain()
        p.resident.master.release_brain()

    async def test_no_brain_lock_when_shared_memory_off(self):
        p = self.make(shared_memory_enabled=False)
        event = FakeEvent()
        await p.on_waiting_llm_request(event)
        self.assertIsNone(p.resident.master._brain_owner)


class ProviderRequestStub:
    """ProviderRequest 的最小替身：只带 bind_master 会改的字段。"""

    def __init__(self):
        self.system_prompt = ""
        self.conversation = None
        self.contexts = []
        self.func_tool = None
        self.prompt = ""
