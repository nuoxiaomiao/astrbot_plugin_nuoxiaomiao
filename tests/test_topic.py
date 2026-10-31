import json
import time
import unittest
from types import SimpleNamespace

from main import ActivityEntry
from resident.topic import Topic
from test_plugin_logic import make_plugin, FakeContext
from resident_fixtures import ScriptedProvider


def fake_provider_manager(provider):
    return type(
        "PM",
        (),
        {
            "get_provider_by_id": None,
            "get_using_provider": lambda *a, **k: provider,
            "get_using_provider_async": None,
        },
    )()


class TopicTest(unittest.IsolatedAsyncioTestCase):
    def make(self, **cfg):
        p = make_plugin(context=FakeContext(admins=["10001"]), **cfg)
        self.addAsyncCleanup(p.terminate)
        return p

    def record(self, p, text, sender="9", name="", mentioned=False):
        return p._record_activity(
            ActivityEntry(
                ts=time.time(),
                sender_id=sender,
                sender_name=name,
                conversation_id="20001",
                conversation_name="20001",
                kind="group",
                text=text,
                platform_id="aiocqhttp",
                message_id=f"mid-{time.time_ns()}",
                mentioned=mentioned,
            )
        )

    def seed(self, p, count, min_unread=5):
        setattr(p, "topic_min_unread", min_unread)
        for i in range(count):
            self.record(p, f"第{i}条消息")

    # ------------------------------------------------------------------

    def test_build_material_anonymizes_unknown_senders(self):
        p = self.make(topic_summary_enabled=True)
        rows = [
            {"id": 1, "ts": time.time(), "text": "早上好", "sender_id": "9",
             "sender_name": "", "mentioned": 0},
            {"id": 2, "ts": time.time(), "text": "今天去哪", "sender_id": "8",
             "sender_name": "", "mentioned": 0},
            {"id": 3, "ts": time.time(), "text": "去公园", "sender_id": "9",
             "sender_name": "", "mentioned": 0},
            {"id": 4, "ts": time.time(), "text": "好的", "sender_id": "bot",
             "sender_name": "糯小喵", "mentioned": 0},
            {"id": 5, "ts": time.time(), "text": "记得叫我", "sender_id": "7",
             "sender_name": "小丽", "mentioned": 0},
        ]
        material = p.resident.topic._build_material(
            "aiocqhttp:GroupMessage:20001", rows, backfill=0
        )
        self.assertIn("甲：早上好", material)
        self.assertIn("乙：今天去哪", material)
        self.assertIn("甲：去公园", material)
        self.assertIn("机器人自己：好的", material)
        self.assertIn("小丽：记得叫我", material)
        self.assertNotIn("sender_id", material)

    def test_parse_topic_extracts_json(self):
        topic, summary = Topic._parse_topic(
            '好的{"topic":"大家在聊周末去哪","summary":"几个人商量周末去公园"}完毕', 24
        )
        self.assertEqual(topic, "大家在聊周末去哪")
        self.assertEqual(summary, "几个人商量周末去公园")

    def test_parse_topic_handles_garbage(self):
        self.assertEqual(Topic._parse_topic("完全没有 json", 24), ("", ""))
        self.assertEqual(Topic._parse_topic("", 24), ("", ""))
        self.assertEqual(Topic._parse_topic('{"no_topic":1}', 24), ("", ""))

    def test_parse_topic_truncates_to_max_chars(self):
        topic, _ = Topic._parse_topic('{"topic":"这是一个很长很长很长很长的句子"}', 6)
        self.assertEqual(len(topic), 6)

    async def test_summarize_skips_when_too_few_rows(self):
        p = self.make(topic_summary_enabled=True, topic_min_unread=5)
        self.seed(p, 3, min_unread=5)
        provider = ScriptedProvider(
            responses=[
                SimpleNamespace(
                    completion_text='{"topic":"太少","summary":"不足"}', usage=None
                )
            ]
        )
        p.context.provider_manager = fake_provider_manager(provider)
        result = await p.resident.topic.summarize("aiocqhttp:GroupMessage:20001")
        self.assertFalse(result)
        self.assertEqual(p.resident.store.topic_segments("aiocqhttp:GroupMessage:20001"), [])

    async def test_summarize_writes_segment_and_advances_cursor(self):
        p = self.make(topic_summary_enabled=True, topic_min_unread=5)
        self.seed(p, 8, min_unread=5)
        provider = ScriptedProvider(
            responses=[
                SimpleNamespace(
                    completion_text='{"topic":"大家在聊周末活动","summary":"商量去公园"}',
                    usage=None,
                )
            ]
        )
        p.context.provider_manager = fake_provider_manager(provider)
        umo = "aiocqhttp:GroupMessage:20001"
        result = await p.resident.topic.summarize(umo)
        self.assertTrue(result)
        state = p.resident.store.topic_state(umo)
        self.assertEqual(state["topic"], "大家在聊周末活动")
        segments = p.resident.store.topic_segments(umo)
        self.assertEqual(len(segments), 1)
        self.assertGreaterEqual(state["topic_to_id"], 5)

    async def test_summarize_skips_when_disabled(self):
        p = self.make(topic_summary_enabled=False)
        self.seed(p, 8, min_unread=5)
        result = await p.resident.topic.summarize("aiocqhttp:GroupMessage:20001")
        self.assertFalse(result)

    async def test_summarize_ignores_garbage_output(self):
        p = self.make(topic_summary_enabled=True, topic_min_unread=5)
        self.seed(p, 8, min_unread=5)
        provider = ScriptedProvider(
            responses=[
                SimpleNamespace(completion_text="完全不是 json", usage=None)
            ]
        )
        p.context.provider_manager = fake_provider_manager(provider)
        umo = "aiocqhttp:GroupMessage:20001"
        result = await p.resident.topic.summarize(umo)
        self.assertFalse(result)
        self.assertEqual(p.resident.store.topic_segments(umo), [])

    async def test_maybe_summarize_respects_cooldown(self):
        p = self.make(topic_summary_enabled=True, topic_min_unread=5, topic_cooldown_seconds=999)
        self.seed(p, 8, min_unread=5)
        provider = ScriptedProvider(
            responses=[
                SimpleNamespace(
                    completion_text='{"topic":"在聊周末","summary":"商量活动"}', usage=None
                )
            ]
        )
        p.context.provider_manager = fake_provider_manager(provider)
        umo = "aiocqhttp:GroupMessage:20001"
        # 冷启动：无话题状态 → 冷却视为过期，触发一次概括。
        triggered = await p.resident.topic.maybe_summarize(umo)
        self.assertTrue(triggered)
        await asyncio_gather_bg(p)
        # 刚概括完：冷却期内不再触发。
        second = await p.resident.topic.maybe_summarize(umo)
        self.assertFalse(second)
        self.assertEqual(p.resident.store.topic_state(umo)["topic"], "在聊周末")


async def asyncio_gather_bg(p):
    import asyncio

    tasks = list(p._resident_bg_tasks)
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


if __name__ == "__main__":
    unittest.main()
