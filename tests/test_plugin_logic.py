import asyncio
import base64
import json
import itertools
import os
import random
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from astrbot.core.message.components import At, Image, Json, Plain, Reply
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.provider.entities import ProviderRequest

from main import (
    ActivityEntry,
    DISPLAY_NAME,
    HumanReplyState,
    MergeSession,
    PILImage,
    NuoxiaomiaoPlugin,
    RunState,
    SELF_WAKE_EVENT_EXTRA,
    _split_reply_text,
)
from scripts.migrate_config import (
    CURRENT_CONFIG_NAME,
    LEGACY_CONFIG_NAME,
    migrate_config,
)


class FakeEvent:
    def __init__(
        self,
        sender_id="10001",
        session="aiocqhttp:GroupMessage:20001",
        group_id="20001",
        is_private=False,
        is_trigger=True,
        pipeline_wake=None,
        message_str="",
        message_id="",
        components=None,
        activated_handlers=None,
        sender_name="测试用户",
        self_id="bot",
        group_name=None,
        is_admin=False,
    ):
        self._sender_id = sender_id
        self._sender_name = sender_name
        self._self_id = self_id
        self.unified_msg_origin = session
        self._group_id = group_id
        self._is_private = is_private
        self._is_admin = is_admin
        self.is_at_or_wake_command = is_trigger
        self._is_wake = is_trigger if pipeline_wake is None else pipeline_wake
        self.message_str = message_str
        self.extras = {}
        if activated_handlers is not None:
            self.extras["activated_handlers"] = activated_handlers
        self.stopped = False
        self.call_llm = False
        self.sent = []
        # 拟人回复接管用： decorating 钩子读写结果链；bot 用来模拟 OneBot 客户端。
        self._result = None
        self.bot = None
        self.session_id = session
        self.message_obj = type(
            "MO",
            (),
            {
                "message_id": message_id or f"mid-{id(self)}",
                "session_id": session,
                "type": type("T", (), {"value": "GroupMessage"})(),
                "sender": type(
                    "S",
                    (),
                    {"user_id": sender_id, "nickname": sender_name},
                )(),
                "group": (
                    None
                    if group_name is None
                    else type("G", (), {"group_id": group_id, "group_name": group_name})()
                ),
                "message": list(components or []),
                "timestamp": 0,
            },
        )()

    def get_sender_id(self):
        return self._sender_id

    def get_group_id(self):
        return self._group_id

    def get_self_id(self):
        return self._self_id

    def get_sender_name(self):
        return self._sender_name

    def get_platform_id(self):
        return "aiocqhttp"

    def get_platform_name(self):
        return "aiocqhttp"

    def is_private_chat(self):
        return self._is_private

    def is_admin(self):
        return self._is_admin

    def is_wake_up(self):
        return self._is_wake

    def get_messages(self):
        return self.message_obj.message

    def get_message_outline(self):
        parts = []
        for component in self.message_obj.message:
            if isinstance(component, Plain):
                parts.append(component.text)
            elif isinstance(component, Image):
                parts.append("[图片]")
            elif isinstance(component, Reply):
                parts.append(
                    f"[引用 {component.sender_nickname or component.sender_id}: "
                    f"{component.message_str or '内容不可用'}]"
                )
            elif isinstance(component, At):
                parts.append(f"@{component.qq}")
            else:
                parts.append(f"[{type(component).__name__}]")
        return " ".join(parts)

    def set_extra(self, key, value):
        self.extras[key] = value

    def get_extra(self, key=None, default=None):
        if key is None:
            return self.extras
        return self.extras.get(key, default)

    def should_call_llm(self, value):
        self.call_llm = value

    def get_result(self):
        return self._result

    def clear_result(self):
        self._result = None

    def stop_event(self):
        self.stopped = True

    def is_stopped(self):
        return self.stopped

    async def send(self, message):
        self.sent.append(message)


class FakeProvider:
    """程序化调 LLM 的假提供方：可指定固定回复，也可以让它报错。"""

    def __init__(self, text="来找我喵～", raise_error=False):
        self.text = text
        self.raise_error = raise_error
        self.calls = []

    async def text_chat(self, prompt=None, system_prompt=None, contexts=None, **kwargs):
        if self.raise_error:
            raise RuntimeError("LLM 挂了")
        self.calls.append({"prompt": prompt, "system_prompt": system_prompt})
        return SimpleNamespace(completion_text=self.text, result_chain=None)


class FakePersonaManager:
    """返回固定人设提示词。"""

    def __init__(self, prompt=""):
        self.prompt = prompt

    async def get_default_persona_v3(self, umo=None):
        return SimpleNamespace(system_prompt=self.prompt)


class FakeProviderManager:
    """始终返回同一个假提供方；记录按 ID 取模型的调用。"""

    def __init__(self, provider):
        self.provider = provider
        self.fetched_ids = []

    def get_using_provider(self, provider_type, umo=None):
        return self.provider

    async def get_provider_by_id(self, provider_id):
        self.fetched_ids.append(str(provider_id))
        return self.provider


class FakeContext:
    def __init__(self, wake_prefixes=None, admins=None, provider=None, persona_prompt=""):
        self.wake_prefixes = list(wake_prefixes or [])
        self.admins = list(admins or [])
        self.provider = provider if provider is not None else FakeProvider()
        self.persona_prompt = persona_prompt
        self.sent = []
        self.provider_manager = FakeProviderManager(self.provider)
        self.persona_manager = FakePersonaManager(persona_prompt)
        # 自醒挑会话时会问"这个会话属于哪个平台"，没有历史记录时退回第一个平台实例。
        meta = SimpleNamespace(id="aiocqhttp", name="aiocqhttp", description="test")
        inst = SimpleNamespace(meta=meta)
        self.platform_manager = SimpleNamespace(platform_insts=[inst])

    def get_config(self, umo=None):
        return {"wake_prefix": self.wake_prefixes, "admins_id": self.admins}

    def register_web_api(self, *args, **kwargs):
        pass

    def get_llm_tool_manager(self):
        return type("FTM", (), {"func_list": []})()

    def get_registered_star(self, name):
        return None

    async def send_message(self, umo, chain):
        self.sent.append((umo, chain))
        return True


_TEST_RUNTIME_DIR = tempfile.TemporaryDirectory(prefix="nxm_test_runtime_")
_TEST_DB_COUNTER = itertools.count()


def make_plugin(context=None, **cfg):
    base = {
        "activity_db_path": str(Path(_TEST_RUNTIME_DIR.name) / f"activity_{next(_TEST_DB_COUNTER)}.db"),
        "trigger_rounds": 3,
        "meme_tags": [],
        "meme_cooldown_seconds": 0,
        "merge_enabled": True,
        "merge_idle_seconds": 4,
        "merge_max_total_seconds": 12,
        "force_wake_prefix": "小喵",
    }
    base.update(cfg)
    return NuoxiaomiaoPlugin(context or FakeContext(), base)


class GuardLogicTest(unittest.IsolatedAsyncioTestCase):
    async def test_blocks_on_third_consecutive_dialogue(self):
        plugin = make_plugin(merge_enabled=False)

        first = FakeEvent(message_str="小喵一")
        second = FakeEvent(message_str="小喵二")
        third = FakeEvent(message_str="小喵三")

        await plugin.on_message(first)
        await plugin.on_message(second)
        await plugin.on_message(third)

        self.assertFalse(first.stopped)
        self.assertFalse(second.stopped)
        self.assertTrue(third.stopped)
        self.assertTrue(third.call_llm)  # True = 禁止默认 LLM
        self.assertTrue(third.extras["nuoxiaomiao_blocked"])

    async def test_sender_change_resets_counter(self):
        plugin = make_plugin(merge_enabled=False)

        await plugin.on_message(FakeEvent(sender_id="10001", message_str="a"))
        await plugin.on_message(FakeEvent(sender_id="10001", message_str="b"))
        await plugin.on_message(FakeEvent(sender_id="10002", message_str="c"))
        event = FakeEvent(sender_id="10001", message_str="d")
        await plugin.on_message(event)

        self.assertFalse(event.stopped)

    async def test_whitelist_takes_precedence_over_blacklist(self):
        plugin = make_plugin(
            merge_enabled=False,
            trigger_rounds=2,
            user_whitelist=["10001"],
            user_blacklist=["10001"],
        )

        first = FakeEvent(sender_id="10001", message_str="a")
        second = FakeEvent(sender_id="10001", message_str="b")
        await plugin.on_message(first)
        await plugin.on_message(second)

        self.assertFalse(first.stopped)
        self.assertFalse(second.stopped)

    async def test_blacklist_limits_checked_users_when_not_empty(self):
        plugin = make_plugin(
            merge_enabled=False,
            trigger_rounds=2,
            user_blacklist=["10002"],
        )

        unchecked_second = FakeEvent(sender_id="10001", message_str="b")
        await plugin.on_message(FakeEvent(sender_id="10001", message_str="a"))
        await plugin.on_message(unchecked_second)
        await plugin.on_message(FakeEvent(sender_id="10002", message_str="c"))
        checked_second = FakeEvent(sender_id="10002", message_str="d")
        await plugin.on_message(checked_second)

        self.assertFalse(unchecked_second.stopped)
        self.assertTrue(checked_second.stopped)

    async def test_non_wake_group_message_is_ignored(self):
        plugin = make_plugin(merge_enabled=False, trigger_rounds=2)

        first = FakeEvent(is_trigger=False, message_str="闲聊")
        second = FakeEvent(is_trigger=False, message_str="闲聊2")
        await plugin.on_message(first)
        await plugin.on_message(second)

        self.assertFalse(second.stopped)

    async def test_matched_command_bypasses_merge_and_guard(self):
        for filter_name in ("CommandFilter", "CommandGroupFilter"):
            with self.subTest(filter_name=filter_name):
                command_filter = type(filter_name, (), {})()
                command_handler = type(
                    "Handler",
                    (),
                    {"event_filters": [command_filter]},
                )()
                plugin = make_plugin(trigger_rounds=2)

                for _ in range(3):
                    event = FakeEvent(
                        message_str="help",
                        components=[Plain("/help")],
                        activated_handlers=[command_handler],
                    )
                    await plugin.on_message(event)
                    self.assertFalse(event.stopped)
                    self.assertFalse(event.call_llm)

                self.assertEqual(plugin._states, {})
                self.assertEqual(plugin._merges, {})
                await plugin.terminate()

    async def test_command_fallback_uses_raw_prefixed_text(self):
        plugin = make_plugin(context=FakeContext(["/"]), trigger_rounds=2)
        event = FakeEvent(message_str="help", components=[Plain("/help")])
        event.get_extra = None

        await plugin.on_message(event)

        self.assertFalse(event.stopped)
        self.assertEqual(plugin._states, {})
        self.assertEqual(plugin._merges, {})
        await plugin.terminate()

    async def test_pipeline_wake_does_not_make_plain_group_message_a_trigger(self):
        plugin = make_plugin(merge_idle_seconds=30, merge_max_total_seconds=60)

        event = FakeEvent(
            is_trigger=False,
            pipeline_wake=True,
            message_str="普通群聊",
        )
        await plugin.on_message(event)

        key = plugin._merge_key(event.unified_msg_origin, "10001")
        self.assertNotIn(key, plugin._merges)
        self.assertFalse(event.stopped)
        self.assertFalse(event.call_llm)
        await plugin.terminate()

    @unittest.skipIf(PILImage is None, "PIL is unavailable")
    async def test_static_meme_converts_to_gif_and_cleans_temp_file(self):
        plugin = make_plugin(meme_tags=["angry"], merge_enabled=False)

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            source = tmp_dir / "source.png"
            config_path = tmp_dir / "meme_manager_config.json"
            PILImage.new("RGB", (8, 8), (255, 0, 0)).save(source)
            config_path.write_text(
                json.dumps({"convert_static_to_gif": True}),
                encoding="utf-8",
            )

            plugin._meme_manager_config_paths = lambda: [config_path]
            plugin._select_meme_file = lambda: source

            event = FakeEvent()
            await plugin._send_meme(event)

            image = event.sent[0].chain[0]
            sent_path = Path(image.path)
            self.assertEqual(sent_path.suffix, ".gif")
            self.assertFalse(sent_path.exists())
            self.assertTrue(source.exists())


