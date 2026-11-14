"""整轮发送排队：让本插件的出站消息不穿插 AstrNa 的群聊并发回复。

背景（AstrNa 1.7.3 / AstrBot 4.29.3）
------------------------------------
AstrNa 的「解锁群聊并发回复」把群聊 LLM 排队粒度改成按发送者拆分，同时用一把
`SendRound.group_lock` 保证「同一个群的实际发送按整轮排队」：谁先产生第一条
需要发送的消息，谁先获得发送权，连续发完本轮工具状态、分段回复、媒体等，下一轮
才能开始发送（AstrNa `astrna/modules/group_sender_concurrency.py` 的
`SendRound` / `send_with_round`）。

那把锁的生命周期挂在 `InternalAgentSubStage.process` 这个 async generator 上：
- 第一次 `event.send` / `event.send_streaming` 时惰性获取；
- generator 结束（即下游 ResultDecorate / Respond 全部返回）时 `close_now()` 释放；
- 轮次关闭后再次发送会直接放行（不加锁）——这正是「拟人回复把发送放进后台任务、
  pipeline 先结束」时气泡交叉乱序的根因。

本模块只做两件事
----------------
1. `hold`：接管发送权。若当前 task 处于 AstrNa 本轮（同一 umo、未关闭），调用
   `SendRound.ensure_acquired()`（幂等）把群锁提前拿到手，确保打字等待期间不会
   被别的轮次插队；若没有轮次上下文，则用 AstrNa 的 `get_group_send_lock(umo)`
   或插件自己的本地锁。
2. `one_shot`：旁路发送（主动消息、合并失败通知、刷屏表情）在没有轮次上下文时
   也按同一把锁排队。

单一锁宇宙（重要）
------------------
绝不「持 AstrNa 群锁 → 等插件本地锁」或反过来，否则会形成死锁环：
- 轮次 B 在本轮内用工具发过消息（`context.send_message` 会加入本轮并持群锁），
  之后如果去等插件会话锁；而拟人回复 A 持插件会话锁、等群锁 —— 互等。
因此：AstrNa 生效时只用 AstrNa 的群锁；AstrNa 未安装/未开启时才用本地锁。

同理，绝不在「已经由 AstrNa guard 保护、且可能持有群锁」的 task 里裸取群锁
（`asyncio.Lock` 不可重入）。这种情形下本模块直接空操作，交给 guard。

所有等待都有上限：超时记一条限频告警后**无锁放行**，宁可退化为修复前的行为，
也不把整个会话永久堵死。
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
import time
from typing import Any

#: 拟人回复等待发送权的上限（秒）。打字本身已受 QQ 官方被动窗口约束，
#: 这里给足余量，避免慢轮次把同群的其它回复排到窗口之外。
HOLD_WAIT_SECONDS = 180.0

#: 旁路发送（主动消息等）等待发送权的上限（秒）；超时放行，绝不堵住主动流程。
ONE_SHOT_WAIT_SECONDS = 30.0

#: 普通回复（非拟人接管）单次发送等待发送权的上限（秒）：整轮气泡最多几十秒，
#: 超过就放行，避免一个卡住的轮次把后面的回复全部堵死。
OBSERVER_WAIT_SECONDS = 60.0

#: 本地降级锁的空闲回收阈值（秒）。
LOCAL_LOCK_IDLE_SECONDS = 3600.0

#: 探测失败后的重试间隔（秒）：AstrNa 可能在本插件之后加载或热重载，
#: 不能因为启动时一次探测失败就永远降级。
RESOLVE_RETRY_SECONDS = 30.0

#: 告警限频间隔（秒）。
WARN_INTERVAL_SECONDS = 60.0

#: AstrNa 给 event.send 打补丁后留下的标记属性。
_ASTRNA_SEND_PATCH_ATTR = "_astrna_group_sender_concurrency_send_patch"

#: AstrNa 插件名（metadata.yaml / @register 的名字）。
_ASTRNA_PLUGIN_NAME = "astrbot_plugin_AstrNa"

#: AstrNa 轮次 ContextVar 的名字（没有公开 accessor，只能按模块属性取）。
_ASTRNA_ROUND_CTXVAR_NAME = "_CURRENT_SEND_ROUND"

#: AstrNaRuntime 上群聊并发模块的属性名。
_ASTRNA_RUNTIME_ATTR = "group_sender_concurrency"


def _safe_get(obj: Any, name: str) -> Any:
    try:
        return getattr(obj, name, None)
    except Exception:  # noqa: BLE001 - 对第三方对象只做能力探测，绝不抛错
        return None


def _safe_call(func: Any, *args: Any, **kwargs: Any) -> Any:
    if not callable(func):
        return None
    try:
        return func(*args, **kwargs)
    except Exception:  # noqa: BLE001
        return None


class AstrNaBackend:
    """AstrNa 群聊并发回复的能力探测与锁获取（全部 best-effort）。

    AstrNa 没有给第三方插件留公开接口，所以这里只读它的既有结构：

    - `context.get_registered_star("astrbot_plugin_AstrNa")` 拿到插件实例，
      `instance.runtime.group_sender_concurrency` 是并发模块；
    - 并发模块所在模块里有 `_CURRENT_SEND_ROUND` ContextVar 与
      `get_group_send_lock(umo)`；
    - `_installed` 为真表示功能已启用（配置 unlock_group_sender_concurrency）。

    任何一步失败都只意味着「按未启用处理」：发送照旧，只是不提前占轮次。
    """

    def __init__(self, plugin: Any):
        self._plugin = plugin
        self._resolved = False
        self._resolved_at = 0.0
        self._available = False
        self._instance = None
        self._round_ctxvar = None
        self._disabled_reason = ""

    # ------------------------------------------------------------------
    # 探测
    # ------------------------------------------------------------------
    def refresh(self) -> bool:
        """重新探测；AstrNa 可能后加载或热重载，插件初始化阶段不应永久缓存失败。"""
        self._resolved = True
        self._resolved_at = time.monotonic()
        self._available = False
        self._instance = None
        self._round_ctxvar = None
        self._disabled_reason = ""
        try:
            context = _safe_get(self._plugin, "context")
            registered = _safe_call(
                _safe_get(context, "get_registered_star"), _ASTRNA_PLUGIN_NAME
            )
            instance = _safe_get(registered, "star_cls")
            if instance is None:
                self._disabled_reason = "AstrNa 未安装或未加载"
                return False
            runtime = _safe_get(instance, "runtime")
            module_owner = _safe_get(runtime, _ASTRNA_RUNTIME_ATTR)
            if module_owner is None:
                self._disabled_reason = "AstrNa 版本过旧，没有群聊并发模块"
                return False
            if not bool(_safe_get(module_owner, "_installed")):
                self._disabled_reason = "AstrNa 群聊并发回复未开启"
                return False
            if not callable(_safe_get(module_owner, "get_group_send_lock")):
                self._disabled_reason = "AstrNa 群聊并发接口不兼容（缺 get_group_send_lock）"
                return False
            # 轮次 ContextVar 定义在 astrna.modules.group_sender_concurrency 里，
            # 插件主模块只 import 了类、没有把 ContextVar 暴露成属性；两个模块
            # 命名空间都试（也兼容以后把 ContextVar 提到主模块的版本）。
            round_ctxvar = None
            candidate_modules = [
                _safe_get(type(instance), "__module__"),
                _safe_get(type(module_owner), "__module__"),
            ]
            for module_path in candidate_modules:
                if not isinstance(module_path, str):
                    continue
                round_ctxvar = _safe_get(
                    sys.modules.get(module_path), _ASTRNA_ROUND_CTXVAR_NAME
                )
                if round_ctxvar is not None:
                    break
            if round_ctxvar is None or not callable(_safe_get(round_ctxvar, "get")):
                self._disabled_reason = "AstrNa 群聊并发接口不兼容（缺轮次上下文）"
                return False
            self._instance = instance
            self._round_ctxvar = round_ctxvar
            self._available = True
            return True
        except Exception as exc:  # noqa: BLE001
            self._disabled_reason = f"AstrNa 能力探测失败: {exc}"
            return False

    def _ensure(self) -> bool:
        if not self._resolved:
            return self.refresh()
        if not self._available:
            now = time.monotonic()
            if now - self._resolved_at >= RESOLVE_RETRY_SECONDS:
                return self.refresh()
        return self._available

    @property
    def available(self) -> bool:
        return self._ensure()

    @property
    def disabled_reason(self) -> str:
        if not self._resolved:
            self.refresh()
        return self._disabled_reason

    # ------------------------------------------------------------------
    # 轮次 / 锁
    # ------------------------------------------------------------------
    def current_round(self) -> Any:
        """当前 task 继承到的 AstrNa 发送轮次（没有则 None）。"""
        if not self._ensure():
            return None
        try:
            return self._round_ctxvar.get()
        except Exception:  # noqa: BLE001
            return None

    def round_for(self, umo: str) -> Any | None:
        """当前 task 若正处于 umo 这一轮、且轮次未关闭，返回轮次对象。"""
        session = str(umo or "")
        if not session:
            return None
        round_obj = self.current_round()
        if round_obj is None:
            return None
        if bool(_safe_get(round_obj, "closed")):
            return None
        if str(_safe_get(round_obj, "umo") or "") != session:
            return None
        if not callable(_safe_get(round_obj, "ensure_acquired")):
            return None
        return round_obj

    def in_open_round(self) -> bool:
        """当前 task 是否继承着一个未关闭的 AstrNa 轮次（旁路发送判定用）。"""
        round_obj = self.current_round()
        if round_obj is None:
            return False
        return not bool(_safe_get(round_obj, "closed"))

    def group_lock(self, umo: str) -> Any | None:
        """AstrNa 给某个群会话用的整轮发送锁（同一把锁，可跨轮共享）。"""
        if not self._ensure():
            return None
        session = str(umo or "")
        if not session:
            return None
        module_owner = _safe_get(
            _safe_get(self._instance, "runtime"), _ASTRNA_RUNTIME_ATTR
        )
        return _safe_call(_safe_get(module_owner, "get_group_send_lock"), session)


class _LocalLockRecord:
    """本地降级锁的一次持有记录：owner + 重入计数 + 持有代次 + task 结束兜底释放。"""

    __slots__ = ("lock", "owner", "depth", "generation", "last_used", "cleanup_task")

    def __init__(self, lock: asyncio.Lock):
        self.lock = lock
        self.owner: asyncio.Task | None = None
        self.depth = 0
        self.generation = 0
        self.last_used = time.monotonic()
        # 已经登记过结束兜底释放的 task：同一个长任务反复发送时不再重复登记。
        self.cleanup_task: asyncio.Task | None = None


class _GroupLockLease:
    """AstrNa 群锁的一次整轮租约（事件没有轮次上下文时用）。

    AstrNa 的整轮锁正常由 `SendRound` 持有到轮末；但命令类/定时类事件不会
    进入 AstrNa 的 agent 子阶段，没有 `SendRound`，若只按单条发送加锁，
    SplitStep 的分段气泡之间会被别的轮次整轮插入。这里直接把同一把群锁
    当整轮租约占住，`after_reply_sent` 释放；task 结束兜底释放，绝不会
    因为观察器收尾缺失而永久占锁。
    """

    __slots__ = ("lock", "released", "owner_task")

    def __init__(self, lock: Any):
        self.lock = lock
        self.released = False
        self.owner_task: asyncio.Task | None = None


class SendOrderGate:
    """按群会话串行化本插件的出站消息（单一锁宇宙，见模块 docstring）。

    `hold` 是异步上下文管理器：

    ```python
    async with gate.hold(umo, event=event):
        ...
    ```

    可能的实现路径（按优先级）：
    1. 当前 task 处于 AstrNa 本轮（同 umo、未关闭）：`ensure_acquired()` 提前占轮，
       退出时不做任何释放（轮末由 AstrNa 统一释放）；
    2. 当前 task 不在本轮，但 AstrNa 生效：裸取 `get_group_send_lock(umo)`，退出释放。
       若发送链已由 AstrNa guard 保护（可能同 task 已持锁），直接空操作，避免
       `asyncio.Lock` 重入死锁；
    3. AstrNa 不可用：本地锁，支持同 task 重入，task 结束兜底释放，退出释放。

    所有等待都受 `timeout` 约束，超时放行并限频告警。
    """

    def __init__(self, plugin: Any):
        self._plugin = plugin
        self._backend = AstrNaBackend(plugin)
        self._local: dict[tuple[int, str], _LocalLockRecord] = {}
        self._warned_at: dict[str, float] = {}

    # ------------------------------------------------------------------
    # 诊断
    # ------------------------------------------------------------------
    def status(self) -> dict[str, Any]:
        backend = self._backend
        if backend.available:
            return {"backend": "astrna", "detail": "AstrNa 群聊并发回复"}
        return {"backend": "local", "detail": backend.disabled_reason}

    def describe(self) -> str:
        status = self.status()
        if status["backend"] == "astrna":
            return "AstrNa 群聊并发回复：按群整轮排队"
        reason = str(status.get("detail") or "").strip()
        return f"本地整轮排队（{reason}）" if reason else "本地整轮排队"

    def is_astrna_backend(self) -> bool:
        """是否由 AstrNa 负责整轮排队（此时本插件只提前占轮，不重复加锁）。"""
        return self._backend.available

    def _warn(self, key: str, message: str, *args: Any) -> None:
        now = time.monotonic()
        last = self._warned_at.get(key, 0.0)
        if now - last < WARN_INTERVAL_SECONDS:
            return
        self._warned_at[key] = now
        logger = _safe_get(self._plugin, "logger")
        method = _safe_get(logger, "warning")
        if callable(method):
            try:
                method(message, *args)
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------
    # 本地降级锁
    # ------------------------------------------------------------------
    def _local_record(self, umo: str) -> _LocalLockRecord:
        try:
            loop_id = id(asyncio.get_running_loop())
        except RuntimeError:
            loop_id = 0
        key = (loop_id, str(umo))
        record = self._local.get(key)
        if record is None:
            record = _LocalLockRecord(asyncio.Lock())
            self._local[key] = record
        return record

    def _release_on_task_done(self, record: _LocalLockRecord, owner_task: asyncio.Task) -> None:
        """task 结束兜底：任务已退出，强制归还它仍持有的整层本地锁。"""
        if record.cleanup_task is owner_task:
            record.cleanup_task = None
        if record.owner is owner_task and record.depth > 0:
            record.depth = 0
            record.owner = None
            record.last_used = time.monotonic()
            with contextlib.suppress(RuntimeError):
                record.lock.release()

    def _release_local(self, record: _LocalLockRecord) -> None:
        if record.depth <= 0:
            return
        record.depth -= 1
        record.last_used = time.monotonic()
        if record.depth == 0:
            record.owner = None
            record.cleanup_task = None
            with contextlib.suppress(RuntimeError):
                record.lock.release()

    async def _acquire_local(
        self, record: _LocalLockRecord, timeout: float, umo: str
    ) -> bool:
        task = asyncio.current_task()
        if record.owner is task and record.depth > 0:
            record.depth += 1
            return True
        try:
            await asyncio.wait_for(record.lock.acquire(), timeout=timeout)
        except asyncio.TimeoutError:
            self._warn(
                "local-timeout",
                "[糯小喵] 等待同群发送队列超过 %.0f 秒，已放行以不阻塞后续消息: %s",
                timeout,
                umo,
            )
            return False
        record.owner = task
        record.depth = 1
        record.generation += 1
        record.last_used = time.monotonic()
        if task is not None and record.cleanup_task is not task:
            record.cleanup_task = task
            task.add_done_callback(
                lambda _t, rec=record, ot=task: self._release_on_task_done(rec, ot)
            )
        return True

    # ------------------------------------------------------------------
    # 整轮锁定接口
    # ------------------------------------------------------------------
    class _RoundLease:
        def __init__(self, record: _LocalLockRecord, generation: int):
            self.record = record
            self.generation = generation
            self.released = False

    async def acquire_round(
        self, umo: str, *, event: Any = None, timeout: float = OBSERVER_WAIT_SECONDS
    ) -> _RoundLease | _GroupLockLease | None:
        """占住整轮发送权；调用方在 after_reply_sent 时 release_round。

        三种情况：
        - AstrNa 轮内：`hold` 会幂等提前占住整轮群锁、轮末由 AstrNa 释放，
          这里不重复占（返回 None）；
        - AstrNa 生效但事件没有轮次（命令/定时等）：group_lock 当整轮租约；
        - 未启用 AstrNa：本地整轮锁。
        """
        session = str(umo or "")
        if not session:
            return None
        backend = self._backend
        if backend.available:
            if backend.round_for(session) is not None:
                return None
            # 发送链已由 AstrNa guard 保护时，裸取群锁会与 guard 里的
            # ensure_acquired 形成同 task 重入死锁，交给 guard。
            if self._guarded(event):
                return None
            lock = backend.group_lock(session)
            if lock is None or not await self._acquire_raw(lock, session, timeout):
                return None
            lease = _GroupLockLease(lock)
            task = asyncio.current_task()
            lease.owner_task = task
            if task is not None:
                task.add_done_callback(
                    lambda _task, lease=lease: self._release_group_lease(lease)
                )
            return lease
        record = self._local_record(session)
        if await self._acquire_local(record, timeout, session):
            return self._RoundLease(record, record.generation)
        return None

    def release_round(self, lease: Any) -> None:
        if lease is None or getattr(lease, "released", False):
            return
        if isinstance(lease, _GroupLockLease):
            self._release_group_lease(lease)
            return
        lease.released = True
        if lease.record.generation != lease.generation:
            return
        self._release_local(lease.record)

    def _release_group_lease(self, lease: _GroupLockLease) -> None:
        """释放 AstrNa 群锁整轮租约（正常收尾与 task 结束兜底共用）。"""
        if lease.released:
            return
        lease.released = True
        with contextlib.suppress(RuntimeError):
            lease.lock.release()

    # ------------------------------------------------------------------
    # 发送权
    # ------------------------------------------------------------------
    @contextlib.asynccontextmanager
    async def hold(
        self,
        umo: str,
        *,
        event: Any = None,
        timeout: float = HOLD_WAIT_SECONDS,
    ):
        """取得某会话的发送权，整段发送期间保持，退出时释放。"""
        session = str(umo or "")
        if not session:
            yield False
            return
        backend = self._backend
        if backend.available:
            round_obj = backend.round_for(session)
            if round_obj is not None:
                # 本轮自己的整轮锁：提前（打字前）占住，退出时不释放。
                acquired = await self._ensure_round(round_obj, session, timeout)
                yield acquired
                return
            if self._guarded(event):
                # 发送链已有 AstrNa guard：真正的锁会在第一次发送时获取，
                # 此刻裸取会与 guard 里的 ensure_acquired 重入死锁。
                yield False
                return
            lock = backend.group_lock(session)
            if lock is not None:
                acquired = await self._acquire_raw(lock, session, timeout)
                try:
                    yield acquired
                finally:
                    if acquired:
                        with contextlib.suppress(RuntimeError):
                            lock.release()
                return
        record = self._local_record(session)
        acquired = await self._acquire_local(record, timeout, session)
        try:
            yield acquired
        finally:
            if acquired:
                self._release_local(record)

    @contextlib.asynccontextmanager
    async def one_shot(
        self,
        umo: str,
        *,
        event: Any = None,
        timeout: float = ONE_SHOT_WAIT_SECONDS,
    ):
        """旁路发送（不经过本轮 event.send）的单次排队。

        只有目标会话恰好就是当前 task 正在进行的 AstrNa 轮次时才让路：
        AstrNa 的 `context.send_message` 只在 session 等于本轮 umo 时加入本轮；
        跨会话投递（send_message 工具）仍要按目标群排队，否则会插进目标群
        正在分段发送的气泡之间。
        """
        if (
            self._backend.available
            and self._backend.round_for(str(umo or "")) is not None
        ):
            yield False
            return
        async with self.hold(umo, event=event, timeout=timeout) as acquired:
            yield acquired

    async def _ensure_round(self, round_obj: Any, umo: str, timeout: float) -> bool:
        try:
            return bool(
                await asyncio.wait_for(round_obj.ensure_acquired(), timeout=timeout)
            )
        except asyncio.TimeoutError:
            self._warn(
                "round-timeout",
                "[糯小喵] 等待 AstrNa 群聊整轮发送权超过 %.0f 秒，已放行: %s",
                timeout,
                umo,
            )
            return False
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self._warn(
                "round-error",
                "[糯小喵] 获取 AstrNa 群聊整轮发送权失败，已放行: %s (%s)",
                umo,
                exc,
            )
            return False

    async def _acquire_raw(self, lock: Any, umo: str, timeout: float) -> bool:
        try:
            await asyncio.wait_for(lock.acquire(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            self._warn(
                "group-timeout",
                "[糯小喵] 等待群聊整轮发送权超过 %.0f 秒，已放行: %s",
                timeout,
                umo,
            )
            return False
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self._warn(
                "group-error",
                "[糯小喵] 获取群聊整轮发送权失败，已放行: %s (%s)",
                umo,
                exc,
            )
            return False

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------
    @staticmethod
    def _guarded(event: Any) -> bool:
        """发送链是否已经由 AstrNa 的 event.send guard 保护。"""
        candidates = []
        send = _safe_get(event, "send")
        if send is not None:
            candidates.append(send)
        observed = _safe_call(
            _safe_get(event, "get_extra"), "nuoxiaomiao_send_observer"
        )
        if isinstance(observed, dict):
            candidates.append(observed.get("original"))
        for candidate in candidates:
            if candidate is None:
                continue
            if bool(_safe_get(candidate, _ASTRNA_SEND_PATCH_ATTR)):
                return True
        return False

    def prune(self, now: float, max_idle: float = LOCAL_LOCK_IDLE_SECONDS) -> None:
        """回收长期空闲的本地锁（按 event loop 隔离，重载后不会串锁）。"""
        for key, record in list(self._local.items()):
            if record.depth > 0 or record.lock.locked():
                continue
            if now - record.last_used > max_idle:
                self._local.pop(key, None)

    async def close(self) -> None:
        """插件卸载：只丢弃不再被持有的本地锁，绝不强抢他人正在用的锁。"""
        for key, record in list(self._local.items()):
            if record.depth <= 0 and not record.lock.locked():
                self._local.pop(key, None)
        self._warned_at.clear()
