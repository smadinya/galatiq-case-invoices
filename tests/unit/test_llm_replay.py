import datetime as dt
import json

import pytest

from llm.base import ScriptError, StaleRecording
from llm.fake import ScriptedFake, critic, memo, tool_call
from llm.recorder import RecordingProvider, fixture_path
from llm.replay import ReplayProvider
from models import Ceiling, CallContext, CriticOutput, Message, ToolSpec, Verdict, VPMemoOutput
from obs import MemoryLogger

KEY = "xai-key-that-must-not-leak"
HASH = "ab" * 32
TOOL = ToolSpec(name="t", description="d", parameters={"type": "object", "properties": {}, "required": [], "additionalProperties": False})


def ctx(i=0, stage="critic", version="critic-v1"):
    return CallContext(trace_id="t", file_hash=HASH, file_name="invoice_1.json", stage=stage, prompt_version=version, call_index=i)


def record(tmp_path, steps, stage="critic"):
    fake = ScriptedFake({("invoice_1.json", stage): steps})
    clock = lambda: dt.datetime(2026, 3, 4, 5, 6, 7, tzinfo=dt.timezone.utc)
    rec = RecordingProvider(fake, tmp_path, clock=clock)
    return fake, rec


def test_round_trip(tmp_path):
    steps = [tool_call("c1", "t", x=1), critic(Verdict.AGREE, Ceiling.APPROVE)]
    _, rec = record(tmp_path, steps)
    msgs = [Message(role="user", content="hello")]
    a = rec.complete(ctx=ctx(0), system="S", messages=msgs, schema=CriticOutput, tools=[TOOL])
    b = rec.complete(ctx=ctx(1), system="S", messages=msgs, schema=CriticOutput, tools=[TOOL])
    assert rec.name == "fake"
    assert fixture_path(tmp_path, HASH, "critic", 1) == tmp_path / HASH[:16] / "critic-001.json"
    data = json.loads(fixture_path(tmp_path, HASH, "critic", 0).read_text())
    assert data["key"]["prompt_version"] == "critic-v1" and data["recorded_at"] == "2026-03-04T05:06:07Z"
    assert data["request"]["tools"] == ["t"] and data["request"]["schema"] == "CriticOutput"

    replay = ReplayProvider(tmp_path)
    assert replay.name == "replay" and replay.has_recording(HASH) and not replay.has_recording("cd" * 32)
    ra = replay.complete(ctx=ctx(0), system="S", messages=msgs, schema=CriticOutput, tools=[TOOL])
    rb = replay.complete(ctx=ctx(1), system="S", messages=msgs, schema=CriticOutput, tools=[TOOL])
    assert ra.tool_calls == a.tool_calls and ra.parsed is None
    assert rb.parsed == b.parsed and isinstance(rb.parsed, CriticOutput) and rb.provider == "replay"
    assert replay.drifted == []


def test_fixture_has_no_secrets(tmp_path):
    _, rec = record(tmp_path, [memo("ok")], stage="vp_memo")
    c = ctx(0, "vp_memo", "vp_memo-v1")
    rec.complete(ctx=c, system="S", messages=[Message(role="user", content="x")], schema=VPMemoOutput)
    text = fixture_path(tmp_path, HASH, "vp_memo", 0).read_text().lower()
    for bad in ("authorization", "bearer", "api_key", KEY.lower()):
        assert bad not in text


def test_stale_prompt_version(tmp_path):
    _, rec = record(tmp_path, [critic(Verdict.AGREE, Ceiling.APPROVE)])
    rec.complete(ctx=ctx(0), system="S", messages=[], schema=CriticOutput)
    with pytest.raises(StaleRecording) as ei:
        ReplayProvider(tmp_path).complete(ctx=ctx(0, version="critic-v2"), system="S", messages=[], schema=CriticOutput)
    assert "critic-v1 -> critic-v2" in str(ei.value) and "scripts/record.py" in str(ei.value)


def test_missing_recording(tmp_path):
    with pytest.raises(StaleRecording) as ei:
        ReplayProvider(tmp_path).complete(ctx=ctx(3), system="S", messages=[], schema=CriticOutput)
    msg = str(ei.value)
    assert "invoice_1.json critic#3" in msg and msg.endswith("uv run python scripts/record.py")


def test_request_drift_warns_but_serves(tmp_path):
    _, rec = record(tmp_path, [critic(Verdict.AGREE, Ceiling.APPROVE)])
    rec.complete(ctx=ctx(0), system="S", messages=[], schema=CriticOutput)
    log = MemoryLogger()
    out = ReplayProvider(tmp_path, log=log).complete(ctx=ctx(0), system="CHANGED", messages=[], schema=CriticOutput)
    assert isinstance(out.parsed, CriticOutput)
    assert log.of("error")[0]["data"]["kind"] == "replay_request_drift"


def test_manifest_date_and_models(tmp_path):
    assert ReplayProvider(tmp_path).recording_date() is None
    (tmp_path / "manifest.json").write_text(json.dumps({"recorded_at": "2026-02-01T00:00:00Z", "models": {"reasoning": "m"}}))
    r = ReplayProvider(tmp_path)
    assert r.recording_date() == "2026-02-01T00:00:00Z" and r.models() == {"reasoning": "m"}


def test_recorder_propagates_inner_error(tmp_path):
    fake = ScriptedFake({})
    rec = RecordingProvider(fake, tmp_path)
    with pytest.raises(ScriptError):
        rec.complete(ctx=ctx(), system="S", messages=[], schema=CriticOutput)
    assert not list(tmp_path.glob("**/*.json"))
