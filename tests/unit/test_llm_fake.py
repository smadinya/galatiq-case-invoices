import pytest

from llm.base import ScriptError
from llm.fake import ScriptedFake, critic, memo, tool_call, vp
from llm.fake_golden import GOLDEN_EXTRACTIONS, golden_script
from models import Ceiling, CallContext, CriticOutput, FlagCode, ToolSpec, Verdict, VPMemoOutput, VPOutput

TOOL = ToolSpec(name="t", description="d", parameters={"type": "object", "properties": {}, "required": [], "additionalProperties": False})


def ctx(file="a.json", stage="vp", i=0):
    return CallContext(trace_id="t", file_hash="h", file_name=file, stage=stage, prompt_version="vp-v1", call_index=i)


def call(fake, c, schema=VPOutput, tools=None):
    return fake.complete(ctx=c, system="S", messages=[], schema=schema, tools=tools)


def test_exact_index_lookup_and_recording():
    one, two = vp(Ceiling.APPROVE, "one"), vp(Ceiling.REJECT, "two")
    fake = ScriptedFake({("a.json", "vp"): [one, two]})
    assert call(fake, ctx(i=1)).parsed is two
    out = call(fake, ctx(i=0))
    assert out.parsed is one and out.provider == "fake" and out.model == "scripted"
    assert VPOutput.model_validate_json(out.content) == one
    assert [c.ctx.call_index for c in fake.calls] == [1, 0]
    assert fake.calls[0].schema_name == "VPOutput" and fake.calls[0].tool_names == []
    assert len(fake.calls_for("a.json")) == 2 and fake.calls_for("a.json", "critic") == []


def test_fallback_by_stage():
    fake = ScriptedFake({("a.json", "vp"): [vp(Ceiling.APPROVE, "x")]}, fallback={"vp": vp(Ceiling.REJECT, "fb")})
    assert call(fake, ctx(i=5)).parsed.reasoning == "fb"
    assert call(fake, ctx(file="zzz.json")).parsed.reasoning == "fb"
    with pytest.raises(ScriptError):
        call(fake, ctx(stage="critic"), CriticOutput)


def test_no_step_raises():
    with pytest.raises(ScriptError, match="no scripted step for a.json vp#0"):
        call(ScriptedFake(), ctx())
    fake = ScriptedFake({("a.json", "vp"): [vp(Ceiling.APPROVE, "x")]})
    with pytest.raises(ScriptError, match="vp#1"):
        call(fake, ctx(i=1))


def test_exception_step():
    fake = ScriptedFake({("a.json", "vp"): [RuntimeError("boom")]})
    with pytest.raises(RuntimeError, match="boom"):
        call(fake, ctx())


def test_tool_call_steps():
    fake = ScriptedFake({("a.json", "vp"): [tool_call("c", "t", x=1), tool_call("c", "other")]})
    out = call(fake, ctx(), tools=[TOOL])
    assert out.tool_calls[0].name == "t" and out.tool_calls[0].arguments == {"x": 1} and out.parsed is None
    with pytest.raises(ScriptError, match="not offered"):
        call(fake, ctx(i=1), tools=[TOOL])
    with pytest.raises(ScriptError, match="no tools"):
        call(ScriptedFake({("a.json", "vp"): [tool_call("c", "t")]}), ctx())


def test_model_step_type_checks():
    fake = ScriptedFake({("a.json", "vp"): [memo("m"), vp(Ceiling.APPROVE, "x")]})
    with pytest.raises(ScriptError, match="does not match schema"):
        call(fake, ctx(i=0))
    with pytest.raises(ScriptError, match="no schema"):
        call(fake, ctx(i=1), schema=None)
    assert isinstance(call(ScriptedFake({("a.json", "vp"): [memo("m")]}), ctx(), VPMemoOutput).parsed, VPMemoOutput)


def test_helpers_build_models():
    out = vp(Ceiling.APPROVE, "r", flags=[(FlagCode.MATH_MISMATCH, "ok")], answers=[("o", "a")])
    assert out.flags_addressed[0].code == FlagCode.MATH_MISMATCH and out.objections_answered[0].answer == "a"
    cr = critic(Verdict.OBJECT, Ceiling.REJECT, objections=[("about", "ev", "chg")])
    assert cr.objections[0].requested_change == "chg"


def test_golden_script_shape():
    assert len(GOLDEN_EXTRACTIONS) == 10
    script = golden_script()
    for name in GOLDEN_EXTRACTIONS:
        assert (name, "extraction") in script
    for key in [
        ("invoice_1004_revised.json", "vp"), ("invoice_1004_revised.json", "critic"),
        ("invoice_1005.json", "vp"), ("invoice_1005.json", "critic"),
        ("invoice_1007.csv", "vp"), ("invoice_1007.csv", "critic"),
        ("invoice_1010.txt", "vp"), ("invoice_1010.txt", "critic"),
        ("invoice_1012.pdf", "vp"), ("invoice_1012.pdf", "critic"),
        ("invoice_1014.xml", "vp"), ("invoice_1014.xml", "critic"),
        ("invoice_1002.txt", "vp_memo"), ("invoice_1013.json", "vp_memo"),
    ]:
        assert key in script, key
    assert len(script[("invoice_1010.txt", "vp")]) == 2 and len(script[("invoice_1010.txt", "critic")]) == 2
    assert len(script[("invoice_1005.json", "vp")]) == 2  # tool call, then the decision
    assert script[("invoice_1012.pdf", "vp")][0].decision == Ceiling.HUMAN
