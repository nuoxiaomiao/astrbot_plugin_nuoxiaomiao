"""Ref-counted deferred proactive send serialization under original run budget."""

from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable

if __package__ and "." in __package__:
    from .autonomy_budget import CURRENT_RUN, BudgetExceeded, Run, RunStopped
else:
    from resident.autonomy_budget import CURRENT_RUN, BudgetExceeded, Run, RunStopped


class DeferredDeliveryGate:
    """Serialize deferred sends per run_id without lock-pool eviction races."""

    def __init__(self, autonomy: Any) -> None:
        self.autonomy = autonomy
        self._locks: dict[int, tuple[asyncio.Lock, int]] = {}

    def _acquire_ref(self, rid: int) -> asyncio.Lock:
        lock, count = self._locks.get(rid, (None, 0))
        if lock is None:
            lock = asyncio.Lock()
        self._locks[rid] = (lock, count + 1)
        return lock

    def _release_ref(self, rid: int, lock: asyncio.Lock) -> None:
        entry = self._locks.get(rid)
        if entry is None or entry[0] is not lock:
            return
        _, count = entry
        if count <= 1:
            self._locks.pop(rid, None)
        else:
            self._locks[rid] = (lock, count - 1)

    async def deliver_pending(self, row: dict[str, Any], callback: Callable[[dict[str, Any]], Awaitable[Any]]) -> Any:
        service = self.autonomy.service
        store = service.store
        host = service.host
        owners = store.rows("SELECT run_id FROM resident_deferred WHERE pending_id=?", (row["id"],))
        if not owners:
            return await callback(row)
        rid = int(owners[0]["run_id"])
        lock = self._acquire_ref(rid)
        try:
            async with lock:
                while rid in self.autonomy.runs:
                    await asyncio.sleep(0.2)
                saved = store.rows("SELECT * FROM resident_runs WHERE id=?", (rid,))[0]
                if saved["status"] not in ("succeeded", "limited"):
                    host.update_proactive_row(row["id"], status="skipped", done_ts=time.time())
                    return None
                event = service.web_event(saved["umo"])
                run = Run(
                    rid,
                    event,
                    saved["persona"],
                    steps=saved["requests"],
                    task=asyncio.current_task(),
                )
                self.autonomy.runs[rid] = run
                token = CURRENT_RUN.set(run)
                status = saved["status"]
                detail = saved["summary"]
                try:
                    await self.autonomy.checkpoint(event, "静默顺延发送恢复")
                    return await callback(row)
                except BudgetExceeded as exc:
                    status = "limited"
                    detail = str(exc)
                    host.update_proactive_row(row["id"], status="skipped", done_ts=time.time())
                except RunStopped:
                    status = "interrupted"
                    host.update_proactive_row(row["id"], status="skipped", done_ts=time.time())
                except asyncio.CancelledError:
                    status = "interrupted"
                    unknown_sends = store.rows(
                        "SELECT id FROM resident_sends WHERE run_id=? AND status='unknown'",
                        (rid,),
                    )
                    host.update_proactive_row(
                        row["id"],
                        status="unknown" if unknown_sends else "skipped",
                        done_ts=time.time(),
                    )
                    raise
                except Exception as exc:
                    status = "failed"
                    detail = str(exc)
                    host.update_proactive_row(row["id"], status="failed", done_ts=time.time())
                finally:
                    if store.rows("SELECT id FROM resident_sends WHERE run_id=? AND status='unknown'", (rid,)):
                        status = "unknown"
                    store.finish(rid, status, "spoke" if run.sent else saved["outcome"], detail)
                    self.autonomy.runs.pop(rid, None)
                    CURRENT_RUN.reset(token)
        finally:
            self._release_ref(rid, lock)
