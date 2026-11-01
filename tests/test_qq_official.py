"""QQ 官方机器人（qq_official / qq_official_webhook）平台适配测试。

覆盖两层：
- qq_official.py 的纯函数（平台判定、出站清洗、@ 可合并性、气泡规划、
  @ 目标可投递性、群主动消息预检、正文 @ 标记清理）；
- main.py 里 QQ 官方的适配分支（合成消息透传原始消息对象与入站 msg_id、
  拟人回复气泡压缩 + 纯文本、@ 目标解析、群主动消息门禁、正文清理、新配置项）。

复用 test_plugin_logic.py 的 FakeEvent / FakeContext / make_plugin，只补
QQ 官方平台需要的那点差异（平台名、平台实例的 meta()、raw_message）。
"""

import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from astrbot.core.message.components import At, AtAll, Image, Plain, Reply

import qq_official
from main import ActivityEntry, HumanReplyState, MergeSession
from test_plugin_logic import FakeContext, FakeEvent, FakeProvider, make_plugin

QQ_OPENID = "66DEB259637EEB4BCD616C41F7154987"
OTHER_OPENID = "0F1E2D3C4B5A69788796A5B4C3D2E1F0"
AT_MARKUP_TEXT = f'<qqbot-at-user id="{QQ_OPENID}" /> 你好 <@!123456>'


class QQOfficialFakeEvent(FakeEvent):
    """带 QQ 官方平台标识的 FakeEvent。

    FakeEvent 把 get_platform_name/get_platform_id 写死成 aiocqhttp，
    这里只覆盖这两处，并支持挂上 botpy 的原始消息对象（raw_message）。
    """

    def __init__(
        self,
        *args,
        platform_name="qq_official",
        platform_id="qq_official",
        raw_message=None,
        message_id="",
        **kwargs,
    ):
        if message_id:
            kwargs["message_id"] = message_id
        super().__init__(*args, **kwargs)
        self._platform_name = platform_name
        self._platform_id = platform_id
        if raw_message is not None:
            self.message_obj.raw_message = raw_message

    def get_platform_name(self):
        return self._platform_name

    def get_platform_id(self):
        return self._platform_id


def make_platform_inst(inst_id="qq_official", name="qq_official", session_scene=None):
    """伪造一个平台适配器实例：meta 是方法（与 AstrBot 真实实现一致）。"""
    meta = SimpleNamespace(id=inst_id, name=name, description="test")
    inst = SimpleNamespace(meta=lambda: meta)
    if session_scene is not None:
        inst._session_scene = session_scene
    return inst


def make_qq_official_context(
    inst_id="qq_official", name="qq_official", session_scene=None, provider=None
):
    """FakeContext，但平台实例列表里放一个 QQ 官方（或指定）适配器。"""
    context = FakeContext(provider=provider)
    context.platform_manager = SimpleNamespace(
        platform_insts=[make_platform_inst(inst_id, name, session_scene)]
    )
    return context


def group_resolved(platform_id="qq_official", group_id="G1", name="测试群"):
    """_resolve_at_target / _deliver_proactive_message 用的群目标描述。"""
    return {
        "platform_id": platform_id,
        "kind": "group",
        "id": group_id,
        "name": name,
        "umo": f"{platform_id}:GroupMessage:{group_id}",
        "desc": f"群「{name}」",
    }


class QQOfficialPlatformDetectTest(unittest.TestCase):
    """qq_official.is_qq_official：平台判定。"""

    def test_is_qq_official_platform_name(self):
        """qq_official / qq_official_webhook 为真，aiocqhttp 与其它为假，缺方法不抛异常。"""
        for name in ("qq_official", "qq_official_webhook"):
            with self.subTest(name=name):
                self.assertTrue(
                    qq_official.is_qq_official(
                        SimpleNamespace(get_platform_name=lambda: name)
                    )
                )
        for name in ("aiocqhttp", "telegram", "webchat", "", None):
            with self.subTest(name=name):
                self.assertFalse(
                    qq_official.is_qq_official(
                        SimpleNamespace(get_platform_name=lambda: name)
                    )
                )
        # 没有 get_platform_name 的对象不能把调用方炸掉。
        self.assertFalse(qq_official.is_qq_official(object()))
        self.assertFalse(qq_official.is_qq_official(None))
        self.assertFalse(qq_official.is_qq_official_platform_name(None))
        self.assertTrue(qq_official.is_qq_official_platform_name("qq_official"))


