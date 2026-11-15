import datetime as dt
from decimal import Decimal as D

from models import BudgetLedgerEntry, InvoiceUpdate, PaymentWrite


def _invoice(repo, name):
    iid = repo.create_invoice(trace_id=f"t-{name}", file_path=f"/x/{name}", file_name=name, file_hash=f"h-{name}")
    repo.update_invoice(iid, InvoiceUpdate(vendor_key="widgets", invoice_number=name))
    return iid


def _pay(repo, iid, key, amount, month="2026-01"):
    repo.record_payment(PaymentWrite(
        idempotency_key=key, invoice_id=iid, trace_id="t", vendor="Widgets Inc.", amount=D(amount), currency="USD",
        amount_usd=D(amount), attempts=1, month=month, stock_deltas={"WidgetA": -1}, final_status="PAID",
        bank_response={"status": "success"},
    ))


def test_list_payments_empty_on_fresh_db(repo):
    assert repo.list_payments() == []


def test_list_payments_success_and_failed_in_id_order(repo):
    a, b = _invoice(repo, "a.json"), _invoice(repo, "b.json")
    repo.record_payment_failure(idempotency_key="k-fail", invoice_id=a, amount=D("500.00"), currency="USD",
                                attempts=4, error="bank down")
    _pay(repo, b, "k-ok", "9975.00")
    rows = repo.list_payments()
    assert [(p.idempotency_key, p.status) for p in rows] == [("k-fail", "FAILED"), ("k-ok", "SUCCESS")]
    assert rows[0].amount == D("500.00") and rows[1].amount == D("9975.00")
    assert isinstance(rows[1].amount, D) and rows[1].amount_usd == D("9975.00")
    for p in rows:
        assert isinstance(p.created_at, dt.datetime) and isinstance(p.updated_at, dt.datetime)
    assert rows[0].error == "bank down" and rows[1].receipt is not None


def test_budget_ledger_with_and_without_month(repo):
    a, b, c = _invoice(repo, "a.json"), _invoice(repo, "b.json"), _invoice(repo, "c.json")
    _pay(repo, a, "k1", "9975.00", month="2026-01")
    _pay(repo, b, "k2", "120.50", month="2026-02")
    _pay(repo, c, "k3", "10.00", month="2026-01")
    assert repo.budget_ledger() != [] and [e.id for e in repo.budget_ledger()] == sorted(e.id for e in repo.budget_ledger())
    jan = repo.budget_ledger("2026-01")
    assert [(e.invoice_id, e.amount_usd, e.month) for e in jan] == [
        (a, D("9975.00"), "2026-01"), (c, D("10.00"), "2026-01")]
    assert all(isinstance(e, BudgetLedgerEntry) and isinstance(e.ts, dt.datetime) for e in jan)
    assert str(jan[0].amount_usd) == "9975.00"
    assert [e.invoice_id for e in repo.budget_ledger()] == [a, b, c]
    assert repo.budget_ledger("2030-01") == []


def test_budget_ledger_empty_on_fresh_db(repo):
    assert repo.budget_ledger() == []
