from decimal import Decimal

import pytest

from config.settings import REPO_ROOT
from models import Status
from ui.kit import process_view as pv
from ui.kit.data import file_sha256
from ui.kit.jobs import Job
from ui.kit.runtime import Preflight

SAMPLES = REPO_ROOT / "data/invoices"


def run(rt, path):
    job = rt.start_invoice(rt.default_config, path)
    snap = job.snapshot()
    assert snap.error is None
    return snap.value


def test_sample_target_is_in_place_and_unstored_on_a_fresh_book(ui_rt):
    t = pv.sample_target(ui_rt, ui_rt.default_config, "invoice_1001.txt")
    assert t.path == SAMPLES / "invoice_1001.txt" and t.file_name == "invoice_1001.txt"
    assert t.file_hash == file_sha256(t.path) and t.stored is None


def test_sample_target_finds_the_stored_result_after_a_run(ui_rt):
    first = run(ui_rt, SAMPLES / "invoice_1001.txt")
    t = pv.sample_target(ui_rt, ui_rt.default_config, "invoice_1001.txt")
    assert t.stored is not None and t.stored.trace_id == first.trace_id


def test_upload_target_is_content_addressed_and_detects_a_stored_result(ui_rt):
    data = (SAMPLES / "invoice_1004.json").read_bytes()
    t = pv.upload_target(ui_rt, ui_rt.default_config, "invoice_1004.json", data)
    assert t.path.parent.parent == ui_rt.upload_dir and t.path.parent.name == t.file_hash[:12]
    assert t.stored is None and t.file_name == "invoice_1004.json"
    run(ui_rt, t.path)
    again = pv.upload_target(ui_rt, ui_rt.default_config, "invoice_1004.json", data)
    assert again.path == t.path and again.stored is not None and again.stored.status == Status.PAID


def test_upload_target_refuses_empty_and_unsupported_files(ui_rt):
    with pytest.raises(ValueError):
        pv.upload_target(ui_rt, ui_rt.default_config, "a.txt", b"")
    with pytest.raises(ValueError):
        pv.upload_target(ui_rt, ui_rt.default_config, "a.exe", b"x")


def target(stored=None):
    return pv.RunTarget(SAMPLES / "invoice_1001.txt", "invoice_1001.txt", "h", stored)


def test_run_blocker_priorities(ui_rt):
    ok = Preflight(True, "fake", None)
    none = Preflight(False, "none", "No model available. Add XAI_API_KEY and the two model names to .env.")
    replay = Preflight(False, "replay", "Replay has no recording for 1 of 1 files.")
    busy = Job("batch", "x")
    assert pv.run_blocker(None, None, None) == "Choose a sample or upload a file to enable Process."
    assert "run is in progress" in pv.run_blocker(target(), ok, busy)
    assert "run is in progress" in pv.run_blocker(None, None, busy)
    assert pv.run_blocker(target(), ok, None) is None
    assert "XAI_API_KEY" in pv.run_blocker(target(), none, None)
    assert "no recording" in pv.run_blocker(target(), replay, None)


def test_a_stored_target_stays_runnable_without_a_model(ui_rt):
    stored = run(ui_rt, SAMPLES / "invoice_1001.txt")
    none = Preflight(False, "none", "No model available.")
    assert pv.run_blocker(target(stored), none, None) is None
    assert pv.run_blocker(target(stored), None, Job("invoice", "x")) is not None


def test_no_model_ways_lists_the_numbered_ways_without_their_numbers():
    msg = ("No model provider is available. Ways to get one:\n  1. Put XAI_API_KEY in .env (Grok).\n"
           "  2. Record once.\n  3. Ollama arrives in tier 3.\nFiles without a recording: abc")
    assert pv.no_model_ways(msg) == ["Put XAI_API_KEY in .env (Grok).", "Record once.", "Ollama arrives in tier 3."]
    assert pv.no_model_ways("Grok needs XAI_API_KEY") == ["Grok needs XAI_API_KEY"]


def test_preview_text(tmp_path):
    assert "QuickShip" not in (pv.preview_text(SAMPLES / "invoice_1001.txt") or "QuickShip")
    assert pv.preview_text(SAMPLES / "invoice_1012.pdf") is None
    assert pv.preview_text(tmp_path / "missing.txt") is None


def test_payment_rows_for_1001_after_the_golden_batch(ui_rt_batch):
    cfg = ui_rt_batch.default_config
    with ui_rt_batch.reader(cfg) as r:
        rec = next(i for i in r.invoices() if i.file_name == "invoice_1001.txt")
        rows = pv.payment_rows(rec, r.stock_ledger(), r.budget_ledger(), "January")
        later = next(i for i in r.invoices() if i.file_name == "invoice_1004.json")
        later_rows = pv.payment_rows(later, r.stock_ledger(), r.budget_ledger(), "January")
        rejected = next(i for i in r.invoices() if i.status == Status.REJECTED)
        none_rows = pv.payment_rows(rejected, r.stock_ledger(), r.budget_ledger(), "January")
    assert rows == [("Stock used", "WidgetA −10, WidgetB −5"), ("January spending after this payment", "$5,000.00")]
    assert later_rows[1][1] == "$6,890.00"  # cumulative: 5,000 + 1,890
    assert none_rows == []


def test_record_result_rebuilds_an_error_row_and_skips_one_still_processing(tmp_path):
    from ui.kit.testing import make_runtime
    rt = make_runtime(tmp_path, provider=None, env={})
    job = rt.start_invoice(rt.default_config, SAMPLES / "invoice_1001.txt")
    assert job.snapshot().error is not None  # no model: the run stops and leaves an ERROR row
    with rt.reader(rt.default_config) as r:
        [rec] = r.invoices()
    result = pv.record_result(rec)
    assert result.status == Status.ERROR and result.trace_id == rec.trace_id and result.file == "invoice_1001.txt"
    assert result.error and "No model" in result.error or "NoProviderAvailable" in result.error
    assert pv.record_result(rec.model_copy(update={"status": Status.PROCESSING})) is None