class QQOfficialSanitizeTest(unittest.TestCase):
    """qq_official.sanitize_outbound_components：出站组件清洗。"""

    def test_drops_placeholder_and_numeric_at(self):
        """QQ 官方丢弃占位/数字 At 与 AtAll，保留 openid At、Plain、Image、Reply。"""
        event = QQOfficialFakeEvent()
        kept_at = At(qq=QQ_OPENID)
        components = [
            At(qq="qq_official"),
            At(qq="unknown_selfid"),
            At(qq=""),
            At(qq="123456"),
            AtAll(),
            kept_at,
            Plain("正文"),
            Image(file="https://example.invalid/pic.png"),
            Reply(id="1", sender_id=OTHER_OPENID),
        ]

        cleaned = qq_official.sanitize_outbound_components(components, event)

        self.assertEqual(
            [type(component).__name__ for component in cleaned],
            ["At", "Plain", "Image", "Reply"],
        )
        self.assertIs(cleaned[0], kept_at)
        self.assertEqual(cleaned[0].qq, QQ_OPENID)

    def test_non_qq_official_keeps_components_unchanged(self):
        """非 QQ 官方平台原样返回（保持旧行为）。"""
        event = FakeEvent()
        components = [At(qq="qq_official"), At(qq="123456"), Plain("正文")]

        cleaned = qq_official.sanitize_outbound_components(components, event)

        self.assertEqual(len(cleaned), len(components))
        self.assertIs(cleaned[0], components[0])
        self.assertIs(cleaned[2], components[2])


class QQOfficialMergeableAtTest(unittest.TestCase):
    """qq_official.is_mergeable_at：这条 At 算不算"没有实质内容"。"""

    def test_placeholder_at_is_mergeable_on_qq_official(self):
        """QQ 官方：占位 At 可合并，真实 openid 与非 self 的 At 不可合并。"""
        event = QQOfficialFakeEvent(self_id="qq_official")

        self.assertTrue(qq_official.is_mergeable_at(At(qq="qq_official"), event))
        self.assertTrue(qq_official.is_mergeable_at(At(qq="unknown_selfid"), event))
        self.assertFalse(qq_official.is_mergeable_at(At(qq=QQ_OPENID), event))
        self.assertFalse(qq_official.is_mergeable_at(At(qq="123456"), event))
        self.assertFalse(qq_official.is_mergeable_at(Plain("正文"), event))

    def test_only_self_id_at_is_mergeable_on_other_platforms(self):
        """非 QQ 官方：只有 At.qq == self_id 才算无内容。"""
        event = FakeEvent(self_id="bot")

        self.assertTrue(qq_official.is_mergeable_at(At(qq="bot"), event))
        self.assertFalse(qq_official.is_mergeable_at(At(qq="qq_official"), event))
        self.assertFalse(qq_official.is_mergeable_at(At(qq="123456"), event))


class QQOfficialPlanBubblesTest(unittest.TestCase):
    """qq_official.plan_bubbles：拟人回复分条压缩。"""

    def test_under_limit_is_returned_as_is(self):
        """没超上限时原样返回。"""
        segments = ["一", "二", "三"]
        self.assertEqual(qq_official.plan_bubbles(segments, 4), segments)
        self.assertEqual(qq_official.plan_bubbles(segments, 3), segments)

    def test_over_limit_merges_tail_into_last_bubble(self):
        """超上限时把多出来的段落按顺序合并进最后一条。"""
        merged = qq_official.plan_bubbles(["一", "二", "三", "四"], 2)
        self.assertEqual(merged, ["一", "二\n三\n四"])
        self.assertEqual(
            qq_official.plan_bubbles(["一", "二", "三", "四"], 2, " / "),
            ["一", "二 / 三 / 四"],
        )

    def test_zero_means_unlimited(self):
        """max_bubbles=0 表示不限制。"""
        segments = ["一", "二", "三", "四"]
        self.assertEqual(qq_official.plan_bubbles(segments, 0), segments)
        self.assertEqual(qq_official.plan_bubbles(segments, -1), segments)
        self.assertEqual(qq_official.plan_bubbles([], 2), [])


