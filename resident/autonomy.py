"""Bounded native Agent turns, per-run execution gates, and durable receipts."""

from __future__ import annotations

import asyncio
import contextvars
import importlib
import json
import time
from dataclasses import dataclass, field
from datetime import datetime

from .integration import is_dispatcher

CURRENT_RUN = contextvars.ContextVar("nuoxiaomiao_resident_run", default=None)


class BudgetExceeded(RuntimeError):
    pass


@dataclass
class Run:
    id: int
    event: object
    persona: str
    request: object = None
    runner: object = None
    status: str = "running"
    steps: int = 0
    gate: asyncio.Event = field(default_factory=asyncio.Event)
    sent: list = field(default_factory=list)
    task: object = None
    allowed: set | None = None
    effective: set | None = None
    request_day: str = ""
    limit_reason: str = ""

    def __post_init__(self):
        self.gate.set()


class BudgetProvider:
    def __init__(self, provider, autonomy, run):
        self.provider, self.autonomy, self.run = provider, autonomy, run

    def __getattr__(self, name):
        return getattr(self.provider, name)

    async def text_chat(self, *args, **kwargs):
        try:
            await self.autonomy.before_request(self.run)
        except BudgetExceeded as exc:
            self.run.limit_reason = str(exc)
            raise
        if "func_tool" in kwargs and self.run.request is not None:
            kwargs["func_tool"] = self.autonomy.filter_payload(
                self.run, kwargs["func_tool"]
            )
        response = None
        invoked = False
        try:
            invoked = True
            response = await self.provider.text_chat(*args, **kwargs)
            self.autonomy.service.store.tool_requests(self.run.id, response)
            return response
        finally:
            # before_request 被 BudgetExceeded 短路时模型根本没被调用，
            # 不要记 usage_unknown（审查 H10）。
            if invoked:
                self.autonomy.record_usage(self.run, getattr(response, "usage", None))

    async def text_chat_stream(self, *args, **kwargs):
        try:
            await self.autonomy.before_request(self.run)
        except BudgetExceeded as exc:
            self.run.limit_reason = str(exc)
            raise
        if "func_tool" in kwargs and self.run.request is not None:
            kwargs["func_tool"] = self.autonomy.filter_payload(
                self.run, kwargs["func_tool"]
            )
        usage = None
        invoked = False
        try:
            invoked = True
            async for response in self.provider.text_chat_stream(*args, **kwargs):
                if getattr(response, "usage", None) is not None:
                    usage = response.usage
                yield response
        finally:
            # before_request 被 BudgetExceeded 短路时模型根本没被调用，
            # 不要记 usage_unknown（审查 H10）。
            if invoked:
                self.autonomy.record_usage(self.run, usage)


class ScopedExecutor:
    def __init__(self, original, autonomy, run):
        self.original, self.autonomy, self.run = original, autonomy, run

    def __getattr__(self, name):
        return getattr(self.original, name)

    async def execute(self, tool, run_context, **args):
        from mcp.types import CallToolResult, TextContent

        await self.autonomy.checkpoint(self.run.event, "工具执行前")
        name = str(getattr(tool, "name", ""))
        self.autonomy.restrict_run(self.run)
        current = getattr(self.run.request, "func_tool", None)
        raw = getattr(self.run.runner, "_skill_like_raw_tool_set", None) or current
        permitted = (
            name in (self.run.effective or set())
            and any(
                getattr(t, "name", "") == getattr(tool, "name", "")
                for t in getattr(raw, "tools", []) or []
            )
            and getattr(tool, "active", True)
            and not is_dispatcher(tool)
        )
        store = self.autonomy.service.store
        aid = store.action(self.run.id, name, args)
        if not permitted:
            store.end_action(aid, "rejected", "该工具不在当前会话允许的自主工具范围内")
            yield CallToolResult(
                isError=True,
                content=[
                    TextContent(
                        type="text", text="该工具不在当前会话允许的自主工具范围内"
                    )
                ],
            )
            return
        chunks = []
        error = False
        try:
            async for result in self.original.execute(tool, run_context, **args):
                if result is not None:
                    error = error or bool(getattr(result, "isError", False))
                    chunks.extend(
                        str(getattr(c, "text", ""))
                        for c in getattr(result, "content", []) or []
                    )
                yield result
            text = "\n".join(chunks)
            # A tool may return a JSON error using the ordinary text channel.
            try:
                payload = json.loads(text)
                error = error or (
                    isinstance(payload, dict) and bool(payload.get("error"))
                )
            except (ValueError, TypeError):
                pass
            store.end_action(aid, "failed" if error else "succeeded", text)
        except asyncio.CancelledError:
            store.end_action(aid, "unknown", "调用被中断，外部动作结果尚无法确认")
            raise
        except BaseException as exc:
            store.end_action(aid, "unknown", str(exc))
            raise


