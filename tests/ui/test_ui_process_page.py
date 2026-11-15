import sqlite3
import threading
import time

import pytest

from config.settings import REPO_ROOT
from llm.fake_golden import golden_script
from llm.fake import ScriptedFake
from models import Status
from ui.kit.testing import app_test, make_runtime, open_page, page_text, widget

SAMPLES = REPO_ROOT / "data/invoices"


class Boom(ScriptedFake):
    """A model that fails every call: the pipeline contains it as an ERROR result."""

    def complete(self, *args, **kwargs):
        raise RuntimeError("model unreachable")


def ok(at):
    assert not at.exception, [e.value for e in at.exception]


def text(at):
    parts = [m.value for m in at.markdown] + [c.value for c in at.caption] + [m.value for m in at.info]
    parts += [m.value for m in at.warning] + [m.value for m in at.error] + [h.value for h in at.header]
    return ("\n".join(parts) + "\n" + page_text(at)).replace("\\", "")


def choose_and_run(at, name):
    widget(at, "process.sample").select(name)
    at.run()
    widget(at, "process.run").click()
    return at.run()


def rows(rt, status=None):
    with rt.reader(rt.default_config) as r:
        return [i for i in r.invoices() if status is None or i.status == status]


def test_nothing_chosen_shows_the_guide_and_a_disabled_button(app):
    open_page(app, "process")
    ok(app)
    assert widget(app, "process.run").disabled
    assert "Choose a sample or upload a file to enable Process." in text(app)
    assert "Pick a sample or upload an invoice" in text(app)
    assert len(app.dataframe) == 1
    assert len(app.dataframe[0].value) == 20
    assert widget(app, "process.sample").value is None
    assert widget(app, "process.sample").options[0] == "invoice_1001.txt"


def test_chosen_not_run_shows_the_source_preview(app):
    open_page(app, "process")
    widget(app, "process.sample").select("invoice_1001.txt")
    app.run()
    ok(app)
    assert not widget(app, "process.run").disabled
    html = page_text(app)
    assert "k-src" in html and "Text as printed" in html
    assert not rows(app.session_state["acme.runtime"])


def test_a_pdf_preview_says_the_text_layer_comes_later(app):
    open_page(app, "process")
    widget(app, "process.sample").select("invoice_1012.pdf")
    app.run()
    ok(app)
    assert "text layer of this PDF" in text(app)


def test_running_sample_1001_pays_and_shows_the_case(app):
    open_page(app, "process")
    choose_and_run(app, "invoice_1001.txt")
    ok(app)
    rt = app.session_state["acme.runtime"]
    assert [i.status for i in rows(rt)] == [Status.PAID]
    assert app.session_state["process.trace"] == rows(rt)[0].trace_id
    body = text(app)
    assert "Paid" in "\n".join(h.value for h in app.header)
    assert "k-src" in body
    assert "WidgetA −10, WidgetB −5" in body
    assert "January spending after this payment" in body and "5,000.00" in body
    assert len(app.get("chat_message")) >= 1
    assert any("VP" in str(c.proto) for c in app.get("chat_message"))
    assert app.get("status")[0].proto.label.replace("\\", "") == "Processed invoice_1001.txt in 0.02 s. 2 model calls." or (
        app.get("status")[0].proto.label.replace("\\", "").startswith("Processed invoice_1001.txt in "))


def test_uploading_1004_pays_and_stores_under_the_hash(app):
    open_page(app, "process")
    widget(app, "process.source").set_value("upload")
    app.run()
    widget(app, "process.upload").upload("invoice_1004.json", (SAMPLES / "invoice_1004.json").read_bytes(), "application/json")
    app.run()
    ok(app)
    widget(app, "process.run").click()
    app.run()
    ok(app)
    rt = app.session_state["acme.runtime"]
    [rec] = rows(rt)
    assert rec.status == Status.PAID and "1,890.00" in text(app)
    assert rec.file_path.startswith(str(rt.upload_dir)) and rec.file_path.endswith("invoice_1004.json")
    assert len(rec.file_path.split("/")[-2]) == 12


def test_rerunning_a_stored_sample_shows_already_processed_without_a_new_row(app):
    open_page(app, "process")
    choose_and_run(app, "invoice_1001.txt")
    rt = app.session_state["acme.runtime"]
    widget(app, "process.run").click()
    app.run()
    ok(app)
    assert "Already processed on" in text(app)
    assert len(rows(rt)) == 1


def test_no_model_disables_process_and_names_the_key(tmp_path):
    rt = make_runtime(tmp_path, provider=None, env={})
    at = open_page(app_test(rt), "process")
    widget(at, "process.sample").select("invoice_1001.txt")
    at.run()
    ok(at)
    assert widget(at, "process.run").disabled
    body = text(at)
    assert "XAI_API_KEY" in body and "No model available" in body


def test_a_stored_file_can_still_be_processed_without_a_model(ui_rt_batch, tmp_path):
    rt = make_runtime(tmp_path / "x", provider=None, env={})
    from ui.kit.testing import runtime_from_snapshot
    rt = runtime_from_snapshot(tmp_path / "y", ui_rt_batch.settings.db_path.parent, provider=None, env={})
    at = open_page(app_test(rt), "process")
    widget(at, "process.sample").select("invoice_1003.txt")
    at.run()
    assert not widget(at, "process.run").disabled
    widget(at, "process.run").click()
    at.run()
    ok(at)
    assert "Already processed on" in text(at)
    assert "Rejected" in "\n".join(h.value for h in at.header)