class QQOfficialAtIdDeliverableTest(unittest.TestCase):
    """qq_official.at_id_deliverable：@ 目标 id 能不能直接发。"""

    def test_qq_official_only_accepts_openid(self):
        """QQ 官方：纯数字为假、openid 为真。"""
        self.assertFalse(qq_official.at_id_deliverable("qq_official", "123456"))
        self.assertFalse(qq_official.at_id_deliverable("qq_official_webhook", "123456"))
        self.assertTrue(qq_official.at_id_deliverable("qq_official", QQ_OPENID))
        self.assertFalse(qq_official.at_id_deliverable("qq_official", ""))
        self.assertFalse(qq_official.at_id_deliverable("qq_official", None))

    def test_other_platforms_accept_numeric_qq(self):
        """其它平台保持旧行为：数字 QQ 号也能 @。"""
        self.assertTrue(qq_official.at_id_deliverable("aiocqhttp", "123456"))
        self.assertTrue(qq_official.at_id_deliverable(None, "123456"))


class QQOfficialTextCleanTest(unittest.TestCase):
    """qq_official.strip_at_markup / clean_platform_text：正文 @ 标记清理。"""

    def test_strip_at_markup_removes_qqbot_markup(self):
        """<qqbot-at-user .../> 与 <@!id> 都要清掉，正常文本保留。"""
        self.assertEqual(qq_official.strip_at_markup(AT_MARKUP_TEXT), "你好")
        self.assertEqual(
            qq_official.strip_at_markup(f'<qqbot-at-user id="{QQ_OPENID}"/>在吗'),
            "在吗",
        )
        self.assertEqual(qq_official.strip_at_markup(""), "")
        self.assertEqual(qq_official.strip_at_markup(None), "")
        self.assertEqual(qq_official.strip_at_markup("普通文本"), "普通文本")

    def test_clean_platform_text_only_for_qq_official(self):
        """QQ 官方清理，非 QQ 官方不清理；开关关掉时也不清理。"""
        qq_event = QQOfficialFakeEvent()
        other_event = FakeEvent()

        self.assertEqual(qq_official.clean_platform_text(qq_event, AT_MARKUP_TEXT), "你好")
        self.assertEqual(
            qq_official.clean_platform_text(other_event, AT_MARKUP_TEXT), AT_MARKUP_TEXT
        )
        self.assertEqual(
            qq_official.clean_platform_text(qq_event, AT_MARKUP_TEXT, enabled=False),
            AT_MARKUP_TEXT,
        )


class QQOfficialGroupSendReadyTest(unittest.TestCase):
    """qq_official.qq_official_group_send_ready：群主动消息预检。"""

    def test_scene_group_is_ready(self):
        """_session_scene 记成 group 时返回 (True, '')。"""
        inst = SimpleNamespace(_session_scene={"G1": "group"})
        self.assertEqual(qq_official.qq_official_group_send_ready(inst, "G1"), (True, ""))

    def test_missing_scene_explains_window_not_ready(self):
        """scene 缺失时返回失败原因（重启后需先收到该群消息）。"""
        ready, reason = qq_official.qq_official_group_send_ready(SimpleNamespace(), "G1")
        self.assertFalse(ready)
        self.assertIn("消息窗口", reason)

        ready, reason = qq_official.qq_official_group_send_ready(
            SimpleNamespace(_session_scene={}), "G1"
        )
        self.assertFalse(ready)
        self.assertIn("消息窗口", reason)

    def test_channel_scene_and_missing_group_are_rejected(self):
        """频道会话与缺群标识都要明确拒绝。"""
        channel = SimpleNamespace(_session_scene={"G1": "channel"})
        ready, reason = qq_official.qq_official_group_send_ready(channel, "G1")
        self.assertFalse(ready)
        self.assertIn("频道", reason)

        for group_id in ("", None, "   "):
            with self.subTest(group_id=group_id):
                ready, reason = qq_official.qq_official_group_send_ready(
                    SimpleNamespace(_session_scene={"G1": "group"}), group_id
                )
                self.assertFalse(ready)
                self.assertIn("群标识", reason)

    def test_malformed_scene_cache_does_not_raise(self):
        """_session_scene 类型不对（没有 .get）也不能抛异常。"""
        ready, _reason = qq_official.qq_official_group_send_ready(
            SimpleNamespace(_session_scene=["G1"]), "G1"
        )
        self.assertFalse(ready)
        ready, _reason = qq_official.qq_official_group_send_ready(None, "G1")
        self.assertFalse(ready)


