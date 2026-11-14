"""Debounce, merge buffer, image localization, and dynamic idle window tests."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

from astrbot.core.message.components import At, Image, Plain, Reply

from infra.constants import MergeSession, RunState
from tests.fakes import FakeContext, FakeEvent, make_plugin


class MergeLogicTest(unittest.IsolatedAsyncioTestCase):
    async def test_wake_starts_merge_and_blocks_llm(self):
        plugin = make_plugin(
            merge_idle_seconds=30, merge_max_total_seconds=60, debounce_long_chars=100
        )
        plugin.merger.send_merged_event = AsyncMock()

        event = FakeEvent(message_str="小喵你的尾巴本来就是归我管的")
        await plugin.on_message(event)

        key = plugin.merger.merge_key(event.unified_msg_origin, "10001")
        self.assertIn(key, plugin.merger._merges)
        self.assertEqual(
            plugin.merger._merges[key].parts,
            ["小喵你的尾巴本来就是归我管的"],
        )
        self.assertTrue(event.call_llm)
        self.assertTrue(event.stopped)
        self.assertTrue(event.extras["nuoxiaomiao_buffered"])

        await plugin.terminate()

    async def test_followup_appends_without_wake(self):
        plugin = make_plugin(
            merge_idle_seconds=30, merge_max_total_seconds=60, debounce_long_chars=100
        )
        plugin.merger.send_merged_event = AsyncMock()

        e1 = FakeEvent(message_str="小喵你的尾巴本来就是归我管的")
        e2 = FakeEvent(is_trigger=False, message_str="揉两下那叫例行检查")
        e3 = FakeEvent(is_trigger=False, message_str="什么凑热闹")

        await plugin.on_message(e1)
        await plugin.on_message(e2)
        await plugin.on_message(e3)

        key = plugin.merger.merge_key(e1.unified_msg_origin, "10001")
        self.assertEqual(
            plugin.merger._merges[key].parts,
            [
                "小喵你的尾巴本来就是归我管的",
                "揉两下那叫例行检查",
                "什么凑热闹",
            ],
        )
        self.assertTrue(e1.stopped)
        self.assertTrue(e2.stopped)
        self.assertTrue(e3.stopped)
        await plugin.terminate()

    async def test_image_only_trigger_starts_merge(self):
        plugin = make_plugin(merge_idle_seconds=30, merge_max_total_seconds=60)
        image = Image(file="https://example.invalid/first.png")
        event = FakeEvent(message_str="", components=[image])

        await plugin.on_message(event)

        key = plugin.merger.merge_key(event.unified_msg_origin, "10001")
        self.assertIn(key, plugin.merger._merges)
        self.assertEqual(plugin.merger._merges[key].parts, [""])
        self.assertEqual(plugin.merger._merges[key].component_parts, [[image]])
        self.assertTrue(event.stopped)
        await plugin.terminate()

    async def test_image_followup_is_buffered(self):
        plugin = make_plugin(merge_idle_seconds=30, merge_max_total_seconds=60)
        image = Image(file="https://example.invalid/followup.png")
        first = FakeEvent(message_str="hello", components=[Plain("hello")])
        followup = FakeEvent(is_trigger=False, message_str="", components=[image])

        await plugin.on_message(first)
        await plugin.on_message(followup)

        key = plugin.merger.merge_key(first.unified_msg_origin, "10001")
        self.assertEqual(plugin.merger._merges[key].parts, ["hello", ""])
        self.assertEqual(plugin.merger._merges[key].component_parts[1], [image])
        self.assertTrue(followup.stopped)
        await plugin.terminate()

    async def test_interleaved_components_preserve_order_on_release(self):
        plugin = make_plugin(
            context=FakeContext(["/"]),
            merge_idle_seconds=30,
            merge_max_total_seconds=60,
        )
        first_image = Image(file="https://example.invalid/first.png")
        second_image = Image(file="https://example.invalid/second.png")
        events = [
            FakeEvent(message_str="one", components=[Plain("one"), first_image]),
            FakeEvent(is_trigger=False, message_str="", components=[second_image]),
            FakeEvent(
                is_trigger=False,
                message_str="two",
                components=[Plain("two")],
            ),
        ]
        for event in events:
            await plugin.on_message(event)

        created = []

        async def create_message(**kwargs):
            created.append(kwargs)
            return type("Synthetic", (), {"message_id": "interleaved-mid"})()

        with (
            patch(
                "astrbot.core.star.star_tools.StarTools.create_message",
                new=create_message,
            ),
            patch(
                "astrbot.core.star.star_tools.StarTools.create_event",
                new=AsyncMock(),
            ),
        ):
            key = plugin.merger.merge_key(events[0].unified_msg_origin, "10001")
            await plugin.merger.release_merge(key, reason="test")

        components = created[0]["message"]
        self.assertEqual(
            [type(component).__name__ for component in components],
            ["Plain", "Plain", "Image", "Image", "Plain"],
        )
        self.assertEqual(components[0].text, "/")
        self.assertEqual(components[1].text, "one")
        self.assertIs(components[2], first_image)
        self.assertIs(components[3], second_image)
        self.assertEqual(components[4].text, "\ntwo")
        self.assertEqual(created[0]["message_str"], "/one\ntwo")
        await plugin.terminate()

    async def test_other_sender_does_not_append(self):
        plugin = make_plugin(merge_idle_seconds=30, merge_max_total_seconds=60)
        plugin.merger.send_merged_event = AsyncMock()

        e1 = FakeEvent(sender_id="10001", message_str="小喵你好")
        e2 = FakeEvent(
            sender_id="10002",
            is_trigger=False,
            message_str="别人插话",
        )
        await plugin.on_message(e1)
        await plugin.on_message(e2)

        key = plugin.merger.merge_key(e1.unified_msg_origin, "10001")
        self.assertEqual(plugin.merger._merges[key].parts, ["小喵你好"])
        await plugin.terminate()

    async def test_release_merges_text(self):
        plugin = make_plugin(merge_idle_seconds=30, merge_max_total_seconds=60)
        released = []

        async def capture(ms, full_text):
            released.append((list(ms.parts), full_text))

        plugin.merger.send_merged_event = capture

        e1 = FakeEvent(message_str="小喵句一")
        e2 = FakeEvent(is_trigger=False, message_str="句二")
        await plugin.on_message(e1)
        await plugin.on_message(e2)
        key = plugin.merger.merge_key(e1.unified_msg_origin, "10001")
        await plugin.merger.release_merge(key, reason="test")

        self.assertTrue(released)
        parts, full = released[0]
        self.assertEqual(parts, ["小喵句一", "句二"])
        self.assertIn("小喵句一", full)
        self.assertIn("句二", full)
        self.assertNotIn(key, plugin.merger._merges)
        await plugin.terminate()

    async def test_max_total_force_release(self):
        plugin = make_plugin(merge_idle_seconds=30, merge_max_total_seconds=1)
        released = []

        async def capture(ms, full_text):
            released.append(full_text)

        plugin.merger.send_merged_event = capture

        await plugin.on_message(FakeEvent(message_str="小喵超时测试"))
        for _ in range(80):
            if released:
                break
            await asyncio.sleep(0.05)

        self.assertTrue(released)
        self.assertIn("超时测试", released[0])
        await plugin.terminate()

    async def test_guard_counts_each_completed_batch_once(self):
        plugin = make_plugin(
            trigger_rounds=3,
            merge_idle_seconds=30,
            merge_max_total_seconds=60,
        )
        plugin.merger.send_merged_event = AsyncMock()

        events = []
        for index in range(1, 4):
            event = FakeEvent(message_str=f"小喵第{index}轮")
            events.append(event)
            await plugin.on_message(event)
            key = plugin.merger.merge_key(event.unified_msg_origin, "10001")
            await plugin.merger.release_merge(key, reason="test")

        self.assertEqual(plugin.merger.send_merged_event.await_count, 2)
        self.assertNotIn("nuoxiaomiao_blocked", events[0].extras)
        self.assertNotIn("nuoxiaomiao_blocked", events[1].extras)
        self.assertTrue(events[2].extras["nuoxiaomiao_blocked"])
        await plugin.terminate()

    async def test_skip_id_does_not_rebuffer(self):
        plugin = make_plugin(merge_idle_seconds=30, merge_max_total_seconds=60)
        plugin.merger.send_merged_event = AsyncMock()

        mid = "synthetic-1"
        await plugin.merger.mark_synthetic_id(mid)
        event = FakeEvent(message_str="小喵合成放行", message_id=mid)
        await plugin.on_message(event)

        key = plugin.merger.merge_key(event.unified_msg_origin, "10001")
        self.assertNotIn(key, plugin.merger._merges)
        self.assertFalse(event.stopped)
        self.assertFalse(event.call_llm)
        self.assertEqual(plugin.guard._states, {})
        await plugin.terminate()

    async def test_concurrent_triggers_are_kept_in_one_buffer(self):
        plugin = make_plugin(merge_idle_seconds=30, merge_max_total_seconds=60)
        plugin.merger.send_merged_event = AsyncMock()
        first = FakeEvent(message_str="小喵并发一")
        second = FakeEvent(message_str="小喵并发二")

        await asyncio.gather(
            plugin.on_message(first),
            plugin.on_message(second),
        )

        key = plugin.merger.merge_key(first.unified_msg_origin, "10001")
        self.assertEqual(
            plugin.merger._merges[key].parts,
            ["小喵并发一", "小喵并发二"],
        )
        self.assertTrue(first.stopped)
        self.assertTrue(second.stopped)
        await plugin.terminate()

    async def test_expired_runtime_state_is_pruned(self):
        plugin = make_plugin(
            window_seconds=10,
            meme_cooldown_seconds=10,
        )
        now = time.monotonic()
        plugin.guard._states["old-session"] = RunState("10001", 2, now - 11)
        plugin.guard._last_meme_at[("old-session", "10001")] = now - 11
        plugin.merger._skip_merge_ids.add("old-synthetic")
        plugin.merger._skip_merge_timestamps["old-synthetic"] = now - 301

        await plugin.guard.prune_expired(now)
        await plugin.merger.prune_expired(now)

        self.assertEqual(plugin.guard._states, {})
        self.assertEqual(plugin.guard._last_meme_at, {})
        self.assertEqual(plugin.merger._skip_merge_ids, set())
        self.assertEqual(plugin.merger._skip_merge_timestamps, {})
        await plugin.terminate()

    async def test_synthetic_id_is_rolled_back_when_submission_fails(self):
        plugin = make_plugin()
        origin = FakeEvent(message_str="小喵提交失败")
        session = MergeSession(
            session_key=origin.unified_msg_origin,
            sender_id="10001",
            parts=[origin.message_str],
            origin_event=origin,
        )
        new_message = type("Synthetic", (), {"message_id": "failed-mid"})()

        create_event = AsyncMock(side_effect=RuntimeError("submit failed"))
        with (
            patch(
                "astrbot.core.star.star_tools.StarTools.create_message",
                new=AsyncMock(return_value=new_message),
            ),
            patch(
                "astrbot.core.star.star_tools.StarTools.create_event",
                new=create_event,
            ),
            patch("pipeline.debounce_merge.asyncio.sleep", new=AsyncMock()),
        ):
            with self.assertRaisesRegex(RuntimeError, "submit failed"):
                await plugin.merger.send_merged_event(session, origin.message_str)

        self.assertNotIn("failed-mid", plugin.merger._skip_merge_ids)
        self.assertNotIn("failed-mid", plugin.merger._skip_merge_timestamps)
        self.assertEqual(create_event.await_count, 2)
        await plugin.terminate()

    async def test_submission_retry_succeeds_and_retains_only_successful_id(self):
        plugin = make_plugin(context=FakeContext(["/"]))
        origin = FakeEvent(message_str="retry", components=[Plain("retry")])
        session = MergeSession(
            session_key=origin.unified_msg_origin,
            sender_id="10001",
            parts=[origin.message_str],
            component_parts=[[Plain("retry")]],
            origin_event=origin,
        )
        messages = [
            type("Synthetic", (), {"message_id": "failed-mid"})(),
            type("Synthetic", (), {"message_id": "successful-mid"})(),
        ]
        create_event = AsyncMock(side_effect=[RuntimeError("temporary"), None])

        with (
            patch(
                "astrbot.core.star.star_tools.StarTools.create_message",
                new=AsyncMock(side_effect=messages),
            ),
            patch(
                "astrbot.core.star.star_tools.StarTools.create_event",
                new=create_event,
            ),
            patch("pipeline.debounce_merge.asyncio.sleep", new=AsyncMock()),
        ):
            await plugin.merger.send_merged_event(session, origin.message_str)

        self.assertEqual(create_event.await_count, 2)
        self.assertNotIn("failed-mid", plugin.merger._skip_merge_ids)
        self.assertIn("successful-mid", plugin.merger._skip_merge_ids)
        await plugin.terminate()

    async def test_release_failure_notifies_user(self):
        plugin = make_plugin()
        origin = FakeEvent(message_str="cannot deliver")
        key = plugin.merger.merge_key(origin.unified_msg_origin, "10001")
        plugin.merger._merges[key] = MergeSession(
            session_key=origin.unified_msg_origin,
            sender_id="10001",
            parts=[origin.message_str],
            origin_event=origin,
        )
        plugin.merger.send_merged_event = AsyncMock(side_effect=RuntimeError("failed"))

        await plugin.merger.release_merge(key, reason="test")

        self.assertEqual(len(origin.sent), 1)
        self.assertIn("重新发送", origin.sent[0].chain[0].text)
        await plugin.terminate()

    async def test_group_release_uses_active_wake_prefix(self):
        plugin = make_plugin(context=FakeContext(["/"]))
        origin = FakeEvent(message_str="hello", components=[Plain("/hello")])
        session = MergeSession(
            session_key=origin.unified_msg_origin,
            sender_id="10001",
            parts=[origin.message_str],
            component_parts=[[Plain("/hello")]],
            origin_event=origin,
        )
        created = []

        async def create_message(**kwargs):
            created.append(kwargs)
            return type("Synthetic", (), {"message_id": "wake-mid"})()

        with (
            patch(
                "astrbot.core.star.star_tools.StarTools.create_message",
                new=create_message,
            ),
            patch(
                "astrbot.core.star.star_tools.StarTools.create_event",
                new=AsyncMock(),
            ),
        ):
            await plugin.merger.send_merged_event(session, origin.message_str)

        self.assertEqual(created[0]["message_str"], "/hello")
        self.assertEqual(created[0]["message"][0].text, "/")
        await plugin.terminate()

    async def test_at_reply_and_private_release_need_no_text_prefix(self):
        cases = [
            (
                FakeEvent(message_str="at", components=[At(qq="bot"), Plain("at")]),
                [At(qq="bot"), Plain("at")],
            ),
            (
                FakeEvent(
                    message_str="reply",
                    components=[Reply(id="1", sender_id="bot"), Plain("reply")],
                ),
                [Reply(id="1", sender_id="bot"), Plain("reply")],
            ),
            (
                FakeEvent(
                    session="aiocqhttp:FriendMessage:10001",
                    group_id="",
                    is_private=True,
                    message_str="private",
                    components=[Plain("private")],
                ),
                [Plain("private")],
            ),
        ]

        for index, (origin, components) in enumerate(cases):
            with self.subTest(index=index):
                plugin = make_plugin(context=FakeContext(["/"]))
                session = MergeSession(
                    session_key=origin.unified_msg_origin,
                    sender_id="10001",
                    parts=[origin.message_str],
                    component_parts=[components],
                    origin_event=origin,
                )
                created = []

                async def create_message(**kwargs):
                    created.append(kwargs)
                    return type(
                        "Synthetic",
                        (),
                        {"message_id": f"case-{index}"},
                    )()

                with (
                    patch(
                        "astrbot.core.star.star_tools.StarTools.create_message",
                        new=create_message,
                    ),
                    patch(
                        "astrbot.core.star.star_tools.StarTools.create_event",
                        new=AsyncMock(),
                    ),
                ):
                    await plugin.merger.send_merged_event(session, origin.message_str)

                self.assertEqual(created[0]["message_str"], origin.message_str)
                self.assertFalse(created[0]["message_str"].startswith("/"))
                await plugin.terminate()


class MergeImageLocalizationTest(unittest.IsolatedAsyncioTestCase):
    """合并缓冲时把 Image 提前本地化，避免后续识图拿不到字节。"""

    def _make_plugin(self, localize_dir: str, **cfg):
        return make_plugin(
            merge_localize_images=True,
            merge_localize_images_dir=localize_dir,
            merge_idle_seconds=30,
            merge_max_total_seconds=60,
            **cfg,
        )

    async def test_http_image_is_downloaded_and_component_rewritten(self):
        with tempfile.TemporaryDirectory() as localize_dir:
            plugin = self._make_plugin(localize_dir)
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                tmp.write(b"\x89PNG\r\n\x1a\n fake png bytes")
                downloaded_path = tmp.name

            async def fake_to_path(self, *, target_format=None, preserve_mp3=False):
                return downloaded_path

            image = Image(file="https://multimedia.nt.qq.com.cn/download?rkey=expired")
            event = FakeEvent(message_str="", components=[image])

            with patch("infra.media.MediaResolver.to_path", new=fake_to_path):
                await plugin.on_message(event)

            key = plugin.merger.merge_key(event.unified_msg_origin, "10001")
            buffered = plugin.merger._merges[key].component_parts[0][0]
            self.assertTrue(buffered.url.startswith("file://"))
            self.assertTrue(os.path.exists(buffered.path))
            self.assertTrue(Path(buffered.path).parent.resolve() == Path(localize_dir).resolve())
            os.unlink(downloaded_path)
            await plugin.terminate()

    async def test_temp_local_path_is_copied_to_persist_dir(self):
        with tempfile.TemporaryDirectory() as localize_dir:
            plugin = self._make_plugin(localize_dir)
            with tempfile.TemporaryDirectory() as tmp_dir:
                src = Path(tmp_dir) / "temp_image.png"
                src.write_bytes(b"\x89PNG temp image bytes")
                local_path = str(src)

                called = []

                async def fake_to_path(self, *, target_format=None, preserve_mp3=False):
                    called.append(True)
                    return local_path

                image = Image(file=local_path)
                event = FakeEvent(message_str="", components=[image])

                with patch("infra.media.MediaResolver.to_path", new=fake_to_path):
                    await plugin.on_message(event)

                key = plugin.merger.merge_key(event.unified_msg_origin, "10001")
                buffered = plugin.merger._merges[key].component_parts[0][0]
                self.assertEqual(called, [])
                self.assertTrue(buffered.url.startswith("file://"))
                self.assertTrue(os.path.exists(buffered.path))
                persisted = list(Path(localize_dir).iterdir())
                self.assertEqual(len(persisted), 1)
                self.assertNotEqual(
                    os.path.abspath(buffered.path),
                    os.path.abspath(local_path),
                )
            await plugin.terminate()

    async def test_already_in_persist_dir_is_skipped(self):
        with tempfile.TemporaryDirectory() as localize_dir:
            plugin = self._make_plugin(localize_dir)
            existing = Path(localize_dir) / "already_here.png"
            existing.write_bytes(b"persisted png")

            called = []

            async def fake_to_path(self, *, target_format=None, preserve_mp3=False):
                called.append(True)
                return str(existing)

            image = Image(file=str(existing))
            event = FakeEvent(message_str="", components=[image])

            with patch("infra.media.MediaResolver.to_path", new=fake_to_path):
                await plugin.on_message(event)

            key = plugin.merger.merge_key(event.unified_msg_origin, "10001")
            buffered = plugin.merger._merges[key].component_parts[0][0]
            self.assertEqual(called, [])
            self.assertEqual(buffered.file, str(existing))
            self.assertEqual(len(list(Path(localize_dir).iterdir())), 1)
            await plugin.terminate()

    async def test_missing_local_path_falls_back_to_download(self):
        with tempfile.TemporaryDirectory() as localize_dir:
            plugin = self._make_plugin(localize_dir)
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                tmp.write(b"\x89PNG downloaded png")
                downloaded_path = tmp.name

            called = []

            async def fake_to_path(self, *, target_format=None, preserve_mp3=False):
                called.append(True)
                return downloaded_path

            image = Image(file="https://example.test/missing.png")
            event = FakeEvent(message_str="", components=[image])

            with patch("infra.media.MediaResolver.to_path", new=fake_to_path):
                await plugin.on_message(event)

            key = plugin.merger.merge_key(event.unified_msg_origin, "10001")
            buffered = plugin.merger._merges[key].component_parts[0][0]
            self.assertEqual(len(called), 1)
            self.assertTrue(buffered.url.startswith("file://"))
            self.assertTrue(os.path.exists(buffered.path))
            os.unlink(downloaded_path)
            await plugin.terminate()

    async def test_download_failure_keeps_original_component(self):
        with tempfile.TemporaryDirectory() as localize_dir:
            plugin = self._make_plugin(localize_dir)

            async def fake_to_path(self, *, target_format=None, preserve_mp3=False):
                raise RuntimeError("network unavailable")

            image = Image(file="https://example.invalid/lost.png")
            event = FakeEvent(message_str="", components=[image])

            with patch("infra.media.MediaResolver.to_path", new=fake_to_path):
                await plugin.on_message(event)

            key = plugin.merger.merge_key(event.unified_msg_origin, "10001")
            buffered = plugin.merger._merges[key].component_parts[0][0]
            self.assertIs(buffered, image)
            self.assertEqual(buffered.file, "https://example.invalid/lost.png")
            await plugin.terminate()

    async def test_disabled_keeps_original_component(self):
        with tempfile.TemporaryDirectory() as localize_dir:
            plugin = make_plugin(
                merge_localize_images=False,
                merge_localize_images_dir=localize_dir,
                merge_idle_seconds=30,
                merge_max_total_seconds=60,
            )
            called = []

            async def fake_to_path(self, *, target_format=None, preserve_mp3=False):
                called.append(True)
                return "/nonexistent"

            image = Image(file="https://example.invalid/disabled.png")
            event = FakeEvent(message_str="", components=[image])

            with patch("infra.media.MediaResolver.to_path", new=fake_to_path):
                await plugin.on_message(event)

            key = plugin.merger.merge_key(event.unified_msg_origin, "10001")
            buffered = plugin.merger._merges[key].component_parts[0][0]
            self.assertEqual(called, [])
            self.assertIs(buffered, image)
            await plugin.terminate()

    async def test_non_image_components_are_passed_through(self):
        with tempfile.TemporaryDirectory() as localize_dir:
            plugin = self._make_plugin(localize_dir)
            called = []

            async def fake_to_path(self, *, target_format=None, preserve_mp3=False):
                called.append(True)
                return "/nonexistent"

            plain = Plain("hi")
            at = At(qq="bot")
            event = FakeEvent(message_str="hi", components=[plain, at])

            with patch("infra.media.MediaResolver.to_path", new=fake_to_path):
                await plugin.on_message(event)

            key = plugin.merger.merge_key(event.unified_msg_origin, "10001")
            buffered = plugin.merger._merges[key].component_parts[0]
            self.assertEqual(called, [])
            self.assertIn(plain, buffered)
            self.assertIn(at, buffered)
            await plugin.terminate()

    async def test_localized_files_cleaned_up_on_llm_response(self):
        with tempfile.TemporaryDirectory() as localize_dir:
            plugin = self._make_plugin(localize_dir)
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                tmp.write(b"\x89PNG fake")
                downloaded_path = tmp.name

            async def fake_to_path(self, *, target_format=None, preserve_mp3=False):
                return downloaded_path

            image = Image(file="https://multimedia.nt.qq.com.cn/download?rkey=test")
            event = FakeEvent(message_str="", components=[image])

            with patch("infra.media.MediaResolver.to_path", new=fake_to_path):
                await plugin.on_message(event)

            key = plugin.merger.merge_key(event.unified_msg_origin, "10001")
            buffered = plugin.merger._merges[key].component_parts[0][0]
            persisted_path = buffered.path
            self.assertTrue(os.path.exists(persisted_path))
            epoch = plugin.merger._merges[key].epoch
            composite = (key, epoch)
            self.assertIn(composite, plugin.media._tracked)
            self.assertEqual(len(plugin.media._tracked[composite]), 1)

            event.set_extra("nuoxiaomiao_merge_epoch", epoch)
            response = type("R", (), {"completion_text": "ok"})()
            await plugin.on_llm_response(event, response)
            self.assertFalse(os.path.exists(persisted_path))
            self.assertNotIn(composite, plugin.media._tracked)

            os.unlink(downloaded_path)
            await plugin.terminate()

    async def test_orphan_sweep_removes_only_stale_own_files(self):
        with tempfile.TemporaryDirectory() as localize_dir:
            plugin = self._make_plugin(localize_dir)
            stale = Path(localize_dir) / "img_20260101000000_123_abcdef1234.jpg"
            fresh = Path(localize_dir) / "img_20990101000000_123_abcdef1234.png"
            almost = Path(localize_dir) / "img_2026010100000_1_abcdef1234.jpg"
            foreign = Path(localize_dir) / "someone_else.jpg"
            for path in (stale, fresh, almost, foreign):
                path.write_bytes(b"x")
            old_ts = time.time() - 7200
            os.utime(stale, (old_ts, old_ts))
            os.utime(almost, (old_ts, old_ts))
            os.utime(foreign, (old_ts, old_ts))

            removed = plugin.media.sweep_orphans(3600.0)

            self.assertEqual(removed, 1)
            self.assertFalse(stale.exists())
            self.assertTrue(fresh.exists())
            self.assertTrue(almost.exists())
            self.assertTrue(foreign.exists())
            await plugin.terminate()


class DynamicDebounceTest(unittest.IsolatedAsyncioTestCase):
    """智能防抖：等多久按内容决定，外加群聊/私聊开关和白名单。"""

    def _plugin(self, **cfg):
        plugin = make_plugin(**cfg)
        plugin.merger.send_merged_event = AsyncMock()
        return plugin

    def _key(self, plugin, event):
        return plugin.merger.merge_key(event.unified_msg_origin, event.get_sender_id())

    async def test_short_message_opens_slow_window(self):
        plugin = self._plugin()
        event = FakeEvent(message_str="小喵在吗")
        await plugin.on_message(event)

        merge = plugin.merger._merges[self._key(plugin, event)]
        self.assertAlmostEqual(merge.idle_seconds, 1.2)
        self.assertTrue(event.stopped)
        await plugin.terminate()

    async def test_sentence_with_ending_punctuation_replies_immediately(self):
        plugin = self._plugin()
        event = FakeEvent(message_str="小喵在吗。")
        await plugin.on_message(event)

        self.assertNotIn(self._key(plugin, event), plugin.merger._merges)
        self.assertFalse(event.stopped)
        await plugin.terminate()

    async def test_long_message_replies_immediately(self):
        plugin = self._plugin()
        event = FakeEvent(message_str="小喵" + "很长的一句话" * 3)
        await plugin.on_message(event)

        self.assertNotIn(self._key(plugin, event), plugin.merger._merges)
        self.assertFalse(event.stopped)
        await plugin.terminate()

    async def test_fast_window_can_be_configured_to_still_wait(self):
        plugin = self._plugin(debounce_fast_idle_seconds=0.3)
        event = FakeEvent(message_str="小喵在吗。")
        await plugin.on_message(event)

        merge = plugin.merger._merges[self._key(plugin, event)]
        self.assertAlmostEqual(merge.idle_seconds, 0.3)
        await plugin.terminate()

    async def test_dynamic_off_falls_back_to_fixed_idle(self):
        plugin = self._plugin(debounce_dynamic_enabled=False, merge_idle_seconds=7)
        event = FakeEvent(message_str="小喵在吗。")
        await plugin.on_message(event)

        merge = plugin.merger._merges[self._key(plugin, event)]
        self.assertIsNone(merge.idle_seconds)
        await plugin.terminate()

    async def test_group_switch_off_disables_debounce(self):
        plugin = self._plugin(debounce_group_enabled=False)
        event = FakeEvent(message_str="小喵在吗")
        await plugin.on_message(event)

        self.assertNotIn(self._key(plugin, event), plugin.merger._merges)
        self.assertFalse(event.stopped)
        await plugin.terminate()

    async def test_private_switch_off_disables_debounce(self):
        plugin = self._plugin(debounce_private_enabled=False)
        event = FakeEvent(
            is_private=True,
            session="aiocqhttp:FriendMessage:10001",
            group_id="",
            message_str="小喵在吗",
        )
        await plugin.on_message(event)

        self.assertNotIn(self._key(plugin, event), plugin.merger._merges)
        self.assertFalse(event.stopped)
        await plugin.terminate()

    async def test_whitelist_limits_debounce_to_listed_users(self):
        plugin = self._plugin(debounce_user_whitelist=["99999"])
        event = FakeEvent(message_str="小喵在吗")
        await plugin.on_message(event)

        self.assertNotIn(self._key(plugin, event), plugin.merger._merges)
        await plugin.terminate()

    async def test_whitelist_hit_keeps_debounce(self):
        plugin = self._plugin(debounce_user_whitelist=["10001"])
        event = FakeEvent(message_str="小喵在吗")
        await plugin.on_message(event)

        self.assertIn(self._key(plugin, event), plugin.merger._merges)
        await plugin.terminate()

    async def test_too_many_parts_releases_immediately(self):
        plugin = self._plugin(debounce_max_parts=2, merge_max_total_seconds=60)
        first = FakeEvent(message_str="第一句")
        second = FakeEvent(is_trigger=False, message_str="第二句")
        await plugin.on_message(first)
        await plugin.on_message(second)

        merge = plugin.merger._merges[self._key(plugin, first)]
        self.assertEqual(merge.idle_seconds, 0.0)
        await plugin.terminate()

    async def test_keyword_wake_short_message_enters_debounce(self):
        plugin = self._plugin(keyword_wake_enabled=True, keyword_wake_list=["小喵"])
        event = FakeEvent(is_trigger=False, message_str="小喵你看这个")
        await plugin.on_message(event)

        self.assertIn(self._key(plugin, event), plugin.merger._merges)
        self.assertTrue(event.stopped)
        await plugin.terminate()

    async def test_legacy_config_without_new_key_still_works(self):
        plugin = self._plugin(merge_enabled=False)
        event = FakeEvent(message_str="小喵在吗")
        await plugin.on_message(event)

        self.assertNotIn(self._key(plugin, event), plugin.merger._merges)
        await plugin.terminate()

    async def test_plain_group_message_does_no_extra_work(self):
        plugin = self._plugin(merge_idle_seconds=30, merge_max_total_seconds=60)
        downloaded = []

        async def fake_localize(components, *, enabled, session_key="", epoch=0):
            downloaded.append(session_key)
            return components, []

        plugin.media.localize_components = fake_localize
        event = FakeEvent(
            is_trigger=False,
            message_str="",
            components=[Image(file="https://example.invalid/plain.png")],
        )
        await plugin.on_message(event)

        self.assertEqual(downloaded, [])
        self.assertNotIn(self._key(plugin, event), plugin.merger._merges)
        await plugin.terminate()

    async def test_trigger_image_is_still_localized(self):
        plugin = self._plugin(merge_idle_seconds=30, merge_max_total_seconds=60)
        downloaded = []

        async def fake_localize(components, *, enabled, session_key="", epoch=0):
            downloaded.append(session_key)
            return components, []

        plugin.media.localize_components = fake_localize
        event = FakeEvent(
            message_str="",
            components=[Image(file="https://example.invalid/wake.png")],
        )
        await plugin.on_message(event)

        self.assertEqual(len(downloaded), 1)
        self.assertIn(self._key(plugin, event), plugin.merger._merges)
        await plugin.terminate()

    def _private(self, text):
        return FakeEvent(
            is_private=True,
            session="aiocqhttp:FriendMessage:10001",
            group_id="",
            message_str=text,
        )

    def _typing_event(self, event_type=1, user_id="10001"):
        event = FakeEvent(
            is_private=True,
            session=f"aiocqhttp:FriendMessage:{user_id}",
            group_id="",
            sender_id=user_id,
            message_str="",
        )
        event.message_obj.message = []
        event.message_obj.raw_message = {
            "post_type": "notice",
            "notice_type": "notify",
            "sub_type": "input_status",
            "user_id": user_id,
            "event_type": event_type,
        }
        return event

    async def test_bare_name_call_waits_longer(self):
        plugin = self._plugin(
            keyword_wake_enabled=True,
            keyword_wake_list=["小喵"],
            debounce_wake_idle_seconds=4.0,
        )
        event = FakeEvent(is_trigger=False, message_str="小喵")
        await plugin.on_message(event)

        merge = plugin.merger._merges[self._key(plugin, event)]
        self.assertAlmostEqual(merge.idle_seconds, 4.0)
        await plugin.terminate()

    async def test_name_with_real_question_uses_short_window(self):
        plugin = self._plugin(
            keyword_wake_enabled=True,
            keyword_wake_list=["小喵"],
            debounce_wake_idle_seconds=4.0,
            debounce_slow_idle_seconds=1.2,
        )
        event = FakeEvent(is_trigger=False, message_str="小喵在吗")
        await plugin.on_message(event)

        merge = plugin.merger._merges[self._key(plugin, event)]
        self.assertAlmostEqual(merge.idle_seconds, 1.2)
        await plugin.terminate()

    async def test_typing_notice_marks_user_as_typing(self):
        plugin = self._plugin()
        await plugin.on_message(self._typing_event(event_type=1))

        key = plugin.merger.merge_key("aiocqhttp:FriendMessage:10001", "10001")
        self.assertGreater(plugin.guard._typing_until.get(key, 0.0), time.monotonic())
        await plugin.terminate()

    async def test_typing_hold_prevents_idle_release(self):
        plugin = self._plugin(
            debounce_slow_idle_seconds=0.2,
            debounce_typing_hold_seconds=3.0,
            merge_max_total_seconds=20,
        )
        released = []

        async def capture(ms, full_text):
            released.append(full_text)

        plugin.merger.send_merged_event = capture

        chat = self._private("贴贴")
        await plugin.on_message(chat)
        key = self._key(plugin, chat)
        self.assertIn(key, plugin.merger._merges)

        await plugin.on_message(self._typing_event(event_type=1))
        await asyncio.sleep(0.6)
        self.assertIn(key, plugin.merger._merges)
        self.assertEqual(released, [])

        await plugin.on_message(self._typing_event(event_type=2))
        await asyncio.sleep(0.05)
        self.assertNotIn(key, plugin.merger._merges)
        self.assertEqual(len(released), 1)
        await plugin.terminate()

    async def test_typing_stop_without_merge_is_harmless(self):
        plugin = self._plugin()
        await plugin.on_message(self._typing_event(event_type=2))
        await plugin.terminate()

    async def test_private_debounce_works_even_if_guard_scope_off(self):
        plugin = self._plugin(include_private=False)
        event = self._private("小喵在吗")
        await plugin.on_message(event)

        self.assertIn(self._key(plugin, event), plugin.merger._merges)
        await plugin.terminate()

    async def test_private_guard_stays_off_when_scope_off(self):
        plugin = self._plugin(include_private=False, debounce_private_enabled=False)
        events = []
        for index in range(4):
            event = self._private(f"私聊第{index}句")
            events.append(event)
            await plugin.on_message(event)

        self.assertFalse(any("nuoxiaomiao_blocked" in e.extras for e in events))
        await plugin.terminate()

    async def test_private_guard_blocks_when_scope_on(self):
        plugin = self._plugin(include_private=True, debounce_private_enabled=False)
        events = []
        for index in range(4):
            event = self._private(f"私聊第{index}句")
            events.append(event)
            await plugin.on_message(event)

        self.assertTrue(any("nuoxiaomiao_blocked" in e.extras for e in events))
        await plugin.terminate()

    async def test_private_merge_is_not_counted_by_guard_when_scope_off(self):
        plugin = self._plugin(
            include_private=False, debounce_private_enabled=True, trigger_rounds=2
        )
        released = []

        async def capture(ms, full_text):
            released.append(full_text)

        plugin.merger.send_merged_event = capture
        for index in range(4):
            event = self._private(f"私聊短句{index}")
            await plugin.on_message(event)
            key = self._key(plugin, event)
            if key in plugin.merger._merges:
                await plugin.merger.release_merge(key, reason="test")

        self.assertEqual(len(released), 4)
        await plugin.terminate()
