"""Run state, BudgetExceeded / RunStopped controls, and BudgetProvider proxy."""

from __future__ import annotations

import asyncio
import contextvars
from dataclasses import dataclass, field
from typing import Any

if __package__ and "." in __package__:
    from .repos.autonomy_repo import BudgetExceeded
else:
    from resident.repos.autonomy_repo import BudgetExceeded

CURRENT_RUN: contextvars.ContextVar[Run | None] = contextvars.ContextVar(
    "nuoxiaomiao_resident_run", default=None
)


class RunStopped(Exception):
    """Internal graceful stop raised by checkpoint() when an operator stops a run."""


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

    def __post_init__(self) -> None:
        self.gate.set()


class BudgetProvider:
    def __init__(self, provider: Any, autonomy: Any, run: Run) -> None:
        self.provider = provider
        self.autonomy = autonomy
        self.run = run

    def __getattr__(self, name: str) -> Any:
        return getattr(self.provider, name)

    async def text_chat(self, *args: Any, **kwargs: Any) -> Any:
        try:
            await self.autonomy.before_request(self.run)
        except BudgetExceeded as exc:
            self.run.limit_reason = str(exc)
            raise
        if "func_tool" in kwargs and self.run.request is not None:
            kwargs["func_tool"] = self.autonomy.filter_payload(self.run, kwargs["func_tool"])
        response = None
        invoked = False
        try:
            invoked = True
            response = await self.provider.text_chat(*args, **kwargs)
            self.autonomy.service.store.tool_requests(self.run.id, response)
            return response
        finally:
            if invoked:
                self.autonomy.record_usage(self.run, getattr(response, "usage", None))

    async def text_chat_stream(self, *args: Any, **kwargs: Any):
        try:
            await self.autonomy.before_request(self.run)
        except BudgetExceeded as exc:
            self.run.limit_reason = str(exc)
            raise
        if "func_tool" in kwargs and self.run.request is not None:
            kwargs["func_tool"] = self.autonomy.filter_payload(self.run, kwargs["func_tool"])
        usage = None
        invoked = False
        try:
            invoked = True
            async for response in self.provider.text_chat_stream(*args, **kwargs):
                if getattr(response, "usage", None) is not None:
                    usage = response.usage
                if getattr(response, "tools_call_name", None):
                    self.autonomy.service.store.tool_requests(self.run.id, response)
                yield response
        finally:
            if invoked:
                self.autonomy.record_usage(self.run, usage)