class QQOfficialMergedEventTest(unittest.IsolatedAsyncioTestCase):
    """main._send_merged_event：QQ 官方合成消息必须透传原始消息对象与 msg_id。"""

    async def test_send_merged_event_passes_raw_message_and_message_id(self):
        """QQ 官方透传 raw_message / message_id；aiocqhttp 平台不传这两个参数。"""
        plugin = make_plugin()
        self.addAsyncCleanup(plugin.terminate)
        created = []
        create_event = AsyncMock()

        async def create_message(**kwargs):
            created.append(kwargs)
            return SimpleNamespace(message_id="synthetic-mid")

        raw_message = object()
        origin = QQOfficialFakeEvent(
            session="qq_official:GroupMessage:G1",
            group_id="G1",
            message_str="你好",
            raw_message=raw_message,
            message_id="ROBOT1.0_x",
        )
        session = MergeSession(
            session_key=origin.unified_msg_origin,
            sender_id="10001",
            parts=["你好"],
            origin_event=origin,
        )

        with (
            patch(
                "astrbot.core.star.star_tools.StarTools.create_message",
                new=create_message,
            ),
            patch(
                "astrbot.core.star.star_tools.StarTools.create_event",
                new=create_event,
            ),
        ):
            await plugin._send_merged_event(session, "你好")

        self.assertEqual(len(created), 1)
        # 少了这两项，QQOfficialMessageEvent.send 会静默丢弃整条回复。
        self.assertIs(created[0]["raw_message"], raw_message)
        self.assertEqual(created[0]["message_id"], "ROBOT1.0_x")
        self.assertIn("你好", created[0]["message_str"])
        self.assertEqual(create_event.await_args.kwargs["platform"], "qq_official")

        # 对照：aiocqhttp 平台不传原始消息对象，message_id 传空串。
        created.clear()
        other = FakeEvent(message_str="你好")
        other_session = MergeSession(
            session_key=other.unified_msg_origin,
            sender_id="10001",
            parts=["你好"],
            origin_event=other,
        )
        with (
            patch(
                "astrbot.core.star.star_tools.StarTools.create_message",
                new=create_message,
            ),
            patch(
                "astrbot.core.star.star_tools.StarTools.create_event",
                new=create_event,
            ),
        ):
            await plugin._send_merged_event(other_session, "你好")

        self.assertEqual(len(created), 1)
        self.assertIsNone(created[0]["raw_message"])
        self.assertEqual(created[0]["message_id"], "")


