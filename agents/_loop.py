"""Shared structured-output call and tool-calling loop for extraction, the VP and the Critic."""

import json
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

from pydantic import BaseModel

from llm.base import StructuredOutputError
from models import BoundTool, CallContext, CallUsage, Completion, Message, Provider, ToolTrace
from obs import EventLogger

T = TypeVar("T", bound=BaseModel)

FINAL_NUDGE = "Tool budget used up. Give your final answer now."


@dataclass
class LoopResult(Generic[T]):
    output: T
    tool_traces: list[ToolTrace] = field(default_factory=list)
    calls: int = 0
    usages: list[CallUsage] = field(default_factory=list)


def _call(
    provider: Provider, ctx: CallContext, system: str, messages: list[Message], schema: type[T], tools, log: EventLogger
) -> Completion:
    completion = provider.complete(ctx=ctx, system=system, messages=messages, schema=schema, tools=tools)
    log.event(
        "llm_call",
        trace_id=ctx.trace_id,
        node=ctx.stage,
        invoice=ctx.file_name,
        duration_ms=completion.usage.latency_ms,
        tokens_in=completion.usage.tokens_in,
        tokens_out=completion.usage.tokens_out,
        provider=provider.name,
        data={"stage": ctx.stage, "call_index": ctx.call_index, "tool_calls": len(completion.tool_calls)},
    )
    return completion


def _final(completion: Completion, schema: type[T], ctx: CallContext) -> T:
    if completion.tool_calls or not isinstance(completion.parsed, schema):
        raise StructuredOutputError(
            f"{ctx.stage}#{ctx.call_index}: model returned neither a {schema.__name__} nor usable output"
        )
    return completion.parsed


def run_structured(
    *, provider: Provider, ctx: CallContext, system: str, messages: list[Message], schema: type[T], log: EventLogger
) -> tuple[T, CallUsage]:
    completion = _call(provider, ctx, system, messages, schema, None, log)
    return _final(completion, schema, ctx), completion.usage


def run_tool_loop(
    *,
    provider: Provider,
    ctx: CallContext,
    system: str,
    messages: list[Message],
    tools: list[BoundTool],
    schema: type[T],
    max_tool_calls: int,
    log: EventLogger,
) -> LoopResult[T]:
    by_name = {t.spec.name: t for t in tools}
    specs = [t.spec for t in tools]
    msgs = list(messages)
    traces: list[ToolTrace] = []
    usages: list[CallUsage] = []
    executed = 0
    calls = 0
    offer: list | None = specs or None

    while True:
        step_ctx = ctx.model_copy(update={"call_index": ctx.call_index + calls})
        completion = _call(provider, step_ctx, system, msgs, schema, offer, log)
        calls += 1
        usages.append(completion.usage)
        if not completion.tool_calls:
            return LoopResult(_final(completion, schema, step_ctx), traces, calls, usages)
        if offer is None:  # forced final call still asked for tools
            raise StructuredOutputError(f"{ctx.stage}: model kept calling tools after the budget was used up")
        msgs.append(Message(role="assistant", content=completion.content, tool_calls=completion.tool_calls))
        for tc in completion.tool_calls:
            if executed >= max_tool_calls:
                result: dict[str, Any] = {"error": "tool budget exhausted"}
            else:
                executed += 1
                tool = by_name.get(tc.name)
                if tool is None:
                    result = {"error": f"unknown tool {tc.name!r}"}
                else:
                    try:
                        result = tool.fn(**tc.arguments)
                        if not isinstance(result, dict):
                            result = {"result": result}
                    except Exception as exc:  # tool failures are results, not crashes
                        result = {"error": f"{type(exc).__name__}: {exc}"}
            text = json.dumps(result, default=str, sort_keys=True)
            traces.append(ToolTrace(call_id=tc.id, name=tc.name, arguments=tc.arguments, result=result))
            msgs.append(Message(role="tool", content=text, tool_call_id=tc.id))
            log.event(
                "tool_call",
                trace_id=ctx.trace_id,
                node=ctx.stage,
                invoice=ctx.file_name,
                data={"tool": tc.name, "arguments": tc.arguments, "result": text[:300]},
            )
        if executed >= max_tool_calls:
            msgs.append(Message(role="user", content=FINAL_NUDGE))
            offer = None
