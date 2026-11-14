"""整轮发送排队（send_order.py）与 AstrNa 群聊并发回复兼容的回归测试。"""

import asyncio
import contextvars
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

from astrbot.core.message.components import Plain
from astrbot.core.message.message_event_result import MessageChain, MessageEventResult, ResultContentType

from main import SEND_OBSERVER_EXTRA
from send_order import SendOrderGate
from tests.test_plugin_logic import FakeContext, FakeEvent, make_plugin

# 生产代码按「AstrNa 主模块的模块命名空间」取轮次 ContextVar：
# 测试里让假的 AstrNa 插件类也定义在本模块，于是这里就是那个命名空间。
_CURRENT_SEND_ROUND = contextvars.ContextVar("test_astrna_send_round", default=None)

# 已安装 AstrNa 时额外跑一组「真对象」集成测试（未安装则跳过）。
PLUGIN_ROOT = Path(__file__).resolve().parents[1]
try:
    sys.path.insert(0, str(PLUGIN_ROOT.parent))
    from astrbot_plugin_AstrNa.astrna.modules import (
        group_sender_concurrency as _astrna_gsc,
    )

    _ASTRNA_IMPORT_OK = True
except Exception:  # noqa: BLE001 - 外部插件缺失不影响默认测试
    _astrna_gsc = None
    _ASTRNA_IMPORT_OK = False

_ASTRNA_SEND_PATCH_ATTR = "_astrna_group_sender_concurrency_send_patch"


class FakeSendRound:
    """AstrNa SendRound 的最小替身：惰性、幂等地占住群发送锁。"""

    def __init__(self, umo, group_lock):
        self.umo = umo
        self.group_lock = group_lock
        self.acquired = False
        self.closed = False
        self.acquire_attempts = 0

    async def ensure_acquired(self):
        self.acquire_attempts += 1
        if self.closed:
            return False
        if not self.acquired:
            await self.group_lock.acquire()
            self.acquired = True
        return True

    def close_now(self):
        if self.closed:
            return
        self.closed = True
        if self.acquired:
            self.group_lock.release()
            self.acquired = False


class FakeConcurrencyModule:
    """AstrNa GroupSenderConcurrencyModule 的最小替身。"""

    _installed = True

    def __init__(self):
        self.locks = {}

    def get_group_send_lock(self, umo):
        lock = self.locks.get(umo)
        if lock is None:
            lock = asyncio.Lock()
            self.locks[umo] = lock
        return lock


class FakeAstrNa:
    """AstrNa 插件实例的替身：上下文里按插件名注册。"""

    def __init__(self):
        self.concurrency = FakeConcurrencyModule()
        self.runtime = SimpleNamespace(group_sender_concurrency=self.concurrency)


class FakeStarContext:
    """只够 send_order 探测用的最小上下文（单元测试用）。"""

    def __init__(self, instance):
        self.instance = instance

    def get_registered_star(self, name):
        if name != "astrbot_plugin_AstrNa" or self.instance is None:
            return None
        return SimpleNamespace(star_cls=self.instance)