class QQOfficialHumanReplyTest(unittest.IsolatedAsyncioTestCase):
    """main._run_human_reply：QQ 官方气泡压缩与纯文本模式。"""

    async def test_caps_bubbles_and_uses_plain_text(self):
        """4 段文本压到 2 条气泡，最后一条含合并内容，且消息链不走 markdown。"""
        plugin = make_plugin(
            qq_official_max_bubbles=2,
            qq_official_passive_window_seconds=1,
        )
        self.addAsyncCleanup(plugin.terminate)
        # 打字等待对本用例没有意义，打桩成 0 让用例瞬间跑完。
        plugin._human_reply_delay = lambda segment: 0.0

        event = QQOfficialFakeEvent(
            session="qq_official:GroupMessage:G1",
            group_id="G1",
            message_str="",
        )
        segments = ["第一段", "第二段", "第三段", "第四段"]
        state = HumanReplyState(
            total=len(segments), umo=event.unified_msg_origin, segments=list(segments)
        )

        await plugin._run_human_reply(event, [], list(segments), "qq-key", state)

        self.assertLessEqual(len(event.sent), 2)
        self.assertEqual(len(event.sent), 2)
        self.assertEqual(event.sent[0].get_plain_text(), "第一段")
        last_text = event.sent[-1].get_plain_text()
        self.assertIn("第三段", last_text)
        self.assertIn("第四段", last_text)
        self.assertEqual(state.total, 2)
        self.assertEqual(state.sent_count, 2)
        # qq_official_content_mode=True：每条链都声明纯文本（use_markdown_ 为 False）。
        self.assertTrue(all(chain.use_markdown_ is False for chain in event.sent))


class QQOfficialResolveAtTargetTest(unittest.IsolatedAsyncioTestCase):
    """main._resolve_at_target：QQ 官方只认 openid，纯数字 QQ 号不 @。"""

    async def test_skips_numeric_qq_on_qq_official(self):
        """qq_official 下纯数字返回 ('','')，openid/昵称命中速览库照常返回。"""
        context = make_qq_official_context(inst_id="qq_official")
        plugin = make_plugin(context=context)
        self.addAsyncCleanup(plugin.terminate)
        resolved = group_resolved("qq_official")

        self.assertEqual(plugin._resolve_at_target(resolved, "123456"), ("", ""))

        plugin._record_activity(
            ActivityEntry(
                ts=time.time(),
                sender_id=QQ_OPENID,
                sender_name="群友",
                conversation_id="G1",
                conversation_name="测试群",
                kind="group",
                text="在吗",
                platform_id="qq_official",
            )
        )
        self.assertEqual(
            plugin._resolve_at_target(resolved, "群友"), (QQ_OPENID, "群友")
        )
        # 直接给 openid 也能命中速览库，解析出同一个目标。
        self.assertEqual(
            plugin._resolve_at_target(resolved, QQ_OPENID), (QQ_OPENID, "群友")
        )
        # 库里查不到的非数字串不瞎猜，宁可不 @。
        self.assertEqual(plugin._resolve_at_target(resolved, "查无此人"), ("", ""))

        # 对照：aiocqhttp 下数字 QQ 号照旧返回。
        other_context = make_qq_official_context(inst_id="aiocqhttp", name="aiocqhttp")
        other_plugin = make_plugin(context=other_context)
        self.addAsyncCleanup(other_plugin.terminate)
        self.assertEqual(
            other_plugin._resolve_at_target(group_resolved("aiocqhttp"), "123456"),
            ("123456", ""),
        )


class QQOfficialProactiveGroupGateTest(unittest.IsolatedAsyncioTestCase):
    """main._deliver_proactive_message：QQ 官方群主动消息门禁。"""

    async def test_group_proactive_disabled_is_rejected(self):
        """qq_official_group_proactive=False 时拒绝群目标且不发送。"""
        context = make_qq_official_context(inst_id="qq_official")
        plugin = make_plugin(context=context, qq_official_group_proactive=False)
        self.addAsyncCleanup(plugin.terminate)

        result = await plugin._deliver_proactive_message(
            group_resolved("qq_official"), "打个招呼"
        )

        self.assertIn("error", result)
        self.assertIn("qq_official_group_proactive", result["error"])
        self.assertEqual(context.sent, [])

    async def test_group_without_session_window_is_rejected(self):
        """开启群主动但适配器还没有该群的消息窗口时返回失败原因。"""
        context = make_qq_official_context(inst_id="qq_official")
        plugin = make_plugin(context=context, qq_official_group_proactive=True)
        self.addAsyncCleanup(plugin.terminate)

        result = await plugin._deliver_proactive_message(
            group_resolved("qq_official"), "打个招呼"
        )

        self.assertIn("error", result)
        self.assertIn("暂不可发", result["error"])
        self.assertIn("消息窗口", result["error"])
        self.assertEqual(context.sent, [])

    async def test_group_with_session_window_is_sent_as_plain_text(self):
        """适配器已有该群窗口时正常投递，并声明纯文本。"""
        context = make_qq_official_context(
            inst_id="qq_official",
            session_scene={"G1": "group"},
            provider=FakeProvider(text="群里说一句喵"),
        )
        plugin = make_plugin(context=context, qq_official_group_proactive=True)
        self.addAsyncCleanup(plugin.terminate)

        result = await plugin._deliver_proactive_message(
            group_resolved("qq_official"), "打个招呼"
        )

        self.assertEqual(result.get("status"), "sent")
        self.assertEqual(len(context.sent), 1)
        umo, chain = context.sent[0]
        self.assertEqual(umo, "qq_official:GroupMessage:G1")
        self.assertEqual(chain.get_plain_text(), "群里说一句喵")
        self.assertIs(chain.use_markdown_, False)


