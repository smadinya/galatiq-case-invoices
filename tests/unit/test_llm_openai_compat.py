import json
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from llm.base import ProviderError, StructuredOutputError
from llm.openai_compat import OpenAICompatProvider
from models import CallContext, CriticOutput, Message, ToolCall, ToolSpec, ExtractedInvoice

KEY = "xai-secret-test-key-123"
FORBIDDEN = {"presence_penalty", "frequency_penalty", "stop", "reasoning_effort", "logprobs", "seed"}


def ctx(stage="vp"):
    return CallContext(trace_id="t", file_hash="h" * 64, file_name="f.json", stage=stage, prompt_version="x-v1", call_index=0)


def resp(content=None, tool_calls=None, pt=5, ct=7):
    msg = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=msg)], usage=SimpleNamespace(prompt_tokens=pt, completion_tokens=ct), model="m-x"
    )


def raw_call(id_, name, args):
    return SimpleNamespace(id=id_, function=SimpleNamespace(name=name, arguments=json.dumps(args)))


class Stub:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.requests.append(kw)
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def provider(stub):
    return OpenAICompatProvider(
        api_key=KEY, base_url="https://x/v1", extraction_model="ext-m", reasoning_model="rea-m", client=stub
    )


CRITIC_JSON = json.dumps({"verdict": "AGREE", "proposed_decision": "APPROVE", "objections": []})
TOOL = ToolSpec(
    name="get_stock_level", description="d",
    parameters={"type": "object", "properties": {"item": {"type": "string"}}, "required": ["item"], "additionalProperties": False},
)


def test_extraction_request_shape():
    stub = Stub(resp(ExtractedInvoice().model_dump_json()))
    out = provider(stub).complete(ctx=ctx("extraction"), system="SYS", messages=[Message(role="user", content="hi")], schema=ExtractedInvoice)
    kw = stub.requests[0]
    assert kw["model"] == "ext-m" and kw["temperature"] == 0
    assert kw["messages"] == [{"role": "system", "content": "SYS"}, {"role": "user", "content": "hi"}]
    assert not FORBIDDEN & set(kw)
    rf = kw["response_format"]
    assert rf["type"] == "json_schema" and rf["json_schema"]["strict"] is True
    assert rf["json_schema"]["name"] == "ExtractedInvoice" and "tools" not in kw
    assert isinstance(out.parsed, ExtractedInvoice) and out.provider == "grok" and out.model == "m-x"
    assert (out.usage.tokens_in, out.usage.tokens_out) == (5, 7)


def test_reasoning_stage_has_no_temperature_and_tools_are_strict():
    stub = Stub(resp(CRITIC_JSON))
    provider(stub).complete(ctx=ctx("critic"), system="S", messages=[], schema=CriticOutput, tools=[TOOL])
    kw = stub.requests[0]
    assert kw["model"] == "rea-m" and "temperature" not in kw
    assert not FORBIDDEN & set(kw)
    assert kw["tools"] == [
        {"type": "function", "function": {"name": "get_stock_level", "description": "d", "parameters": TOOL.parameters, "strict": True}}
    ]
    assert "response_format" in kw  # both in one request


def test_tool_calls_parsed_and_messages_mapped():
    stub = Stub(resp(tool_calls=[raw_call("c1", "get_stock_level", {"item": "WidgetA"})]))
    history = [
        Message(role="user", content="u"),
        Message(role="assistant", content=None, tool_calls=[ToolCall(id="p", name="n", arguments={"a": 1})]),
        Message(role="tool", content='{"x":1}', tool_call_id="p"),
    ]
    out = provider(stub).complete(ctx=ctx(), system="S", messages=history, schema=CriticOutput, tools=[TOOL])
    assert out.tool_calls == [ToolCall(id="c1", name="get_stock_level", arguments={"item": "WidgetA"})]
    assert out.parsed is None
    m = stub.requests[0]["messages"]
    assert m[2] == {"role": "assistant", "content": None, "tool_calls": [
        {"id": "p", "type": "function", "function": {"name": "n", "arguments": '{"a": 1}'}}]}
    assert m[3] == {"role": "tool", "tool_call_id": "p", "content": '{"x":1}'}


def test_one_validation_retry_then_success_sums_usage():
    stub = Stub(resp("not json", pt=1, ct=1), resp(CRITIC_JSON, pt=2, ct=3))
    out = provider(stub).complete(ctx=ctx(), system="S", messages=[], schema=CriticOutput)
    assert isinstance(out.parsed, CriticOutput)
    assert len(stub.requests) == 2
    retry = stub.requests[1]["messages"]
    assert retry[-2] == {"role": "assistant", "content": "not json"}
    assert retry[-1]["role"] == "user" and "schema" in retry[-1]["content"]
    assert (out.usage.tokens_in, out.usage.tokens_out) == (3, 4)


def test_two_failures_raise_structured_output_error():
    stub = Stub(resp("bad"), resp('{"verdict": "MAYBE"}'))
    with pytest.raises(StructuredOutputError):
        provider(stub).complete(ctx=ctx(), system="S", messages=[], schema=CriticOutput)
    assert len(stub.requests) == 2


def test_sdk_error_wrapped_without_key_and_repr_hides_key():
    stub = Stub(RuntimeError(f"401 bad key {KEY}"))
    p = provider(stub)
    with pytest.raises(ProviderError) as ei:
        p.complete(ctx=ctx(), system="S", messages=[], schema=CriticOutput)
    assert KEY not in str(ei.value) and "[redacted]" in str(ei.value)
    assert KEY not in repr(p)
    assert KEY not in repr(vars(p))


def test_no_schema_returns_content():
    out = provider(Stub(resp("hello"))).complete(ctx=ctx(), system="S", messages=[])
    assert out.content == "hello" and out.parsed is None
