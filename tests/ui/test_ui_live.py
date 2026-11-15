
from streamlit.testing.v1 import AppTest

from config.settings import REPO_ROOT
from models import Status
from ui.kit import live
from ui.kit.jobs import JobEvent

SAMPLES = REPO_ROOT / "data/invoices"


def run_file(rt, name):
    return rt.start_invoice(rt.default_config, SAMPLES / name).snapshot()


def stages(snap, name, finished=True):
    return {s.key: s for s in live.timeline(snap.events, name, finished=finished)}


def nodes(stage):
    return [s.node for s in stage.steps]


def ev(seq, kind, node, *, file="a.txt", data=None, text=""):
    return JobEvent(seq, float(seq), kind, file, node, text or (node or kind), data or {})


def test_fast_path_text_invoice_groups_into_read_check_approve_finish(ui_rt):
    snap = run_file(ui_rt, "invoice_1001.txt")
    st = stages(snap, "invoice_1001.txt")
    assert list(st) == ["read", "check", "approve", "finish"]
    assert [s.label for s in st.values()] == ["Read", "Check", "Approve", "Finish"]
    assert nodes(st["read"]) == ["load_document", "extract_with_llm", "ground_check", "normalize"]
    assert nodes(st["check"]) == ["dedupe", "validate", "compute_ceiling"]  # score_risk is hidden
    assert nodes(st["approve"]) == ["vp_decide", "clamp_decision"]
    assert nodes(st["finish"]) == ["pay", "finish"]
    assert all(s.state == "done" for s in st.values())
    assert all(step.state == "done" for s in st.values() for step in s.steps)
    steps = {step.node: step for s in st.values() for step in s.steps}
    assert steps["load_document"].detail == "Text"
    assert steps["ground_check"].detail == "all found"
    assert steps["vp_decide"].detail.startswith("proposes Approve")
    assert steps["pay"].detail == "$5,000.00, 1 attempt"
    assert steps["load_document"].ms is not None and steps["load_document"].visits == 1


def test_rejection_with_memo_skips_the_vp_decision_and_payment(ui_rt):
    st = stages(run_file(ui_rt, "invoice_1002.txt"), "invoice_1002.txt")
    assert nodes(st["approve"]) == ["vp_memo"]
    assert nodes(st["finish"]) == ["record_rejection", "finish"]
    assert "pay" not in nodes(st["finish"])


def test_duplicate_skips_the_approve_stage(ui_rt):
    run_file(ui_rt, "invoice_1011.pdf")
    snap = run_file(ui_rt, "invoice_1011.txt")
    st = stages(snap, "invoice_1011.txt")
    assert st["approve"].state == "skipped" and st["approve"].steps == ()
    assert nodes(st["finish"]) == ["record_duplicate", "finish"]
    dedupe = next(s for s in st["check"].steps if s.node == "dedupe")
    assert dedupe.detail == "same invoice as invoice_1011.pdf"


def test_pending_human_ends_inside_the_paused_node(ui_rt):
    st = stages(run_file(ui_rt, "invoice_1012.pdf"), "invoice_1012.pdf")
    assert nodes(st["approve"])[:2] == ["vp_decide", "critic_review"]
    finish = nodes(st["finish"])
    assert finish[:2] == ["prepare_human", "await_human"] and "pay" not in finish
    assert all(step.state == "done" for step in st["finish"].steps)  # the interrupt ends the paused node
    assert st["finish"].state == "done"


def test_extraction_retry_adds_a_note():
    events = [
        ev(1, "node_start", "extract_with_llm"), ev(2, "node_end", "extract_with_llm", data={"duration_ms": 5}),
        ev(3, "retry", "ground_check", data={"stage": "extraction", "attempt": 1}),
        ev(4, "node_start", "extract_with_llm"), ev(5, "node_end", "extract_with_llm", data={"duration_ms": 7}),
        ev(6, "update", "extract_with_llm", text="Extracted fields with the model: second attempt",
           data={"detail": "second attempt"}),
    ]
    read = live.timeline(events, "a.txt", finished=False)[0]
    assert any("again" in n for n in read.notes)
    (step,) = read.steps
    assert step.visits == 2 and step.ms == 12 and step.detail == "second attempt"


def test_error_mid_run_leaves_the_active_stage_active_and_the_rest_pending():
    events = [ev(1, "node_start", "load_document"), ev(2, "node_end", "load_document"),
              ev(3, "node_start", "extract_with_llm"), ev(4, "error", None)]
    st = {s.key: s for s in live.timeline(events, "a.txt", finished=True)}
    assert st["read"].state == "active" and st["read"].steps[-1].state == "active"
    assert [st[k].state for k in ("check", "approve", "finish")] == ["pending"] * 3


def test_running_state_is_active_then_pending():
    events = [ev(1, "node_start", "load_document")]
    st = live.timeline(events, "a.txt", finished=False)
    assert [s.state for s in st] == ["active", "pending", "pending", "pending"]


def test_events_of_other_files_are_ignored():
    events = [ev(1, "node_start", "load_document", file="b.txt")]
    assert all(not s.steps for s in live.timeline(events, "a.txt", finished=False))


def test_status_label_running_and_completed(ui_rt):
    snap = run_file(ui_rt, "invoice_1001.txt")
    label = live.status_label(snap, "invoice_1001.txt")
    assert label.startswith("Processed invoice_1001.txt in ") and label.endswith(" model calls.")
    assert live.status_label(snap, "invoice_1001.txt") == (
        f"Processed invoice_1001.txt in {_secs(snap)}. {snap.results[0].usage.llm_calls} model calls.")
    running = snap.__class__(**{**snap.__dict__, "state": "running", "events": (ev(1, "node_start", "vp_decide"),)})
    assert live.status_label(running, "a.txt") == "VP is deciding…"


def _secs(snap):
    from ui.kit import fmt

    return fmt.seconds(sum(snap.results[0].usage.node_ms.values()))


def test_status_label_for_a_system_error(ui_rt):
    snap = run_file(ui_rt, "invoice_1001.txt")
    err = snap.results[0].model_copy(update={"status": Status.ERROR})
    snap = snap.__class__(**{**snap.__dict__, "results": (err,)})
    assert live.status_label(snap, "invoice_1001.txt") == "System error while processing invoice_1001.txt"


def _follow_app():
    import threading
    import time

    import streamlit as st

    from ui.kit import live
    from ui.kit.jobs import JobRunner

    runner = JobRunner()

    def work(ctx):
        for i in range(3):
            time.sleep(0.15)
            ctx.observe({"event": "node_start", "node": "load_document", "invoice": "a.txt", "trace_id": "t"})
            ctx.on_update("a.txt", "load_document", {"format": "txt"})
        return "done"

    job = runner.submit("invoice", "demo", work)
    final = live.follow(job, lambda snap: st.text(f"{snap.state}:{len(snap.events)}"), interval=0.05)
    st.write(f"final {final.state} {len(final.events)}")


def test_follow_renders_the_final_state_through_apptest():
    at = AppTest.from_function(_follow_app, default_timeout=30).run()
    assert not at.exception
    assert [t.value for t in at.text] == ["done:6"]  # one placeholder, replaced on every pass
    assert "final done 6" in at.markdown[0].value