class Autonomy:
    def __init__(self, service):
        self.service = service
        self.runs = {}
        self.delivery_locks = {}

    def cancel_pending(self, run_id=None, umo=None):
        rows = self.service.store.rows(
            "SELECT p.id FROM proactive_pending p JOIN resident_deferred d ON d.pending_id=p.id JOIN resident_runs r ON r.id=d.run_id WHERE p.status='pending' AND (? IS NULL OR r.id=?) AND (? IS NULL OR r.umo=?)",
            (run_id, run_id, umo, umo),
        )
        for row in rows:
            p = self.service.plugin
            task = p._proactive_tasks.pop(row["id"], None)
            if task:
                task.cancel()
            p._proactive_pending.pop(row["id"], None)
            p._update_proactive_row(row["id"], status="skipped", done_ts=time.time())
        return bool(rows)

    async def control(self, run_id, action):
        run = self.runs.get(run_id)
        if not run and action == "stop" and self.cancel_pending(run_id=run_id):
            self.service.store.finish(
                run_id, "interrupted", "interrupted", "管理员取消了尚未执行的顺延发送"
            )
            return
        if not run:
            raise ValueError("该运行已经结束或断点已失效，不能重放旧动作")
        if action not in ("pause", "resume", "stop"):
            raise ValueError("未知运行操作")
        if action == "stop":
            self.cancel_pending(run_id=run_id)
        run.status = {"pause": "paused", "resume": "running", "stop": "stopped"}[action]
        if action == "pause":
            run.gate.clear()
        else:
            run.gate.set()
        with self.service.store.transaction() as db:
            db.execute(
                "UPDATE resident_runs SET status=? WHERE id=?", (run.status, run_id)
            )

    async def checkpoint(self, event, phase):
        p = self.service.plugin
        run = CURRENT_RUN.get()
        while True:
            if not p.enabled or (
                run
                and (
                    run.status == "stopped"
                    or not p.proactive_silent_wake_enabled
                    or not p.proactive_enabled
                )
            ):
                raise asyncio.CancelledError()
            if run is None or run.status != "paused":
                break
            # Poll also handles plugin reloads or control events received before run.
            await asyncio.sleep(0.2)

    def restrict_run(self, run):
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

    def filter_payload(self, run, toolset):
        from astrbot.core.agent.tool import ToolSet

        return ToolSet(
            tools=[
                t
                for t in getattr(toolset, "tools", []) or []
                if t.name in (run.effective or set()) and getattr(t, "active", True)
            ]
        )

    async def before_request(self, run):
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
            if row["usage_unknown"] or (
                p.resident_daily_token_limit and daily["unknown"]
            ):
                raise BudgetExceeded("模型未报告完整用量，已停止后续请求")
        if (
            p.resident_run_token_limit
            and row["input_other"] + row["output"] >= p.resident_run_token_limit
        ):
            raise BudgetExceeded("本轮 token 阈值已达到")
        if (
            p.resident_daily_token_limit
            and daily["tokens"] >= p.resident_daily_token_limit
        ):
            raise BudgetExceeded("今日 token 阈值已达到")
        store.charge(run.id, day, p.proactive_silent_wake_daily_budget)
        if run.request is not None:
            self.restrict_run(run)
        with store.transaction() as db:
            db.execute(
                "UPDATE resident_runs SET requests=requests+1 WHERE id=?", (run.id,)
            )
        run.request_day = day
        run.steps += 1

    def record_usage(self, run, usage):
        if usage is None:
            self.service.store.usage(run.id, day=run.request_day or None)
        else:
            vals = tuple(
                max(0, int(getattr(usage, k, 0) or 0))
                for k in ("input_other", "input_cached", "output")
            )
            self.service.store.usage(run.id, vals, day=run.request_day or None)

    def wrap_provider(self, provider):
        run = CURRENT_RUN.get()
        if run is None or isinstance(provider, BudgetProvider):
            return provider
        return BudgetProvider(provider, self, run)

    async def close(self):
        tasks = []
        for run in list(self.runs.values()):
            run.status = "stopped"
            run.gate.set()
            if run.task and run.task is not asyncio.current_task():
                run.task.cancel()
                tasks.append(run.task)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def deliver_pending(self, row, callback):
        store = self.service.store
        owners = store.rows(
            "SELECT run_id FROM resident_deferred WHERE pending_id=?", (row["id"],)
        )
        if not owners:
            return await callback(row)
        rid = owners[0]["run_id"]
        async with self.delivery_locks.setdefault(rid, asyncio.Lock()):
            while rid in self.runs:
                await asyncio.sleep(0.2)
            saved = store.rows("SELECT * FROM resident_runs WHERE id=?", (rid,))[0]
            if saved["status"] not in ("succeeded", "limited"):
                self.service.plugin._update_proactive_row(
                    row["id"], status="skipped", done_ts=time.time()
                )
                return
            event = self.service.web_event(saved["umo"])
            run = Run(
                rid,
                event,
                saved["persona"],
                steps=saved["requests"],
                task=asyncio.current_task(),
            )
            self.runs[rid] = run
            token = CURRENT_RUN.set(run)
            status = saved["status"]
            detail = saved["summary"]
            try:
                await self.checkpoint(event, "静默顺延发送恢复")
                await callback(row)
            except BudgetExceeded as exc:
                status = "limited"
                detail = str(exc)
                self.service.plugin._update_proactive_row(
                    row["id"], status="skipped", done_ts=time.time()
                )
            except asyncio.CancelledError:
                status = "interrupted"
                self.service.plugin._update_proactive_row(
                    row["id"], status="skipped", done_ts=time.time()
                )
                raise
            finally:
                if store.rows(
                    "SELECT id FROM resident_sends WHERE run_id=? AND status='unknown'",
                    (rid,),
                ):
                    status = "unknown"
                store.finish(
                    rid, status, "spoke" if run.sent else saved["outcome"], detail
                )
                self.runs.pop(rid, None)
                CURRENT_RUN.reset(token)
        self.delivery_locks.pop(rid, None)

    async def dispatch_request_hooks(self, event, req):
        from astrbot.core.pipeline.context_utils import call_event_hook
        from astrbot.core.star.star_handler import EventType

        return await call_event_hook(event, EventType.OnLLMRequestEvent, req)

    async def run(self, quiet, probability, roll):
        p = self.service.plugin
        core = importlib.import_module(p.__class__.__module__)
        legacy = {
            "started_ts": time.time(),
            "quiet_minutes": quiet,
            "probability": probability,
            "roll": roll,
            "outcome": "error",
        }
        admins = sorted(p._admin_ids())
        pending_id = None
        picked = None
        for row in self.service.store.rows(
            "SELECT id,umo FROM resident_runs WHERE status='pending' AND charged=0 ORDER BY id"
        ):
            try:
                platform, typ, target = row["umo"].split(":", 2)
                kind = "group" if typ == "GroupMessage" else "private"
                allowed = (
                    p._proactive_group_allowed(target, "")
                    if kind == "group"
                    else p._proactive_person_allowed(target)
                )
                if allowed:
                    picked = {
                        "umo": row["umo"],
                        "platform_id": platform,
                        "kind": kind,
                        "id": target,
                    }
                    pending_id = row["id"]
                    with self.service.store.transaction() as db:
                        db.execute(
                            "UPDATE resident_runs SET status='preparing' WHERE id=?",
                            (pending_id,),
                        )
                    break
                self.service.store.finish(
                    row["id"], "skipped", "skipped", "恢复时目标已不在允许范围"
                )
            except (ValueError, TypeError):
                # umo 格式损坏的挂起行不能中断整个恢复循环，跳过继续（审查 H9）。
                core.logger.debug(
                    "[糯小喵] 恢复挂起运行失败：umo=%r，跳过该行", row["umo"]
                )
                continue
        if picked is None:
            picked = await p._pick_self_wake_session()
        if not admins or picked is None:
            legacy.update(ended_ts=time.time(), outcome="skipped")
            p._record_self_wake_run(legacy)
            return True, "skipped"
        if not all(
            getattr(core, key, None)
            for key in (
                "_CronMessageEvent",
                "_MessageSession",
                "_build_main_agent",
                "_MainAgentBuildConfig",
            )
        ):
            # Old hosts retain chat features; executing unscoped autonomous tools
            # would silently enlarge permissions, so don't fall back to them.
            legacy.update(
                ended_ts=time.time(),
                outcome="skipped",
                thinking="当前宿主缺少安全自主运行接口",
            )
            p._record_self_wake_run(legacy)
            return True, "skipped"
        session = core._MessageSession.from_str(picked["umo"])
        prompt = p._self_wake_prompt(quiet)
        kwargs = {
            "context": p.context,
            "session": session,
            "message": prompt,
            "extras": {core.SELF_WAKE_EVENT_EXTRA: True},
        }
        if core._MessageType is not None:
            kwargs["message_type"] = (
                core._MessageType.GROUP_MESSAGE
                if picked["kind"] == "group"
                else core._MessageType.FRIEND_MESSAGE
            )
        event = core._CronMessageEvent(**kwargs)
        # 不要伪装成管理员：插件自己的权限检查靠 SELF_WAKE_EVENT_EXTRA 标记
        # 与 sender=admins[0]（落在 _admin_ids() 内）判定，角色保持普通成员可
        # 避免其他插件/宿主把自主轮次当作真人管理员对待（审查 C4/H1）。
        event.role = "member"
        try:
            plugin_set = p.context.get_config(umo=event.unified_msg_origin).get(
                "plugin_set", ["*"]
            )
        except (AttributeError, TypeError):
            plugin_set = ["*"]
        event.plugins_name = None if "*" in plugin_set else list(plugin_set)
        if event.plugins_name is not None and not any(
            name in event.plugins_name
            for name in ("astrbot_plugin_nuoxiaomiao",)
        ):
            legacy.update(
                ended_ts=time.time(), outcome="skipped", thinking="当前会话禁用了糯小喵"
            )
            p._record_self_wake_run(legacy)
            return True, "skipped"
        event.message_obj.sender.user_id = admins[0]
        event.message_obj.sender.nickname = "糯小喵（自主运行）"
        if picked["kind"] == "group":
            event.message_obj.group_id = picked["id"]
        run = None
        token = None
        result = None
        reset_coro = None
        status = "failed"
        outcome = "error"
        summary = ""
        try:
            await self.checkpoint(event, "自醒开始前")
            from astrbot.api.provider import ProviderRequest

            req = ProviderRequest()
            req.prompt = prompt
            req.conversation = await core._get_session_conv(event, p.context)
            persona, _ = await self.service.persona.resolve(event, req)
            rid = pending_id or self.service.store.new_run(
                picked["umo"], persona, datetime.now().strftime("%Y-%m-%d")
            )
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
            provider = await p._select_self_wake_provider()
            legacy["provider_id"] = p._self_wake_provider_id(provider)
            if not p.proactive_silent_wake_allow_tools:
                # The legacy decision path still uses the same request gate and ledger.
                # No global provider mutation: _ask_heartbeat_llm wraps via CURRENT_RUN.
                result = await p._run_self_wake_fallback(legacy, quiet)
                status = "succeeded" if result[0] else "failed"
                outcome = result[1]
                return result
            config = p._build_self_wake_agent_config(event)
            config.fallback_provider_ids = []
            config.request_max_retries = 0
            result = await core._build_main_agent(
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
                return True, outcome
            legacy["provider_id"] = p._self_wake_provider_id(
                getattr(result, "provider", provider)
            )
            runner = result.agent_runner
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
            if not all(
                hasattr(runner, key)
                for key in ("provider", "tool_executor", "step", "done")
            ):
                raise RuntimeError("宿主运行器缺少请求/工具执行边界，已停止自醒")
            runner.provider = self.wrap_provider(runner.provider)
            # Compression uses a separate provider owned by the context manager.
            compressor = getattr(
                getattr(runner, "request_context_manager", None), "compressor", None
            )
            if (
                compressor is not None
                and getattr(compressor, "provider", None) is not None
            ):
                compressor.provider = self.wrap_provider(compressor.provider)
            runner.tool_executor = ScopedExecutor(runner.tool_executor, self, run)
            self.restrict_run(run)
            # Don't use step_until_done: it starts an extra unbudgeted final request.
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
        except asyncio.CancelledError:
            status = "interrupted"
            outcome = "interrupted"
            summary = "运行已停止；已发生的动作保留回执"
            if asyncio.current_task().cancelling():
                raise
            return True, "interrupted"
        except Exception as exc:
            if run and run.limit_reason:
                status = "limited"
                outcome = "spoke" if run.sent else "quiet"
                summary = run.limit_reason
                return True, outcome
            summary = str(exc)
            core.logger.warning("[糯小喵] 自主运行失败：%s", exc)
            return False, "error"
        finally:
            if reset_coro is not None:
                reset_coro.close()
            if run:
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
                p._record_self_wake_run(legacy)
                self.runs.pop(run.id, None)
            if token is not None:
                CURRENT_RUN.reset(token)