class FakeAstrNaContext(FakeContext):
    """插件级测试用：在完整 FakeContext 上挂一个假的 AstrNa 插件实例。"""

    def __init__(self, instance, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.astrna = instance

    def get_registered_star(self, name):
        if name != "astrbot_plugin_AstrNa" or self.astrna is None:
            return None
        return SimpleNamespace(star_cls=self.astrna)


class FakePlugin:
    def __init__(self, context):
        self.context = context
        self.logger = SimpleNamespace(
            warning=lambda *a, **k: None,
            info=lambda *a, **k: None,
            debug=lambda *a, **k: None,
        )


async def _wait_until(predicate, timeout=2.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.005)
    return predicate()


class SendOrderGateTest(unittest.IsolatedAsyncioTestCase):
    """AstrNa 后端发现、提前占轮、裸取锁与降级锁。"""

    def _gate(self, astrna=None):
        return SendOrderGate(FakePlugin(FakeStarContext(astrna)))

    async def test_missing_plugin_falls_back_to_local(self):
        gate = self._gate(None)
        self.assertFalse(gate.is_astrna_backend())
        self.assertEqual(gate.status()["backend"], "local")

    async def test_uninstalled_module_falls_back_to_local(self):
        astrna = FakeAstrNa()
        astrna.concurrency._installed = False
        gate = self._gate(astrna)
        self.assertFalse(gate.is_astrna_backend())
        self.assertIn("未开启", gate.describe())

    async def test_round_hold_preacquires_group_lock_and_keeps_it(self):
        astrna = FakeAstrNa()
        gate = self._gate(astrna)
        self.assertTrue(gate.is_astrna_backend())
        umo = "aiocqhttp:GroupMessage:G1"
        group_lock = astrna.concurrency.get_group_send_lock(umo)
        round_obj = FakeSendRound(umo, group_lock)
        token = _CURRENT_SEND_ROUND.set(round_obj)
        try:
            async with gate.hold(umo) as acquired:
                self.assertTrue(acquired)
                # 打字前就拿到整轮锁，别的轮次无法插队。
                self.assertTrue(group_lock.locked())
                self.assertEqual(round_obj.acquire_attempts, 1)
            # 退出后不释放：轮末由 AstrNa 统一释放。
            self.assertTrue(group_lock.locked())
            round_obj.close_now()
            self.assertFalse(group_lock.locked())
        finally:
            _CURRENT_SEND_ROUND.reset(token)

    async def test_round_hold_is_idempotent_and_does_not_double_acquire(self):
        astrna = FakeAstrNa()
        gate = self._gate(astrna)
        umo = "aiocqhttp:GroupMessage:G1"
        group_lock = astrna.concurrency.get_group_send_lock(umo)
        round_obj = FakeSendRound(umo, group_lock)
        token = _CURRENT_SEND_ROUND.set(round_obj)
        try:
            async with gate.hold(umo) as first:
                async with gate.hold(umo) as second:
                    self.assertTrue(first)
                    self.assertTrue(second)
            self.assertTrue(group_lock.locked())
            round_obj.close_now()
        finally:
            _CURRENT_SEND_ROUND.reset(token)

    async def test_hold_takes_group_lock_when_no_round_context(self):
        astrna = FakeAstrNa()
        gate = self._gate(astrna)
        umo = "aiocqhttp:GroupMessage:G1"
        group_lock = astrna.concurrency.get_group_send_lock(umo)
        async with gate.hold(umo) as acquired:
            self.assertTrue(acquired)
            self.assertTrue(group_lock.locked())
        self.assertFalse(group_lock.locked())

    async def test_hold_skips_guarded_send_to_avoid_reentrant_deadlock(self):
        astrna = FakeAstrNa()
        gate = self._gate(astrna)
        umo = "aiocqhttp:GroupMessage:G1"
        group_lock = astrna.concurrency.get_group_send_lock(umo)

        async def guarded_send(message):
            return None

        setattr(guarded_send, _ASTRNA_SEND_PATCH_ATTR, True)
        event = SimpleNamespace(send=guarded_send, get_extra=lambda key, default=None: None)
        async with gate.hold(umo, event=event) as acquired:
            # 空操作：锁会在 guard 首次发送时获取，这里裸取会重入死锁。
            self.assertFalse(acquired)
            self.assertFalse(group_lock.locked())

    async def test_one_shot_skips_inside_open_round(self):
        astrna = FakeAstrNa()
        gate = self._gate(astrna)
        umo = "aiocqhttp:GroupMessage:G1"
        group_lock = astrna.concurrency.get_group_send_lock(umo)
        round_obj = FakeSendRound(umo, group_lock)
        token = _CURRENT_SEND_ROUND.set(round_obj)
        try:
            async with gate.one_shot(umo) as acquired:
                self.assertFalse(acquired)
                self.assertFalse(group_lock.locked())
        finally:
            _CURRENT_SEND_ROUND.reset(token)

    async def test_one_shot_takes_group_lock_outside_round(self):
        astrna = FakeAstrNa()
        gate = self._gate(astrna)
        umo = "aiocqhttp:GroupMessage:G1"
        group_lock = astrna.concurrency.get_group_send_lock(umo)
        round_obj = FakeSendRound(umo, group_lock)
        round_obj.closed = True  # 已关闭的轮次不再算「在轮内」
        token = _CURRENT_SEND_ROUND.set(round_obj)
        try:
            async with gate.one_shot(umo) as acquired:
                self.assertTrue(acquired)
                self.assertTrue(group_lock.locked())
            self.assertFalse(group_lock.locked())
        finally:
            _CURRENT_SEND_ROUND.reset(token)

    async def test_local_hold_is_task_reentrant(self):
        gate = self._gate(None)
        umo = "aiocqhttp:GroupMessage:G1"
        async with gate.hold(umo) as first:
            async with gate.hold(umo) as second:
                # 同一 task 重入：不能自己把自己锁死。
                self.assertTrue(first)
                self.assertTrue(second)

    async def test_local_one_shot_waits_for_holder(self):
        gate = self._gate(None)
        umo = "aiocqhttp:GroupMessage:G1"
        order = []
        release = asyncio.Event()

        async def holder():
            async with gate.hold(umo):
                order.append("hold-in")
                await release.wait()
                order.append("hold-out")

        async def other():
            await asyncio.sleep(0.01)
            async with gate.one_shot(umo):
                order.append("other")

        holder_task = asyncio.create_task(holder())
        other_task = asyncio.create_task(other())
        await _wait_until(lambda: "hold-in" in order)
        await asyncio.sleep(0.03)
        self.assertEqual(order, ["hold-in"])  # 旁路发送在队列里等
        release.set()
        await asyncio.wait_for(asyncio.gather(holder_task, other_task), timeout=2.0)
        self.assertEqual(order, ["hold-in", "hold-out", "other"])

    async def test_local_hold_timeout_releases_anyway(self):
        gate = self._gate(None)
        umo = "aiocqhttp:GroupMessage:G1"
        holder_ready = asyncio.Event()
        release = asyncio.Event()

        async def holder():
            async with gate.hold(umo):
                holder_ready.set()
                await release.wait()

        async def waiter():
            await holder_ready.wait()
            started = asyncio.get_running_loop().time()
            async with gate.hold(umo, timeout=0.05) as acquired:
                return acquired, asyncio.get_running_loop().time() - started

        holder_task = asyncio.create_task(holder())
        acquired, elapsed = await asyncio.wait_for(waiter(), timeout=2.0)
        self.assertFalse(acquired)  # 超时放行，绝不永久堵死
        self.assertGreaterEqual(elapsed, 0.04)
        release.set()
        await asyncio.wait_for(holder_task, timeout=2.0)

    async def test_prune_drops_idle_local_locks_only(self):
        gate = self._gate(None)
        umo = "aiocqhttp:GroupMessage:G1"
        async with gate.hold(umo):
            pass
        gate.prune(asyncio.get_running_loop().time() + 10_000, max_idle=1.0)
        self.assertFalse(gate._local)
        await gate.close()

    async def test_acquire_round_holds_group_lock_without_round_context(self):
        """AstrNa 生效但没有轮次的事件（命令/定时）：群锁当整轮租约占住。"""
        astrna = FakeAstrNa()
        gate = self._gate(astrna)
        umo = "aiocqhttp:GroupMessage:G1"
        group_lock = astrna.concurrency.get_group_send_lock(umo)
        order = []

        async def plain_send(message):
            return None

        event = SimpleNamespace(
            send=plain_send, get_extra=lambda key, default=None: None
        )
        lease = await gate.acquire_round(umo, event=event)
        self.assertIsNotNone(lease)
        self.assertTrue(group_lock.locked())

        async def waiter():
            async with gate.hold(umo):
                order.append("second")

        task = asyncio.create_task(waiter())
        await asyncio.sleep(0.03)
        self.assertEqual(order, [])  # 整轮租约没放，别人进不来
        gate.release_round(lease)
        await asyncio.wait_for(task, timeout=2.0)
        self.assertEqual(order, ["second"])
        self.assertFalse(group_lock.locked())

    async def test_acquire_round_skips_guarded_send(self):
        """已由 AstrNa guard 保护的发送链不能裸取群锁（同 task 重入死锁）。"""
        astrna = FakeAstrNa()
        gate = self._gate(astrna)
        umo = "aiocqhttp:GroupMessage:G1"
        group_lock = astrna.concurrency.get_group_send_lock(umo)

        async def guarded_send(message):
            return None

        setattr(guarded_send, _ASTRNA_SEND_PATCH_ATTR, True)
        event = SimpleNamespace(
            send=guarded_send, get_extra=lambda key, default=None: None
        )
        self.assertIsNone(await gate.acquire_round(umo, event=event))
        self.assertFalse(group_lock.locked())

    async def test_acquire_round_is_noop_inside_round(self):
        """AstrNa 轮内由 hold 幂等提前占轮，轮末由 AstrNa 释放，不重复占。"""
        astrna = FakeAstrNa()
        gate = self._gate(astrna)
        umo = "aiocqhttp:GroupMessage:G1"
        group_lock = astrna.concurrency.get_group_send_lock(umo)
        round_obj = FakeSendRound(umo, group_lock)
        token = _CURRENT_SEND_ROUND.set(round_obj)
        try:
            self.assertIsNone(await gate.acquire_round(umo))
            self.assertFalse(group_lock.locked())
        finally:
            _CURRENT_SEND_ROUND.reset(token)
            round_obj.close_now()

    async def test_one_shot_cross_session_uses_target_group_lock(self):
        """send_message 工具跨会话投递：只有同 umo 轮内才让路。"""
        astrna = FakeAstrNa()
        gate = self._gate(astrna)
        umo_a = "aiocqhttp:GroupMessage:G1"
        umo_b = "aiocqhttp:GroupMessage:G2"
        lock_a = astrna.concurrency.get_group_send_lock(umo_a)
        lock_b = astrna.concurrency.get_group_send_lock(umo_b)
        round_obj = FakeSendRound(umo_a, lock_a)
        token = _CURRENT_SEND_ROUND.set(round_obj)
        try:
            async with gate.one_shot(umo_b) as acquired:
                self.assertTrue(acquired)
                self.assertTrue(lock_b.locked())
            self.assertFalse(lock_b.locked())
            async with gate.one_shot(umo_a) as skipped:
                self.assertFalse(skipped)
        finally:
            _CURRENT_SEND_ROUND.reset(token)
            round_obj.close_now()


def _llm_result(*texts):
    return MessageEventResult(
        chain=[Plain(text) for text in texts],
        result_content_type=ResultContentType.LLM_RESULT,
    )


class SendOrderWithPluginTest(unittest.IsolatedAsyncioTestCase):
    """插件级：同群不同群友的两轮拟人回复必须整轮连续发送。"""

    def _plugin(self, gate_context):
        plugin = make_plugin(
            context=gate_context,
            debounce_enabled=False,
        )
        self.addAsyncCleanup(plugin.terminate)
        return plugin

    async def test_two_senders_do_not_interleave(self):
        astrna = FakeAstrNa()
        plugin = self._plugin(FakeAstrNaContext(astrna))
        umo = "aiocqhttp:GroupMessage:G1"
        group_lock = astrna.concurrency.get_group_send_lock(umo)
        log = []

        def make_event(sender_id, *texts):
            event = FakeEvent(
                sender_id=sender_id,
                session=umo,
                group_id="G1",
                message_str="在吗",
            )
            event._result = _llm_result(*texts)

            async def recorder(message):
                body = "".join(
                    comp.text for comp in message.chain if isinstance(comp, Plain)
                )
                log.append((sender_id, body))

            event.send = recorder
            return event

        first = make_event("10001", "第一句话来了，第二句话也来。")
        second = make_event("10002", "第三句话来了，第四句话也来。")

        async def run(event):
            # 模拟 AstrNa：每个群友一个并发轮次，整轮共享同一把群发送锁。
            round_obj = FakeSendRound(umo, group_lock)
            token = _CURRENT_SEND_ROUND.set(round_obj)
            try:
                await plugin.observe_outgoing_result(event)
                segments = [Plain(text) for text in event._result.chain[0].text.split("，")]
                for seg in segments:
                    await event.send(MessageChain([seg]))
                    await asyncio.sleep(0.02)
                await plugin.after_reply_sent(event)
            finally:
                round_obj.close_now()
                _CURRENT_SEND_ROUND.reset(token)

        await asyncio.wait_for(
            asyncio.gather(run(first), run(second)), timeout=10.0
        )

        senders = [sender for sender, _ in log]
        self.assertEqual(len(log), 4)
        # 只允许 A,A,B,B 或 B,B,A,A；A,B,A,B 就是这次的 bug。
        self.assertEqual(
            senders, [senders[0]] * 2 + [senders[2]] * 2, f"气泡穿插了: {log}"
        )

    async def test_local_mode_observer_send_is_reentrant_and_never_deadlocks(self):
        # 本地降级模式：event.send 的观察器也会按群排队。任务已经
        # 持有同一把锁，发送时必须重入而不是自己等自己。
        plugin = self._plugin(FakeAstrNaContext(None))
        event = FakeEvent(message_str="在吗")
        event._result = _llm_result("第一句话来了，第二句话也来。")
        sent = []

        async def recorder(message):
            sent.append(message)

        event.send = recorder
        await plugin.observe_outgoing_result(event)
        segments = [Plain(text) for text in event._result.chain[0].text.split("，")]
        for seg in segments:
            await event.send(MessageChain([seg]))
        await plugin.after_reply_sent(event)
        self.assertEqual(len(sent), 2)

    async def test_round_lease_releases_on_after_reply_sent(self):
        plugin = self._plugin(FakeAstrNaContext(None))
        event = FakeEvent(message_str="在吗")
        event._result = _llm_result("第一句话来了，第二句话也来。")

        await plugin.observe_outgoing_result(event)
        observed = event.get_extra(SEND_OBSERVER_EXTRA)
        self.assertIsInstance(observed, dict)
        segments = [Plain(text) for text in event._result.chain[0].text.split("，")]
        for seg in segments:
            await event.send(MessageChain([seg]))

        # 首条气泡就占住整轮本地锁，后续气泡不再重复加锁。
        self.assertIsNotNone(observed.get("round_lease"))
        self.assertIsNone(event.get_extra("round_lease"))
        await plugin.after_reply_sent(event)
        self.assertEqual(len(event.sent), 2)
        self.assertIsNone(observed.get("round_lease"))

    async def test_roundless_astrna_events_do_not_interleave(self):
        """AstrNa 生效但没有轮次的事件，也要整轮连续发送（SplitStep 分段不穿插）。"""
        astrna = FakeAstrNa()
        plugin = self._plugin(FakeAstrNaContext(astrna))
        umo = "aiocqhttp:GroupMessage:G1"
        log = []

        def make_event(sender_id, *texts):
            event = FakeEvent(
                sender_id=sender_id,
                session=umo,
                group_id="G1",
                message_str="在吗",
            )
            event._result = _llm_result(*texts)

            async def recorder(message):
                body = "".join(
                    comp.text for comp in message.chain if isinstance(comp, Plain)
                )
                log.append((sender_id, body))

            event.send = recorder
            return event

        first = make_event("10001", "第一句话来了，第二句话也来。")
        second = make_event("10002", "第三句话来了，第四句话也来。")

        async def run(event):
            # 不设置轮次上下文：模拟命令/定时类事件（AstrNa 无 SendRound）。
            await plugin.observe_outgoing_result(event)
            segments = [Plain(text) for text in event._result.chain[0].text.split("，")]
            for seg in segments:
                await event.send(MessageChain([seg]))
                await asyncio.sleep(0.02)
            await plugin.after_reply_sent(event)

        await asyncio.wait_for(
            asyncio.gather(run(first), run(second)), timeout=10.0
        )

        senders = [sender for sender, _ in log]
        self.assertEqual(len(log), 4)
        self.assertEqual(
            senders, [senders[0]] * 2 + [senders[2]] * 2, f"气泡穿插了: {log}"
        )

    async def test_out_of_band_send_waits_for_observer(self):
        plugin = self._plugin(FakeAstrNaContext(None))
        umo = "aiocqhttp:GroupMessage:20001"
        order = []

        event = FakeEvent(message_str="在吗", session=umo, group_id="20001")
        event._result = _llm_result("第一句话来了，第二句话也来。")

        async def recorder(message):
            order.append("reply")

        event.send = recorder
        await plugin.observe_outgoing_result(event)

        async def out_of_band():
            async with plugin._send_order.one_shot(umo):
                order.append("proactive")

        # First segment acquires the round lease; out-of-band send must wait
        # until after_reply_sent releases the round lease.
        await event.send(MessageChain([Plain("第一句话来了")]))
        proactive = asyncio.create_task(out_of_band())
        await asyncio.sleep(0.05)
        self.assertEqual(order, ["reply"])

        # Second segment and round completion release the lease.
        await event.send(MessageChain([Plain("第二句话也来")]))
        await plugin.after_reply_sent(event)
        await asyncio.wait_for(proactive, timeout=2.0)
        self.assertEqual(order, ["reply", "reply", "proactive"])


@unittest.skipUnless(_ASTRNA_IMPORT_OK, "需要已安装的 AstrNa 才能运行真对象集成测试")
class RealAstrNaGateTest(unittest.IsolatedAsyncioTestCase):
    """直接用 AstrNa 真实的 SendRound / 群发送锁验证提前占轮与互斥。"""

    UMO = "aiocqhttp:GroupMessage:REAL-G1"

    def setUp(self):
        # 让探测走「第二个候选模块」（AstrNa 并发模块）里的真轮次 ContextVar：
        # 假插件实例的类故意放在一个不存在的模块名下。
        self.real_owner = object.__new__(_astrna_gsc.GroupSenderConcurrencyModule)
        self.real_owner._installed = True
        self.addCleanup(setattr, self.real_owner, "_installed", False)
        instance_cls = type(
            "FakeAstrNaPlugin",
            (),
            {"__module__": "fake_astrna_plugin_main_not_importable"},
        )
        self.instance = instance_cls()
        self.instance.runtime = SimpleNamespace(
            group_sender_concurrency=self.real_owner
        )
        self.gate = SendOrderGate(FakePlugin(FakeStarContext(self.instance)))

    def _round(self):
        lock = self.real_owner.get_group_send_lock(self.UMO)
        return _astrna_gsc.SendRound(umo=self.UMO, group_lock=lock), lock

    async def test_real_backend_detected(self):
        self.assertTrue(self.gate.is_astrna_backend())
        self.assertEqual(self.gate.status()["backend"], "astrna")

    async def test_real_round_preacquired_before_typing(self):
        round_obj, lock = self._round()
        token = _astrna_gsc._CURRENT_SEND_ROUND.set(round_obj)
        try:
            async with self.gate.hold(self.UMO) as acquired:
                self.assertTrue(acquired)
                self.assertTrue(lock.locked())
                self.assertTrue(round_obj.acquired)
            # 轮末由 AstrNa 释放，不是我们释放。
            self.assertTrue(lock.locked())
            round_obj.close_now()
            self.assertFalse(lock.locked())
        finally:
            _astrna_gsc._CURRENT_SEND_ROUND.reset(token)

    async def test_other_round_waits_until_first_round_closes(self):
        first_round, lock = self._round()
        order = []
        token = _astrna_gsc._CURRENT_SEND_ROUND.set(first_round)
        try:
            async with self.gate.hold(self.UMO):
                order.append("first-in")

                async def other_round():
                    second_round = _astrna_gsc.SendRound(
                        umo=self.UMO, group_lock=lock
                    )
                    second_token = _astrna_gsc._CURRENT_SEND_ROUND.set(
                        second_round
                    )
                    try:
                        async with self.gate.hold(self.UMO):
                            order.append("second-in")
                    finally:
                        _astrna_gsc._CURRENT_SEND_ROUND.reset(second_token)
                        second_round.close_now()

                waiter = asyncio.create_task(other_round())
                await asyncio.sleep(0.05)
                self.assertEqual(order, ["first-in"])  # 第二轮还在排队
                first_round.close_now()
                await asyncio.wait_for(waiter, timeout=2.0)
                self.assertEqual(order, ["first-in", "second-in"])
        finally:
            _astrna_gsc._CURRENT_SEND_ROUND.reset(token)

    async def test_one_shot_outside_round_uses_real_group_lock(self):
        _, lock = self._round()
        async with self.gate.one_shot(self.UMO) as acquired:
            self.assertTrue(acquired)
            self.assertTrue(lock.locked())
        self.assertFalse(lock.locked())

    async def test_two_human_replies_with_real_guards_do_not_interleave(self):
        """复现 13:04 现场：同群两个群友并发，AstrNa 真实 guard + 真实群锁。"""

        class _RealContext(FakeContext):
            def __init__(self, owner):
                super().__init__()
                self.owner = owner

            def get_registered_star(self, name):
                if name != "astrbot_plugin_AstrNa":
                    return None
                return SimpleNamespace(star_cls=self.instance)

        _RealContext.instance = self.instance
        plugin = make_plugin(
            context=_RealContext(self.real_owner),
            debounce_enabled=False,
        )
        self.addAsyncCleanup(plugin.terminate)
        self.assertTrue(plugin._send_order.is_astrna_backend())

        log = []
        lock = self.real_owner.get_group_send_lock(self.UMO)

        def make_event(sender_id, text):
            event = FakeEvent(
                sender_id=sender_id, session=self.UMO, group_id="REAL-G1",
                message_str="在吗",
            )
            event._result = _llm_result(text)

            async def recorder(message):
                body = "".join(
                    comp.text for comp in message.chain if isinstance(comp, Plain)
                )
                log.append((sender_id, body))

            event.send = recorder
            return event

        first = make_event("A", "第一句话来了，第二句话也来。")
        second = make_event("B", "第三句话来了，第四句话也来。")

        async def run(event, round_obj, restore):
            token = _astrna_gsc._CURRENT_SEND_ROUND.set(round_obj)
            try:
                await plugin.observe_outgoing_result(event)
                segments = [Plain(text) for text in event._result.chain[0].text.split("，")]
                for seg in segments:
                    await event.send(MessageChain([seg]))
                    await asyncio.sleep(0.02)
                await plugin.after_reply_sent(event)
            finally:
                _astrna_gsc._CURRENT_SEND_ROUND.reset(token)
                round_obj.close_now()
                restore()

        first_round = _astrna_gsc.SendRound(umo=self.UMO, group_lock=lock)
        second_round = _astrna_gsc.SendRound(umo=self.UMO, group_lock=lock)
        restore_first = self.real_owner.install_event_send_guards(
            first, first_round
        )
        restore_second = self.real_owner.install_event_send_guards(
            second, second_round
        )

        await asyncio.wait_for(
            asyncio.gather(
                run(first, first_round, restore_first),
                run(second, second_round, restore_second),
            ),
            timeout=10.0,
        )

        senders = [sender for sender, _ in log]
        self.assertEqual(len(log), 4, log)
        self.assertEqual(
            senders, [senders[0]] * 2 + [senders[2]] * 2, f"气泡穿插了: {log}"
        )


if __name__ == "__main__":
    unittest.main()
