"""整轮发送排队网关：与 AstrNa 并发回复协同，或降级为本地按群排队。"""

from __future__ import annotations

import asyncio
import contextlib
import sys
import threading
import time
import weakref
from dataclasses import dataclass
from typing import Any

OBSERVER_WAIT_SECONDS = 60.0
ONE_SHOT_WAIT_SECONDS = 30.0
HOLD_WAIT_SECONDS = 180.0
LOCAL_LOCK_IDLE_SECONDS = 3600.0
RESOLVE_RETRY_SECONDS = 30.0
ACTIVE_REVALIDATE_SECONDS = 5.0
WARN_INTERVAL_SECONDS = 300.0

_ASTRNA_PLUGIN_NAME = "astrbot_plugin_AstrNa"
_ASTRNA_RUNTIME_ATTR = "group_sender_concurrency"
_ASTRNA_ROUND_CTXVAR_NAME = "_CURRENT_SEND_ROUND"
_ASTRNA_SEND_PATCH_ATTR = "_astrna_group_sender_concurrency_send_patch"


def _safe_get(obj: Any, name: str, default: Any = None) -> Any:
    try:
        return getattr(obj, name)
    except Exception:  # noqa: BLE001
        return default


def _safe_call(fn: Any, *args: Any, **kwargs: Any) -> Any:
    if not callable(fn):
        return None
    try:
        return fn(*args, **kwargs)
    except Exception:  # noqa: BLE001
        return None


@dataclass
class _LocalLockRecord:
    lock: asyncio.Lock
    owner: asyncio.Task | None = None
    depth: int = 0
    generation: int = 0
    last_used: float = 0.0
    cleanup_task: asyncio.Task | None = None
    cleanup_cb: Any = None


@dataclass
class _GroupLockLease:
    lock: Any
    session: str = ""
    owner_task: asyncio.Task | None = None
    cleanup_cb: Any = None
    released: bool = False


class AstrNaBackend:
    """AstrNa 群聊并发回复的能力探测与锁获取（全部 best-effort）。"""

    def __init__(self, plugin: Any):
        self._plugin = plugin
        self._resolved = False
        self._resolved_at = 0.0
        self._available = False
        self._instance = None
        self._module_owner = None
        self._round_ctxvar = None
        self._disabled_reason = ""

    def refresh(self) -> bool:
        self._resolved = True
        self._resolved_at = time.monotonic()
        self._available = False
        self._instance = None
        self._module_owner = None
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
            self._module_owner = module_owner
            self._round_ctxvar = round_ctxvar
            self._available = True
            return True
        except Exception as exc:  # noqa: BLE001
            self._disabled_reason = f"AstrNa 能力探测失败: {exc}"
            return False

    def _ensure(self) -> bool:
        if not self._resolved:
            return self.refresh()
        now = time.monotonic()
        if self._available:
            if not bool(_safe_get(self._module_owner, "_installed")):
                return self.refresh()
            if now - self._resolved_at >= ACTIVE_REVALIDATE_SECONDS:
                context = _safe_get(self._plugin, "context")
                registered = _safe_call(
                    _safe_get(context, "get_registered_star"), _ASTRNA_PLUGIN_NAME
                )
                if _safe_get(registered, "star_cls") is not self._instance:
                    return self.refresh()
                self._resolved_at = now
            return self._available
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

    def round_for(self, umo: str) -> Any | None:
        if not self._ensure() or self._round_ctxvar is None:
            return None
        round_obj = _safe_call(self._round_ctxvar.get, None)
        if round_obj is None:
            return None
        round_umo = _safe_get(round_obj, "umo")
        if round_umo and umo and str(round_umo) != str(umo):
            return None
        if bool(_safe_get(round_obj, "closed")):
            return None
        return round_obj

    def group_lock(self, umo: str) -> Any | None:
        if not self._ensure() or self._instance is None:
            return None
        runtime = _safe_get(self._instance, "runtime")
        module_owner = _safe_get(runtime, _ASTRNA_RUNTIME_ATTR)
        getter = _safe_get(module_owner, "get_group_send_lock")
        if not callable(getter):
            return None
        try:
            return getter(str(umo))
        except Exception:  # noqa: BLE001
            return None


