"""Bounded native Agent turns, per-run execution gates, and durable receipts."""

from __future__ import annotations

import asyncio
from datetime import datetime
import time
from typing import Any

from astrbot.api import logger

if __package__ and "." in __package__:
    from ..infra.constants import SELF_WAKE_EVENT_EXTRA
    from .autonomy_budget import (
        CURRENT_RUN,
        BudgetExceeded,
        BudgetProvider,
        Run,
        RunStopped,
    )
    from .autonomy_delivery import DeferredDeliveryGate
    from .autonomy_sandbox import ScopedExecutor
    from .ports import resolve_agent_core
else:
    from infra.constants import SELF_WAKE_EVENT_EXTRA
    from resident.autonomy_budget import (
        CURRENT_RUN,
        BudgetExceeded,
        BudgetProvider,
        Run,
        RunStopped,
    )
    from resident.autonomy_delivery import DeferredDeliveryGate
    from resident.autonomy_sandbox import ScopedExecutor
    from resident.ports import resolve_agent_core

__all__ = [
    "CURRENT_RUN",
    "BudgetExceeded",
    "RunStopped",
    "Run",
    "BudgetProvider",
    "ScopedExecutor",
    "Autonomy",
]


class Autonomy:
    def __init__(self, service: Any) -> None:
        self.service = service
        self.runs: dict[int, Run] = {}
        self._delivery = DeferredDeliveryGate(self)

    @property
    def delivery_locks(self):
        return self._delivery._locks

    async def control(self, run_id: int, action: str) -> None:
        run = self.runs.get(run_id)
        if run is None:
            return
        if action == "pause":
            run.status = "paused"
            run.gate.clear()
        elif action == "resume":
            run.status = "running"
            run.gate.set()
        elif action == "stop":
            run.status = "stopped"
            run.gate.set()
            host = self.service.host
            for row in self.service.store.rows(
                "SELECT p.id FROM proactive_pending p JOIN resident_deferred d ON d.pending_id=p.id WHERE d.run_id=? AND p.status='pending'",
                (run_id,),
            ):
                host.cancel_proactive_timer(row["id"])
                host.update_proactive_row(row["id"], status="skipped", done_ts=time.time())
        else:
            raise ValueError("控制动作须为 pause / resume / stop")
        with self.service.store.transaction() as db:
            db.execute("UPDATE resident_runs SET status=? WHERE id=?", (run.status, run_id))

    async def checkpoint(self, event: Any, phase: str) -> None:
        p = self.service.plugin
        run = CURRENT_RUN.get()
        while True:
            if not p.enabled or not p.proactive_silent_wake_enabled or not p.proactive_enabled:
                raise RunStopped("功能或自醒已关闭")
            if run and run.status == "stopped":
                raise RunStopped("运行已由管理员停止")
            if run is None or run.status != "paused":
                break
            try:
                await asyncio.wait_for(run.gate.wait(), timeout=0.5)
            except asyncio.TimeoutError:
                continue

    def restrict_run(self, run: Run) -> None:
        integration = self.service.integration
        if run.allowed is None:
            run.allowed = set(integration.allowed())
        current = integration.restrict(run.event, run.request)
        names = {t.name for t in current.tools} & run.allowed
        if run.effective is not None:
            names &= run.effective
        run.effective = names
        run.request.func_tool = self.filter_payload(run, current)
        for attr in ("_skill_like_raw_tool_set", "_tool_schema_param_set"):
            original = getattr(run.runner, attr, None)
            if original is not None:
                setattr(run.runner, attr, self.filter_payload(run, original))

    @staticmethod
    def filter_payload(run: Run, toolset: Any) -> Any:
        from astrbot.core.agent.tool import ToolSet

        return ToolSet(
            tools=[
                t
                for t in getattr(toolset, "tools", []) or []
                if t.name in (run.effective or set()) and getattr(t, "active", True)
            ]
        )

    async def before_request(self, run: Run) -> None:
        await self.checkpoint(run.event, "模型请求前")
        p = self.service.plugin
        store = self.service.store
        if run.steps >= p.proactive_silent_wake_max_steps:
            raise BudgetExceeded("本轮请求步数已达到上限")
        if store.rows(
            "SELECT id FROM resident_actions WHERE run_id=? AND status='unknown' UNION ALL SELECT id FROM resident_sends WHERE run_id=? AND status='unknown'",
            (run.id, run.id),
        ):
            raise BudgetExceeded("本轮有结果未知的动作，核实之前停止后续请求")
        row = store.rows("SELECT * FROM resident_runs WHERE id=?", (run.id,))[0]
        day = datetime.now().strftime("%Y-%m-%d")
        daily = store.budget(day)
        if p.resident_run_token_limit or p.resident_daily_token_limit:
            if row["usage_unknown"] or (p.resident_daily_token_limit and daily["unknown"]):
                raise BudgetExceeded("模型未报告完整用量，已停止后续请求")
        if p.resident_run_token_limit and row["input_other"] + row["output"] >= p.resident_run_token_limit:
            raise BudgetExceeded("本轮 token 阈值已达到")
        if p.resident_daily_token_limit and daily.get("uncached_tokens", daily["tokens"]) >= p.resident_daily_token_limit:
            raise BudgetExceeded("今日 token 阈值已达到")
        store.charge(run.id, day, p.proactive_silent_wake_daily_budget)
        if run.request is not None:
            self.restrict_run(run)
        with store.transaction() as db:
            db.execute("UPDATE resident_runs SET requests=requests+1 WHERE id=?", (run.id,))
        run.request_day = day
        run.steps += 1

    def record_usage(self, run: Run, usage: Any) -> None:
        if usage is None:
            self.service.store.usage(run.id, day=run.request_day or None)
        else:
            vals = tuple(
                max(0, int(getattr(usage, k, 0) or 0))
                for k in ("input_other", "input_cached", "output")
            )
            self.service.store.usage(run.id, vals, day=run.request_day or None)

    def wrap_provider(self, provider: Any) -> Any:
        run = CURRENT_RUN.get()
        if run is None or isinstance(provider, BudgetProvider):
            return provider
        return BudgetProvider(provider, self, run)

    async def close(self) -> None:
        tasks = []
        for run in list(self.runs.values()):
            run.status = "stopped"
            run.gate.set()
            if run.task and run.task is not asyncio.current_task():
                run.task.cancel()
                tasks.append(run.task)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def deliver_pending(self, row: dict[str, Any], callback) -> Any:
        return await self._delivery.deliver_pending(row, callback)

    async def dispatch_request_hooks(self, event: Any, req: Any) -> Any:
        from astrbot.core.pipeline.context_utils import call_event_hook
        from astrbot.core.star.star_handler import EventType

        return await call_event_hook(event, EventType.OnLLMRequestEvent, req)

    async def run(self, quiet: float, probability: float, roll: float) -> tuple[bool, str]:
        p = self.service.plugin
        host = self.service.host
        core = resolve_agent_core(p)
        legacy: dict[str, Any] = {
            "started_ts": time.time(),
            "quiet_minutes": quiet,
            "probability": probability,
            "roll": roll,
            "outcome": "error",
        }
        admins = sorted(host.admin_ids())
        pending_id: int | None = None
        picked = None
        for row in self.service.store.rows(
            "SELECT id,umo FROM resident_runs WHERE status='pending' AND charged=0 ORDER BY id"
        ):
            try:
                platform, typ, target = row["umo"].split(":", 2)
                kind = "group" if typ == "GroupMessage" else "private"
                allowed = (
                    host.proactive_group_allowed(target, "")
                    if kind == "group"
                    else host.proactive_person_allowed(target)
                )
                if allowed:
                    picked = {
                        "umo": row["umo"],
                        "platform_id": platform,
                        "kind": kind,
                        "id": target,
                    }
                    pending_id = int(row["id"])
                    with self.service.store.transaction() as db:
                        db.execute("UPDATE resident_runs SET status='preparing' WHERE id=?", (pending_id,))
                    break
                self.service.store.finish(row["id"], "skipped", "skipped", "恢复时目标已不在允许范围")
            except (ValueError, TypeError):
                logger.debug("[糯小喵] 恢复挂起运行失败：umo=%r，跳过该行", row["umo"])
                continue

        run: Run | None = None
        token = None
        reset_coro = None
        status = "failed"
        outcome = "error"
        summary = ""
        try:
            if picked is None:
                picked = await host.pick_self_wake_session()
            if not admins or picked is None:
                if pending_id is not None:
                    self.service.store.finish(pending_id, "skipped", "skipped", "没有可用会话或未配置管理员")
                legacy.update(ended_ts=time.time(), outcome="skipped")
                host.record_self_wake_run(legacy)
                return True, "skipped"
            if not all((core.cron_event_cls, core.message_session_cls, core.build_main_agent, core.build_config_cls)):
                if pending_id is not None:
                    self.service.store.finish(pending_id, "skipped", "skipped", "当前宿主缺少安全自主运行接口")
                legacy.update(ended_ts=time.time(), outcome="skipped", thinking="当前宿主缺少安全自主运行接口")
                host.record_self_wake_run(legacy)
                return True, "skipped"

            session = core.message_session_cls.from_str(picked["umo"])
            prompt = host.self_wake_prompt(quiet)
            kwargs: dict[str, Any] = {
                "context": p.context,
                "session": session,
                "message": prompt,
                "extras": {SELF_WAKE_EVENT_EXTRA: True},
            }
            if core.message_type_enum is not None:
                kwargs["message_type"] = (
                    core.message_type_enum.GROUP_MESSAGE
                    if picked["kind"] == "group"
                    else core.message_type_enum.FRIEND_MESSAGE
                )
            event = core.cron_event_cls(**kwargs)
            event.role = "member"
            try:
                plugin_set = p.context.get_config(umo=event.unified_msg_origin).get("plugin_set", ["*"])
            except (AttributeError, TypeError):
                plugin_set = ["*"]
            event.plugins_name = None if "*" in plugin_set else list(plugin_set)
            if event.plugins_name is not None and "astrbot_plugin_nuoxiaomiao" not in event.plugins_name:
                if pending_id is not None:
                    self.service.store.finish(pending_id, "skipped", "skipped", "当前会话禁用了糯小喵")
                legacy.update(ended_ts=time.time(), outcome="skipped", thinking="当前会话禁用了糯小喵")
                host.record_self_wake_run(legacy)
                return True, "skipped"
            event.message_obj.sender.user_id = admins[0]
            event.message_obj.sender.nickname = "糯小喵（自主运行）"
            if picked["kind"] == "group":
                event.message_obj.group_id = picked["id"]

            await self.checkpoint(event, "自醒开始前")
            from astrbot.api.provider import ProviderRequest

            req = ProviderRequest()
            req.prompt = prompt
            if core.get_session_conv is not None:
                req.conversation = await core.get_session_conv(event, p.context)
            persona, _ = await self.service.persona.resolve(event, req)
            day = datetime.now().strftime("%Y-%m-%d")
            rid = pending_id or self.service.store.new_run(picked["umo"], persona, day)
            with self.service.store.transaction() as db:
                db.execute(
                    "UPDATE resident_runs SET status='preparing',persona=? WHERE id=?",
                    (persona, rid),
                )
            run = Run(rid, event, persona, request=req, task=asyncio.current_task())
            run.allowed = set(self.service.integration.allowed())
            self.runs[rid] = run
            token = CURRENT_RUN.set(run)
            event.set_extra("nuoxiaomiao_resident_run_id", rid)
            self.service.integration.migrate_selection()
            provider = await host.select_self_wake_provider()
            legacy["provider_id"] = host.provider_id_of(provider)
            if not p.proactive_silent_wake_allow_tools:
                res_tuple = await host.run_self_wake_fallback(legacy, quiet)
                status = "succeeded" if res_tuple[0] else "failed"
                outcome = res_tuple[1]
                return res_tuple
            config = host.build_self_wake_agent_config(event)
            if hasattr(config, "fallback_provider_ids"):
                config.fallback_provider_ids = []
            if hasattr(config, "request_max_retries"):
                config.request_max_retries = 0
            result = await core.build_main_agent(
                event=event,
                plugin_context=p.context,
                config=config,
                provider=provider,
                req=req,
                apply_reset=False,
            )
            if result is None:
                status = "skipped"
                outcome = "skipped"
                return True, "skipped"
            runner = getattr(result, "agent_runner", None)
            run.runner = runner
            req = getattr(result, "provider_request", req)
            run.request = req
            reset_coro = getattr(result, "reset_coro", None)
            if await self.dispatch_request_hooks(event, req):
                status = "skipped"
                outcome = "skipped"
                return True, outcome
            self.service.integration.restrict(event, req)
            if reset_coro is not None:
                await reset_coro
                reset_coro = None
            if not all(hasattr(runner, key) for key in ("provider", "tool_executor", "step", "done")):
                raise RuntimeError("宿主运行器缺少请求/工具执行边界，已停止自醒")
            runner.provider = self.wrap_provider(runner.provider)
            compressor = getattr(getattr(runner, "request_context_manager", None), "compressor", None)
            if compressor is not None and getattr(compressor, "provider", None) is not None:
                compressor.provider = self.wrap_provider(compressor.provider)
            runner.tool_executor = ScopedExecutor(runner.tool_executor, self, run)
            self.restrict_run(run)
            while not runner.done() and run.steps < p.proactive_silent_wake_max_steps:
                await self.checkpoint(event, "Agent 下一步")
                before = run.steps
                async for _ in runner.step():
                    pass
                await self.checkpoint(event, "Agent 步骤完成")
                if run.steps == before and not runner.done():
                    raise RuntimeError("运行器未经过请求边界，已停止空转")
            final = runner.get_final_llm_resp()
            summary = str(getattr(final, "completion_text", "") or "")
            if getattr(final, "role", "") == "err":
                raise RuntimeError(summary or "自醒模型请求失败")
            if str(getattr(getattr(runner, "state", None), "name", "")) == "ERROR":
                raise RuntimeError(summary or "宿主运行器报告失败")
            status = "succeeded" if runner.done() else "limited"
            outcome = "spoke" if run.sent else "quiet"
            return True, outcome
        except BudgetExceeded as exc:
            status = "limited"
            outcome = "spoke" if run and run.sent else "quiet"
            summary = str(exc)
            return True, outcome
        except RunStopped:
            status = "interrupted"
            outcome = "interrupted"
            summary = "运行已停止；已发生的动作保留回执"
            return True, "interrupted"
        except asyncio.CancelledError:
            status = "interrupted"
            outcome = "interrupted"
            summary = "运行已停止；已发生的动作保留回执"
            raise
        except Exception as exc:
            if run and run.limit_reason:
                status = "limited"
                outcome = "spoke" if run.sent else "quiet"
                summary = run.limit_reason
                return True, outcome
            summary = str(exc)
            logger.warning("[糯小喵] 自主运行失败：%s", exc)
            return False, "error"
        finally:
            if reset_coro is not None:
                reset_coro.close()
            if run is not None:
                unknown = self.service.store.rows(
                    "SELECT id FROM resident_actions WHERE run_id=? AND status='unknown'",
                    (run.id,),
                )
                unknown_sends = self.service.store.rows(
                    "SELECT id FROM resident_sends WHERE run_id=? AND status='unknown'",
                    (run.id,),
                )
                if unknown or unknown_sends:
                    status = "unknown"
                elif status == "succeeded" and self.service.store.rows(
                    "SELECT id FROM resident_actions WHERE run_id=? AND status='failed'",
                    (run.id,),
                ):
                    status = "failed"
                self.service.store.finish(run.id, status, outcome, summary)
                legacy.update(
                    ended_ts=time.time(),
                    steps=run.steps,
                    outcome=outcome,
                    thinking=summary,
                    said=bool(run.sent),
                    said_text="\n".join(x.get("text", "") for x in run.sent),
                    sent_to="、".join(x.get("umo", "") for x in run.sent),
                    tool_calls=self.service.store.rows(
                        "SELECT tool AS name,status,result FROM resident_actions WHERE run_id=?",
                        (run.id,),
                    ),
                )
                host.record_self_wake_run(legacy)
                self.runs.pop(run.id, None)
            elif pending_id is not None:
                row_check = self.service.store.rows(
                    "SELECT status FROM resident_runs WHERE id=?", (pending_id,)
                )
                if row_check and row_check[0]["status"] == "preparing":
                    self.service.store.finish(pending_id, "interrupted", "error", summary or "启动前中断")
            if token is not None:
                CURRENT_RUN.reset(token)
