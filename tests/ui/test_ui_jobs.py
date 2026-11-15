import json
import threading

import pytest

from models import Ceiling, InvoiceResult, Status
from obs import MemoryLogger, JsonlLogger
from ui.kit.jobs import Busy, JobRunner, TeeLogger


def result(name="a.txt", status=Status.PAID, **kw):
    return InvoiceResult(trace_id=f"inv-{name}", file=name, status=status, provider="fake", **kw)


def test_inline_submit_runs_synchronously_and_keeps_the_value():
    runner = JobRunner(inline=True)
    job = runner.submit("invoice", "x", lambda ctx: 42)
    snap = job.snapshot()
    assert snap.state == "done" and snap.value == 42 and not snap.running
    assert snap.finished_at is not None
    assert runner.current() is None


def test_threaded_submit_and_busy_while_running():
    runner = JobRunner()
    gate = threading.Event()
    job = runner.submit("batch", "blocked", lambda ctx: gate.wait(10), total_files=3)
    assert job.snapshot().running and runner.current() is job
    with pytest.raises(Busy) as busy:
        runner.submit("invoice", "second", lambda ctx: None)
    assert busy.value.job is job
    gate.set()
    assert job.wait(10)
    assert runner.current() is None
    assert job.snapshot().total_files == 3
    runner.submit("invoice", "third", lambda ctx: 1).wait(10)  # the lock is free again


def test_failure_is_captured_as_the_exception_and_releases_the_lock():
    runner = JobRunner(inline=True)
    boom = ValueError("boom")

    def fail(ctx):
        raise boom

    snap = runner.submit("invoice", "x", fail).snapshot()
    assert snap.state == "failed" and snap.error is boom
    assert runner.submit("invoice", "y", lambda ctx: 1).snapshot().state == "done"


def test_stop_is_honoured_after_the_current_invoice():
    runner = JobRunner(inline=True)

    def work(ctx):
        runner.current().request_stop()  # inline: the job is already current while it runs
        ctx.on_result(result("a.txt"))  # recorded, then BatchStopped
        ctx.on_result(result("b.txt"))
        return "unreachable"

    snap = runner.submit("batch", "x", work).snapshot()
    assert snap.state == "stopped" and snap.stop_requested
    assert [r.file for r in snap.results] == ["a.txt"]
    assert snap.value is None
    assert runner.submit("batch", "y", lambda ctx: 1).snapshot().state == "done"


def test_events_are_ordered_with_monotonic_seq():
    runner = JobRunner(inline=True)

    def work(ctx):
        ctx.observe({"event": "node_start", "node": "load_document", "invoice": "a.txt", "trace_id": "t1"})
        ctx.on_update("a.txt", "load_document", {"format": "txt"})
        ctx.observe({"event": "tool_call", "node": "vp_decide", "invoice": "a.txt", "trace_id": "t1",
                     "data": {"tool": "get_stock_level", "result": "{}"}})
        ctx.on_result(result("a.txt"))

    events = runner.submit("invoice", "x", work).snapshot().events
    assert [e.seq for e in events] == sorted({e.seq for e in events})
    assert [e.kind for e in events] == ["node_start", "update", "tool_call", "result"]
    assert events[0].text == "Read the file"
    assert events[1].data["detail"] == "Text"
    assert events[2].data["tool"] == "get_stock_level"
    assert all(e.at > 0 for e in events)


def test_live_invoice_follows_on_update_payloads_with_pydantic_objects():
    from models import AgentTurn, make_flag, FlagCode

    runner = JobRunner(inline=True)
    flag = make_flag(FlagCode.UNKNOWN_VENDOR, "Unknown vendor")
    turn = AgentTurn(actor="vp", kind="decision", round=0, output={"decision": "APPROVE"})

    def work(ctx):
        ctx.observe({"event": "node_start", "node": "dedupe", "invoice": "a.txt", "trace_id": "inv-a-1"})
        ctx.on_update("a.txt", "validate", {"flags": [flag]})
        ctx.on_update("a.txt", "compute_ceiling", {"ceiling": Ceiling.HUMAN})
        ctx.on_update("a.txt", "vp_decide", {"transcript": [turn]})
        ctx.on_update("a.txt", "finish", {"status": Status.PENDING_HUMAN})

    live = runner.submit("invoice", "x", work).snapshot().live["a.txt"]
    assert live.trace_id == "inv-a-1"
    assert live.flags == (flag,) and live.ceiling == Ceiling.HUMAN
    assert live.transcript == (turn,) and live.status == Status.PENDING_HUMAN


def test_batch_trace_ids_are_not_attached_to_files():
    runner = JobRunner(inline=True)
    snap = runner.submit("batch", "x", lambda ctx: ctx.observe(
        {"event": "run_start", "trace_id": "batch-abc", "invoice": None})).snapshot()
    assert snap.live == {} and snap.events[0].kind == "run_start"


def test_callbacks_never_raise():
    runner = JobRunner(inline=True)

    def work(ctx):
        ctx.on_update("a.txt", "validate", object())  # not a mapping: swallowed
        ctx.observe({"event": 3, "data": 5})
        return "fine"

    assert runner.submit("invoice", "x", work).snapshot().state == "done"


def test_keep_limit_and_clear_history():
    runner = JobRunner(inline=True, keep=2)
    jobs = [runner.submit("invoice", f"j{i}", lambda ctx: None) for i in range(4)]
    assert runner.get(jobs[0].id) is None and runner.get(jobs[1].id) is None
    assert runner.get(jobs[3].id) is jobs[3] and runner.latest() is jobs[3]
    assert runner.latest("batch") is None
    runner.clear_history()
    assert runner.latest() is None


def test_clear_history_keeps_the_running_job():
    runner = JobRunner()
    gate = threading.Event()
    job = runner.submit("batch", "x", lambda ctx: gate.wait(10))
    runner.clear_history()
    assert runner.current() is job
    gate.set()
    job.wait(10)


def test_tee_logger_forwards_to_the_inner_logger_and_the_sink(tmp_path):
    seen = []
    inner = JsonlLogger(tmp_path / "logs")
    tee = TeeLogger(inner, seen.append)
    tee.event("run_start", trace_id="t1", invoice="a.txt", data={"files": 1})
    (line,) = next((tmp_path / "logs").glob("*.jsonl")).read_text().splitlines()
    assert json.loads(line)["trace_id"] == "t1"
    assert seen[0]["event"] == "run_start" and seen[0]["data"] == {"files": 1}


def test_tee_logger_survives_a_failing_sink():
    mem = MemoryLogger()

    def bad(record):
        raise RuntimeError("sink down")

    TeeLogger(mem, bad).event("run_start", trace_id="t1")
    assert len(mem.events) == 1
