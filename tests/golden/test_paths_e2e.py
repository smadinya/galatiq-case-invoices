"""One end-to-end test per path through the real graph, repo and JSONL logger (fresh DB per test)."""

import json
import shutil
from decimal import Decimal as D

import pytest

from config.settings import REPO_ROOT
from llm.fake import ScriptedFake, memo, vp
from llm.fake_golden import golden_script
from models import Ceiling, Status
from service import ApproveNotAllowed

SAMPLES = REPO_ROOT / "data" / "invoices"
MONTH = "2026-01"


def test_paid(make_service):
    svc = make_service()
    res = svc.run_invoice(SAMPLES / "invoice_1001.txt")
    assert res.status == Status.PAID and res.payment.amount == D("5000.00") and res.decision.by == "vp"
    assert svc.repo.stock_levels()["WidgetA"] == 80 and svc.repo.budget_spent(MONTH) == D("5000.00")


def test_rejected(make_service):
    svc = make_service()
    res = svc.run_invoice(SAMPLES / "invoice_1016.json")
    assert res.status == Status.REJECTED and res.payment is None and res.decision.by == "rules"
    assert [f.code.value for f in res.flags] == ["unknown_item"]
    assert svc.list_queue() == [] and svc.repo.budget_spent(MONTH) == 0


def test_human(make_service):
    svc = make_service()
    res = svc.run_invoice(SAMPLES / "invoice_1014.xml")
    assert res.status == Status.PENDING_HUMAN and res.human.reason.value == "foreign_currency"
    assert [q.invoice_ref for q in svc.list_queue()] == ["INV-1014"]
    assert svc.repo.budget_spent(MONTH) == 0


def test_duplicate(make_service):
    svc = make_service()
    first = svc.run_invoice(SAMPLES / "invoice_1011.pdf")
    second = svc.run_invoice(SAMPLES / "invoice_1011.txt")
    assert first.status == Status.PAID
    assert second.status == Status.DUPLICATE and second.duplicate_of == "invoice_1011.pdf"
    assert second.payment is None and svc.repo.budget_spent(MONTH) == D("3000.00")
    assert len(svc.repo.budget_entries(MONTH)) == 1


def test_error_is_isolated_in_batch(make_service, tmp_path):
    d = tmp_path / "in"
    d.mkdir()
    for n in ("invoice_1001.txt", "invoice_1006.csv", "invoice_1015.csv"):
        shutil.copy(SAMPLES / n, d / n)

    def hook(name, state):
        if name == "validate" and state.file_name == "invoice_1006.csv":
            raise RuntimeError("injected")

    svc = make_service(node_hook=hook)
    batch = svc.run_batch(d)
    by = {r.file: r for r in batch.results}
    assert by["invoice_1006.csv"].status == Status.ERROR and "injected" in by["invoice_1006.csv"].error
    assert by["invoice_1006.csv"].payment is None
    assert by["invoice_1001.txt"].status == Status.PAID and by["invoice_1015.csv"].status == Status.PAID
    assert svc.list_queue() == []
    stock = svc.repo.stock_levels()
    assert (stock["WidgetA"], stock["WidgetB"], stock["GadgetX"]) == (70, 40, 11)
    assert svc.repo.budget_spent(MONTH) == D("11500.00")
    rec = svc.repo.get_invoice_by_trace(by["invoice_1006.csv"].trace_id)
    assert rec.status == Status.ERROR
    lines = [json.loads(l) for f in make_service.settings.log_dir.glob("*.jsonl") for l in f.read_text().splitlines()]
    errors = [l for l in lines if l["event"] == "error"]
    assert any(e["trace_id"] == by["invoice_1006.csv"].trace_id for e in errors)


@pytest.mark.parametrize("name,content", [
    ("invoice_9001.json", b'{"invoice_number": "INV-9001", "vendor": {"name": "Wid'),
    ("invoice_9002.pdf", b"not a pdf"),
    ("invoice_9003.docx", b"PK\x03\x04 garbage"),
])
def test_unreadable_documents(make_service, tmp_path, name, content):
    f = tmp_path / name
    f.write_bytes(content)
    fake = ScriptedFake({})
    svc = make_service(provider=fake)
    res = svc.run_invoice(f)
    assert res.status == Status.PENDING_HUMAN
    assert [x.code.value for x in res.flags] == ["unreadable_document"]
    assert res.human.reason.value == "unreadable_document" and res.human.recommendation is None
    assert fake.calls == []
    assert [q.file_name for q in svc.list_queue()] == [name]
    with pytest.raises(ApproveNotAllowed):
        svc.resolve(name, action="approve", note="try")
    assert [q.file_name for q in svc.list_queue()] == [name]
    out = svc.resolve(name, action="reject", note="cannot read")
    assert out.status == Status.REJECTED and svc.list_queue() == []
    assert fake.calls == []