class MergeLogicTest(unittest.IsolatedAsyncioTestCase):
    # 这一组用例测的是合并骨架（缓冲、追加、放行、图片本地化）。
    # 把“多长算说完”的门槛调高，让长句也走缓冲，动态窗口本身由
    # DynamicDebounceTest 专门覆盖。
    async def test_wake_starts_merge_and_blocks_llm(self):
        plugin = make_plugin(
            merge_idle_seconds=30, merge_max_total_seconds=60, debounce_long_chars=100
        )
        plugin._send_merged_event = AsyncMock()

        event = FakeEvent(message_str="小喵你的尾巴本来就是归我管的")
        await plugin.on_message(event)

        key = plugin._merge_key(event.unified_msg_origin, "10001")
        self.assertIn(key, plugin._merges)
        self.assertEqual(
            plugin._merges[key].parts,
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
        plugin._send_merged_event = AsyncMock()

        e1 = FakeEvent(message_str="小喵你的尾巴本来就是归我管的")
        e2 = FakeEvent(is_trigger=False, message_str="揉两下那叫例行检查")
        e3 = FakeEvent(is_trigger=False, message_str="什么凑热闹")

        await plugin.on_message(e1)
        await plugin.on_message(e2)
        await plugin.on_message(e3)

        key = plugin._merge_key(e1.unified_msg_origin, "10001")
        self.assertEqual(
            plugin._merges[key].parts,
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

        key = plugin._merge_key(event.unified_msg_origin, "10001")
        self.assertIn(key, plugin._merges)
        self.assertEqual(plugin._merges[key].parts, [""])
        self.assertEqual(plugin._merges[key].component_parts, [[image]])
        self.assertTrue(event.stopped)
        await plugin.terminate()

    async def test_image_followup_is_buffered(self):
        plugin = make_plugin(merge_idle_seconds=30, merge_max_total_seconds=60)
        image = Image(file="https://example.invalid/followup.png")
        first = FakeEvent(message_str="hello", components=[Plain("hello")])
        followup = FakeEvent(is_trigger=False, message_str="", components=[image])

        await plugin.on_message(first)
        await plugin.on_message(followup)

        key = plugin._merge_key(first.unified_msg_origin, "10001")
        self.assertEqual(plugin._merges[key].parts, ["hello", ""])
        self.assertEqual(plugin._merges[key].component_parts[1], [image])
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
            key = plugin._merge_key(events[0].unified_msg_origin, "10001")
            await plugin._release_merge(key, reason="test")

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
        plugin._send_merged_event = AsyncMock()

        e1 = FakeEvent(sender_id="10001", message_str="小喵你好")
        e2 = FakeEvent(
            sender_id="10002",
            is_trigger=False,
            message_str="别人插话",
        )
        await plugin.on_message(e1)
        await plugin.on_message(e2)

        key = plugin._merge_key(e1.unified_msg_origin, "10001")
        self.assertEqual(plugin._merges[key].parts, ["小喵你好"])
        await plugin.terminate()

    async def test_release_merges_text(self):
        plugin = make_plugin(merge_idle_seconds=30, merge_max_total_seconds=60)
        released = []

        async def capture(ms, full_text):
            released.append((list(ms.parts), full_text))

        plugin._send_merged_event = capture

        e1 = FakeEvent(message_str="小喵句一")
        e2 = FakeEvent(is_trigger=False, message_str="句二")
        await plugin.on_message(e1)
        await plugin.on_message(e2)
        key = plugin._merge_key(e1.unified_msg_origin, "10001")
        await plugin._release_merge(key, reason="test")

        self.assertTrue(released)
        parts, full = released[0]
        self.assertEqual(parts, ["小喵句一", "句二"])
        self.assertIn("小喵句一", full)
        self.assertIn("句二", full)
        self.assertNotIn(key, plugin._merges)
        await plugin.terminate()

    async def test_max_total_force_release(self):
        plugin = make_plugin(merge_idle_seconds=30, merge_max_total_seconds=1)
        released = []

        async def capture(ms, full_text):
            released.append(full_text)

        plugin._send_merged_event = capture

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
        plugin._send_merged_event = AsyncMock()

        events = []
        for index in range(1, 4):
            event = FakeEvent(message_str=f"小喵第{index}轮")
            events.append(event)
            await plugin.on_message(event)
            key = plugin._merge_key(event.unified_msg_origin, "10001")
            await plugin._release_merge(key, reason="test")

        self.assertEqual(plugin._send_merged_event.await_count, 2)
        self.assertNotIn("nuoxiaomiao_blocked", events[0].extras)
        self.assertNotIn("nuoxiaomiao_blocked", events[1].extras)
        self.assertTrue(events[2].extras["nuoxiaomiao_blocked"])
        await plugin.terminate()

    async def test_skip_id_does_not_rebuffer(self):
        plugin = make_plugin(merge_idle_seconds=30, merge_max_total_seconds=60)
        plugin._send_merged_event = AsyncMock()

        mid = "synthetic-1"
        plugin._skip_merge_ids.add(mid)
        plugin._skip_merge_timestamps[mid] = time.monotonic()
        event = FakeEvent(message_str="小喵合成放行", message_id=mid)
        await plugin.on_message(event)

        key = plugin._merge_key(event.unified_msg_origin, "10001")
        self.assertNotIn(key, plugin._merges)
        # 合成消息不再次计守卫，也不阻止后续 LLM。
        self.assertFalse(event.stopped)
        self.assertFalse(event.call_llm)
        self.assertEqual(plugin._states, {})
        await plugin.terminate()

    async def test_concurrent_triggers_are_kept_in_one_buffer(self):
        plugin = make_plugin(merge_idle_seconds=30, merge_max_total_seconds=60)
        plugin._send_merged_event = AsyncMock()
        first = FakeEvent(message_str="小喵并发一")
        second = FakeEvent(message_str="小喵并发二")

        await asyncio.gather(
            plugin.on_message(first),
            plugin.on_message(second),
        )

        key = plugin._merge_key(first.unified_msg_origin, "10001")
        self.assertEqual(
            plugin._merges[key].parts,
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
        plugin._states["old-session"] = RunState("10001", 2, now - 11)
        plugin._last_meme_at[("old-session", "10001")] = now - 11
        plugin._skip_merge_ids.add("old-synthetic")
        plugin._skip_merge_timestamps["old-synthetic"] = now - 301
        plugin._last_state_cleanup_ts = 0

        await plugin._cleanup_expired_state()

        self.assertEqual(plugin._states, {})
        self.assertEqual(plugin._last_meme_at, {})
        self.assertEqual(plugin._skip_merge_ids, set())
        self.assertEqual(plugin._skip_merge_timestamps, {})
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
            patch("main.asyncio.sleep", new=AsyncMock()),
        ):
            with self.assertRaisesRegex(RuntimeError, "submit failed"):
                await plugin._send_merged_event(session, origin.message_str)

        self.assertNotIn("failed-mid", plugin._skip_merge_ids)
        self.assertNotIn("failed-mid", plugin._skip_merge_timestamps)
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
            patch("main.asyncio.sleep", new=AsyncMock()),
        ):
            await plugin._send_merged_event(session, origin.message_str)

        self.assertEqual(create_event.await_count, 2)
        self.assertNotIn("failed-mid", plugin._skip_merge_ids)
        self.assertIn("successful-mid", plugin._skip_merge_ids)
        await plugin.terminate()

    async def test_release_failure_notifies_user(self):
        plugin = make_plugin()
        origin = FakeEvent(message_str="cannot deliver")
        key = plugin._merge_key(origin.unified_msg_origin, "10001")
        plugin._merges[key] = MergeSession(
            session_key=origin.unified_msg_origin,
            sender_id="10001",
            parts=[origin.message_str],
            origin_event=origin,
        )
        plugin._send_merged_event = AsyncMock(side_effect=RuntimeError("failed"))

        await plugin._release_merge(key, reason="test")

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
            await plugin._send_merged_event(session, origin.message_str)

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
                    await plugin._send_merged_event(session, origin.message_str)

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

            with patch("main.MediaResolver.to_path", new=fake_to_path):
                await plugin.on_message(event)

            key = plugin._merge_key(event.unified_msg_origin, "10001")
            buffered = plugin._merges[key].component_parts[0][0]
            self.assertTrue(buffered.url.startswith("file://"))
            self.assertTrue(os.path.exists(buffered.path))
            self.assertTrue(Path(buffered.path).parent.resolve() == Path(localize_dir).resolve())
            os.unlink(downloaded_path)
            await plugin.terminate()

    async def test_temp_local_path_is_copied_to_persist_dir(self):
        with tempfile.TemporaryDirectory() as localize_dir:
            plugin = self._make_plugin(localize_dir)
            # 模拟 core 下载到临时目录的图（随时可能被清理）。
            with tempfile.TemporaryDirectory() as tmp_dir:
                src = Path(tmp_dir) / "temp_image.png"
                src.write_bytes(b"local png")
                local_path = str(src)

                called = []

                async def fake_to_path(self, *, target_format=None, preserve_mp3=False):
                    called.append(True)
                    return local_path

                image = Image(file=local_path)
                event = FakeEvent(message_str="", components=[image])

                with patch("main.MediaResolver.to_path", new=fake_to_path):
                    await plugin.on_message(event)

                key = plugin._merge_key(event.unified_msg_origin, "10001")
                buffered = plugin._merges[key].component_parts[0][0]
                # 临时目录里的本地路径应被复制到持久目录、不触发远程下载。
                self.assertEqual(called, [])
                self.assertTrue(buffered.url.startswith("file://"))
                self.assertTrue(os.path.exists(buffered.path))
                # 持久目录里现在有文件。
                persisted = list(Path(localize_dir).iterdir())
                self.assertEqual(len(persisted), 1)
                # 原始临时文件不在持久目录内。
                self.assertNotEqual(
                    os.path.abspath(buffered.path),
                    os.path.abspath(local_path),
                )
            await plugin.terminate()

    async def test_already_in_persist_dir_is_skipped(self):
        with tempfile.TemporaryDirectory() as localize_dir:
            plugin = self._make_plugin(localize_dir)
            # 在持久目录内放一个已存在的文件。
            existing = Path(localize_dir) / "already_here.png"
            existing.write_bytes(b"persisted png")

            called = []

            async def fake_to_path(self, *, target_format=None, preserve_mp3=False):
                called.append(True)
                return str(existing)

            image = Image(file=str(existing))
            event = FakeEvent(message_str="", components=[image])

            with patch("main.MediaResolver.to_path", new=fake_to_path):
                await plugin.on_message(event)

            key = plugin._merge_key(event.unified_msg_origin, "10001")
            buffered = plugin._merges[key].component_parts[0][0]
            # 已在持久目录内的文件不应重复拷贝/下载。
            self.assertEqual(called, [])
            self.assertEqual(buffered.file, str(existing))
            # 持久目录里只有原来那一个文件。
            self.assertEqual(len(list(Path(localize_dir).iterdir())), 1)
            await plugin.terminate()

    async def test_missing_local_path_falls_back_to_download(self):
        with tempfile.TemporaryDirectory() as localize_dir:
            plugin = self._make_plugin(localize_dir)
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                tmp.write(b"downloaded png")
                downloaded_path = tmp.name

            called = []

            async def fake_to_path(self, *, target_format=None, preserve_mp3=False):
                called.append(True)
                return downloaded_path

            # 指向一个不存在的本地路径，应回退到 MediaResolver 下载。
            image = Image(file="/nonexistent/path/ghost.png")
            event = FakeEvent(message_str="", components=[image])

            with patch("main.MediaResolver.to_path", new=fake_to_path):
                await plugin.on_message(event)

            key = plugin._merge_key(event.unified_msg_origin, "10001")
            buffered = plugin._merges[key].component_parts[0][0]
            # 不存在的本地路径应触发下载。
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

            with patch("main.MediaResolver.to_path", new=fake_to_path):
                await plugin.on_message(event)

            key = plugin._merge_key(event.unified_msg_origin, "10001")
            buffered = plugin._merges[key].component_parts[0][0]
            # 下载失败时原样保留组件，不阻断合并。
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

            with patch("main.MediaResolver.to_path", new=fake_to_path):
                await plugin.on_message(event)

            key = plugin._merge_key(event.unified_msg_origin, "10001")
            buffered = plugin._merges[key].component_parts[0][0]
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

            with patch("main.MediaResolver.to_path", new=fake_to_path):
                await plugin.on_message(event)

            key = plugin._merge_key(event.unified_msg_origin, "10001")
            buffered = plugin._merges[key].component_parts[0]
            self.assertEqual(called, [])
            self.assertIn(plain, buffered)
            self.assertIn(at, buffered)
            await plugin.terminate()

    async def test_localized_files_cleaned_up_on_llm_response(self):
        with tempfile.TemporaryDirectory() as localize_dir:
            # v1.9.4 起 on_llm_response 会顺手记 bot 回复进速览库，
            # 给个临时库，别把测试数据写进真实 data/activity.db。
            plugin = self._make_plugin(
                localize_dir,
                activity_db_path=str(
                    Path(tempfile.mkdtemp(prefix="nxm_localize_")) / "activity.db"
                ),
            )
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                tmp.write(b"\x89PNG fake")
                downloaded_path = tmp.name

            async def fake_to_path(self, *, target_format=None, preserve_mp3=False):
                return downloaded_path

            image = Image(file="https://multimedia.nt.qq.com.cn/download?rkey=test")
            event = FakeEvent(message_str="", components=[image])

            with patch("main.MediaResolver.to_path", new=fake_to_path):
                await plugin.on_message(event)

            key = plugin._merge_key(event.unified_msg_origin, "10001")
            buffered = plugin._merges[key].component_parts[0][0]
            persisted_path = buffered.path
            # 本地化后文件存在、被追踪。
            self.assertTrue(os.path.exists(persisted_path))
            self.assertIn(key, plugin._localized_files)
            self.assertEqual(len(plugin._localized_files[key]), 1)

            # on_llm_response 后文件应被清理、追踪记录应清空。
            response = type("R", (), {"completion_text": "ok"})()
            await plugin.on_llm_response(event, response)
            self.assertFalse(os.path.exists(persisted_path))
            self.assertNotIn(key, plugin._localized_files)

            os.unlink(downloaded_path)
            await plugin.terminate()


    async def test_orphan_sweep_removes_only_stale_own_files(self):
        # 孤儿清扫只删「本插件命名格式 + 超龄」的文件：
        # 新鲜的在途图片、不认格式的文件、别人的文件都不许动。
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

            removed = plugin._sweep_orphan_localized_images(3600.0)

            self.assertEqual(removed, 1)
            self.assertFalse(stale.exists())
            self.assertTrue(fresh.exists())     # mtime 新鲜，在途不动
            self.assertTrue(almost.exists())    # 时间戳不是 14 位，不认
            self.assertTrue(foreign.exists())   # 命名不是本插件的，不碰
            await plugin.terminate()


class ConfigMigrationTest(unittest.TestCase):
    def test_migrates_legacy_config_when_current_is_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            legacy = config_dir / LEGACY_CONFIG_NAME
            legacy.write_text('{"window_seconds": 240}', encoding="utf-8")

            result = migrate_config(config_dir)

            current = config_dir / CURRENT_CONFIG_NAME
            self.assertTrue(current.is_file())
            self.assertEqual(current.read_text(encoding="utf-8"), legacy.read_text())
            self.assertIn("已迁移", result)

    def test_existing_current_config_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            current = config_dir / CURRENT_CONFIG_NAME
            legacy = config_dir / LEGACY_CONFIG_NAME
            current.write_text('{"window_seconds": 300}', encoding="utf-8")
            legacy.write_text('{"window_seconds": 240}', encoding="utf-8")

            result = migrate_config(config_dir)

            self.assertEqual(
                current.read_text(encoding="utf-8"),
                '{"window_seconds": 300}',
            )
            self.assertIn("未覆盖", result)


class PokeLogicTest(unittest.IsolatedAsyncioTestCase):
    class _DummyReq:
        pass

    class FakePokeEvent:
        def __init__(self, raw, sender_id="10001", self_id="bot", group_id="20001"):
            self.message_obj = type(
                "PokeMO", (), {"raw_message": raw, "message": []}
            )()
            self._sender_id = sender_id
            self._self_id = self_id
            self._group_id = group_id
            self.unified_msg_origin = "aiocqhttp:GroupMessage:20001"
            self.message_str = ""
            self.stopped = False
            self.call_llm = False
            self.extras = {}

        def get_sender_id(self):
            return self._sender_id

        def get_self_id(self):
            return self._self_id

        def get_group_id(self):
            return self._group_id

        def get_platform_id(self):
            return "aiocqhttp"

        def get_sender_name(self):
            return "戳戳怪"

        def set_extra(self, key, value):
            self.extras[key] = value

        def get_extra(self, key=None, default=None):
            if key is None:
                return self.extras
            return self.extras.get(key, default)

        def should_call_llm(self, value):
            self.call_llm = value

        def stop_event(self):
            self.stopped = True

        def request_llm(self, **kwargs):
            self.requested_prompt = kwargs.get("prompt")
            self.requested_conversation = kwargs.get("conversation")
            return PokeLogicTest._DummyReq()

    @staticmethod
    def _raw(group_id="20001", target_id="bot", user_id="10001"):
        raw = {
            "post_type": "notice",
            "notice_type": "notify",
            "sub_type": "poke",
            "self_id": "bot",
            "user_id": user_id,
            "target_id": target_id,
        }
        if group_id is not None:
            raw["group_id"] = group_id
        return raw

    def _make_plugin(self, **cfg):
        return make_plugin(context=FakeContext(), **cfg)

    async def test_poke_triggers_llm_and_skips_chat_logic(self):
        plugin = self._make_plugin()
        event = self.FakePokeEvent(self._raw())
        result = await plugin.on_message(event)

        self.assertIsInstance(result, self._DummyReq)
        self.assertFalse(event.stopped)
        self.assertFalse(event.call_llm)
        self.assertIn("戳戳怪", event.requested_prompt)
        self.assertIn("戳了你一下", event.requested_prompt)
        self.assertNotIn("nuoxiaomiao_buffered", event.extras)
        self.assertEqual(plugin._merges, {})
        await plugin.terminate()

    async def test_group_poke_cooldown_drops_burst(self):
        plugin = self._make_plugin(poke_reply_cooldown_seconds=0.5)
        first = self.FakePokeEvent(self._raw(user_id="10001"))
        second = self.FakePokeEvent(self._raw(user_id="10002"))

        r1 = await plugin.on_message(first)
        r2 = await plugin.on_message(second)

        self.assertIsInstance(r1, self._DummyReq)
        self.assertIsNone(r2)
        self.assertTrue(second.stopped)
        await plugin.terminate()

    async def test_private_poke_has_no_cooldown(self):
        plugin = self._make_plugin(poke_reply_cooldown_seconds=0.5)
        first = self.FakePokeEvent(self._raw(group_id=None, user_id="10001"), group_id="")
        second = self.FakePokeEvent(self._raw(group_id=None, user_id="10002"), group_id="")

        r1 = await plugin.on_message(first)
        r2 = await plugin.on_message(second)

        self.assertIsInstance(r1, self._DummyReq)
        self.assertIsInstance(r2, self._DummyReq)
        self.assertFalse(second.stopped)
        await plugin.terminate()

    async def test_non_self_poke_is_ignored(self):
        plugin = self._make_plugin()
        event = self.FakePokeEvent(self._raw(target_id="someone_else"))

        result = await plugin.on_message(event)

        self.assertIsNone(result)
        self.assertFalse(event.stopped)
        self.assertFalse(event.call_llm)
        await plugin.terminate()

    async def test_on_llm_request_does_not_block_poke(self):
        plugin = self._make_plugin(merge_enabled=True)
        event = self.FakePokeEvent(self._raw())
        key = plugin._merge_key(event.unified_msg_origin, event.get_sender_id())
        plugin._merges[key] = MergeSession(
            session_key=event.unified_msg_origin,
            sender_id=event.get_sender_id(),
            parts=["小喵等待中"],
            start_ts=time.monotonic(),
            last_ts=time.monotonic(),
            origin_event=event,
        )

        await plugin.on_llm_request(event, object())

        self.assertFalse(event.stopped)
        await plugin.terminate()


class DynamicDebounceTest(unittest.IsolatedAsyncioTestCase):
    """智能防抖：等多久按内容决定，外加群聊/私聊开关和白名单。"""

    def _plugin(self, **cfg):
        plugin = make_plugin(**cfg)
        plugin._send_merged_event = AsyncMock()
        return plugin

    def _key(self, plugin, event):
        return plugin._merge_key(event.unified_msg_origin, event.get_sender_id())

    async def test_short_message_opens_slow_window(self):
        plugin = self._plugin()
        event = FakeEvent(message_str="小喵在吗")
        await plugin.on_message(event)

        merge = plugin._merges[self._key(plugin, event)]
        self.assertAlmostEqual(merge.idle_seconds, 1.2)
        self.assertTrue(event.stopped)
        await plugin.terminate()

    async def test_sentence_with_ending_punctuation_replies_immediately(self):
        plugin = self._plugin()
        event = FakeEvent(message_str="小喵在吗。")
        await plugin.on_message(event)

        self.assertNotIn(self._key(plugin, event), plugin._merges)
        self.assertFalse(event.stopped)
        await plugin.terminate()

    async def test_long_message_replies_immediately(self):
        plugin = self._plugin()
        event = FakeEvent(message_str="小喵" + "很长的一句话" * 3)
        await plugin.on_message(event)

        self.assertNotIn(self._key(plugin, event), plugin._merges)
        self.assertFalse(event.stopped)
        await plugin.terminate()

    async def test_fast_window_can_be_configured_to_still_wait(self):
        plugin = self._plugin(debounce_fast_idle_seconds=0.3)
        event = FakeEvent(message_str="小喵在吗。")
        await plugin.on_message(event)

        merge = plugin._merges[self._key(plugin, event)]
        self.assertAlmostEqual(merge.idle_seconds, 0.3)
        await plugin.terminate()

    async def test_dynamic_off_falls_back_to_fixed_idle(self):
        plugin = self._plugin(debounce_dynamic_enabled=False, merge_idle_seconds=7)
        event = FakeEvent(message_str="小喵在吗。")
        await plugin.on_message(event)

        merge = plugin._merges[self._key(plugin, event)]
        self.assertIsNone(merge.idle_seconds)
        await plugin.terminate()

    async def test_group_switch_off_disables_debounce(self):
        plugin = self._plugin(debounce_group_enabled=False)
        event = FakeEvent(message_str="小喵在吗")
        await plugin.on_message(event)

        self.assertNotIn(self._key(plugin, event), plugin._merges)
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

        self.assertNotIn(self._key(plugin, event), plugin._merges)
        await plugin.terminate()

    async def test_whitelist_limits_debounce_to_listed_users(self):
        plugin = self._plugin(debounce_user_whitelist=["99999"])
        event = FakeEvent(message_str="小喵在吗")
        await plugin.on_message(event)

        self.assertNotIn(self._key(plugin, event), plugin._merges)
        await plugin.terminate()

    async def test_whitelist_hit_keeps_debounce(self):
        plugin = self._plugin(debounce_user_whitelist=["10001"])
        event = FakeEvent(message_str="小喵在吗")
        await plugin.on_message(event)

        self.assertIn(self._key(plugin, event), plugin._merges)
        await plugin.terminate()

    async def test_too_many_parts_releases_immediately(self):
        plugin = self._plugin(debounce_max_parts=2, merge_max_total_seconds=60)
        first = FakeEvent(message_str="第一句")
        second = FakeEvent(is_trigger=False, message_str="第二句")
        await plugin.on_message(first)
        await plugin.on_message(second)

        merge = plugin._merges[self._key(plugin, first)]
        self.assertEqual(merge.idle_seconds, 0.0)
        await plugin.terminate()

    async def test_keyword_wake_short_message_enters_debounce(self):
        plugin = self._plugin(keyword_wake_enabled=True, keyword_wake_list=["小喵"])
        event = FakeEvent(is_trigger=False, message_str="小喵你看这个")
        await plugin.on_message(event)

        self.assertIn(self._key(plugin, event), plugin._merges)
        self.assertTrue(event.stopped)
        await plugin.terminate()

    async def test_legacy_config_without_new_key_still_works(self):
        plugin = self._plugin(merge_enabled=False)
        event = FakeEvent(message_str="小喵在吗")
        await plugin.on_message(event)

        self.assertNotIn(self._key(plugin, event), plugin._merges)
        await plugin.terminate()

    async def test_plain_group_message_does_no_extra_work(self):
        """没叫机器人、也没在等的消息，连图片都不该下载。"""
        plugin = self._plugin(merge_idle_seconds=30, merge_max_total_seconds=60)
        downloaded = []

        async def fake_localize(event, components, key=""):
            downloaded.append(key)
            return components

        plugin._localize_image_components = fake_localize
        event = FakeEvent(
            is_trigger=False,
            message_str="",
            components=[Image(file="https://example.invalid/plain.png")],
        )
        await plugin.on_message(event)

        self.assertEqual(downloaded, [])
        self.assertNotIn(self._key(plugin, event), plugin._merges)
        await plugin.terminate()

    async def test_trigger_image_is_still_localized(self):
        """叫了机器人、要进缓冲的消息，图片照旧提前落盘。"""
        plugin = self._plugin(merge_idle_seconds=30, merge_max_total_seconds=60)
        downloaded = []

        async def fake_localize(event, components, key=""):
            downloaded.append(key)
            return components

        plugin._localize_image_components = fake_localize
        event = FakeEvent(
            message_str="",
            components=[Image(file="https://example.invalid/wake.png")],
        )
        await plugin.on_message(event)

        self.assertEqual(len(downloaded), 1)
        self.assertIn(self._key(plugin, event), plugin._merges)
        await plugin.terminate()

    def _private(self, text):
        return FakeEvent(
            is_private=True,
            session="aiocqhttp:FriendMessage:10001",
            group_id="",
            message_str=text,
        )

    def _typing_event(self, event_type=1, user_id="10001"):
        """伪造一条 QQ「对方正在输入」通知。"""
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
        """只叫一声名字时等久一点，因为名字后面通常还有正题。"""
        plugin = self._plugin(
            keyword_wake_enabled=True,
            keyword_wake_list=["小喵"],
            debounce_wake_idle_seconds=4.0,
        )
        event = FakeEvent(is_trigger=False, message_str="小喵")
        await plugin.on_message(event)

        merge = plugin._merges[self._key(plugin, event)]
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

        merge = plugin._merges[self._key(plugin, event)]
        self.assertAlmostEqual(merge.idle_seconds, 1.2)
        await plugin.terminate()

    async def test_typing_notice_marks_user_as_typing(self):
        plugin = self._plugin()
        await plugin.on_message(self._typing_event(event_type=1))

        key = plugin._merge_key("aiocqhttp:FriendMessage:10001", "10001")
        self.assertGreater(plugin._typing_until.get(key, 0.0), time.monotonic())
        await plugin.terminate()

    async def test_typing_hold_prevents_idle_release(self):
        """对方在打字时，合并不会按静默超时放行；一停手立刻放行。"""
        plugin = self._plugin(
            debounce_slow_idle_seconds=0.2,
            debounce_typing_hold_seconds=3.0,
            merge_max_total_seconds=20,
        )
        released = []

        async def capture(ms, full_text):
            released.append(full_text)

        plugin._send_merged_event = capture

        chat = self._private("贴贴")
        await plugin.on_message(chat)
        key = self._key(plugin, chat)
        self.assertIn(key, plugin._merges)

        await plugin.on_message(self._typing_event(event_type=1))
        await asyncio.sleep(0.6)
        self.assertIn(key, plugin._merges)
        self.assertEqual(released, [])

        await plugin.on_message(self._typing_event(event_type=2))
        await asyncio.sleep(0.05)
        self.assertNotIn(key, plugin._merges)
        self.assertEqual(len(released), 1)
        await plugin.terminate()

    async def test_typing_stop_without_merge_is_harmless(self):
        plugin = self._plugin()
        await plugin.on_message(self._typing_event(event_type=2))
        await plugin.terminate()

    async def test_private_debounce_works_even_if_guard_scope_off(self):
        """「私聊里启用刷屏拦截」关着，私聊防抖照样生效。"""
        plugin = self._plugin(include_private=False)
        event = self._private("小喵在吗")
        await plugin.on_message(event)

        self.assertIn(self._key(plugin, event), plugin._merges)
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
        """私聊合并放行时也不该被连刷拦截记账。"""
        plugin = self._plugin(
            include_private=False, debounce_private_enabled=True, trigger_rounds=2
        )
        released = []

        async def capture(ms, full_text):
            released.append(full_text)

        plugin._send_merged_event = capture
        for index in range(4):
            event = self._private(f"私聊短句{index}")
            await plugin.on_message(event)
            key = self._key(plugin, event)
            if key in plugin._merges:
                await plugin._release_merge(key, reason="test")

        self.assertEqual(len(released), 4)
        await plugin.terminate()


def make_activity_plugin(context=None, **cfg):
    """给每个测试实例一个独立的临时 SQLite，避免污染生产库。"""
    cfg.setdefault(
        "activity_db_path",
        str(Path(tempfile.mkdtemp(prefix="nxm_activity_")) / "activity.db"),
    )
    return make_plugin(context, **cfg)


class GroupActivityTest(unittest.IsolatedAsyncioTestCase):
    """会话速览：写进 SQLite，重开连接不丢，按天数清理。"""

    async def test_group_message_is_recorded(self):
        plugin = make_activity_plugin()
        await plugin._capture_chat_activity(
            FakeEvent(message_str="大家好", group_name="猫娘群", sender_name="楠酱")
        )

        sessions = plugin._activity_sessions()
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
        await plugin._capture_chat_activity(
            FakeEvent(
                is_private=True,
                session="aiocqhttp:FriendMessage:10001",
                group_id="",
                sender_id="10001",
                sender_name="主人",
                message_str="私聊一句话",
            )
        )

        sessions = plugin._activity_sessions("private")
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["conversation_id"], "10001")
        self.assertEqual(sessions[0]["name"], "主人")
        await plugin.terminate()

    async def test_private_recording_can_be_switched_off(self):
        plugin = make_activity_plugin(activity_record_private=False)
        await plugin._capture_chat_activity(
            FakeEvent(
                is_private=True,
                session="aiocqhttp:FriendMessage:10001",
                group_id="",
                sender_id="10001",
                message_str="不记我",
            )
        )

        self.assertEqual(plugin._activity_sessions(), [])
        await plugin.terminate()

    async def test_records_survive_reopen(self):
        plugin = make_activity_plugin()
        await plugin._capture_chat_activity(FakeEvent(message_str="重启前的消息"))
        db_path = str(plugin._activity_db_file())
        await plugin.terminate()

        reopened = make_activity_plugin(activity_db_path=db_path)
        sessions = reopened._activity_sessions()
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["last_text"], "重启前的消息")
        await reopened.terminate()

    async def test_old_messages_are_pruned(self):
        plugin = make_activity_plugin(activity_retention_days=1)
        plugin._activity_prune_ts = 0.0
        plugin._record_activity(
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
        plugin._maybe_prune_activity()

        self.assertEqual(plugin._activity_sessions(), [])
        await plugin.terminate()

    async def test_long_text_is_truncated(self):
        plugin = make_activity_plugin(activity_buffer_max_text_chars=10)
        await plugin._capture_chat_activity(FakeEvent(message_str="字" * 50))

        text = plugin._activity_sessions()[0]["last_text"]
        self.assertEqual(len(text), 11)
        self.assertTrue(text.endswith("…"))
        await plugin.terminate()

    async def test_synthetic_and_self_messages_are_skipped(self):
        plugin = make_activity_plugin()
        plugin._skip_merge_ids.add("mid-syn")
        await plugin._capture_chat_activity(
            FakeEvent(message_str="合并后重新投递的消息", message_id="mid-syn")
        )
        await plugin._capture_chat_activity(
            FakeEvent(message_str="机器人自己说的", sender_id="bot")
        )

        self.assertEqual(plugin._activity_sessions(), [])
        await plugin.terminate()

    async def test_commands_are_skipped(self):
        plugin = make_activity_plugin(context=FakeContext(["/"]))
        await plugin._capture_chat_activity(
            FakeEvent(message_str="help", components=[Plain("/help")])
        )

        self.assertEqual(plugin._activity_sessions(), [])
        await plugin.terminate()

    async def test_capture_failure_never_raises(self):
        plugin = make_activity_plugin()

        def boom(entry):
            raise RuntimeError("故意出错")

        plugin._record_activity = boom
        # 采集只是顺手记一笔，出错必须自己吞掉，不能影响聊天。
        await plugin._capture_chat_activity(FakeEvent(message_str="正常一句话"))
        await plugin.terminate()

    async def test_switch_off_records_nothing(self):
        plugin = make_activity_plugin(activity_buffer_enabled=False)
        await plugin._capture_chat_activity(FakeEvent(message_str="大家好"))

        self.assertEqual(plugin._activity_sessions(), [])
        await plugin.terminate()


class CapabilityHintTest(unittest.IsolatedAsyncioTestCase):
    """动态提醒只介绍本轮实际工具，聊天参考由统一入口按策略附带。"""

    class _Req:
        def __init__(self, with_tools=True):
            self.system_prompt = ""
            self.extra_user_content_parts = []
            self.func_tool = SimpleNamespace(tools=[
                SimpleNamespace(name=name) for name in (
                    "list_conversations", "read_conversation_messages", "send_message"
                )
            ]) if with_tools else None

    @staticmethod
    def _event(sender_id="10001"):
        return FakeEvent(
            is_private=True,
            session=f"aiocqhttp:FriendMessage:{sender_id}",
            group_id="",
            sender_id=sender_id,
            sender_name="主人",
            message_str="嗯嗯",
        )

    async def test_keyword_chat_gets_activity_prefetch(self):
        # 命中「群里聊什么」时直接预取：不依赖模型自己想起来调工具。
        plugin = make_activity_plugin(context=FakeContext(admins=["10001"]))
        await plugin._capture_chat_activity(
            FakeEvent(message_str="今晚谁在打游戏", group_name="猫娘群")
        )
        req = self._Req()

        await plugin._maybe_inject_context(
            FakeEvent(
                is_private=True,
                session="aiocqhttp:FriendMessage:10001",
                group_id="",
                sender_id="10001",
                message_str="他们刚才在群里聊什么",
            ),
            req,
        )

        self.assertEqual(len(req.extra_user_content_parts), 1)
        part = req.extra_user_content_parts[0]
        self.assertIn("[别处聊天参考]", part.text)
        self.assertIn("今晚谁在打游戏", part.text)
        self.assertIn("猫娘群", part.text)
        await plugin.terminate()

    async def test_prefetch_off_gets_nothing(self):
        plugin = make_activity_plugin(
            context=FakeContext(admins=["10001"]), activity_prefetch_enabled=False
        )
        await plugin._capture_chat_activity(
            FakeEvent(message_str="今晚谁在打游戏", group_name="猫娘群")
        )
        req = self._Req()

        await plugin._maybe_inject_context(
            FakeEvent(
                is_private=True,
                session="aiocqhttp:FriendMessage:10001",
                group_id="",
                sender_id="10001",
                message_str="他们刚才在群里聊什么",
            ),
            req,
        )

        self.assertEqual(req.extra_user_content_parts, [])
        await plugin.terminate()

    async def test_prefetch_requires_admin(self):
        plugin = make_activity_plugin(context=FakeContext(admins=["99999"]))
        await plugin._capture_chat_activity(
            FakeEvent(message_str="今晚谁在打游戏", group_name="猫娘群")
        )
        req = self._Req()

        await plugin._maybe_inject_context(
            FakeEvent(
                is_private=True,
                session="aiocqhttp:FriendMessage:10001",
                group_id="",
                sender_id="10001",
                message_str="他们刚才在群里聊什么",
            ),
            req,
        )

        self.assertEqual(req.extra_user_content_parts, [])
        await plugin.terminate()

    async def test_only_dynamic_reminder_is_injected(self):
        plugin = make_activity_plugin(context=FakeContext(admins=["10001"]))
        req = self._Req()

        plugin._maybe_add_capability_hint(req)
        plugin._maybe_add_capability_reminder(self._event(), req)

        # 能力说明只保留动态一层，不再重复追加静态索引。
        self.assertEqual(req.system_prompt, "")
        # 每轮动态提醒：场景菜单，作为临时内容块附在用户消息后
        self.assertEqual(len(req.extra_user_content_parts), 1)
        part = req.extra_user_content_parts[0]
        self.assertIn("[糯小喵能力提醒]", part.text)
        self.assertIn("傲娇可等不了太久", part.text)
        self.assertIn("翻到什么说什么", part.text)
        self.assertTrue(part._no_save)
        await plugin.terminate()

    async def test_capability_reminder_not_duplicated(self):
        plugin = make_activity_plugin(context=FakeContext(admins=["10001"]))
        req = self._Req()

        plugin._maybe_add_capability_hint(req)
        plugin._maybe_add_capability_hint(req)
        plugin._maybe_add_capability_reminder(self._event(), req)
        plugin._maybe_add_capability_reminder(self._event(), req)

        self.assertEqual(req.system_prompt.count("[糯小喵能力提示]"), 0)
        self.assertEqual(len(req.extra_user_content_parts), 1)
        await plugin.terminate()

    async def test_hint_and_reminder_skipped_without_tools(self):
        plugin = make_activity_plugin(context=FakeContext(admins=["10001"]))
        req = self._Req(with_tools=False)

        plugin._maybe_add_capability_hint(req)
        plugin._maybe_add_capability_reminder(self._event(), req)

        self.assertEqual(req.system_prompt, "")
        self.assertEqual(req.extra_user_content_parts, [])
        await plugin.terminate()

    async def test_activity_hint_switch_off_excludes_activity_menu(self):
        plugin = make_activity_plugin(activity_hint_enabled=False)
        req = self._Req()

        plugin._maybe_add_capability_reminder(self._event(), req)

        text = req.extra_user_content_parts[0].text if req.extra_user_content_parts else ""
        self.assertNotIn("翻到什么说什么", text)
        self.assertIn("傲娇可等不了太久", text)
        await plugin.terminate()

    async def test_proactive_hint_switch_off_excludes_proactive_menu(self):
        plugin = make_activity_plugin(proactive_hint_enabled=False)
        req = self._Req()

        plugin._maybe_add_capability_reminder(self._event(), req)

        text = req.extra_user_content_parts[0].text if req.extra_user_content_parts else ""
        self.assertIn("翻到什么说什么", text)
        self.assertNotIn("傲娇可等不了太久", text)
        await plugin.terminate()

    async def test_custom_hint_goes_to_reminder_not_static(self):
        plugin = make_activity_plugin(
            context=FakeContext(admins=["10001"]),
            capability_hint_text="想看别处聊天就自己翻，想催人就自己定闹钟喵。",
        )
        req = self._Req()

        plugin._maybe_add_capability_hint(req)
        plugin._maybe_add_capability_reminder(self._event(), req)

        # 自定义文案进每轮提醒，系统提示词不加静态行
        self.assertEqual(req.system_prompt, "")
        self.assertEqual(len(req.extra_user_content_parts), 1)
        self.assertIn("想看别处聊天就自己翻", req.extra_user_content_parts[0].text)
        await plugin.terminate()

    async def test_custom_hint_ignores_feature_flags(self):
        plugin = make_activity_plugin(
            context=FakeContext(admins=["10001"]),
            capability_hint_text="自定义提示",
            activity_hint_enabled=False,
            proactive_hint_enabled=False,
        )
        req = self._Req()

        plugin._maybe_add_capability_reminder(self._event(), req)

        self.assertIn("自定义提示", req.extra_user_content_parts[0].text)
        await plugin.terminate()

    async def test_blank_custom_hint_falls_back_to_builtin(self):
        plugin = make_activity_plugin(
            context=FakeContext(admins=["10001"]),
            capability_hint_text="   ",
        )
        req = self._Req()

        plugin._maybe_add_capability_hint(req)
        plugin._maybe_add_capability_reminder(self._event(), req)

        self.assertEqual(req.system_prompt, "")
        self.assertIn("[糯小喵能力提醒]", req.extra_user_content_parts[0].text)
        await plugin.terminate()

    async def test_custom_reminder_skipped_without_tools(self):
        plugin = make_activity_plugin(
            context=FakeContext(admins=["10001"]),
            capability_hint_text="自定义提示",
        )
        req = self._Req(with_tools=False)

        plugin._maybe_add_capability_hint(req)
        plugin._maybe_add_capability_reminder(self._event(), req)

        self.assertEqual(req.system_prompt, "")
        self.assertEqual(req.extra_user_content_parts, [])
        await plugin.terminate()

    async def _seed_pending_row(self, plugin, note="催他回我晚饭吃什么"):
        """直接插一条内部追击行并挂上定时器（不再有定时工具，用内部登记）。"""
        row = {
            "id": 0,
            "created_ts": time.time(),
            "fire_ts": time.time() + 25 * 60,
            "source_umo": "aiocqhttp:FriendMessage:10001",
            "platform_id": "aiocqhttp",
            "target_kind": "private",
            "target_id": "10001",
            "target_name": "主人",
            "target_umo": "aiocqhttp:FriendMessage:10001",
            "subject_id": "10001",
            "subject_name": "主人",
            "await_sender_id": "10001",
            "note": note,
            "status": "pending",
        }
        row["id"] = plugin._insert_proactive_row(row)
        await plugin._schedule_proactive_timer(row)

    async def test_reminder_lists_pending_proactive(self):
        plugin = make_activity_plugin(context=FakeContext(admins=["10001"]))
        await self._seed_pending_row(plugin)
        req = self._Req()

        plugin._maybe_add_capability_reminder(self._event(), req)

        text = req.extra_user_content_parts[0].text
        self.assertIn("你还没发出", text)
        self.assertIn("催他回我晚饭吃什么", text)
        await plugin.terminate()

    async def test_reminder_hides_pendings_for_non_admin(self):
        # 未发清单含便签里的私聊语境，只在主人/管理员的会话注入，
        # 普通群友的提示词里不能带，防被诱导复述。
        plugin = make_activity_plugin(context=FakeContext(admins=["10001"]))
        await self._seed_pending_row(plugin)
        req = self._Req()

        plugin._maybe_add_capability_reminder(self._event(sender_id="99999"), req)

        text = req.extra_user_content_parts[0].text
        self.assertIn("[糯小喵能力提醒]", text)
        self.assertNotIn("你还没发出", text)
        self.assertNotIn("催他回我晚饭吃什么", text)
        await plugin.terminate()

    async def test_reminder_astrhe_division_when_installed(self):
        plugin = make_activity_plugin(context=FakeContext(admins=["10001"]))

        with_astrhe = plugin._capability_reminder(
            show_pendings=False, has_astrhe=True,
            tool_names={"astrhe_schedule", "send_message"},
        )
        without = plugin._capability_reminder(
            show_pendings=False, has_astrhe=False, tool_names={"send_message"},
        )

        self.assertIn("astrhe_schedule", with_astrhe)
        self.assertIn("工具分工", with_astrhe)
        self.assertNotIn("astrhe_schedule", without)
        await plugin.terminate()

    async def test_reminder_detects_astrhe_tool_in_request(self):
        plugin = make_activity_plugin(context=FakeContext(admins=["10001"]))
        req = self._Req()
        req.func_tool = SimpleNamespace(tools=[
            SimpleNamespace(name="astrhe_schedule"), SimpleNamespace(name="send_message")
        ])

        plugin._maybe_add_capability_reminder(self._event(), req)

        self.assertEqual(len(req.extra_user_content_parts), 1)
        self.assertIn("astrhe_schedule", req.extra_user_content_parts[0].text)
        await plugin.terminate()

    async def test_reminder_includes_other_sessions_digest(self):
        # 开了「每轮带别处近况」时，她在当前会话里天然知道别处刚聊了什么。
        # 注意这项默认是关的：摘要会填平信息缺口，模型就不去调速览工具了。
        plugin = make_activity_plugin(
            context=FakeContext(admins=["10001"]),
            capability_context_digest=True,
        )
        plugin._record_activity(
            ActivityEntry(
                platform_id="aiocqhttp",
                ts=time.time(),
                sender_id="10002",
                sender_name="小初音",
                conversation_id="70000",
                conversation_name="别的群",
                kind="group",
                text="群里聊到晚饭了",
            )
        )
        req = self._Req()

        plugin._maybe_add_capability_reminder(self._event(), req)

        await plugin._maybe_inject_context(self._event(), req)
        text = "\n".join(part.text for part in req.extra_user_content_parts)
        self.assertIn("别处近况", text)
        self.assertIn("群里聊到晚饭了", text)
        await plugin.terminate()

    async def test_reminder_digest_off_by_default(self):
        # 默认不带：模型看不到现成摘要，才会去调会话速览工具。
        plugin = make_activity_plugin(context=FakeContext(admins=["10001"]))
        self.assertFalse(plugin.capability_context_digest)
        plugin._record_activity(
            ActivityEntry(
                platform_id="aiocqhttp",
                ts=time.time(),
                sender_id="10002",
                sender_name="小初音",
                conversation_id="70000",
                conversation_name="别的群",
                kind="group",
                text="群里聊到晚饭了",
            )
        )
        req = self._Req()

        plugin._maybe_add_capability_reminder(self._event(), req)

        await plugin._maybe_inject_context(self._event(), req)
        text = "\n".join(part.text for part in req.extra_user_content_parts)
        self.assertNotIn("别处近况", text)
        self.assertNotIn("群里聊到晚饭了", text)
        await plugin.terminate()

    async def test_reminder_digest_can_be_switched_off(self):
        plugin = make_activity_plugin(
            context=FakeContext(admins=["10001"]),
            capability_context_digest=False,
        )
        plugin._record_activity(
            ActivityEntry(
                platform_id="aiocqhttp",
                ts=time.time(),
                sender_id="10002",
                sender_name="小初音",
                conversation_id="70000",
                conversation_name="别的群",
                kind="group",
                text="群里聊到晚饭了",
            )
        )
        req = self._Req()

        plugin._maybe_add_capability_reminder(self._event(), req)

        await plugin._maybe_inject_context(self._event(), req)
        text = "\n".join(part.text for part in req.extra_user_content_parts)
        self.assertNotIn("别处近况", text)
        await plugin.terminate()

    async def test_digest_private_gate_in_reminder(self):
        # 别处近况：群的动态默认互通，别人的私聊默认不进（防串话）。
        plugin = make_activity_plugin(
            context=FakeContext(admins=["10001"]),
            capability_context_digest=True,
        )
        plugin._record_activity(
            ActivityEntry(
                platform_id="aiocqhttp",
                ts=time.time(),
                sender_id="10002",
                sender_name="小初音",
                conversation_id="70000",
                conversation_name="别的群",
                kind="group",
                text="群里聊到晚饭了",
            )
        )
        plugin._record_activity(
            ActivityEntry(
                platform_id="aiocqhttp",
                ts=time.time(),
                sender_id="10002",
                sender_name="小初音",
                conversation_id="10002",
                conversation_name="小初音",
                kind="private",
                text="私聊里的小秘密",
            )
        )
        req = self._Req()

        plugin._maybe_add_capability_reminder(self._event(), req)

        await plugin._maybe_inject_context(self._event(), req)
        text = "\n".join(part.text for part in req.extra_user_content_parts)
        self.assertIn("群里聊到晚饭了", text)
        self.assertNotIn("小秘密", text)
        # 开了私聊开关才进摘要
        plugin.proactive_context_other_private = True
        req2 = self._Req()
        plugin._maybe_add_capability_reminder(self._event(), req2)
        await plugin._maybe_inject_context(self._event(), req2)
        text2 = "\n".join(part.text for part in req2.extra_user_content_parts)
        self.assertIn("小秘密", text2)
        await plugin.terminate()


class GroupActivityToolTest(unittest.IsolatedAsyncioTestCase):
    """只读工具：主人/管理员在任何会话都能查，其他人拿不到。"""

    def _seed(self, plugin):
        now = time.time()
        plugin._record_activity(
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
        plugin._record_activity(
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
        plugin._record_activity(
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

        # 当前私聊（10001）不该出现在列表里，其它两个会话都在。
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

        # 6 分钟前 → 只有 5 分钟前的技术群和 1 分钟前的私聊能留下。
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
        # 空参不再报错：模型第一次试探就该拿到最近的记录。
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


def make_proactive_plugin(context=None, **cfg):
    """主动消息测试实例：独立临时库 + 极短延迟，方便等触发。

    默认关闭静默时段（start==end）：不然凌晨跑套件时，
    触发路径会被静默判断拦下、fire 用例集体假失败。
    """
    cfg.setdefault(
        "activity_db_path",
        str(Path(tempfile.mkdtemp(prefix="nxm_proactive_")) / "activity.db"),
    )
    cfg.setdefault("proactive_min_delay_minutes", 0.01)  # 0.6 秒
    cfg.setdefault("proactive_cooldown_minutes", 0)
    cfg.setdefault("proactive_quiet_hours_start", "08:00")
    cfg.setdefault("proactive_quiet_hours_end", "08:00")
    return make_plugin(context, **cfg)


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
        # 被拒绝时告诉管理员能发去哪，模型才能改参数重试而不是放弃。
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
        # 实际发到目标会话
        self.assertEqual(len(context.sent), 1)
        umo, _chain = context.sent[0]
        self.assertEqual(umo, "aiocqhttp:FriendMessage:10001")
        # 即时发送落一行 fired 记录（日限/冷却按它算）
        conn = plugin._get_activity_db()
        rows = conn.execute(
            "SELECT status, note FROM proactive_pending"
        ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "fired")
        self.assertIn("晚饭", rows[0][1])
        await plugin.terminate()

    async def test_generation_uses_target_history_and_persona(self):
        # 生成交给"目标会话的她"：那边的对话历史 + 那边的人设进提示。
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
        # 反幻觉：生成必须被告知"一切以真实对话为准，念头可能记错"
        self.assertIn("一切以它为准", call["prompt"])
        self.assertIn("编造没发生的事", call["prompt"])
        await plugin.terminate()

    async def test_group_send_without_at_has_no_at(self):
        # 不指定 at 就谁都不@（旧版强制@最近说话的人，已移除）。
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
        plugin._record_activity(
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
        # 纯数字群号（不带 群: 前缀）命中允许名单就直接当群处理。
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
        _umo, chain = context.sent[0]
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
        # 静默时段内：内部顺延到时段结束，fire_at 如实返回。
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
        self.assertEqual(len(plugin._proactive_pending), 1)
        row = next(iter(plugin._proactive_pending.values()))
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
        # 生成失败宁可不发，绝不发模板话；返回错误让模型知道。
        provider = FakeProvider(raise_error=True)
        context = FakeContext(admins=["10001"], provider=provider)
        plugin = make_proactive_plugin(context=context)

        data = json.loads(
            await plugin.send_message(self._private(), intent="想说点什么")
        )

        self.assertIn("error", data)
        self.assertEqual(len(context.sent), 0)
        conn = plugin._get_activity_db()
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
        # 顺延堆叠上限：同一目标排队满额后再登记直接拒绝，不让模型无限堆闹钟。
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
        self.assertEqual(len(plugin._proactive_pending), 2)
        await plugin.terminate()


class ProactiveFireTest(unittest.IsolatedAsyncioTestCase):
    """内部追击/顺延到点触发：目标语境生成、发送、@ 与边界（直接喂行）。"""

    def _private(self, sender_id="10001"):
        return FakeEvent(
            is_private=True,
            session=f"aiocqhttp:FriendMessage:{sender_id}",
            group_id="",
            sender_id=sender_id,
            sender_name="主人",
            message_str="我先去忙了",
        )

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
        row["id"] = plugin._insert_proactive_row(row)
        return row

    async def test_timer_fires_private_message(self):
        # 顺延中的内部行到点触发：目标语境生成 + 发送 + 状态置 fired。
        provider = FakeProvider(text="人呢喵？")
        context = FakeContext(admins=["10001"], provider=provider)
        plugin = make_proactive_plugin(context=context)
        row = self._row(plugin)
        await plugin._schedule_proactive_timer({**row, "fire_ts": time.time() + 0.01})

        await asyncio.sleep(1.5)

        self.assertEqual(len(context.sent), 1)
        umo, _chain = context.sent[0]
        self.assertEqual(umo, "aiocqhttp:FriendMessage:10001")
        self.assertEqual(len(plugin._proactive_pending), 0)
        conn = plugin._get_activity_db()
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

        await plugin._fire_proactive(row)

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

        await plugin._fire_proactive(row)

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

        await plugin._fire_proactive(row)

        _umo, chain = context.sent[0]
        self.assertTrue(all(type(comp).__name__ != "At" for comp in chain.chain))
        await plugin.terminate()

    async def test_fire_group_not_allowed_skips(self):
        provider = FakeProvider(text="喵")
        context = FakeContext(admins=["10001"], provider=provider)
        plugin = make_proactive_plugin(context=context)
        row = self._row(plugin, target_kind="group", target_id="90000")

        await plugin._fire_proactive(row)

        self.assertEqual(len(context.sent), 0)
        conn = plugin._get_activity_db()
        status = conn.execute("SELECT status FROM proactive_pending").fetchone()[0]
        self.assertEqual(status, "skipped")
        await plugin.terminate()

    async def test_fire_private_not_allowed_skips(self):
        provider = FakeProvider(text="喵")
        context = FakeContext(admins=["99999"], provider=provider)
        plugin = make_proactive_plugin(context=context)
        row = self._row(plugin, target_kind="private", target_id="10001")

        await plugin._fire_proactive(row)

        self.assertEqual(len(context.sent), 0)
        conn = plugin._get_activity_db()
        status = conn.execute("SELECT status FROM proactive_pending").fetchone()[0]
        self.assertEqual(status, "skipped")
        await plugin.terminate()

    async def test_master_switch_off_skips_pending(self):
        # 插件总开关 enabled=False：已排定的安排到点不发送，按 skipped 落库
        # （schema 承诺"关掉后插件所有功能停止"，定时器路径也必须遵守）。
        provider = FakeProvider(text="喵")
        context = FakeContext(admins=["10001"], provider=provider)
        plugin = make_proactive_plugin(context=context, enabled=False)
        row = self._row(plugin)

        await plugin._fire_proactive(row)

        self.assertEqual(len(context.sent), 0)
        conn = plugin._get_activity_db()
        status = conn.execute("SELECT status FROM proactive_pending").fetchone()[0]
        self.assertEqual(status, "skipped")
        await plugin.terminate()

    async def test_fire_waits_for_target_lock(self):
        # 到点触发必须等 per-target 锁：锁被占着就干等，不许绕过冷却并发发送。
        provider = FakeProvider(text="排队的话喵")
        context = FakeContext(admins=["10001"], provider=provider)
        plugin = make_proactive_plugin(context=context)
        row = self._row(plugin)
        lock = plugin._get_proactive_target_lock("private", "10001")
        await lock.acquire()

        task = asyncio.ensure_future(plugin._fire_proactive(row))
        await asyncio.sleep(0.05)
        self.assertFalse(task.done())
        lock.release()
        await asyncio.wait_for(task, timeout=5)
        self.assertEqual(len(context.sent), 1)
        conn = plugin._get_activity_db()
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
        row["id"] = plugin._insert_proactive_row(row)
        return row

    async def test_daily_limit_skips_send(self):
        context = FakeContext(admins=["10001"])
        plugin = make_proactive_plugin(
            context=context, proactive_daily_limit_per_person=2
        )
        row = self._row(plugin)
        # 预置今天已经发过 2 次
        conn = plugin._get_activity_db()
        for _ in range(2):
            conn.execute(
                "INSERT INTO proactive_pending (created_ts, fire_ts, done_ts,"
                " target_kind, target_id, target_umo, status)"
                " VALUES (?, ?, ?, 'private', '10001', 'x', 'fired')",
                (time.time() - 100, time.time() - 100, time.time() - 100),
            )
        conn.commit()

        await plugin._fire_proactive(row)

        self.assertEqual(len(context.sent), 0)
        conn2 = plugin._get_activity_db()
        status = conn2.execute(
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
        conn = plugin._get_activity_db()
        conn.execute(
            "INSERT INTO proactive_pending (created_ts, fire_ts, done_ts,"
            " target_kind, target_id, target_umo, status)"
            " VALUES (?, ?, ?, 'private', '10001', 'x', 'fired')",
            (time.time() - 60, time.time() - 60, time.time() - 60),
        )
        conn.commit()

        await plugin._fire_proactive(row)

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

        adjusted = plugin._proactive_quiet_adjust(fire_ts)

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

        self.assertEqual(plugin._proactive_quiet_adjust(fire_ts), fire_ts)
        await plugin.terminate()

    async def test_llm_failure_sends_nothing(self):
        context = FakeContext(admins=["10001"], provider=FakeProvider(raise_error=True))
        plugin = make_proactive_plugin(context=context)
        row = self._row(plugin)

        await plugin._fire_proactive(row)

        # 生成失败就宁可不发，绝不用模板话。
        self.assertEqual(len(context.sent), 0)
        conn = plugin._get_activity_db()
        status = conn.execute(
            "SELECT status FROM proactive_pending WHERE id = ?", (row["id"],)
        ).fetchone()[0]
        self.assertEqual(status, "failed")
        await plugin.terminate()

    async def test_persistence_restores_pending(self):
        context = FakeContext(admins=["10001"])
        plugin = make_proactive_plugin(context=context)
        db_path = str(plugin._activity_db_file())
        self._row(plugin, note="重启也要记得找我", fire_ts=time.time() + 1800)
        await plugin.terminate()

        reopened = make_proactive_plugin(
            context=FakeContext(admins=["10001"]), activity_db_path=db_path
        )
        await reopened._load_pending_proactive()

        self.assertEqual(len(reopened._proactive_pending), 1)
        restored = next(iter(reopened._proactive_pending.values()))
        self.assertEqual(restored["note"], "重启也要记得找我")
        await reopened.terminate()

    async def test_stale_pending_not_restored(self):
        context = FakeContext(admins=["10001"])
        plugin = make_proactive_plugin(context=context)
        self._row(plugin, fire_ts=time.time() - 3 * 86400)
        db_path = str(plugin._activity_db_file())
        await plugin.terminate()

        reopened = make_proactive_plugin(
            context=FakeContext(admins=["10001"]), activity_db_path=db_path
        )
        await reopened._load_pending_proactive()

        self.assertEqual(len(reopened._proactive_pending), 0)
        conn = reopened._get_activity_db()
        status = conn.execute("SELECT status FROM proactive_pending").fetchone()[0]
        self.assertEqual(status, "skipped")
        await reopened.terminate()


class FakeConversationManager:
    """补记历史用的假对话管理器：内存版 conversations 表。"""

    def __init__(self):
        self.sessions = {}  # umo -> cid
        self.conversations = {}  # cid -> history 列表

    async def get_curr_conversation_id(self, umo):
        return self.sessions.get(umo)

    async def new_conversation(
        self, umo, platform_id=None, content=None, title=None, persona_id=None
    ):
        cid = f"conv-{len(self.conversations) + 1}"
        self.sessions[umo] = cid
        self.conversations[cid] = []
        return cid

    async def get_conversation(self, umo, cid, create_if_not_exists=False):
        if cid not in self.conversations:
            return None
        return SimpleNamespace(cid=cid, history=json.dumps(self.conversations[cid]))

    async def update_conversation(
        self,
        umo,
        cid=None,
        history=None,
        title=None,
        persona_id=None,
        token_usage=None,
    ):
        if cid is None:
            cid = self.sessions.get(umo)
        if cid is None or cid not in self.conversations:
            return
        self.conversations[cid] = list(history or [])


def fetch_activity_rows(plugin, sender_id=None):
    """直接翻测试库里的速览记录（按发送者过滤可选）。"""
    conn = plugin._get_activity_db()
    sql = (
        "SELECT kind, conversation_id, conversation_name, sender_id, sender_name, "
        "text FROM activity_messages"
    )
    if sender_id:
        return conn.execute(sql + " WHERE sender_id = ?", (sender_id,)).fetchall()
    return conn.execute(sql).fetchall()


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
        # 预置一段已有历史：补记是"追加"，不是给空会话凭空开头。
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
        # 补记历史走的是 create_task，给事件循环一点时间。
        await asyncio.sleep(0.3)

        history = conv_mgr.conversations[cid]
        self.assertEqual(len(history), 2)
        appended = history[-1]
        self.assertEqual(appended["role"], "assistant")
        # 用 AstrBot 4.28.2 的规范格式：content 是分类型部件列表
        self.assertIsInstance(appended["content"], list)
        text = "".join(
            str(part.get("text") or "")
            for part in appended["content"]
            if isinstance(part, dict)
        )
        # 便签注记和正文都在，那边的模型才知道这句话是自己说的、为什么说
        self.assertIn("催他回我晚饭吃什么", text)
        self.assertIn("来找我喵～", text)
        await plugin.terminate()

    async def test_send_skips_history_for_fresh_conversation(self):
        # 从没聊过的会话不再自动新建：速览库里已有记录，历史这条可省。
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
        # 群里 @ 主人需要先有记录可查
        plugin._record_activity(
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



class PendingSummaryTest(unittest.IsolatedAsyncioTestCase):
    """未发/已发清单文案（内部追击可见性）。"""

    async def test_summary_lists_recently_fired(self):
        plugin = make_proactive_plugin(context=FakeContext(admins=["10001"]))
        conn = plugin._get_activity_db()
        conn.execute(
            "INSERT INTO proactive_pending (created_ts, fire_ts, done_ts,"
            " target_kind, target_id, target_name, status)"
            " VALUES (?, ?, ?, 'private', '10001', '主人', 'fired')",
            (time.time() - 100, time.time() - 100, time.time() - 50),
        )
        conn.commit()

        summary = plugin._pending_proactive_summary()

        self.assertIn("已经发出去", summary)
        self.assertIn("主人", summary)
        await plugin.terminate()



class SilentWakeTest(unittest.IsolatedAsyncioTestCase):
    """静默自醒：概率随安静上升，floor/ceil 兜住两端，预算与复位语义正确。"""

    def _plugin(self, **cfg):
        return make_proactive_plugin(context=FakeContext(admins=["10001"]), **cfg)

    async def test_never_before_floor(self):
        plugin = self._plugin()

        for roll in (0.0, 0.5, 0.99):
            hit, probability = plugin._silent_wake_should_trigger(4.9, roll)
            self.assertFalse(hit)
            self.assertEqual(probability, 0.0)
        await plugin.terminate()

    async def test_always_at_ceil(self):
        plugin = self._plugin()

        hit, probability = plugin._silent_wake_should_trigger(60.0, 0.999999)
        self.assertTrue(hit)
        self.assertEqual(probability, 1.0)
        await plugin.terminate()

    async def test_probability_rises_with_quiet(self):
        plugin = self._plugin()
        _, p_low = plugin._silent_wake_should_trigger(10.0, 1.0)
        _, p_mid = plugin._silent_wake_should_trigger(30.0, 1.0)
        _, p_high = plugin._silent_wake_should_trigger(50.0, 1.0)

        self.assertGreater(p_mid, p_low)
        self.assertGreater(p_high, p_mid)
        await plugin.terminate()

    async def test_alpha_solves_target_mean(self):
        # 固定种子模拟：解出的 α 应让平均触发分钟落在目标均值附近（±20%）。
        plugin = self._plugin()
        floor = plugin.proactive_silent_wake_floor_minutes
        ceil = plugin.proactive_silent_wake_ceil_minutes
        target = plugin.proactive_silent_wake_target_mean_minutes
        rng = random.Random(42)
        times = []
        for _ in range(800):
            t = floor
            while t < ceil:
                hit, _probability = plugin._silent_wake_should_trigger(t, rng.random())
                if hit:
                    break
                t += 1
            times.append(t)
        mean = sum(times) / len(times)

        self.assertGreaterEqual(mean, target * 0.8)
        self.assertLessEqual(mean, target * 1.2)
        await plugin.terminate()

    async def test_budget_exhaustion_and_daily_reset(self):
        plugin = self._plugin(proactive_silent_wake_daily_budget=1)

        self.assertTrue(plugin._silent_wake_budget_left())
        day = time.strftime("%Y-%m-%d")
        rid = plugin.resident.store.new_run("test", "default", day)
        plugin.resident.store.charge(rid, day, 1)
        self.assertFalse(plugin._silent_wake_budget_left())
        # 跨天自动重置
        with plugin.resident.store.transaction() as db:
            db.execute("UPDATE resident_runs SET day='2000-01-01' WHERE id=?", (rid,))
        self.assertTrue(plugin._silent_wake_budget_left())
        self.assertEqual(plugin._silent_wake_budget_used, 0)
        await plugin.terminate()

    async def test_prompt_carries_silent_context(self):
        plugin = self._plugin()

        prompt = plugin._build_heartbeat_prompt(silent_minutes=42)

        self.assertIn("静默自醒", prompt)
        self.assertIn("42", prompt)
        self.assertIn("want", prompt)
        await plugin.terminate()

    async def test_judge_uses_configured_provider_id(self):
        # 判断模型由配置明确指定，不做自动回退——烧的哪个模型一目了然。
        provider = FakeProvider(
            text=json.dumps(
                {"want": True, "target_kind": "private", "target": "10002", "note": "x"},
                ensure_ascii=False,
            )
        )
        context = FakeContext(admins=["10001"], provider=provider)
        plugin = make_proactive_plugin(
            context=context,
            proactive_target_whitelist=["10002"],
            proactive_judge_provider_id="my-model",
        )

        # 心跳功能已删除：判断调用直接以自醒回退的姿势发起。
        decision, judge_ok = await plugin._ask_heartbeat_llm(silent_minutes=5)

        manager = plugin.context.provider_manager
        self.assertIn("my-model", manager.fetched_ids)
        self.assertTrue(judge_ok)
        self.assertTrue(decision and decision.get("want"))
        await plugin.terminate()

    async def test_judge_failure_returns_not_ok(self):
        # 判断调用失败：返回 (None, False)，调用方据此不烧预算并退避。
        provider = FakeProvider(raise_error=True)
        context = FakeContext(admins=["10001"], provider=provider)
        plugin = make_proactive_plugin(context=context)

        decision, judge_ok = await plugin._ask_heartbeat_llm()

        self.assertIsNone(decision)
        self.assertFalse(judge_ok)
        await plugin.terminate()

    async def test_watcher_resets_quiet_timer(self):
        plugin = self._plugin()
        plugin._last_human_activity_ts = None

        await plugin._proactive_reply_watcher(
            FakeEvent(
                is_private=True,
                session="aiocqhttp:FriendMessage:10001",
                group_id="",
                sender_id="10001",
                sender_name="主人",
                message_str="我回来啦",
            )
        )

        self.assertIsNotNone(plugin._last_human_activity_ts)
        self.assertGreater(plugin._last_human_activity_ts, time.time() - 5)
        await plugin.terminate()

    async def test_baseline_falls_back_to_now_on_empty_db(self):
        plugin = self._plugin()
        plugin._last_human_activity_ts = None

        plugin._init_silent_wake_baseline()

        self.assertIsNotNone(plugin._last_human_activity_ts)
        self.assertGreater(plugin._last_human_activity_ts, time.time() - 60)
        await plugin.terminate()

    async def test_ask_llm_without_provider_manager_is_failure(self):
        # 取不到模型管理器/接口按「没调通」处理（第二个返回值 False），
        # 调用方据此退避；不能当成成功空转。
        context = FakeContext(admins=["10001"])
        context.provider_manager = None
        plugin = make_proactive_plugin(context=context)

        decision, judge_ok = await plugin._ask_heartbeat_llm()

        self.assertIsNone(decision)
        self.assertFalse(judge_ok)
        await plugin.terminate()


class SelfWakeIdentityTest(unittest.IsolatedAsyncioTestCase):
    """自醒轮次：事件身份要跟锚定会话一致，记账要能看见工具调用。"""

    def _plugin(self, **cfg):
        return make_proactive_plugin(
            context=FakeContext(admins=["10001"]), **cfg
        )

    async def test_pick_session_ignores_bot_only_private_activity(self):
        # 复刻线上事故形态：管理员的私聊里只有机器人自己的发言（最新），
        # 群里有更早的真人消息。挑会话只认真人活跃，不能被自己的独白带偏。
        plugin = self._plugin(proactive_allow_groups=["20001"])
        now = time.time()
        plugin._record_activity(
            ActivityEntry(
                platform_id="aiocqhttp",
                ts=now,
                sender_id="bot",
                sender_name=DISPLAY_NAME,
                conversation_id="10001",
                conversation_name="Scheduler",
                kind="private",
                text="自言自语",
            )
        )
        plugin._record_activity(
            ActivityEntry(
                platform_id="aiocqhttp",
                ts=now - 3600,
                sender_id="9",
                sender_name="路人",
                conversation_id="20001",
                conversation_name="猫娘群",
                kind="group",
                text="大家好",
            )
        )

        picked = await plugin._pick_self_wake_session()

        self.assertIsNotNone(picked)
        self.assertEqual(picked["kind"], "group")
        self.assertEqual(picked["id"], "20001")
        self.assertTrue(picked["umo"].endswith("GroupMessage:20001"))
        await plugin.terminate()

    async def test_pick_session_falls_back_to_allow_groups_without_humans(self):
        # 库里没有任何真人活动时退回配置名单，不能一轮轮空转。
        plugin = self._plugin(proactive_allow_groups=["20001"])
        plugin._record_activity(
            ActivityEntry(
                platform_id="aiocqhttp",
                ts=time.time(),
                sender_id="bot",
                sender_name=DISPLAY_NAME,
                conversation_id="20001",
                conversation_name="Scheduler",
                kind="private",
                text="独白",
            )
        )

        picked = await plugin._pick_self_wake_session()

        self.assertIsNotNone(picked)
        self.assertEqual(picked["kind"], "group")
        self.assertEqual(picked["id"], "20001")
        await plugin.terminate()

    async def test_on_llm_response_skips_self_wake_events(self):
        # 自醒的独白没有真的发进任何会话，不许再记出"只有机器人发言"的幻影会话。
        plugin = self._plugin()
        # 此用例只验证发送记录，不启动无关的图片清扫后台线程。
        plugin._cleanup_expired_localized_files = AsyncMock()
        wake_event = FakeEvent(message_str="", sender_name="Scheduler")
        wake_event.set_extra(SELF_WAKE_EVENT_EXTRA, True)
        chat_event = FakeEvent(message_str="", sender_name="测试用户")
        response = SimpleNamespace(completion_text="一句话", tools_call_name=None)

        await plugin.on_llm_response(wake_event, response)
        self.assertEqual(plugin._activity_sessions(), [])

        await plugin.on_llm_response(chat_event, response)
        self.assertEqual(plugin._activity_sessions(), [])
        plugin._ensure_send_observer(chat_event)
        await chat_event.send(MessageChain([Plain(response.completion_text)]))
        self.assertEqual(len(plugin._activity_sessions()), 1)
        await plugin.terminate()

    async def test_on_llm_request_applies_kept_toolset_for_wake_events(self):
        from resident_fixtures import tool
        from astrbot.core.agent.tool import ToolSet
        plugin = self._plugin()
        allowed = tool("list_conversations", AsyncMock())
        event = FakeEvent(message_str="醒来")
        event.set_extra(SELF_WAKE_EVENT_EXTRA, True)
        req = ProviderRequest()
        req.func_tool = ToolSet(tools=[allowed, tool("unselected", AsyncMock())])

        await plugin.on_llm_request(event, req)

        self.assertEqual(req.func_tool.tools, [allowed])
        self.assertFalse(event.stopped)
        await plugin.terminate()

    async def test_wake_turn_anchors_group_with_admin_identity_and_accounts_tools(self):
        # 群锚定的自醒：事件必须是群身份 + 管理员 sender（否则速览工具全被
        # 权限挡住），tool_call 响应要被记进 self_wake_runs（data 是 dict）。
        plugin = self._plugin(proactive_allow_groups=["20001"])
        plugin._record_activity(
            ActivityEntry(
                platform_id="aiocqhttp",
                ts=time.time() - 60,
                sender_id="9",
                sender_name="路人",
                conversation_id="20001",
                conversation_name="猫娘群",
                kind="group",
                text="大家好",
            )
        )
        wake_events = []

        from resident_fixtures import build_runner, ScriptedProvider, tool
        from astrbot.core.agent.tool import ToolSet
        from astrbot.core.provider.entities import LLMResponse, TokenUsage
        provider = ScriptedProvider([
            LLMResponse(role="assistant", tools_call_name=["send_message"], tools_call_ids=["call-1"], tools_call_args=[{"intent": "找楠酱说句话"}], usage=TokenUsage(input_other=10,output=2)),
            LLMResponse(role="assistant", completion_text="看完了", usage=TokenUsage(input_other=10,output=2)),
        ])
        plugin._select_self_wake_provider = AsyncMock(return_value=provider)
        async def rejected(event, **kwargs):
            return '{"error":"目标不可用"}'
        async def fake_build_main_agent(**kwargs):
            wake_events.append(kwargs["event"])
            return await build_runner(**kwargs, tools=ToolSet(tools=[tool("send_message", rejected)]))

        async def fake_get_session_conv(event, context):
            return __import__("resident_fixtures").conversation()

        with (
            patch("main._build_main_agent", new=fake_build_main_agent),
            patch("main._get_session_conv", new=fake_get_session_conv),
        ):
            ok, outcome = await plugin._run_self_wake_turn(30.0, 0.5, 0.1)

        self.assertTrue(ok)
        self.assertEqual(outcome, "quiet")
        event = wake_events[0]
        self.assertFalse(event.is_private_chat())
        self.assertEqual(event.get_group_id(), "20001")
        self.assertEqual(event.get_sender_id(), "10001")
        row = plugin._get_activity_db().execute("SELECT steps,tool_calls,said,outcome FROM self_wake_runs ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(row[0], 2)
        self.assertIn("send_message", row[1])
        self.assertEqual(row[2], 0)
        self.assertEqual(row[3], "quiet")
        await plugin.terminate()


def make_user_default_plugin(**cfg):
    """「我的默认」测试实例：给每个用例独立的快照文件。"""
    cfg.setdefault(
        "user_default_config_path",
        str(Path(tempfile.mkdtemp(prefix="nxm_udefault_")) / "user_default_config.json"),
    )
    return make_plugin(**cfg)


class UserDefaultConfigTest(unittest.IsolatedAsyncioTestCase):
    """「我的默认」快照：写入、读取、清除、缺文件回退。"""

    async def test_write_and_load_roundtrip(self):
        plugin = make_user_default_plugin()

        snapshot = plugin._write_user_default_config({"enabled": False, "trigger_rounds": 5})
        loaded = plugin._load_user_default_config()

        self.assertEqual(snapshot, {"enabled": False, "trigger_rounds": 5})
        self.assertEqual(loaded, {"enabled": False, "trigger_rounds": 5})
        await plugin.terminate()

    async def test_load_missing_file_returns_none(self):
        plugin = make_user_default_plugin()

        self.assertIsNone(plugin._load_user_default_config())
        await plugin.terminate()

    async def test_load_broken_file_returns_none(self):
        plugin = make_user_default_plugin()
        path = plugin._user_default_config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{这不是合法json", encoding="utf-8")

        self.assertIsNone(plugin._load_user_default_config())
        await plugin.terminate()

    async def test_clear_removes_snapshot(self):
        plugin = make_user_default_plugin()
        plugin._write_user_default_config({"enabled": True})

        self.assertTrue(plugin._clear_user_default_config())
        self.assertIsNone(plugin._load_user_default_config())
        # 再清一次不报错，返回 False
        self.assertFalse(plugin._clear_user_default_config())
        await plugin.terminate()

    async def test_path_defaults_to_plugin_data_dir(self):
        plugin = make_plugin()

        path = plugin._user_default_config_path()
        self.assertEqual(path.name, "user_default_config.json")
        self.assertIn("data", str(path))
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

        self.assertEqual(len(plugin._proactive_pending), 0)
        self.assertFalse(hasattr(plugin, "_maybe_auto_followup"))
        await plugin.terminate()

    async def test_relaxed_frequency_defaults(self):
        plugin = make_plugin()

        self.assertEqual(plugin.proactive_daily_limit_per_person, 10)
        self.assertEqual(plugin.proactive_max_pending_per_person, 3)
        self.assertEqual(plugin.proactive_cooldown_seconds, 10 * 60)
        self.assertFalse(hasattr(plugin, "proactive_auto_followup_enabled"))
        await plugin.terminate()


class LmemGatewayTest(unittest.IsolatedAsyncioTestCase):
    """长期记忆按需检索：命中回忆意图才查，出错或超时都不影响回复。"""

    class _Req:
        def __init__(self):
            self.system_prompt = ""
            self.extra_user_content_parts = []

    class _FakeMemoryTool:
        name = "recall_long_term_memory"

        def __init__(self, payload=None, error=None, delay=0.0):
            self.payload = payload
            self.error = error
            self.delay = delay
            self.calls = []

        async def call(self, context, **kwargs):
            self.calls.append(kwargs)
            if self.delay:
                await asyncio.sleep(self.delay)
            if self.error:
                raise self.error
            return json.dumps(self.payload, ensure_ascii=False)

    def _context_with_tool(self, tool):
        context = FakeContext(admins=["10001"])
        context.get_llm_tool_manager = lambda: type("FTM", (), {"func_list": [tool]})()
        return context

    def _private(self, text):
        return FakeEvent(
            is_private=True,
            session="aiocqhttp:FriendMessage:10001",
            group_id="",
            message_str=text,
        )

    async def test_plain_chat_never_touches_memory(self):
        tool = self._FakeMemoryTool(payload={"results": []})
        plugin = make_plugin(context=self._context_with_tool(tool))
        req = self._Req()

        await plugin._maybe_inject_context(self._private("抱抱，今天好累"), req)

        self.assertEqual(tool.calls, [])
        self.assertEqual(req.extra_user_content_parts, [])
        await plugin.terminate()

    async def test_recall_intent_injects_memory(self):
        tool = self._FakeMemoryTool(
            payload={"results": [{"content": "主人喜欢喝乌龙茶", "score": 0.92}]}
        )
        plugin = make_plugin(context=self._context_with_tool(tool))
        req = self._Req()

        await plugin._maybe_inject_context(self._private("你还记得我喜欢喝什么吗"), req)

        self.assertEqual(len(tool.calls), 1)
        self.assertEqual(len(req.extra_user_content_parts), 1)
        self.assertIn("乌龙茶", req.extra_user_content_parts[0].text)
        await plugin.terminate()

    async def test_recall_failure_is_swallowed(self):
        tool = self._FakeMemoryTool(error=RuntimeError("记忆库炸了"))
        plugin = make_plugin(context=self._context_with_tool(tool))
        req = self._Req()

        await plugin._maybe_inject_context(self._private("你还记得我的生日吗"), req)

        self.assertEqual(req.extra_user_content_parts, [])
        await plugin.terminate()

    async def test_recall_timeout_is_swallowed(self):
        tool = self._FakeMemoryTool(payload={"results": []}, delay=5.0)
        plugin = make_plugin(
            context=self._context_with_tool(tool), lmem_gateway_timeout_seconds=0.3
        )
        req = self._Req()

        await plugin._maybe_inject_context(self._private("你还记得我的生日吗"), req)

        self.assertEqual(req.extra_user_content_parts, [])
        await plugin.terminate()

    async def test_missing_plugin_is_noop(self):
        plugin = make_plugin()
        req = self._Req()

        await plugin._maybe_inject_context(self._private("你还记得我的生日吗"), req)

        self.assertEqual(req.extra_user_content_parts, [])
        await plugin.terminate()

    async def test_switch_off_never_queries(self):
        tool = self._FakeMemoryTool(
            payload={"results": [{"content": "主人喜欢喝乌龙茶", "score": 0.9}]}
        )
        plugin = make_plugin(
            context=self._context_with_tool(tool), lmem_gateway_enabled=False
        )
        req = self._Req()

        await plugin._maybe_inject_context(self._private("你还记得我喜欢喝什么吗"), req)

        self.assertEqual(tool.calls, [])
        await plugin.terminate()


class SelfWakeTurnTest(unittest.IsolatedAsyncioTestCase):
    """静默自醒：醒来起一轮自主活动（可调工具、可发言），并把经过落库。

    这一组钉住的是「醒来之后做什么」——旧的判断式自醒只回一个 want 布尔值，
    现在改成一轮完整 agent 活动，所以断言的重点是：
      · 抽签命中后真的走了自主轮次，不是只调一次判断；
      · 没会话时跳过且不烧预算；
      · 工具调用与是否发言被如实记录进 self_wake_runs。
    """

    def _plugin(self, **cfg):
        return make_proactive_plugin(context=FakeContext(admins=["10001"]), **cfg)

    async def test_self_wake_runs_table_created(self):
        plugin = self._plugin()
        conn = plugin._get_activity_db()
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='self_wake_runs'"
        ).fetchall()
        self.assertEqual(len(rows), 1)
        await plugin.terminate()

    async def test_record_self_wake_run_persists(self):
        plugin = self._plugin()
        plugin._record_self_wake_run(
            {
                "started_ts": 1000.0,
                "ended_ts": 1005.0,
                "quiet_minutes": 25.0,
                "probability": 0.05,
                "roll": 0.01,
                "steps": 3,
                "tool_calls": [{"name": "list_conversations"}],
                "said": True,
                "sent_to": "aiocqhttp:FriendMessage:10001",
                "said_text": "在忙吗",
                "thinking": "看了一眼，想问问",
                "outcome": "spoke",
            }
        )
        conn = plugin._get_activity_db()
        row = conn.execute(
            "SELECT quiet_minutes, steps, said, outcome, tool_calls "
            "FROM self_wake_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertAlmostEqual(row[0], 25.0)
        self.assertEqual(row[1], 3)
        self.assertEqual(row[2], 1)
        self.assertEqual(row[3], "spoke")
        self.assertIn("list_conversations", row[4])
        await plugin.terminate()

    async def test_prompt_does_not_ask_for_json(self):
        """自醒提示词不该要求模型输出 JSON——她要能自由做事。"""
        plugin = self._plugin()
        text = plugin._self_wake_prompt(20.0)
        self.assertIn("不是真人提出的新要求", text)
        self.assertIn("安静结束", text)
        self.assertIn("send_message", text)
        self.assertNotIn("want", text)
        self.assertNotIn('"target_kind"', text)
        await plugin.terminate()

    async def test_no_session_skips_without_budget(self):
        """没有可用会话时跳过，且不消耗日预算。"""
        plugin = self._plugin(
            proactive_allow_groups=[],
            proactive_target_whitelist=[],
        )
        before = plugin._silent_wake_budget_used
        ok, outcome = await plugin._run_self_wake_turn(30.0, 0.1, 0.01)
        self.assertTrue(ok)  # 跳过算"正常执行"，不该触发退避
        self.assertEqual(outcome, "skipped")
        # 钉住循环层的善后逻辑：SKIPPED 不烧预算（这是真正的消耗点）。
        plugin._settle_self_wake_turn(ok, outcome, time.time())
        self.assertEqual(plugin._silent_wake_budget_used, before)
        conn = plugin._get_activity_db()
        row = conn.execute(
            "SELECT outcome FROM self_wake_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(row[0], "skipped")
        await plugin.terminate()

    async def test_settle_self_wake_turn_semantics(self):
        """循环层善后：失败退避、SKIPPED 免预算、正常消耗预算并复位安静计时。"""
        plugin = self._plugin()

        # 执行失败：退避 30 分钟，不烧预算，安静计时不复位。
        plugin._silent_wake_fail_until = 0.0
        plugin._last_human_activity_ts = 1000.0
        plugin._settle_self_wake_turn(False, "error", time.time())
        self.assertGreater(plugin._silent_wake_fail_until, time.time() + 1700)
        self.assertEqual(plugin._silent_wake_budget_used, 0)
        self.assertEqual(plugin._last_human_activity_ts, 1000.0)

        # SKIPPED：不烧预算，但安静计时复位（醒过一眼也算活动）。
        plugin._settle_self_wake_turn(True, "skipped", time.time())
        self.assertEqual(plugin._silent_wake_budget_used, 0)
        self.assertGreater(plugin._last_human_activity_ts, 1000.0)

        # 正常（quiet/spoke）：烧一次预算并复位安静计时。
        used = plugin._silent_wake_budget_used
        plugin._settle_self_wake_turn(True, "quiet", time.time())
        self.assertEqual(plugin._silent_wake_budget_used, used)
        await plugin.terminate()

    async def test_self_wake_skips_when_no_admins_configured(self):
        """管理员为空时自醒是瞎的（速览/发送工具都过不了权限）：跳过且不烧预算。"""
        plugin = self._plugin(proactive_allow_groups=["90000"])
        plugin._admin_ids = lambda: set()

        before = plugin._silent_wake_budget_used
        ok, outcome = await plugin._run_self_wake_turn(30.0, 0.1, 0.01)
        self.assertTrue(ok)
        self.assertEqual(outcome, "skipped")
        plugin._settle_self_wake_turn(ok, outcome, time.time())
        self.assertEqual(plugin._silent_wake_budget_used, before)
        conn = plugin._get_activity_db()
        row = conn.execute(
            "SELECT outcome FROM self_wake_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(row[0], "skipped")
        await plugin.terminate()

    async def test_allow_tools_off_uses_fallback(self):
        """关掉工具开关时退回旧的判断式自醒。"""
        plugin = self._plugin(
            proactive_silent_wake_allow_tools=False,
            proactive_allow_groups=["90000"],
        )
        called = {"fallback": 0}

        async def fake_fallback(row, quiet):
            called["fallback"] += 1
            row["outcome"] = "quiet"
            row["ended_ts"] = 1.0
            plugin._record_self_wake_run(row)
            return True, "quiet"

        plugin._run_self_wake_fallback = fake_fallback
        with patch("main._get_session_conv", AsyncMock(return_value=SimpleNamespace())):
            ok, outcome = await plugin._run_self_wake_turn(30.0, 0.1, 0.01)
        self.assertTrue(ok)
        self.assertEqual(called["fallback"], 1)
        await plugin.terminate()

    async def test_fallback_records_spoke_when_decision(self):
        """降级路径下，投递真的成功才记 spoke；投递失败不许谎报。"""
        plugin = self._plugin()

        async def fake_ask(silent_minutes=None):
            return (
                {"want": True, "target_kind": "private", "target": "10001",
                 "note": "想问问"},
                True,
            )

        delivered = {"n": 0, "result": {"status": "sent"}}

        async def fake_deliver(decision):
            delivered["n"] += 1
            return delivered["result"]

        plugin._ask_heartbeat_llm = fake_ask
        plugin._deliver_heartbeat_decision = fake_deliver
        row = {"started_ts": 1.0}
        ok, outcome = await plugin._run_self_wake_fallback(row, 25.0)
        self.assertTrue(ok)
        self.assertEqual(delivered["n"], 1)
        self.assertEqual(row["outcome"], "spoke")
        self.assertTrue(row["said"])

        # 投递被拦（返回 error）：不算说过话，审计如实记 quiet。
        delivered["result"] = {"error": "目标不在白名单"}
        row2 = {"started_ts": 2.0}
        ok2, outcome2 = await plugin._run_self_wake_fallback(row2, 25.0)
        self.assertTrue(ok2)
        self.assertEqual(row2["outcome"], "quiet")
        self.assertNotIn("said", row2)
        await plugin.terminate()

    async def test_pick_session_respects_allowlist(self):
        """挑会话时只从允许名单里挑。"""
        plugin = self._plugin(proactive_allow_groups=["90000"])
        picked = await plugin._pick_self_wake_session()
        self.assertIsNotNone(picked)
        self.assertEqual(picked["kind"], "group")
        self.assertEqual(picked["id"], "90000")
        self.assertIn("GroupMessage:90000", picked["umo"])
        await plugin.terminate()

    async def test_config_defaults(self):
        plugin = self._plugin()
        self.assertEqual(plugin.proactive_silent_wake_max_steps, 8)
        self.assertTrue(plugin.proactive_silent_wake_allow_tools)
        await plugin.terminate()

    async def test_max_steps_clamped(self):
        plugin = self._plugin(proactive_silent_wake_max_steps=999)
        self.assertEqual(plugin.proactive_silent_wake_max_steps, 30)
        await plugin.terminate()


class PluginNameOfToolTest(unittest.IsolatedAsyncioTestCase):
    """工具归属插件的判定：多候选取最长匹配，兜底从模块名抠目录名。"""

    def _plugin(self):
        return make_plugin()

    async def test_prefers_longest_matching_key(self):
        # 模块路径同时命中多个 star_map 键时，选最长（最精确）的那条。
        plugin = self._plugin()
        star_map = {
            "data.plugins.astrbot_plugin_a": SimpleNamespace(name="短前缀插件"),
            "data.plugins.astrbot_plugin_a.sub": SimpleNamespace(name="长前缀插件"),
        }
        tool = SimpleNamespace(
            handler_module_path="data.plugins.astrbot_plugin_a.sub.main"
        )

        self.assertEqual(
            plugin._plugin_name_of_tool(tool, star_map), "长前缀插件"
        )
        await plugin.terminate()

    async def test_exact_hit_wins_without_ambiguity(self):
        plugin = self._plugin()
        star_map = {
            "data.plugins.astrbot_plugin_b.main": SimpleNamespace(name="乙插件"),
        }
        tool = SimpleNamespace(handler_module_path="data.plugins.astrbot_plugin_b.main")

        self.assertEqual(plugin._plugin_name_of_tool(tool, star_map), "乙插件")
        await plugin.terminate()

    async def test_falls_back_to_directory_name(self):
        # star_map 完全对不上时，从模块路径里抠插件目录名兜底。
        plugin = self._plugin()
        tool = SimpleNamespace(handler_module_path="data.plugins.some_unknown.main")

        self.assertEqual(plugin._plugin_name_of_tool(tool, {}), "some_unknown")
        await plugin.terminate()


def _wait_until(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


async def _await_until(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


class SplitReplyTextTest(unittest.TestCase):
    """拆条规则：中文标点断句、成对符号/URL/颜文字保护、硬切兜底。"""

    def test_comma_and_period_break_without_punctuation(self):
        segments = _split_reply_text("好的，那我先把文件整理一下。整理完叫你。")
        self.assertEqual(segments, ["好的", "那我先把文件整理一下", "整理完叫你"])

    def test_ascii_punctuation_never_breaks(self):
        text = "你打开 server.ts 看看 v1.2 的配置，网址是 https://example.com/a.b 哦。"
        segments = _split_reply_text(text)
        self.assertEqual(len(segments), 2)
        self.assertIn("server.ts", segments[0])
        self.assertIn("v1.2", segments[0])
        self.assertIn("https://example.com/a.b", segments[1])

    def test_brackets_keep_together(self):
        text = "（括号里的内容，包括逗号）不该被拆开"
        self.assertEqual(_split_reply_text(text), [text])

    def test_unbalanced_bracket_never_splits(self):
        text = "(没闭合的括号，里面还有逗号，反正不拆"
        self.assertEqual(_split_reply_text(text), [text])

    def test_kaomoji_protected(self):
        segments = _split_reply_text("开心 (＾▽＾) 得不行，真的吗？")
        self.assertTrue(any("(＾▽＾)" in s for s in segments))
        for segment in segments:
            self.assertNotIn("\u200b", segment)

    def test_codeblock_protected(self):
        segments = _split_reply_text("代码是```a,b\nc```这样。还有一句。")
        self.assertEqual(
            segments, ["代码是```a,b\nc```这样", "还有一句"]
        )

    def test_short_text_not_split(self):
        self.assertEqual(_split_reply_text("嗯！"), ["嗯！"])

    def test_double_ellipsis_merges_into_previous(self):
        segments = _split_reply_text("我觉着吧……这事得从长计议")
        self.assertEqual(segments, ["我觉着吧……", "这事得从长计议"])

    def test_hard_chunk_splits_long_unpunctuated_text(self):
        segments = _split_reply_text("啊" * 200)
        self.assertGreater(len(segments), 1)
        for segment in segments:
            self.assertLessEqual(len(segment), 61)


class HumanReplyTest(unittest.IsolatedAsyncioTestCase):
    """拟人回复：接管清链、逐条发送、打断与「正在输入」。"""

    def _plugin(self, **cfg):
        cfg.setdefault("human_reply_enabled", True)
        cfg.setdefault("human_reply_chars_per_minute", 10_000_000)  # 打字等待≈0，只测逻辑
        return make_plugin(debounce_enabled=False, **cfg)

    def _llm_result(self, *texts, extra=None):
        from astrbot.core.message.message_event_result import (
            MessageEventResult,
            ResultContentType,
        )

        chain = [Plain(t) for t in texts]
        if extra:
            chain.extend(extra)
        return MessageEventResult(
            chain=chain, result_content_type=ResultContentType.LLM_RESULT
        )

    @staticmethod
    def _plain_texts(messages):
        texts = []
        for message in messages:
            for comp in message.chain:
                if isinstance(comp, Plain):
                    texts.append(comp.text)
        return texts

    async def test_takeover_clears_result_and_sends_bubbles(self):
        plugin = self._plugin()
        event = FakeEvent(
            is_private=True,
            session="aiocqhttp:FriendMessage:10001",
            group_id="",
            sender_id="10001",
        )
        event._result = self._llm_result("第一句话来了，第二句话也来。")

        await plugin.on_decorating_result(event)
        self.assertIsNone(event.get_result())
        self.assertTrue(event.get_extra("_streaming_finished", False))

        await _await_until(lambda: not plugin._human_reply_tasks)
        self.assertEqual(
            self._plain_texts(event.sent), ["第一句话来了", "第二句话也来"]
        )
        await plugin.terminate()

    async def test_takeover_skipped_for_media_chain(self):
        plugin = self._plugin()
        event = FakeEvent(message_str="看图")
        # At 出现在文本之后：不是前缀，说明链里有复杂组件，不接管。
        event._result = self._llm_result("看这张图", extra=[At(qq="10001")])

        await plugin.on_decorating_result(event)
        self.assertIsNotNone(event.get_result())
        self.assertEqual(event.sent, [])
        await plugin.terminate()

    async def test_takeover_skipped_when_disabled(self):
        plugin = self._plugin(human_reply_enabled=False)
        event = FakeEvent(
            is_private=True,
            session="aiocqhttp:FriendMessage:10001",
            group_id="",
            sender_id="10001",
        )
        event._result = self._llm_result("第一句话来了，第二句话也来。")

        await plugin.on_decorating_result(event)
        self.assertIsNotNone(event.get_result())
        self.assertEqual(event.sent, [])
        await plugin.terminate()

    async def test_group_reply_keeps_at_prefix_on_first_bubble(self):
        plugin = self._plugin()
        event = FakeEvent(message_str="在吗")
        event._result = self._llm_result("第一句话来了，第二句话也来。")
        event._result.chain.insert(0, At(qq="10001"))

        await plugin.on_decorating_result(event)
        await _await_until(lambda: not plugin._human_reply_tasks)
        self.assertEqual(len(event.sent), 2)
        first = event.sent[0].chain
        self.assertIsInstance(first[0], At)
        self.assertEqual(str(first[0].qq), "10001")
        await plugin.terminate()

    async def test_interrupt_before_first_bubble_discards_all_and_notes(self):
        # 阶段3（回复完未发送）：还没开口就打断，整条回复不发并记账。
        plugin = self._plugin(human_reply_chars_per_minute=150)
        event = FakeEvent(
            is_private=True,
            session="aiocqhttp:FriendMessage:10001",
            group_id="",
            sender_id="10001",
        )
        event._result = self._llm_result("第一句话，第二句话，第三句话")

        await plugin.on_decorating_result(event)
        await _await_until(lambda: bool(plugin._human_reply_tasks))
        # 第一条气泡还在打字（约 0.8s）就打断：一条都不该发。
        self.assertTrue(
            plugin._interrupt_human_reply(event.unified_msg_origin, "10001")
        )
        # 记账发生在打断标记的那一刻，不能等打字任务自己醒来。
        self.assertIn(event.unified_msg_origin, plugin._human_reply_interrupt_notes)
        await _await_until(lambda: not plugin._human_reply_tasks)
        self.assertEqual(self._plain_texts(event.sent), [])

        umo = event.unified_msg_origin
        entry = plugin._human_reply_interrupt_notes[umo]
        self.assertFalse(entry["seen_any"])
        req = ProviderRequest(system_prompt="人设")
        plugin._maybe_inject_interrupt_note(event, req)
        # 没开口的打断用「没发出去」的话术，不需要引用原文。
        self.assertIn("没能发出去", req.system_prompt)
        self.assertNotIn(umo, plugin._human_reply_interrupt_notes)
        # 提示是一次性的：再注入不该有第二次。
        req2 = ProviderRequest(system_prompt="人设")
        plugin._maybe_inject_interrupt_note(event, req2)
        self.assertNotIn("没能发出去", req2.system_prompt)
        await plugin.terminate()

    async def test_no_interrupt_after_first_bubble_sent(self):
        # 阶段4（首条气泡已发到 QQ）：不允许打断，剩余气泡照常发完。
        plugin = self._plugin(human_reply_chars_per_minute=600)
        event = FakeEvent(
            is_private=True,
            session="aiocqhttp:FriendMessage:10001",
            group_id="",
            sender_id="10001",
        )
        event._result = self._llm_result("第一句话，第二句话，第三句话")

        await plugin.on_decorating_result(event)
        await _await_until(lambda: bool(plugin._human_reply_tasks))
        states = next(iter(plugin._human_reply_tasks.values()))
        state = states[0]
        # 等第一条气泡真正发出去（每条约 0.3s 打字时间）。
        await _await_until(lambda: state.sent_count >= 1)
        self.assertFalse(
            plugin._interrupt_human_reply(event.unified_msg_origin, "10001")
        )
        self.assertFalse(state.interrupted)
        await _await_until(lambda: not plugin._human_reply_tasks, timeout=6.0)
        self.assertEqual(
            self._plain_texts(event.sent), ["第一句话", "第二句话", "第三句话"]
        )
        # 没打断就没有「未送达」，不该记账。
        self.assertNotIn(
            event.unified_msg_origin, plugin._human_reply_interrupt_notes
        )
        await plugin.terminate()

    async def test_inflight_turn_aborted_by_new_message(self):
        # 阶段2（已请求未回复完毕）：新消息把在飞的 LLM 回合直接停掉。
        plugin = self._plugin()
        inflight = FakeEvent(message_str="上一轮", is_trigger=False)
        key = plugin._merge_key(inflight.unified_msg_origin, "10001")
        plugin._turn_inflight[key] = inflight

        # 另一个会话里已停止的登记：不会被再次动作，也不影响别的回合。
        stopped = FakeEvent(
            message_str="已停", is_trigger=False,
            session="aiocqhttp:GroupMessage:20002", group_id="20002",
        )
        stopped.stop_event()
        stopped_key = plugin._merge_key(stopped.unified_msg_origin, "10001")
        plugin._turn_inflight[stopped_key] = stopped

        await plugin.on_message(FakeEvent(message_str="插话"))
        self.assertTrue(inflight.stopped)
        # 中止后登记保留（已是停止态，重复中止是 no-op），
        # 等该轮 on_llm_response 到来时再清除。
        self.assertIs(plugin._turn_inflight.get(key), inflight)
        # 已停止的登记不会被再次动作，也不报错。
        self.assertTrue(stopped.stopped)
        self.assertIn(stopped_key, plugin._turn_inflight)
        await plugin.terminate()

    async def test_on_llm_response_clears_inflight_registration(self):
        plugin = self._plugin()
        event = FakeEvent(
            is_private=True,
            session="aiocqhttp:FriendMessage:10001",
            group_id="",
            sender_id="10001",
            message_id="mid-clear",
        )
        key = plugin._merge_key(event.unified_msg_origin, "10001")
        plugin._turn_inflight[key] = event
        plugin._activity_buffer_enabled = False

        await plugin.on_llm_response(event, SimpleNamespace(
            completion_text="回复", tools_call_name=None
        ))
        self.assertNotIn(key, plugin._turn_inflight)
        await plugin.terminate()

    async def test_on_message_interrupts_active_typing(self):
        plugin = self._plugin()
        event = FakeEvent(message_str="插话")
        state = HumanReplyState()
        plugin._human_reply_tasks[plugin._merge_key(
            event.unified_msg_origin, "10001"
        )] = [state]

        await plugin.on_message(event)
        self.assertTrue(state.interrupted)
        await plugin.terminate()

    async def test_typing_status_for_private_chat(self):
        plugin = self._plugin()
        event = FakeEvent(
            is_private=True,
            session="aiocqhttp:FriendMessage:10001",
            group_id="",
            sender_id="10001",
        )
        call_action = AsyncMock(return_value=None)
        event.bot = SimpleNamespace(api=SimpleNamespace(call_action=call_action))
        event._result = self._llm_result("第一句话来了，第二句话也来。")

        await plugin.on_decorating_result(event)
        await _await_until(lambda: not plugin._human_reply_tasks)
        self.assertEqual(
            self._plain_texts(event.sent), ["第一句话来了", "第二句话也来"]
        )
        calls = [
            dict(c.kwargs or {}) | {"_positional": c.args}
            for c in call_action.await_args_list
        ]
        actions = [c["_positional"][0] if c["_positional"] else None for c in calls]
        self.assertIn("set_input_status", actions)
        event_types = {c.get("event_type") for c in calls}
        self.assertIn(1, event_types)
        self.assertIn(2, event_types)
        await plugin.terminate()

    async def test_typing_status_skipped_for_group_and_failures_are_silent(self):
        plugin = self._plugin()
        event = FakeEvent(message_str="在吗")
        call_action = AsyncMock(side_effect=RuntimeError("炸了"))
        event.bot = SimpleNamespace(api=SimpleNamespace(call_action=call_action))
        event._result = self._llm_result("第一句话来了，第二句话也来。")

        await plugin.on_decorating_result(event)
        await _await_until(lambda: not plugin._human_reply_tasks)
        # 群聊根本不该调 set_input_status。
        call_action.assert_not_awaited()
        self.assertEqual(
            self._plain_texts(event.sent), ["第一句话来了", "第二句话也来"]
        )

        # 私聊调用失败也静默：正文照发。
        private = FakeEvent(
            is_private=True,
            session="aiocqhttp:FriendMessage:10001",
            group_id="",
            sender_id="10001",
        )
        private.bot = SimpleNamespace(
            api=SimpleNamespace(call_action=AsyncMock(side_effect=RuntimeError("x")))
        )
        private._result = self._llm_result("第一句话来了，第二句话也来。")
        await plugin.on_decorating_result(private)
        await _await_until(lambda: not plugin._human_reply_tasks)
        self.assertEqual(
            self._plain_texts(private.sent), ["第一句话来了", "第二句话也来"]
        )
        await plugin.terminate()

    async def test_send_failure_midway_records_partial_note(self):
        plugin = self._plugin()
        event = FakeEvent(
            is_private=True,
            session="aiocqhttp:FriendMessage:10001",
            group_id="",
            sender_id="10001",
        )
        event._result = self._llm_result("第一句话来了，第二句话也来。")
        original_send = event.send

        async def flaky_send(message):
            if len(event.sent) >= 1:
                raise RuntimeError("网络炸了")
            await original_send(message)

        event.send = flaky_send

        await plugin.on_decorating_result(event)
        await _await_until(lambda: not plugin._human_reply_tasks)
        # 第二条气泡发送失败：只发出一条，且必须记账让下一轮知道。
        self.assertEqual(self._plain_texts(event.sent), ["第一句话来了"])
        note = plugin._human_reply_interrupt_notes.get(event.unified_msg_origin)
        self.assertIsNotNone(note)
        self.assertTrue(note["seen_any"])
        self.assertIn("第二句话也来", "\n".join(note["chunks"]))
        await plugin.terminate()

    async def test_concurrent_takeovers_send_in_order(self):
        # AstrNa 并发回复/连续多轮问答会产生同会话多个接管任务：
        # 必须排队逐条说，气泡不许交叉乱序。
        plugin = self._plugin(human_reply_chars_per_minute=600)
        order = []

        def recorder(tag):
            async def _send(message):
                text = "".join(
                    c.text for c in message.chain if isinstance(c, Plain)
                )
                order.append((tag, text))

            return _send

        first = FakeEvent(
            is_private=True,
            session="aiocqhttp:FriendMessage:10001",
            group_id="",
            sender_id="10001",
        )
        first._result = self._llm_result("第一条消息来了，第一条结束。")
        first.send = recorder("A")
        second = FakeEvent(
            is_private=True,
            session="aiocqhttp:FriendMessage:10001",
            group_id="",
            sender_id="10001",
        )
        second._result = self._llm_result("第二条消息到了，第二条结束。")
        second.send = recorder("B")

        await plugin.on_decorating_result(first)
        await plugin.on_decorating_result(second)
        await _await_until(lambda: not plugin._human_reply_tasks, timeout=6.0)
        self.assertEqual(
            order,
            [
                ("A", "第一条消息来了"),
                ("A", "第一条结束"),
                ("B", "第二条消息到了"),
                ("B", "第二条结束"),
            ],
        )
        await plugin.terminate()


class EventSpanCloseTest(unittest.IsolatedAsyncioTestCase):
    """被拦下的事件要补收尾 trace：观察面板按 span 重建会话，不收尾会挂着。"""

    @staticmethod
    def _fake_trace():
        calls = []

        def record(action, **fields):
            calls.append((action, fields))

        return SimpleNamespace(record=record), calls

    async def test_buffered_message_closes_span(self):
        plugin = make_plugin(
            debounce_enabled=True,
            merge_idle_seconds=1,
            merge_max_total_seconds=12,
            force_wake_prefix="小喵",
        )
        trace, calls = self._fake_trace()
        event = FakeEvent(message_str="短句", is_trigger=True)
        event.trace = trace

        await plugin.on_message(event)
        self.assertTrue(event.stopped)
        self.assertTrue(calls)
        action, fields = calls[-1]
        self.assertEqual(action, "astr_agent_complete")
        self.assertIn("防抖合并", fields.get("resp", ""))
        await plugin.terminate()

    async def test_blocked_user_closes_span(self):
        plugin = make_plugin(blocked_users=["10001"])
        trace, calls = self._fake_trace()
        event = FakeEvent(message_str="你好", is_trigger=False)
        event.trace = trace

        await plugin.on_message(event)
        self.assertTrue(event.stopped)
        action, fields = calls[-1]
        self.assertEqual(action, "astr_agent_complete")
        self.assertIn("屏蔽名单", fields.get("resp", ""))
        await plugin.terminate()

    async def test_missing_trace_is_silent(self):
        # 没有 trace 属性的事件（旧版核心/测试桩）不能因此报错。
        plugin = NuoxiaomiaoPlugin.__new__(NuoxiaomiaoPlugin)
        NuoxiaomiaoPlugin._close_event_span(FakeEvent(message_str="x"), "说明")
        self.assertTrue(True)


class GroupScopeTest(unittest.IsolatedAsyncioTestCase):
    """群聊黑白名单：黑名单优先、白名单圈定、群号群名均可、私聊不受影响。"""

    def _plugin(self, **cfg):
        return make_plugin(force_wake_prefix="小喵", **cfg)

    def test_scope_semantics(self):
        plugin = self._plugin()
        allows = plugin._group_scope_allows
        self.assertTrue(allows("1", "", set(), set()))
        self.assertFalse(allows("1", "", set(), {"1"}))
        self.assertFalse(allows("2", "猫群", set(), {"猫群"}))
        self.assertFalse(allows("3", "其它", {"1"}, set()))
        self.assertTrue(allows("1", "猫群", {"1"}, set()))
        self.assertTrue(allows("9", "猫群", {"猫群"}, set()))
        # 黑名单优先于白名单。
        self.assertFalse(allows("1", "猫群", {"猫群"}, {"1"}))

    async def test_private_chat_unaffected(self):
        plugin = self._plugin(human_reply_group_blacklist=["10001"])
        event = FakeEvent(
            is_private=True,
            session="aiocqhttp:FriendMessage:10001",
            group_id="",
            sender_id="10001",
            message_str="在吗",
        )
        self.assertTrue(
            plugin._event_group_allowed(
                event,
                plugin.human_reply_group_whitelist,
                plugin.human_reply_group_blacklist,
            )
        )

    async def test_debounce_respects_group_lists(self):
        # 无名单：@她的短句进缓冲（事件被拦下）。
        control = self._plugin()
        buffered = FakeEvent(message_str="短句")
        await control.on_message(buffered)
        self.assertTrue(buffered.stopped)

        # 黑名单命中：不防抖，直接放行。
        blacklisted = self._plugin(debounce_group_blacklist=["20001"])
        passed = FakeEvent(message_str="短句")
        await blacklisted.on_message(passed)
        self.assertFalse(passed.stopped)

        # 白名单不含本群：同样放行。
        whitelisted = self._plugin(debounce_group_whitelist=["99999"])
        outside = FakeEvent(message_str="短句")
        await whitelisted.on_message(outside)
        self.assertFalse(outside.stopped)

    async def test_human_reply_skips_blacklisted_group(self):
        from astrbot.core.message.message_event_result import (
            MessageEventResult,
            ResultContentType,
        )

        plugin = self._plugin(
            human_reply_enabled=True,
            human_reply_chars_per_minute=10_000_000,
            human_reply_group_blacklist=["20001"],
        )
        event = FakeEvent(message_str="在吗")
        event._result = MessageEventResult(
            chain=[Plain("第一句话来了，第二句话也来。")],
            result_content_type=ResultContentType.LLM_RESULT,
        )

        await plugin.on_decorating_result(event)
        self.assertIsNotNone(event.get_result())
        self.assertEqual(event.sent, [])
        await plugin.terminate()

    async def test_guard_scope_respects_group_lists(self):
        event = FakeEvent(message_str="x", is_trigger=True)
        self.assertFalse(
            self._plugin(guard_group_blacklist=["20001"])._guard_scope_applies(event)
        )
        self.assertTrue(self._plugin()._guard_scope_applies(event))

    async def test_keyword_wake_respects_group_lists(self):
        control = self._plugin(
            keyword_wake_enabled=True, keyword_wake_list=["小喵"]
        )
        woken = FakeEvent(message_str="小喵你看这个", is_trigger=False)
        await control.on_message(woken)
        self.assertTrue(woken.is_at_or_wake_command)

        blacklisted = self._plugin(
            keyword_wake_enabled=True,
            keyword_wake_list=["小喵"],
            keyword_wake_group_blacklist=["20001"],
        )
        ignored = FakeEvent(message_str="小喵你看这个", is_trigger=False)
        await blacklisted.on_message(ignored)
        self.assertFalse(ignored.is_at_or_wake_command)

    async def test_activity_capture_respects_group_lists(self):
        plugin = self._plugin(activity_group_blacklist=["20001"])
        recorded = []
        plugin._record_activity = lambda entry: recorded.append(entry)
        await plugin._capture_chat_activity(FakeEvent(message_str="记一下"))
        self.assertEqual(recorded, [])

        control = self._plugin()
        recorded2 = []
        control._record_activity = lambda entry: recorded2.append(entry)
        await control._capture_chat_activity(FakeEvent(message_str="记一下"))
        self.assertEqual(len(recorded2), 1)

    async def test_poke_skips_blacklisted_group(self):
        plugin = self._plugin(poke_group_blacklist=["20001"])
        event = FakeEvent(message_str="", is_trigger=False)
        event.message_obj.raw_message = {
            "post_type": "notice",
            "notice_type": "notify",
            "sub_type": "poke",
            "user_id": "10001",
            "target_id": "bot",
            "group_id": "20001",
        }

        result = await plugin._handle_poke_event(event)

        self.assertIsNone(result)
        self.assertFalse(event.stopped)


if __name__ == "__main__":
    unittest.main()
