import asyncio
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from astrbot.core.message.components import Image

from main import ActivityEntry
from test_plugin_logic import make_plugin, FakeContext, FakeEvent
from resident_fixtures import ScriptedProvider


def image_file(directory, name="photo.png", body=b"fake png bytes"):
    path = Path(directory) / name
    path.write_bytes(body)
    return str(path)


def fake_provider_manager(provider):
    """最小 provider_manager 桩：只支持同步 get_using_provider 兜底。"""
    return type(
        "PM",
        (),
        {
            "get_provider_by_id": None,
            "get_using_provider": lambda *a, **k: provider,
            "get_using_provider_async": None,
        },
    )()


class VisionTest(unittest.IsolatedAsyncioTestCase):
    def make(self, **cfg):
        p = make_plugin(context=FakeContext(admins=["10001"]), **cfg)
        self.addAsyncCleanup(p.terminate)
        return p

    def record(self, p, mid="m1", sender="9"):
        return p._record_activity(
            ActivityEntry(
                ts=time.time(),
                sender_id=sender,
                sender_name="某人",
                conversation_id="20001",
                conversation_name="20001",
                kind="group",
                text="看这张图",
                platform_id="aiocqhttp",
                message_id=mid,
            )
        )

    async def wait_bg(self, p):
        """等待插件后台任务（识图转述）全部完成。"""
        tasks = list(p._resident_bg_tasks)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def test_keep_message_images_persists_and_indexes(self):
        with tempfile.TemporaryDirectory() as tmp:
            img = image_file(tmp)
            p = self.make(vision_enabled=True, image_keep_days=3)
            event = FakeEvent(components=[Image(file=img)])
            activity_id = self.record(p, "m1")
            await p.resident.vision.keep_message_images(event, activity_id, "m1")
            rows = p.resident.store.vision_kept_for(
                "aiocqhttp:GroupMessage:20001", "m1"
            )
            self.assertEqual(len(rows), 1)
            self.assertTrue(os.path.exists(rows[0]["path"]))
            self.assertIn("vision_kept", rows[0]["path"])

    async def test_keep_ignores_when_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            img = image_file(tmp)
            p = self.make(vision_enabled=False)
            event = FakeEvent(components=[Image(file=img)])
            activity_id = self.record(p, "m1")
            await p.resident.vision.keep_message_images(event, activity_id, "m1")
            self.assertEqual(
                p.resident.store.vision_kept_for("aiocqhttp:GroupMessage:20001", "m1"),
                [],
            )

    async def test_keep_skips_unreachable_refs_without_error(self):
        p = self.make(vision_enabled=True)
        event = FakeEvent(components=[Image(file="/nonexistent/ghost.png")])
        activity_id = self.record(p, "m1")
        await p.resident.vision.keep_message_images(event, activity_id, "m1")
        self.assertEqual(
            p.resident.store.vision_kept_for("aiocqhttp:GroupMessage:20001", "m1"),
            [],
        )

    async def test_background_caption_writes_brief_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            img = image_file(tmp)
            p = self.make(vision_enabled=True, vision_brief_max_chars=20)
            provider = ScriptedProvider(
                responses=[
                    SimpleNamespace(
                        completion_text="这是一张很可爱的小猫照片，颜色是白色的",
                        usage=None,
                    )
                ]
            )
            p.context.provider_manager = fake_provider_manager(provider)
            umo = "aiocqhttp:GroupMessage:20001"
            p.resident.store.vision_keep(umo, "m1", 1, 0, img)
            await p.resident.vision._caption_background(umo, "m1")
            key = p.resident.vision._cache_key(img)
            cached = p.resident.store.vision_caption_get(key)
            self.assertTrue(cached.startswith("这是一张"))
            self.assertEqual(cached, cached[:20])

    async def test_describe_once_fails_cleanly_without_provider(self):
        with tempfile.TemporaryDirectory() as tmp:
            img = image_file(tmp)
            p = self.make(vision_enabled=True)
            p.context.provider_manager = fake_provider_manager(None)
            self.assertEqual(await p.resident.vision.describe_once(img, ""), "")

    async def test_read_image_requires_kept_image(self):
        p = self.make(vision_enabled=True)
        result = await p.resident.vision.read_image(
            "aiocqhttp:GroupMessage:20001", "missing", 0
        )
        self.assertIn("找不到", result)

    async def test_read_image_out_of_range_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            img = image_file(tmp)
            p = self.make(vision_enabled=True)
            umo = "aiocqhttp:GroupMessage:20001"
            p.resident.store.vision_keep(umo, "m1", 1, 0, img)
            result = await p.resident.vision.read_image(umo, "m1", 5)
            self.assertIn("只有 1 张图", result)

    async def test_read_image_uses_cached_brief_without_question(self):
        with tempfile.TemporaryDirectory() as tmp:
            img = image_file(tmp)
            p = self.make(vision_enabled=True, vision_brief_max_chars=200)
            umo = "aiocqhttp:GroupMessage:20001"
            p.resident.store.vision_keep(umo, "m1", 1, 0, img)
            key = p.resident.vision._cache_key(img)
            p.resident.store.vision_caption_set(key, "一只白色小猫在晒太阳")
            result = await p.resident.vision.read_image(umo, "m1", 0)
            self.assertIn("白色小猫", result)

    async def test_cleanup_removes_stale_rows_and_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            img = image_file(tmp)
            p = self.make(vision_enabled=True, image_keep_days=0, vision_cache_ttl_days=0)
            umo = "aiocqhttp:GroupMessage:20001"
            p.resident.store.vision_keep(umo, "m1", 1, 0, img)
            p.resident.store.vision_caption_set("k1", "描述")
            with p.resident.store.transaction() as db:
                db.execute(
                    "UPDATE vision_kept_images SET ts=? WHERE umo=?",
                    (time.time() - 86400 * 10, umo),
                )
                db.execute(
                    "UPDATE vision_caption_cache SET ts=?",
                    (time.time() - 86400 * 10,),
                )
            rows, files, descs = p.resident.vision.cleanup()
            self.assertGreaterEqual(rows, 1)
            self.assertGreaterEqual(files, 1)
            self.assertGreaterEqual(descs, 1)

    async def test_read_image_tool_flow(self):
        """端到端：llm_tool read_image 按记录 id 找到留底图并返回后台转述。"""
        with tempfile.TemporaryDirectory() as tmp:
            img = image_file(tmp)
            p = self.make(
                vision_enabled=True,
                vision_brief_max_chars=200,
                activity_tool_enabled=True,
                activity_buffer_enabled=True,
                activity_require_admin=False,
            )
            provider = ScriptedProvider(
                responses=[
                    SimpleNamespace(
                        completion_text="图片上有一只白色的小猫在晒太阳", usage=None
                    )
                ]
            )
            p.context.provider_manager = fake_provider_manager(provider)
            activity_id = self.record(p, "m1")
            umo = "aiocqhttp:GroupMessage:20001"
            await p.resident.vision.keep_message_images(
                FakeEvent(components=[Image(file=img)]), activity_id, "m1"
            )
            await self.wait_bg(p)
            event = FakeEvent(sender_id="10001", is_admin=True)
            raw = await p.read_image(event, conversation=umo, message_id=activity_id, index=0)
            self.assertIn("白色", raw)


if __name__ == "__main__":
    unittest.main()
