import threading
import time
from decimal import Decimal

import pytest

from config.settings import REPO_ROOT
from llm.fake import ScriptedFake
from llm.fake_golden import golden_script
from models import InvoiceResult, Status
from ui.kit import batch_view as bv
from ui.kit.testing import app_test, make_runtime, open_page, page_text, widget

TIMEOUT = 60


def run_batch_page(rt, *, timeout=TIMEOUT):
    at = app_test(rt, timeout=timeout)
    open_page(at, "batch")
    assert not at.exception, [e.value for e in at.exception]
    return at


def table(at):
    return at.dataframe[0].value


def statuses(at):
    return [s[0] for s in table(at)["Status"]]


def texts(at):
    return [m.value for m in at.markdown] + [h.value for h in at.header] + [c.value for c in at.caption]


def test_the_empty_state_names_the_next_action(app):
    open_page(app, "batch")
    assert not app.exception
    assert [t.value for t in app.title] == ["Batch"]
    assert widget(app, "batch.folder").value == "data/invoices"
    assert widget(app, "batch.run").disabled is False
    assert any("Run the folder to process every file in filename order" in t for t in texts(app))
    assert any(t.startswith("20 files") and "Processed in filename order" in t for t in texts(app))
    assert not app.dataframe and not app.metric


def test_running_the_samples_shows_the_golden_outcome(ui_rt):
    at = run_batch_page(ui_rt)
    widget(at, "batch.run").click()
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    frame = table(at)
    assert len(frame) == 20
    assert statuses(at).count("Paid") == 5
    assert statuses(at).count("Rejected") == 9
    assert statuses(at).count("Needs a person") == 3
    assert statuses(at).count("Duplicate") == 3
    assert [h.value for h in at.header] == ["20 files\\: 5 paid, 9 rejected, 3 need a person, 3 duplicates"]
    assert {m.label: m.value for m in at.metric}["Decided without a person"] == "82%"
    assert [m.label for m in at.metric] == [
        "Decided without a person", "Time per invoice", "Model calls per invoice", "Tokens per invoice",
    ]
    assert all(not m.delta for m in at.metric)
    assert [s.value for s in at.subheader] == ["What this run measured", "Estimated annual savings"]
    page = "\n".join(texts(at))
    assert "14 of 17 distinct documents decided without a person" in page
    assert "= \\$1,647,059 a year, before model costs\\. Estimate\\." in page
    assert "Review the 3 invoices waiting for a person" in page_text(at)
    sources = list(at.dataframe[1].value["Source"])
    assert [s.split(":")[0].split(" ")[0] for s in sources] == ["Assumption", "Assumption", "Measured", "Tier"]
    # a finished run does not call its own files "processed before"
    assert not at.info


def test_history_mode_shows_everything_since_the_reset(ui_rt_batch):
    at = run_batch_page(ui_rt_batch)
    assert len(table(at)) == 20
    page = "\n".join(texts(at))
    assert "Since the last reset\\: 20 invoices processed\\." in page
    assert "Measured since the last reset" in [s.value for s in at.subheader]
    assert {m.label: m.value for m in at.metric}["Decided without a person"] == "82%"
    assert "20 of these files were processed before" in at.info[0].value


def test_a_rerun_shows_stored_results_and_says_where_the_figures_come_from(ui_rt_batch):
    at = run_batch_page(ui_rt_batch)
    widget(at, "batch.run").click()
    at.run()
    assert not at.exception
    assert len(table(at)) == 20
    page = "\n".join(texts(at))
    assert "from the original run" in page
    assert "Time per invoice" in [m.label for m in at.metric]
    assert {m.label: m.value for m in at.metric}["Decided without a person"] == "82%"


def test_no_model_disables_run_and_says_why(tmp_path):
    rt = make_runtime(tmp_path / "rt", provider=None)
    at = run_batch_page(rt)
    assert widget(at, "batch.run").disabled is True
    assert any("No model available" in w.value for w in at.warning)


def test_a_missing_and_an_empty_folder_are_explained(app, tmp_path):
    open_page(app, "batch")
    widget(app, "batch.folder").set_value("no/such/folder").run()
    assert widget(app, "batch.run").disabled is True
    assert any("No folder at no/such/folder" in w.value for w in app.warning)
    (tmp_path / "empty").mkdir()
    widget(app, "batch.folder").set_value(str(tmp_path / "empty")).run()
    assert widget(app, "batch.run").disabled is True
    assert any("has no files to process" in w.value for w in app.warning)


def test_inspect_opens_the_case_in_a_dialog(ui_rt_batch):
    at = run_batch_page(ui_rt_batch)
    with ui_rt_batch.reader(ui_rt_batch.default_config) as reader:
        trace = next(r.trace_id for r in reader.invoices() if r.file_name == "invoice_1001.txt")
    widget(at, "batch.inspect").select(trace)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert at.session_state["batch.case"] == trace
    assert "Paid \\$5,000\\.00 to Widgets Inc\\." in [h.value for h in at.header]
    # closing the dialog clears the flag
    at.session_state["batch.case"] = None
    at.run()
    assert "Paid \\$5,000\\.00 to Widgets Inc\\." not in [h.value for h in at.header]


class Broken(ScriptedFake):
    """Fails every call for one file, with a plain exception (a bug, not a business outcome)."""

    def complete(self, *, ctx, **kwargs):
        if ctx.file_name == "invoice_1001.txt":
            raise RuntimeError("boom")
        return super().complete(ctx=ctx, **kwargs)