def test_trace_1012_shows_the_vendor_flagged_and_the_source(app_batch):
    rt = app_batch.session_state["acme.runtime"]
    trace = next(i.trace_id for i in rows(rt) if i.file_name == "invoice_1012.pdf")
    open_page(app_batch, "process", trace=trace)
    ok(app_batch)
    body = text(app_batch)
    assert "QuickShip Distributers" in body
    assert "Waiting for a person: vendor renamed" in "\n".join(h.value for h in app_batch.header).replace("\\", "")
    assert "k-src" in body


def test_trace_of_a_paid_invoice_uses_the_ledger_rows(app_batch):
    rt = app_batch.session_state["acme.runtime"]
    trace = next(i.trace_id for i in rows(rt) if i.file_name == "invoice_1001.txt")
    open_page(app_batch, "process", trace=trace)
    ok(app_batch)
    body = text(app_batch)
    assert "WidgetA −10, WidgetB −5" in body and "5,000.00" in body


def test_a_trace_that_no_longer_exists_gives_a_note_and_the_empty_state(app):
    open_page(app, "process", trace="inv-gone-00000000")
    ok(app)
    body = text(app)
    assert "no longer in the books" in body and "Pick a sample or upload an invoice" in body


def test_choosing_another_file_replaces_the_case_with_its_preview(app):
    open_page(app, "process")
    choose_and_run(app, "invoice_1001.txt")
    widget(app, "process.sample").select("invoice_1002.txt")
    app.run()
    ok(app)
    assert "k-src" in page_text(app) and "Paid" not in "\n".join(h.value for h in app.header)


def test_a_failing_model_gives_an_error_case_and_process_again_adds_a_row(tmp_path):
    rt = make_runtime(tmp_path, provider=Boom())
    at = open_page(app_test(rt), "process")
    choose_and_run(at, "invoice_1001.txt")
    ok(at)
    assert [i.status for i in rows(rt)] == [Status.ERROR]
    assert "System error" in "\n".join(h.value for h in at.header)
    assert "model unreachable" in "\n".join(c.value for c in at.code)
    assert "Nothing was paid" in text(at)
    again = widget(at, "process.again")
    assert not again.disabled and again.label == "Process again"
    again.click()
    at.run()
    ok(at)
    assert [i.status for i in rows(rt)] == [Status.ERROR, Status.ERROR]  # never short-circuited
    assert at.session_state["process.trace"] == rows(rt)[-1].trace_id


def test_no_model_exception_shows_the_numbered_ways(tmp_path):
    rt = make_runtime(tmp_path, provider=None, env={})
    # preflight would block this; drive the job directly the way a race (key removed mid-session) would
    job = rt.start_invoice(rt.default_config, SAMPLES / "invoice_1001.txt")
    at = open_page(app_test(rt), "process")
    at.session_state["process.job_id"] = job.id
    at.run()
    ok(at)
    warn = "\n".join(w.value for w in at.warning)
    assert "No model available" in warn
    assert "XAI_API_KEY" in warn and "\n- " in warn
    assert "Record once with a key" in warn


def test_the_page_comes_back_to_a_running_job_and_finishes_it(tmp_path):
    rt = make_runtime(tmp_path, inline=False)
    gate = threading.Event()

    class Gated(ScriptedFake):
        def complete(self, *a, **k):
            gate.wait(10)
            return super().complete(*a, **k)

    rt2 = make_runtime(tmp_path / "g", provider=Gated(golden_script()), inline=False)
    job = rt2.start_invoice(rt2.default_config, SAMPLES / "invoice_1001.txt")
    threading.Timer(0.8, gate.set).start()
    at = open_page(app_test(rt2), "process")  # reattaches: process.job_id is not set in this session
    ok(at)
    job.wait(10)
    assert at.session_state["process.job_id"] == job.id
    assert at.session_state["process.trace"] == rows(rt2)[0].trace_id
    assert "Paid" in "\n".join(h.value for h in at.header)


def test_a_threaded_run_completes_and_renders(tmp_path):
    rt = make_runtime(tmp_path, inline=False)
    at = open_page(app_test(rt), "process")
    choose_and_run(at, "invoice_1001.txt")
    ok(at)
    assert [i.status for i in rows(rt)] == [Status.PAID]
    assert "k-src" in page_text(at)


def test_inputs_are_disabled_while_another_run_holds_the_lock(tmp_path):
    rt = make_runtime(tmp_path, inline=False)
    gate = threading.Event()
    job = rt.jobs.submit("batch", "Running invoices", lambda ctx: gate.wait(10))
    try:
        at = open_page(app_test(rt), "process")
        ok(at)
        assert widget(at, "process.sample").disabled and widget(at, "process.run").disabled
        assert "A run is in progress" in text(at)
    finally:
        gate.set()
        job.wait(10)


def test_the_page_renders_in_every_state_without_raising(ui_rt_batch):
    at = app_test(ui_rt_batch)
    open_page(at, "process")
    ok(at)
    for name in ("invoice_1003.txt", "invoice_1010.txt", "invoice_1011.pdf", "invoice_1014.xml"):
        widget(at, "process.sample").select(name)
        at.run()
        ok(at)
        widget(at, "process.run").click()
        at.run()
        ok(at)
