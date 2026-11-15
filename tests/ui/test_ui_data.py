import datetime as dt
import threading
from dataclasses import replace
from decimal import Decimal

import pytest

from config.settings import REPO_ROOT
from db.seed import reset_database
from llm.fake import ScriptedFake
from llm.fake_golden import golden_script
from models import Status
from ui.kit.data import ReadSession, file_sha256
from ui.kit.testing import build_settings, make_runtime, runtime_from_snapshot

SAMPLES = REPO_ROOT / "data/invoices"


@pytest.fixture
def reader(ui_rt_batch):
    r = ui_rt_batch.reader(ui_rt_batch.default_config)
    yield r
    r.close()


def by_name(reader, name):
    return next(i for i in reader.invoices() if i.file_name == name)


def test_invoices_and_lookups(reader):
    invoices = reader.invoices()
    assert len(invoices) == 20
    rec = by_name(reader, "invoice_1012.pdf")
    assert rec.status == Status.PENDING_HUMAN
    assert reader.invoice(rec.id).trace_id == rec.trace_id
    assert reader.invoice_by_trace(rec.trace_id).id == rec.id
    assert reader.invoice(9999) is None and reader.invoice_by_trace("nope") is None
    stored = reader.stored_result(file_sha256(SAMPLES / "invoice_1001.txt"))
    assert stored is not None and stored.status == Status.PAID
    assert reader.stored_result("0" * 64) is None
    assert reader.seed_name() == "demo"


def test_queue_budget_stock_and_payments(reader):
    assert [q.invoice_ref for q in reader.queue()] == ["INV-1004 R1", "INV-1012", "INV-1014"]
    assert len(reader.queue(None)) == 3
    status = reader.budget_status()
    assert (status.month, status.spent, status.limit, status.remaining) == (
        "2026-01", Decimal("19140.00"), Decimal("50000"), Decimal("30860.00"))
    assert len(reader.budget_ledger()) == 5
    assert {i.item: i.stock for i in reader.inventory()} == {"WidgetA": 56, "WidgetB": 32, "GadgetX": 11, "FakeItem": 0}
    seeds = [e for e in reader.stock_ledger() if e.reason == "seed"]
    assert len(seeds) == 4 and len(reader.stock_ledger()) > 4
    payments = reader.payments()
    assert len(payments) == 5 and all(p.status == "SUCCESS" for p in payments)
    assert reader.starting_stock() == {"WidgetA": 90, "WidgetB": 50, "GadgetX": 13, "FakeItem": 0}


def test_budget_ledger_all_includes_other_months(ui_rt_batch):
    with ui_rt_batch.reader(ui_rt_batch.default_config) as r:
        assert len(r.budget_ledger_all()) == len(r.budget_ledger()) == 5
    other = replace(ui_rt_batch.default_config, as_of=dt.date(2026, 2, 15))
    with ui_rt_batch.reader(other) as r:
        assert r.budget_ledger() == [] and len(r.budget_ledger_all()) == 5


def test_budget_ledger_all_without_a_database(tmp_path):
    with ReadSession(build_settings(tmp_path), budget_month="2026-01") as r:
        assert r.budget_ledger_all() == []


def test_audit_and_vendors(reader):
    rec = by_name(reader, "invoice_1001.txt")
    assert reader.audit(rec.trace_id)
    assert reader.vendor(rec.vendor_id).name == "Widgets Inc."
    assert reader.vendor_aliases(rec.vendor_id) == []
    assert reader.vendor(9999) is None


def test_starting_stock_follows_the_seed(tmp_path):
    rt = make_runtime(tmp_path)
    with rt.reader(rt.default_config) as r:
        assert r.starting_stock() == {"WidgetA": 90, "WidgetB": 50, "GadgetX": 13, "FakeItem": 0}
    assert rt.start_reset(rt.default_config, "readme").snapshot().state == "done"
    with rt.reader(rt.default_config) as r:
        assert r.starting_stock() == {"WidgetA": 15, "WidgetB": 10, "GadgetX": 5, "FakeItem": 0}


def test_queue_without_a_status_returns_resolved_items_too(ui_rt_batch):
    cfg = ui_rt_batch.default_config
    job = ui_rt_batch.start_resolve(
        cfg, "INV-1012", action="approve", note="called", fx_rate=None, confirm_overspend=False)
    assert job.snapshot().state == "done"
    with ui_rt_batch.reader(cfg) as r:
        assert [q.invoice_ref for q in r.queue()] == ["INV-1004 R1", "INV-1014"]
        everything = r.queue(None)
        assert [(q.invoice_ref, q.status) for q in everything] == [
            ("INV-1004 R1", "PENDING"), ("INV-1012", "RESOLVED"), ("INV-1014", "PENDING")]
        assert everything[1].resolution == "approve" and everything[1].note == "called"


