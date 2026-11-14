import pytest

from agents._loop import FINAL_NUDGE, run_structured, run_tool_loop
from llm.base import ProviderError, StructuredOutputError
from llm.fake import ScriptedFake, critic, memo, tool_call, vp
from models import BoundTool, Ceiling, Message, CallContext, CallUsage, Completion, CriticOutput, ToolCall, ToolSpec, Verdict, VPOutput
from obs import MemoryLogger


def spec(name):
    return ToolSpec(name=name, description="d", parameters={"type": "object", "properties": {"item": {"type": "string"}}, "required": ["item"], "additionalProperties": False})


def stock(item):
    return {"item": item, "stock": 90}


def boom(item):
    raise ValueError("kaput")


TOOLS = [BoundTool(spec("get_stock_level"), stock), BoundTool(spec("get_budget"), lambda item: {"left": 1}), BoundTool(spec("boom"), boom)]


def ctx(i=0, stage="vp"):
    return CallContext(trace_id="tr", file_hash="h", file_name="a.json", stage=stage, prompt_version="vp-v1", call_index=i)


def run(steps, *, i=0, max_calls=6, tools=TOOLS, log=None):
    fake = ScriptedFake({("a.json", "vp"): steps})
    log = log or MemoryLogger()
    res = run_tool_loop(provider=fake, ctx=ctx(i), system="SYS", messages=[Message(role="user", content="go")],
                        tools=tools, schema=VPOutput, max_tool_calls=max_calls, log=log)
    return fake, res, log


FINAL = vp(Ceiling.APPROVE, "done")


def test_two_tools_then_answer():
    fake, res, log = run([tool_call("c1", "get_stock_level", item="WidgetA"), tool_call("c2", "get_budget", item="x"), FINAL])
    assert res.output is FINAL and res.calls == 3 and len(res.usages) == 3
    assert [t.name for t in res.tool_traces] == ["get_stock_level", "get_budget"]
    assert res.tool_traces[0].result == {"item": "WidgetA", "stock": 90}
    assert [c.ctx.call_index for c in fake.calls] == [0, 1, 2]
    # transcript passed to the 3rd call
    msgs = fake.calls[2].messages
    assert [m.role for m in msgs] == ["user", "assistant", "tool", "assistant", "tool"]
    assert msgs[2].content == '{"item": "WidgetA", "stock": 90}' and msgs[2].tool_call_id == "c1"
    assert fake.calls[0].tool_names == ["get_stock_level", "get_budget", "boom"]
    assert len(log.of("llm_call")) == 3 and len(log.of("tool_call")) == 2
    assert all(e["trace_id"] == "tr" for e in log.events)
    assert log.of("tool_call")[0]["data"]["tool"] == "get_stock_level"


def test_call_index_offset():
    steps = [None] * 4 + [tool_call("c", "get_stock_level", item="a"), FINAL]
    fake = ScriptedFake({("a.json", "vp"): steps})
    res = run_tool_loop(provider=fake, ctx=ctx(4), system="S", messages=[], tools=TOOLS, schema=VPOutput, max_tool_calls=6, log=MemoryLogger())
    assert [c.ctx.call_index for c in fake.calls] == [4, 5] and res.calls == 2


def test_unknown_tool_and_raising_tool_become_results():
    fake = ScriptedFake({("a.json", "vp"): [
        [ToolCall(id="c1", name="get_stock_level", arguments={"bogus": 1})],  # invalid args -> TypeError
        [ToolCall(id="c2", name="boom", arguments={"item": "a"})],
        FINAL]})
    res = run_tool_loop(provider=fake, ctx=ctx(), system="S", messages=[], tools=TOOLS, schema=VPOutput, max_tool_calls=6, log=MemoryLogger())
    assert "TypeError" in res.tool_traces[0].result["error"]
    assert res.tool_traces[1].result == {"error": "ValueError: kaput"}
    assert res.output is FINAL


def test_unknown_tool_name_result():
    class Asks:
        name = "x"
        n = 0

        def complete(self, **kw):
            self.n += 1
            if self.n == 1:
                return Completion(tool_calls=[ToolCall(id="c", name="nope", arguments={})])
            return Completion(parsed=FINAL)

    res = run_tool_loop(provider=Asks(), ctx=ctx(), system="S", messages=[], tools=TOOLS, schema=VPOutput, max_tool_calls=6, log=MemoryLogger())
    assert res.tool_traces[0].result == {"error": "unknown tool 'nope'"}


def test_budget_cap_and_forced_final_call():
    steps = [tool_call(f"c{i}", "get_stock_level", item="a") for i in range(6)] + [FINAL]
    fake, res, _ = run(steps, max_calls=6)
    assert res.calls == 7 and len(res.tool_traces) == 6
    last = fake.calls[-1]
    assert last.tool_names == [] and last.messages[-1].role == "user" and last.messages[-1].content == FINAL_NUDGE
    assert all(c.tool_names for c in fake.calls[:-1])


def test_remaining_calls_in_response_get_budget_error():
    many = [ToolCall(id=f"c{i}", name="get_stock_level", arguments={"item": "a"}) for i in range(4)]
    fake = ScriptedFake({("a.json", "vp"): [many, FINAL]})
    res = run_tool_loop(provider=fake, ctx=ctx(), system="S", messages=[], tools=TOOLS, schema=VPOutput, max_tool_calls=2, log=MemoryLogger())
    assert [t.result.get("error") for t in res.tool_traces] == [None, None, "tool budget exhausted", "tool budget exhausted"]
    assert fake.calls[1].tool_names == [] and res.calls == 2


def test_bad_response_raises():
    class Empty:
        name = "x"

        def complete(self, **kw):
            return Completion()

    with pytest.raises(StructuredOutputError):
        run_tool_loop(provider=Empty(), ctx=ctx(), system="S", messages=[], tools=TOOLS, schema=VPOutput, max_tool_calls=6, log=MemoryLogger())


def test_tools_requested_after_budget_raises():
    class Greedy:
        name = "x"

        def complete(self, **kw):
            return Completion(tool_calls=[ToolCall(id="c", name="get_stock_level", arguments={"item": "a"})])

    with pytest.raises(StructuredOutputError, match="budget"):
        run_tool_loop(provider=Greedy(), ctx=ctx(), system="S", messages=[], tools=TOOLS, schema=VPOutput, max_tool_calls=1, log=MemoryLogger())


def test_run_structured():
    fake = ScriptedFake({("a.json", "vp"): [FINAL]})
    out, usage = run_structured(provider=fake, ctx=ctx(), system="S", messages=[], schema=VPOutput, log=MemoryLogger())
    assert out is FINAL and isinstance(usage, CallUsage) and fake.calls[0].tool_names == []


def test_provider_error_propagates():
    fake = ScriptedFake({("a.json", "vp"): [ProviderError("down")]})
    with pytest.raises(ProviderError):
        run_structured(provider=fake, ctx=ctx(), system="S", messages=[], schema=VPOutput, log=MemoryLogger())
