"""Scoped tool executor recording durable receipts and enforcing allowed tools."""

from __future__ import annotations

import asyncio
import json
from typing import Any

if __package__ and "." in __package__:
    from .autonomy_budget import Run, RunStopped
    from .integration import is_dispatcher
else:
    from resident.autonomy_budget import Run, RunStopped
    from resident.integration import is_dispatcher


class ScopedExecutor:
    def __init__(self, original: Any, autonomy: Any, run: Run) -> None:
        self.original = original
        self.autonomy = autonomy
        self.run = run

    def __getattr__(self, name: str) -> Any:
        return getattr(self.original, name)

    async def execute(self, tool: Any, run_context: Any, **args: Any):
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
                content=[TextContent(type="text", text="该工具不在当前会话允许的自主工具范围内")],
            )
            return
        chunks: list[str] = []
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
            text = "\n".join(c for c in chunks if c)
            try:
                payload = json.loads(text)
                error = error or (isinstance(payload, dict) and bool(payload.get("error")))
            except (ValueError, TypeError):
                pass
            store.end_action(aid, "failed" if error else "succeeded", text)
        except RunStopped:
            store.end_action(aid, "rejected", "运行在执行前已停止，未触发外部动作")
            raise
        except asyncio.CancelledError:
            store.end_action(aid, "unknown", "调用被中断，外部动作结果尚无法确认")
            raise
        except BaseException as exc:
            store.end_action(aid, "unknown", str(exc))
            raise