def test_short_circuit(make_service):
    fake = ScriptedFake(golden_script())
    svc = make_service(provider=fake)
    first = svc.run_invoice(SAMPLES / "invoice_1001.txt")
    calls, spent = len(fake.calls), svc.repo.budget_spent(MONTH)
    second = svc.run_invoice(SAMPLES / "invoice_1001.txt")
    assert second.short_circuited and second.trace_id == first.trace_id and second.status == Status.PAID
    assert len(fake.calls) == calls and svc.repo.budget_spent(MONTH) == spent
    assert len(svc.repo.list_invoices()) == 1
    # a new process short-circuits too
    third = make_service(provider=fake).run_invoice(SAMPLES / "invoice_1001.txt")
    assert third.short_circuited and len(fake.calls) == calls


def test_payment_failure_then_retry(make_service):
    fake = ScriptedFake(golden_script())
    svc = make_service(provider=fake, payment_fail_rate=1.0)
    res = svc.run_invoice(SAMPLES / "invoice_1001.txt")
    assert res.status == Status.PENDING_HUMAN and res.human.reason.value == "payment_failed"
    assert res.human.recommendation == Ceiling.APPROVE
    repo = svc.repo
    assert repo.budget_spent(MONTH) == 0 and repo.stock_levels()["WidgetA"] == 90
    rows = repo._conn.execute("SELECT status, attempts FROM payments").fetchall()
    assert [(r["status"], r["attempts"]) for r in rows] == [("FAILED", 4)]
    assert [q.reason.value for q in svc.list_queue()] == ["payment_failed"]

    again = make_service(provider=fake, payment_fail_rate=1.0)
    res2 = again.resolve("INV-1001", action="approve", note="retry")
    assert res2.status == Status.PENDING_HUMAN and res2.human.reason.value == "payment_failed"
    assert again.repo.budget_spent(MONTH) == 0
    assert [q.reason.value for q in again.list_queue()] == ["payment_failed"]

    ok = make_service(provider=fake, payment_fail_rate=0.0)
    res3 = ok.resolve("INV-1001", action="approve", note="retry")
    assert res3.status == Status.PAID and res3.payment.amount == D("5000.00")
    stock = ok.repo.stock_levels()
    assert (stock["WidgetA"], stock["WidgetB"]) == (80, 45)
    assert ok.repo.budget_spent(MONTH) == D("5000.00") and ok.list_queue() == []
    rows = ok.repo._conn.execute("SELECT status FROM payments").fetchall()
    assert [r["status"] for r in rows] == ["SUCCESS"]


def test_rename_alias_learned(make_service, tmp_path):
    fake = ScriptedFake(golden_script(), fallback={"vp": vp(Ceiling.APPROVE, "Known vendor under its new name; approve.")})
    svc = make_service(provider=fake)
    res = svc.run_invoice(SAMPLES / "invoice_1012.pdf")
    assert res.status == Status.PENDING_HUMAN
    paid = svc.resolve("INV-1012", action="approve", note="rename confirmed")
    assert paid.status == Status.PAID and paid.payment.amount == D("9975.00")
    stored = [(v.name, a.alias) for v in svc.repo.list_vendors() for a in svc.repo.list_aliases(v.id)]
    assert any("fastship" in n.lower() or "fastship" in al.lower() for n, al in stored), stored
    assert any("quickship" in n.lower() or "quickship" in al.lower() for n, al in stored), stored
    f = tmp_path / "invoice_9010.json"
    f.write_text(json.dumps({
        "invoice_number": "INV-9010",
        "vendor": {"name": "QuickShip Distributers (formerly FastShip Ltd.)"},
        "date": "2026-01-20", "due_date": "2026-02-19",
        "line_items": [{"item": "WidgetA", "quantity": 1, "unit_price": 250.00}],
        "total": 250.00, "payment_terms": "Net 30",
    }))
    second = make_service(provider=fake).run_invoice(f)
    codes = {x.code.value for x in second.flags}
    assert second.invoice_number == "INV-9010" and second.status == Status.PAID, (second.status, codes)
    assert "unreadable_document" not in codes
    assert "vendor_renamed" not in codes and "unknown_vendor" not in codes