def test_an_error_row_is_marked_and_explained(tmp_path):
    rt = make_runtime(tmp_path / "rt", provider=Broken(golden_script()))
    at = run_batch_page(rt)
    widget(at, "batch.run").click()
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    frame = table(at)
    row = frame[frame["File"] == "invoice_1001.txt"].iloc[0]
    assert row["Status"] == ["System error"]
    assert "boom" in row["Why"]
    assert "1 file hit a system error" in "\n".join(texts(at))
    assert "1 system error" in "\n".join(texts(at))


def test_stop_ends_the_run_after_the_current_file_and_keeps_the_rows(tmp_path):
    gate = threading.Event()

    class Gated(ScriptedFake):
        def complete(self, *, ctx, **kwargs):
            gate.wait(20)
            return super().complete(ctx=ctx, **kwargs)

    rt = make_runtime(tmp_path / "rt", provider=Gated(golden_script()), inline=False)
    at = run_batch_page(rt)

    def stop_when_started():
        for _ in range(400):
            job = rt.jobs.current()
            if job is not None and job.snapshot().events:
                job.request_stop()  # what the batch.stop button does
                gate.set()
                return
            time.sleep(0.05)
        gate.set()

    helper = threading.Thread(target=stop_when_started, daemon=True)
    helper.start()
    widget(at, "batch.run").click()
    at.run()
    helper.join(5)
    assert not at.exception, [e.value for e in at.exception]
    page = "\n".join(texts(at))
    assert "Stopped after 1 of 20 files." in page
    assert len(table(at)) == 1
    job = rt.jobs.latest("batch")
    assert job.snapshot().state == "stopped"


# ------------------------------------------------------------------ pure builders


def make_result(**kw):
    base = dict(trace_id="t1", file="x.txt", status=Status.PAID, provider="fake")
    base.update(kw)
    return InvoiceResult(**base)


def test_result_rows_mark_errors_and_format_amounts():
    rows = bv.result_rows([
        make_result(status=Status.ERROR, error="boom", invoice_number=None),
        make_result(file="y.json", invoice_number="INV-9", revision="R1", vendor="Widgets Inc.", amount=Decimal("4125"), currency="EUR"),
    ])
    assert rows[0].error == "boom" and rows[0].status == "System error" and rows[0].ref == "-"
    assert rows[0].vendor == "No vendor" and rows[0].reason == "boom" and rows[0].amount == "-"
    assert rows[1].error is None and rows[1].ref == "INV-9 R1" and rows[1].amount == "€4,125.00"


def test_count_caption_orders_formats_and_pluralises():
    from pathlib import Path

    files = [Path(n) for n in ("a.csv", "b.txt", "c.txt", "d.pdf")]
    assert bv.count_caption(files) == "4 files: 2 TXT, 1 PDF, 1 CSV. Processed in filename order."
    assert bv.count_caption([Path("a.xml")]) == "1 file: 1 XML. Processed in filename order."


def test_progress_text_lowers_the_step_but_keeps_acronyms():
    assert bv.progress_text(6, 20, "invoice_1007.csv", "Checking inventory…") == (
        "Processing 7 of 20: invoice_1007.csv, checking inventory…"
    )
    assert bv.progress_text(0, 20, "a.txt", "VP is deciding…").endswith(", VP is deciding…")
    assert bv.progress_text(20, 20, None, None) == "Processing 20 of 20"
    assert bv.stopped_text(3, 20) == "Stopped after 3 of 20 files."


def test_ways_to_get_a_model_come_from_the_exceptions_own_message():
    message = "No model provider is available. Ways to get one:\n  1. Put a key in .env.\n  2. Record once.\nFiles: ab"
    assert bv.ways_to_get_a_model(message) == ["Put a key in .env.", "Record once."]


def test_folder_options_start_with_the_samples(ui_rt):
    assert bv.folder_options(ui_rt)[0] == "data/invoices"
    assert bv.folder_path(ui_rt, "data/invoices").is_dir()
    assert bv.folder_path(ui_rt, "/tmp").as_posix() == "/tmp"


def test_uploading_files_selects_the_stored_folder_and_runs_it(ui_rt):
    at = run_batch_page(ui_rt)
    body = (REPO_ROOT / "data/invoices/invoice_1001.txt").read_bytes()
    at.get("file_uploader")[0].set_value([("invoice_1001.txt", body, "text/plain")])
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    folder = widget(at, "batch.folder").value
    assert "batch-" in folder
    assert any(t.startswith("1 file\\: 1 TXT") for t in texts(at))
    widget(at, "batch.run").click()
    at.run()
    assert len(table(at)) == 1
    assert statuses(at) == ["Paid"]


def test_table_height_fits_every_row_up_to_the_cap():
    from ui.kit import batch_view as bv

    assert bv.table_height(0) == bv.table_height(1)
    assert bv.table_height(20) == 38 + 33 * 20
    assert bv.table_height(25) == 38 + 33 * 25
    assert bv.table_height(400) == bv.table_height(25)


def test_the_file_column_is_not_pinned():
    # A pinned column is drawn greyed out as if it were an index; File stays first and in normal ink.
    from ui.kit import batch_view as bv

    cfg = bv.table_config([], "light")
    assert not any(getattr(c, "get", lambda k, d=None: d)("pinned") for c in cfg.values())
