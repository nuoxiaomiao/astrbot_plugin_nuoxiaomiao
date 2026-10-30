"""Real AstrBot Runner with deterministic providers and no external I/O."""

from types import SimpleNamespace
from astrbot.core.agent.hooks import BaseAgentRunHooks
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool, ToolSet
from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
from astrbot.core.provider.entities import LLMResponse, TokenUsage
from mcp.types import CallToolResult, TextContent


class ScriptedProvider:
    def __init__(self, responses=None):
        self.responses = list(
            responses
            or [
                LLMResponse(
                    role="assistant",
                    completion_text="安静结束",
                    usage=TokenUsage(input_other=10, output=3),
                )
            ]
        )
        self.provider_config = {"id": "resident-test", "max_context_tokens": 100000}
        self.calls = []
        self.model_name = "test"

    def get_model(self):
        return "test"

    def meta(self):
        return SimpleNamespace(id="resident-test", type="test", model="test")

    async def text_chat(self, **kwargs):
        self.calls.append(kwargs)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class CallTool(FunctionTool):
    async def call(self, context, **kwargs):
        return await self.handler(context.context.event, **kwargs)


class TestExecutor:
    async def execute(self, tool, run_context, **kwargs):
        result = await tool.call(run_context, **kwargs)
        if isinstance(result, CallToolResult):
            yield result
        else:
            yield CallToolResult(content=[TextContent(type="text", text=str(result))])


def tool(name, handler, parameters=None):
    return CallTool(
        name=name,
        description=name,
        parameters=parameters or {"type": "object", "properties": {}},
        handler=handler,
    )


async def build_runner(
    *,
    event,
    plugin_context,
    config,
    provider,
    req,
    tools=None,
    hooks=None,
    apply_reset=True,
    native_executor=False,
):
    req.func_tool = tools or ToolSet()
    # Execute normal request plugins just as the native builder does.
    if hooks:
        await hooks(event, req)
    runner = ToolLoopAgentRunner()
    from astrbot.core.astr_agent_tool_exec import FunctionToolExecutor

    reset = runner.reset(
        provider=provider,
        request=req,
        run_context=ContextWrapper(
            context=SimpleNamespace(event=event, context=plugin_context)
        ),
        tool_executor=FunctionToolExecutor() if native_executor else TestExecutor(),
        agent_hooks=BaseAgentRunHooks(),
        streaming=False,
        request_max_retries=0,
        tool_schema_mode=getattr(config, "tool_schema_mode", "full"),
    )
    if apply_reset:
        await reset
    return SimpleNamespace(
        agent_runner=runner,
        provider=provider,
        provider_request=req,
        reset_coro=None if apply_reset else reset,
    )


def conversation():
    return SimpleNamespace(cid="test", persona_id=None, history="[]", token_usage=0)