class QQOfficialExtractTextTest(unittest.IsolatedAsyncioTestCase):
    """main._extract_text：正文 @ 标记按平台清理。"""

    async def test_strips_at_markup_only_for_qq_official(self):
        """QQ 官方清理 @ 标记，aiocqhttp 不清理，开关关掉时也不清理。"""
        plugin = make_plugin()
        self.addAsyncCleanup(plugin.terminate)

        qq_event = QQOfficialFakeEvent(message_str=AT_MARKUP_TEXT)
        self.assertEqual(plugin._extract_text(qq_event), "你好")

        other_event = FakeEvent(message_str=AT_MARKUP_TEXT)
        self.assertEqual(plugin._extract_text(other_event), AT_MARKUP_TEXT)

        disabled = make_plugin(qq_official_strip_at_markup=False)
        self.addAsyncCleanup(disabled.terminate)
        self.assertEqual(disabled._extract_text(qq_event), AT_MARKUP_TEXT)


class QQOfficialConfigTest(unittest.IsolatedAsyncioTestCase):
    """新配置项落地：_apply_config 解析 qq_official_* 与默认值。"""

    async def test_defaults(self):
        """默认值：最多 4 条、窗口 240 秒、群主动关闭、清理 @ 标记开启。"""
        plugin = make_plugin()
        self.addAsyncCleanup(plugin.terminate)

        self.assertTrue(plugin.qq_official_enabled)
        self.assertTrue(plugin.qq_official_content_mode)
        self.assertEqual(plugin.qq_official_max_bubbles, 4)
        self.assertEqual(plugin.qq_official_passive_window_seconds, 240)
        self.assertFalse(plugin.qq_official_group_proactive)
        self.assertTrue(plugin.qq_official_strip_at_markup)

    async def test_overrides_and_clamping(self):
        """显式配置生效，越界值按运行期护栏收敛。"""
        plugin = make_plugin(
            qq_official_enabled=False,
            qq_official_content_mode=False,
            qq_official_max_bubbles=2,
            qq_official_passive_window_seconds=30,
            qq_official_group_proactive=True,
            qq_official_strip_at_markup=False,
        )
        self.addAsyncCleanup(plugin.terminate)

        self.assertFalse(plugin.qq_official_enabled)
        self.assertFalse(plugin.qq_official_content_mode)
        self.assertEqual(plugin.qq_official_max_bubbles, 2)
        self.assertEqual(plugin.qq_official_passive_window_seconds, 30)
        self.assertTrue(plugin.qq_official_group_proactive)
        self.assertFalse(plugin.qq_official_strip_at_markup)

        clamped = make_plugin(
            qq_official_max_bubbles=-3,
            qq_official_passive_window_seconds=0,
        )
        self.addAsyncCleanup(clamped.terminate)
        self.assertEqual(clamped.qq_official_max_bubbles, 0)
        self.assertEqual(clamped.qq_official_passive_window_seconds, 1)


if __name__ == "__main__":
    unittest.main()