def test_invoice_state_equals_the_service_state(ui_rt_batch, reader):
    rec = by_name(reader, "invoice_1012.pdf")
    state = reader.invoice_state(rec.trace_id)
    with ui_rt_batch.factory(ui_rt_batch.default_config) as svc:
        assert state == svc._state(rec.trace_id)
    assert reader.invoice_state("inv-unknown") is None


def test_source_text_for_pdf_json_and_a_missing_file(reader):
    pdf = by_name(reader, "invoice_1012.pdf")
    assert "QuickShip" in reader.source_text(pdf, reader.invoice_state(pdf.trace_id))
    js = by_name(reader, "invoice_1004.json")
    assert reader.source_text(js, None) == (SAMPLES / "invoice_1004.json").read_text(encoding="utf-8")
    gone = js.model_copy(update={"file_path": "/nonexistent/upload.json"})
    assert reader.source_text(gone, None) is None
    assert reader.source_text(pdf.model_copy(update={"file_path": "/nonexistent/x.pdf"}), None) is None


def test_case_detail_for_a_pdf_and_a_json_file(reader):
    pdf = reader.case_detail(by_name(reader, "invoice_1012.pdf"))
    assert pdf.source_kind == "pdf" and "QuickShip" in pdf.source
    assert pdf.state is not None and pdf.original_path == SAMPLES / "invoice_1012.pdf"
    assert pdf.processed_at is not None
    js = reader.case_detail(by_name(reader, "invoice_1004.json"))
    assert js.source_kind == "file" and js.source and js.original_path == SAMPLES / "invoice_1004.json"
    assert reader.case_detail(by_name(reader, "invoice_1001.txt")).source_kind == "text"
    missing = by_name(reader, "invoice_1004.json").model_copy(update={"file_path": "/gone/x.json"})
    assert reader.case_detail(missing).original_path is None


def test_reads_change_nothing(reader):
    rec = by_name(reader, "invoice_1012.pdf")
    reader.invoices(), reader.queue(None), reader.budget_status(), reader.payments(), reader.audit(rec.trace_id)
    reader.case_detail(rec), reader.starting_stock(), reader.inventory()
    assert reader._repo._conn.total_changes == 0


def test_a_missing_database_gives_empty_results_and_creates_nothing(tmp_path):
    settings = build_settings(tmp_path / "nothing-here")
    with ReadSession(settings, budget_month="2026-01") as r:
        assert r.seed_name() is None and r.invoices() == [] and r.queue(None) == []
        assert r.inventory() == [] and r.payments() == [] and r.starting_stock() == {}
        assert r.invoice_state("t") is None and r.invoice(1) is None
        status = r.budget_status()
        assert status.spent == 0 and status.remaining == settings.monthly_budget_usd
    assert not (tmp_path / "nothing-here").exists()


def test_a_database_without_tables_gives_empty_results(tmp_path):
    settings = build_settings(tmp_path)
    import sqlite3

    sqlite3.connect(settings.db_path).close()
    with ReadSession(settings, budget_month="2026-01") as r:
        assert r.invoices() == [] and r.budget_ledger() == []


class BlockingProvider:
    name = "fake"

    def __init__(self):
        self.inner = ScriptedFake(golden_script())
        self.entered = threading.Event()
        self.release = threading.Event()

    def complete(self, **kw):
        self.entered.set()
        assert self.release.wait(20)
        return self.inner.complete(**kw)


def test_a_read_while_a_threaded_job_is_blocked_inside_a_node(tmp_path, golden_db):
    provider = BlockingProvider()
    rt = runtime_from_snapshot(tmp_path / "rt", golden_db, provider=provider, inline=False)
    fresh = tmp_path / "invoice_1001.txt"  # same name as a sample (the fake keys on it), different bytes
    fresh.write_text((SAMPLES / "invoice_1001.txt").read_text() + "\n", encoding="utf-8")
    job = rt.start_invoice(rt.default_config, fresh)
    try:
        assert provider.entered.wait(20)
        with rt.reader(rt.default_config) as r:
            assert [q.invoice_ref for q in r.queue()] == ["INV-1004 R1", "INV-1012", "INV-1014"]
            assert len(r.invoices()) >= 20
            assert r.budget_status().spent == Decimal("19140.00")
    finally:
        provider.release.set()
        job.wait(30)
    assert job.snapshot().state == "done"


def test_the_next_session_sees_a_database_reset_behind_its_back(ui_rt_batch):
    with ui_rt_batch.reader(ui_rt_batch.default_config) as first:
        assert len(first.invoices()) == 20
    reset_database(ui_rt_batch.settings.db_path, "demo").close()
    with ui_rt_batch.reader(ui_rt_batch.default_config) as second:
        assert second.invoices() == [] and second.queue(None) == []
        assert second.seed_name() == "demo"
