import datetime as dt
import json

import pytest

from obs import EVENT_NAMES, JsonlLogger, MemoryLogger, utc_now


def fixed_clock():
    return dt.datetime(2026, 1, 31, 12, 0, tzinfo=dt.timezone.utc)


def test_utc_now_is_aware():
    assert utc_now().tzinfo is not None


def test_jsonl_one_object_per_line(tmp_path):
    log = JsonlLogger(tmp_path / "logs", clock=fixed_clock)
    log.event("run_start", trace_id="t1", invoice="invoice_1001.txt")
    log.event("node_end", trace_id="t1", node="validate", duration_ms=5, data={"n": 1})
    lines = (tmp_path / "logs" / "2026-01-31.jsonl").read_text().splitlines()
    assert len(lines) == 2
    first, second = (json.loads(x) for x in lines)
    assert first["trace_id"] == "t1" and first["event"] == "run_start"
    assert second["node"] == "validate" and second["duration_ms"] == 5
    expected = {"ts", "trace_id", "invoice", "node", "event", "duration_ms", "tokens_in", "tokens_out",
                "cost_usd", "provider", "data"}
    assert set(first) == expected
    assert first["cost_usd"] is None


def test_empty_trace_id_raises(tmp_path):
    for log in (JsonlLogger(tmp_path), MemoryLogger()):
        with pytest.raises(ValueError):
            log.event("run_start", trace_id="")


def test_unknown_event_raises(tmp_path):
    for log in (JsonlLogger(tmp_path), MemoryLogger()):
        with pytest.raises(ValueError):
            log.event("nope", trace_id="t")
    assert "payment" in EVENT_NAMES


def test_redaction_at_any_depth(tmp_path):
    log = JsonlLogger(tmp_path, clock=fixed_clock)
    log.event(
        "llm_call", trace_id="t",
        data={"api_key": "sk-1", "x": {"Authorization": "Bearer z", "y": [{"headers": {"a": 1}, "ok": 2}]}},
    )
    text = (tmp_path / "2026-01-31.jsonl").read_text()
    assert "sk-1" not in text and "Bearer z" not in text
    data = json.loads(text)["data"]
    assert data["api_key"] == "[redacted]"
    assert data["x"]["Authorization"] == "[redacted]"
    assert data["x"]["y"][0] == {"headers": "[redacted]", "ok": 2}


def test_memory_logger_of_and_default_str(tmp_path):
    log = MemoryLogger()
    log.event("retry", trace_id="t", data={"d": dt.date(2026, 1, 1)})
    log.event("error", trace_id="t")
    assert len(log.of("retry")) == 1 and log.of("payment") == []
    jl = JsonlLogger(tmp_path, clock=fixed_clock)
    jl.event("retry", trace_id="t", data={"d": dt.date(2026, 1, 1)})
    assert json.loads((tmp_path / "2026-01-31.jsonl").read_text())["data"]["d"] == "2026-01-01"