class SendOrderGate:
    """统一的整轮发送排队入口。"""

    def __init__(self, plugin: Any):
        self._plugin = plugin
        self._backend = AstrNaBackend(plugin)
        self._local: dict[tuple[int, str], _LocalLockRecord] = {}
        self._local_lock = threading.RLock()
        self._task_held: weakref.WeakKeyDictionary[asyncio.Task, dict[str, int]] = (
            weakref.WeakKeyDictionary()
        )
        self._warned_at: dict[str, float] = {}

    def refresh(self) -> bool:
        return self._backend.refresh()

    def status(self) -> dict[str, Any]:
        if self._backend.available:
            return {"backend": "astrna", "detail": ""}
        return {"backend": "local", "detail": self._backend.disabled_reason}

    def describe(self) -> str:
        status = self.status()
        if status["backend"] == "astrna":
            return "AstrNa 群聊并发回复：按群整轮排队"
        reason = str(status.get("detail") or "").strip()
        return f"本地整轮排队（{reason}）" if reason else "本地整轮排队"

    def is_astrna_backend(self) -> bool:
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

    def _is_task_holding(self, session: str, task: asyncio.Task | None) -> bool:
        if task is None:
            return False
        held = self._task_held.get(task)
        return bool(held and held.get(session, 0) > 0)

    def _mark_task_hold(self, session: str, task: asyncio.Task | None) -> None:
        if task is None:
            return
        held = self._task_held.get(task)
        if held is None:
            held = {}
            self._task_held[task] = held
        held[session] = held.get(session, 0) + 1

    def _unmark_task_hold(self, session: str, task: asyncio.Task | None) -> None:
        if task is None:
            return
        held = self._task_held.get(task)
        if not held:
            return
        count = held.get(session, 0)
        if count <= 1:
            held.pop(session, None)
            if not held:
                self._task_held.pop(task, None)
        else:
            held[session] = count - 1

    def _local_record(self, umo: str) -> _LocalLockRecord:
        try:
            loop_id = id(asyncio.get_running_loop())
        except RuntimeError:
            loop_id = 0
        key = (loop_id, str(umo))
        with self._local_lock:
            record = self._local.get(key)
            if record is None:
                record = _LocalLockRecord(asyncio.Lock())
                self._local[key] = record
            record.last_used = time.monotonic()
            return record

    def _detach_local_cb(self, record: _LocalLockRecord) -> None:
        task = record.cleanup_task
        cb = record.cleanup_cb
        record.cleanup_task = None
        record.cleanup_cb = None
        if task is not None and cb is not None:
            with contextlib.suppress(Exception):
                task.remove_done_callback(cb)

    def _release_on_task_done(self, record: _LocalLockRecord, owner_task: asyncio.Task) -> None:
        with self._local_lock:
            if record.cleanup_task is owner_task:
                record.cleanup_task = None
                record.cleanup_cb = None
            if record.owner is owner_task and record.depth > 0:
                record.depth = 0
                record.owner = None
                record.last_used = time.monotonic()
                with contextlib.suppress(RuntimeError):
                    record.lock.release()

    def _release_local(self, record: _LocalLockRecord) -> None:
        with self._local_lock:
            if record.depth <= 0:
                return
            record.depth -= 1
            record.last_used = time.monotonic()
            if record.depth == 0:
                record.owner = None
                self._detach_local_cb(record)
                with contextlib.suppress(RuntimeError):
                    record.lock.release()

    async def _acquire_with_cancel_guard(self, lock: Any, timeout: float) -> bool:
        acquired_flag = False

        async def _do_acquire() -> None:
            nonlocal acquired_flag
            await lock.acquire()
            acquired_flag = True

        try:
            await asyncio.wait_for(_do_acquire(), timeout=timeout)
            return True
        except BaseException:
            if acquired_flag:
                with contextlib.suppress(RuntimeError):
                    lock.release()
            raise

    async def _acquire_local(
        self, record: _LocalLockRecord, timeout: float, umo: str
    ) -> tuple[bool, bool]:
        task = asyncio.current_task()
        with self._local_lock:
            if record.owner is task and record.depth > 0:
                record.depth += 1
                record.last_used = time.monotonic()
                return True, False
        try:
            await self._acquire_with_cancel_guard(record.lock, timeout)
        except asyncio.TimeoutError:
            self._warn(
                "local-timeout",
                "[糯小喵] 等待同群发送队列超过 %.0f 秒，已放行以不阻塞后续消息: %s",
                timeout,
                umo,
            )
            return False, True
        with self._local_lock:
            record.owner = task
            record.depth = 1
            record.generation += 1
            record.last_used = time.monotonic()
            if task is not None and record.cleanup_task is not task:
                def _cb(_t, rec=record, ot=task):
                    self._release_on_task_done(rec, ot)

                record.cleanup_task = task
                record.cleanup_cb = _cb
                task.add_done_callback(_cb)
        return True, False

    class _RoundLease:
        def __init__(self, record: _LocalLockRecord, generation: int):
            self.record = record
            self.generation = generation
            self.released = False

    async def acquire_round_ex(
        self, umo: str, *, event: Any = None, timeout: float = OBSERVER_WAIT_SECONDS
    ) -> tuple[_RoundLease | _GroupLockLease | None, bool]:
        """占住整轮发送权并返回 (lease, timed_out)。"""
        session = str(umo or "")
        if not session:
            return None, False
        backend = self._backend
        task = asyncio.current_task()
        if backend.available:
            if self._is_task_holding(session, task):
                return None, False
            if backend.round_for(session) is not None:
                return None, False
            if self._guarded(event):
                return None, False
            lock = backend.group_lock(session)
            if lock is None:
                return None, False
            acquired, timed_out = await self._acquire_raw_ex(lock, session, timeout)
            if not acquired:
                return None, timed_out
            self._mark_task_hold(session, task)
            lease = _GroupLockLease(lock, session=session, owner_task=task)
            if task is not None:
                def _cb(_task, cur_lease=lease):
                    self._release_group_lease(cur_lease)

                lease.cleanup_cb = _cb
                task.add_done_callback(_cb)
            return lease, False
        record = self._local_record(session)
        acquired, timed_out = await self._acquire_local(record, timeout, session)
        if acquired:
            return self._RoundLease(record, record.generation), False
        return None, timed_out

    async def acquire_round(
        self, umo: str, *, event: Any = None, timeout: float = OBSERVER_WAIT_SECONDS
    ) -> _RoundLease | _GroupLockLease | None:
        lease, _ = await self.acquire_round_ex(umo, event=event, timeout=timeout)
        return lease

    def release_round(self, lease: Any) -> None:
        if lease is None or getattr(lease, "released", False):
            return
        if isinstance(lease, _GroupLockLease):
            self._release_group_lease(lease)
            return
        lease.released = True
        with self._local_lock:
            if lease.record.generation != lease.generation:
                return
        self._release_local(lease.record)

    def _release_group_lease(self, lease: _GroupLockLease) -> None:
        if lease.released:
            return
        lease.released = True
        if lease.owner_task is not None and lease.cleanup_cb is not None:
            with contextlib.suppress(Exception):
                lease.owner_task.remove_done_callback(lease.cleanup_cb)
        lease.cleanup_cb = None
        self._unmark_task_hold(lease.session, lease.owner_task)
        with contextlib.suppress(RuntimeError):
            lease.lock.release()

    @contextlib.asynccontextmanager
    async def hold(
        self,
        umo: str,
        *,
        event: Any = None,
        timeout: float = HOLD_WAIT_SECONDS,
    ):
        session = str(umo or "")
        if not session:
            yield False
            return
        backend = self._backend
        task = asyncio.current_task()
        if backend.available:
            if self._is_task_holding(session, task):
                yield True
                return
            round_obj = backend.round_for(session)
            if round_obj is not None:
                acquired = await self._ensure_round(round_obj, session, timeout)
                yield acquired
                return
            if self._guarded(event):
                yield False
                return
            lock = backend.group_lock(session)
            if lock is not None:
                acquired, _ = await self._acquire_raw_ex(lock, session, timeout)
                if acquired:
                    self._mark_task_hold(session, task)
                try:
                    yield acquired
                finally:
                    if acquired:
                        self._unmark_task_hold(session, task)
                        with contextlib.suppress(RuntimeError):
                            lock.release()
                return
        record = self._local_record(session)
        acquired, _ = await self._acquire_local(record, timeout, session)
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
        session = str(umo or "")
        if self._backend.available and self._backend.round_for(session) is not None:
            yield False
            return
        async with self.hold(session, event=event, timeout=timeout) as acquired:
            yield acquired

    async def _ensure_round(self, round_obj: Any, umo: str, timeout: float) -> bool:
        try:
            return bool(await asyncio.wait_for(round_obj.ensure_acquired(), timeout=timeout))
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

    async def _acquire_raw_ex(self, lock: Any, umo: str, timeout: float) -> tuple[bool, bool]:
        try:
            await self._acquire_with_cancel_guard(lock, timeout)
            return True, False
        except asyncio.TimeoutError:
            self._warn(
                "group-timeout",
                "[糯小喵] 等待群聊整轮发送权超过 %.0f 秒，已放行: %s",
                timeout,
                umo,
            )
            return False, True
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self._warn(
                "group-error",
                "[糯小喵] 获取群聊整轮发送权失败，已放行: %s (%s)",
                umo,
                exc,
            )
            return False, False

    @staticmethod
    def _guarded(event: Any) -> bool:
        candidates = []
        send = _safe_get(event, "send")
        if send is not None:
            candidates.append(send)
        observed = _safe_call(_safe_get(event, "get_extra"), "nuoxiaomiao_send_observer")
        if isinstance(observed, dict):
            candidates.append(observed.get("original"))
        for candidate in candidates:
            if candidate is None:
                continue
            if bool(_safe_get(candidate, _ASTRNA_SEND_PATCH_ATTR)):
                return True
        return False

    def prune(self, now: float, max_idle: float = LOCAL_LOCK_IDLE_SECONDS) -> None:
        with self._local_lock:
            for key, record in list(self._local.items()):
                if record.depth > 0 or record.lock.locked():
                    continue
                if now - record.last_used > max_idle:
                    self._local.pop(key, None)

    async def close(self) -> None:
        with self._local_lock:
            for key, record in list(self._local.items()):
                if record.depth <= 0 and not record.lock.locked():
                    self._local.pop(key, None)
        self._warned_at.clear()
