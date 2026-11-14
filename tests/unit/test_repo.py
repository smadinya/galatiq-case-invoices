import datetime as dt
from decimal import Decimal
import sqlite3

import pytest

from models import (
    Ceiling, FlagCode, HumanReview, InvoiceResult, InvoiceUpdate, PaymentWrite, QueueWrite, Status,
)

D = Decimal


def new_invoice(repo, name="invoice_1.json", trace=None, file_hash=None, **update):
    trace = trace or f"t-{name}"
    iid = repo.create_invoice(trace_id=trace, file_path=f"/x/{name}", file_name=name, file_hash=file_hash or f"h-{name}")
    if update:
        repo.update_invoice(iid, InvoiceUpdate(**update))
    return iid


def payment(iid, *, key="widgets:INV-1:R0", amount="1000.00", deltas=None, status="PAID", month="2026-01"):
    return PaymentWrite(
        idempotency_key=key, invoice_id=iid, trace_id="t", vendor="Widgets Inc.", amount=D(amount), currency="USD",
        amount_usd=D(amount), attempts=1, month=month, stock_deltas=deltas or {"WidgetA": -4}, final_status=status,
        bank_response={"status": "success"},
    )


def review(reason=FlagCode.OVER_BUDGET):
    return HumanReview(reason=reason, reasons=[reason], recommendation=Ceiling.HUMAN)


def test_init_schema_idempotent(repo):
    repo.init_schema()
    repo.init_schema()
    assert len(repo.list_vendors()) == 11
    assert repo.get_meta("missing") is None
    repo.set_meta("k", "v1")
    repo.set_meta("k", "v2")
    assert repo.get_meta("k") == "v2"


def test_item_lookup_by_key(repo):
    assert repo.get_item_by_key("widgeta").item == "WidgetA"
    assert repo.get_item_by_key("widgetc") is None
    assert repo.stock_levels()["GadgetX"] == 13


def test_vendor_exact_and_alias_lookup(repo):
    fast = repo.find_vendor_by_key("fastship")
    assert fast and repo.find_vendor_by_key("quickship") is None
    assert repo.find_vendor_by_alias("quickship") is None
    repo.add_vendor_alias(fast.id, "QuickShip Distributers", "approved_rename")
    repo.add_vendor_alias(fast.id, "Quickship Distributers, Inc.", "approved_rename")  # same key: idempotent
    vendor, alias = repo.find_vendor_by_alias("quickshipdistributers")
    assert vendor.id == fast.id and alias.alias == "QuickShip Distributers" and alias.kind == "approved_rename"
    assert len(repo.list_aliases(fast.id)) == 1
    assert repo.get_vendor(fast.id).name == "FastShip Ltd."
    assert repo.get_vendor(9999) is None


def test_create_update_and_get_invoice(repo):
    iid = new_invoice(repo, "invoice_1001.txt")
    rec = repo.get_invoice(iid)
    assert rec.status is Status.PROCESSING and rec.fingerprint is None and rec.result is None
    vid = repo.find_vendor_by_key("widgets").id
    repo.update_invoice(
        iid,
        InvoiceUpdate(
            fingerprint="fp", vendor_id=vid, vendor_key="widgets", vendor_name="Widgets Inc.",
            invoice_number="INV-1001", status=Status.REJECTED, ceiling=Ceiling.REJECT, amount=D("9975.00"),
            currency="USD", amount_usd=D("9975.00"), invoice_date=dt.date(2026, 1, 15),
        ),
    )
    rec = repo.get_invoice(iid)
    assert rec.status is Status.REJECTED and rec.ceiling is Ceiling.REJECT
    assert rec.amount == D("9975.00") and str(rec.amount) == "9975.00"
    assert rec.invoice_date == dt.date(2026, 1, 15) and rec.vendor_id == vid
    assert repo.get_invoice_by_trace("t-invoice_1001.txt").id == iid
    assert repo.get_invoice(999) is None and repo.get_invoice_by_trace("nope") is None
    # only set fields are written
    repo.update_invoice(iid, InvoiceUpdate(revision="R1"))
    rec = repo.get_invoice(iid)
    assert rec.revision == "R1" and rec.amount == D("9975.00")
    # explicitly setting None clears
    repo.update_invoice(iid, InvoiceUpdate(amount=None))
    assert repo.get_invoice(iid).amount is None
    result = InvoiceResult(trace_id="t", file="invoice_1001.txt", status=Status.REJECTED, provider="fake",
                           amount=D("9975.00"))
    repo.update_invoice(iid, InvoiceUpdate(result=result))
    assert repo.get_invoice(iid).result == result
    assert len(repo.list_invoices()) == 1


def test_find_methods_with_exclusions(repo):
    a = new_invoice(repo, "a.json", file_hash="same", fingerprint="fp1", vendor_key="widgets",
                    invoice_number="INV-1", status=Status.PAID)
    b = new_invoice(repo, "b.json", file_hash="same", fingerprint="fp1", vendor_key="widgets",
                    invoice_number="INV-1", status=Status.PENDING_HUMAN)
    c = new_invoice(repo, "c.json", file_hash="same", fingerprint="fp1", vendor_key="widgets",
                    invoice_number="INV-1", status=Status.DUPLICATE)
    d = new_invoice(repo, "d.json", file_hash="same", status=Status.ERROR)
    e = new_invoice(repo, "e.json", fingerprint="fp1", vendor_key="widgets", invoice_number="INV-1",
                    status=Status.SUPERSEDED)
    new_invoice(repo, "f.json", fingerprint="fp1", vendor_key="widgets", invoice_number="INV-1")  # PROCESSING
    # file hash: newest non-ERROR
    assert repo.find_by_file_hash("same").id == c
    assert repo.find_by_file_hash("zzz") is None
    repo.mark_error(c, "boom")
    assert repo.get_invoice(c).status is Status.ERROR and repo.get_invoice(c).error == "boom"
    assert repo.find_by_file_hash("same").id == b
    # fingerprint: oldest live, honouring exclusion
    assert repo.find_by_fingerprint("fp1", exclude_id=99).id == a
    assert repo.find_by_fingerprint("fp1", exclude_id=a).id == b
    assert repo.find_by_fingerprint("nope", exclude_id=0) is None
    # vendor+number: live only, oldest first
    assert [r.id for r in repo.find_by_vendor_number("widgets", "INV-1", exclude_id=99)] == [a, b]
    assert [r.id for r in repo.find_by_vendor_number("widgets", "INV-1", exclude_id=a)] == [b]
    assert repo.find_by_vendor_number("widgets", "INV-2", exclude_id=0) == []
    # vendor_invoices: everything but ERROR, oldest first
    ids = [r.id for r in repo.vendor_invoices(vendor_id=None, vendor_key="widgets")]
    assert ids == [a, b, e, e + 1] and c not in ids and d not in ids
    ids = [r.id for r in repo.vendor_invoices(vendor_id=None, vendor_key="widgets", exclude_trace="t-a.json")]
    assert a not in ids
    assert repo.vendor_invoices(vendor_id=None, vendor_key=None) == []


def test_supersede_closes_pending_queue_row(repo):
    iid = new_invoice(repo, "x.json")
    repo.upsert_queue_item(QueueWrite(invoice_id=iid, trace_id="t-x.json", reason=FlagCode.OVER_BUDGET,
                                      review=review()))
    repo.supersede_invoice(iid)
    assert repo.get_invoice(iid).status is Status.SUPERSEDED
    assert repo.get_queue_item(iid).status == "SUPERSEDED"
    assert repo.list_queue() == []
    # a superseded row is never reopened
    repo.upsert_queue_item(QueueWrite(invoice_id=iid, trace_id="t-x.json", reason=FlagCode.OVER_BUDGET,
                                      review=review()))
    assert repo.get_queue_item(iid).status == "SUPERSEDED"


def test_record_payment_is_one_transaction(repo):
    iid = new_invoice(repo, "p.json", vendor_key="widgets", invoice_number="INV-1")
    receipt = repo.record_payment(payment(iid, deltas={"WidgetA": -4, "WidgetB": -2}))
    assert receipt.amount == D("1000.00") and receipt.attempts == 1
    rec = repo.get_payment("widgets:INV-1:R0")
    assert rec.status == "SUCCESS" and rec.receipt == receipt and rec.amount == D("1000.00")
    assert repo.stock_levels()["WidgetA"] == 86 and repo.stock_levels()["WidgetB"] == 48
    entries = repo.stock_ledger(iid)
    assert sorted((e.item, e.delta, e.reason) for e in entries) == [("WidgetA", -4, "payment"),
                                                                     ("WidgetB", -2, "payment")]
    assert repo.budget_entries("2026-01") == [(iid, D("1000.00"))]
    assert repo.get_invoice(iid).status is Status.PAID
    # repeat call returns the existing receipt with no second effect
    assert repo.record_payment(payment(iid)) == receipt
    assert repo.stock_levels()["WidgetA"] == 86 and len(repo.budget_entries("2026-01")) == 1


def test_record_payment_rolls_back_when_stock_would_go_negative(repo):
    iid = new_invoice(repo, "p.json")
    with pytest.raises(sqlite3.IntegrityError):
        repo.record_payment(payment(iid, deltas={"WidgetA": -4, "GadgetX": -14}))
    assert repo.stock_levels() == {"WidgetA": 90, "WidgetB": 50, "GadgetX": 13, "FakeItem": 0}
    assert repo.get_payment("widgets:INV-1:R0") is None
    assert repo.budget_entries("2026-01") == []
    assert len(repo.stock_ledger(iid)) == 0
    assert repo.get_invoice(iid).status is Status.PROCESSING
    # the connection is still usable and not stuck in a transaction
    repo.record_payment(payment(iid, deltas={"WidgetA": -1}))
    assert repo.stock_levels()["WidgetA"] == 89


def test_failed_payment_row_then_success(repo):
    iid = new_invoice(repo, "p.json")
    repo.record_payment_failure(idempotency_key="k", invoice_id=iid, amount=D("500.00"), currency="USD",
                                attempts=4, error="bank down")
    rec = repo.get_payment("k")
    assert rec.status == "FAILED" and rec.attempts == 4 and rec.error == "bank down" and rec.receipt is None
    repo.record_payment_failure(idempotency_key="k", invoice_id=iid, amount=D("500.00"), currency="USD",
                                attempts=8, error="still down")
    assert repo.get_payment("k").attempts == 8
    assert repo.budget_entries("2026-01") == []
    repo.record_payment(payment(iid, key="k", amount="500.00", deltas={"WidgetA": -2}))
    rec = repo.get_payment("k")
    assert rec.status == "SUCCESS" and rec.error is None and rec.receipt is not None
    # a later failure record never downgrades a success
    repo.record_payment_failure(idempotency_key="k", invoice_id=iid, amount=D("500.00"), currency="USD",
                                attempts=1, error="late")
    assert repo.get_payment("k").status == "SUCCESS"


def test_paid_to_date(repo):
    a = new_invoice(repo, "a.json", vendor_key="widgets", invoice_number="INV-1")
    b = new_invoice(repo, "b.json", vendor_key="widgets", invoice_number="INV-1", revision="R1")
    other = new_invoice(repo, "o.json", vendor_key="gadgets", invoice_number="INV-1")
    assert repo.paid_to_date("widgets", "INV-1") == D("0")
    repo.record_payment(payment(a, key="k1", amount="1890.00", deltas={"WidgetA": -1}))
    repo.record_payment_failure(idempotency_key="k2", invoice_id=b, amount=D("999"), currency="USD", attempts=4,
                                error="x")
    repo.record_payment(payment(other, key="k3", amount="77.00", deltas={"WidgetA": -1}))
    assert repo.paid_to_date("widgets", "INV-1") == D("1890.00")
    repo.record_payment(payment(b, key="k2", amount="4050.00", deltas={"WidgetA": -1}))
    assert repo.paid_to_date("widgets", "INV-1") == D("5940.00")


def test_budget_status(repo):
    a = new_invoice(repo, "a.json")
    b = new_invoice(repo, "b.json")
    repo.record_payment(payment(a, key="k1", amount="9975.00", deltas={"WidgetA": -1}))
    repo.record_payment(payment(b, key="k2", amount="100.50", deltas={"WidgetA": -1}))
    repo.record_payment(payment(b, key="k3", amount="5.00", deltas={"WidgetA": -1}, month="2026-02"))
    assert repo.budget_spent("2026-01") == D("10075.50")
    st = repo.budget_status("2026-01", D("50000"))
    assert (st.spent, st.remaining, st.limit, st.month) == (D("10075.50"), D("39924.50"), D("50000"), "2026-01")
    assert repo.budget_status("2025-12", D("50000")).spent == D("0")
    assert [x for _, x in repo.budget_entries("2026-01")] == [D("9975.00"), D("100.50")]


def test_queue_upsert_idempotent_and_reopens_resolved(repo):
    iid = new_invoice(repo, "q.json", invoice_number="INV-1004", revision="R1", vendor_name="Precision Parts Ltd.")
    w = QueueWrite(invoice_id=iid, trace_id="t-q.json", reason=FlagCode.REVISION_OF_PAID_INVOICE,
                   review=review(FlagCode.REVISION_OF_PAID_INVOICE))
    repo.upsert_queue_item(w)
    repo.upsert_queue_item(w)
    items = repo.list_queue()
    assert len(items) == 1
    it = items[0]
    assert it.status == "PENDING" and it.invoice_ref == "INV-1004 R1" and it.vendor == "Precision Parts Ltd."
    assert it.review.reason is FlagCode.REVISION_OF_PAID_INVOICE and it.reason is FlagCode.REVISION_OF_PAID_INVOICE
    # a pending row is refreshed
    repo.upsert_queue_item(w.model_copy(update={"review": review(FlagCode.OVER_BUDGET), "reason": FlagCode.OVER_BUDGET}))
    assert repo.get_queue_item(iid).reason is FlagCode.OVER_BUDGET
    repo.resolve_queue_item(iid, resolution="approve", note="ok")
    done = repo.get_queue_item(iid)
    assert done.status == "RESOLVED" and done.resolution == "approve" and done.note == "ok" and done.resolved_at
    assert repo.list_queue() == [] and len(repo.list_queue(None)) == 1
    assert len(repo.list_queue("RESOLVED")) == 1
    # resolving twice is a no-op
    repo.resolve_queue_item(iid, resolution="reject", note="again")
    assert repo.get_queue_item(iid).resolution == "approve"
    # an approved invoice whose payment then failed goes back to the queue
    repo.upsert_queue_item(w.model_copy(update={"review": review(FlagCode.PAYMENT_FAILED), "reason": FlagCode.PAYMENT_FAILED}))
    back = repo.get_queue_item(iid)
    assert back.status == "PENDING" and back.reason is FlagCode.PAYMENT_FAILED
    assert back.resolution is None and back.note is None and back.resolved_at is None
    # unknown invoice raises
    with pytest.raises(ValueError):
        repo.resolve_queue_item(12345, resolution="reject", note="x")
    repo.discard_queue_item(iid)
    assert repo.get_queue_item(iid) is None


def test_queue_ordering_oldest_first(repo):
    ids = [new_invoice(repo, f"o{i}.json", invoice_number=f"INV-{i}") for i in range(3)]
    for iid in reversed(ids):
        repo.upsert_queue_item(QueueWrite(invoice_id=iid, trace_id=f"t{iid}", reason=FlagCode.OVER_BUDGET,
                                          review=review()))
    assert sorted(i.invoice_id for i in repo.list_queue()) == ids


def test_find_pending_reference_forms(repo):
    a = new_invoice(repo, "invoice_1004.json", trace="inv-invoice_1004-aaaa0000", invoice_number="INV-1004")
    b = new_invoice(repo, "invoice_1004_revised.json", trace="inv-invoice_1004_revised-bbbb0000",
                    invoice_number="INV-1004", revision="R1")
    c = new_invoice(repo, "invoice_1012.pdf", trace="inv-invoice_1012-cccc0000", invoice_number="INV-1012")
    n = new_invoice(repo, "weird.txt", trace="inv-weird-dddd0000")  # no invoice number
    for iid in (a, b, c, n):
        repo.upsert_queue_item(QueueWrite(invoice_id=iid, trace_id=repo.get_invoice(iid).trace_id,
                                          reason=FlagCode.OVER_BUDGET, review=review()))

    def ids(ref):
        return sorted(i.invoice_id for i in repo.find_pending(ref))

    for ref in ("INV-1012", "INV 1012", "1012", "inv-1012", "invoice_1012.pdf", "inv-invoice_1012-cccc0000"):
        assert ids(ref) == [c], ref
    assert ids("INV-1004 R1") == [b]
    assert ids("INV-1004-R1") == [b]
    assert ids("1004 r1") == [b]
    assert ids("INV-1004") == sorted([a, b])  # ambiguous: caller decides
    assert ids("invoice_1004_revised.json") == [b]
    assert ids("weird.txt") == [n] and ids("weird") == [n]
    assert ids("INV-9999") == [] and ids("nonsense") == []
    assert repo.get_queue_item(n).invoice_ref == "weird.txt"
    repo.resolve_queue_item(c, resolution="reject", note="n")
    assert ids("INV-1012") == []  # only pending items match


def test_audit_order_and_json(repo):
    repo.audit(trace_id="t1", actor="system", event="first", payload={"a": 1})
    repo.audit(trace_id="t2", actor="vp", event="other", payload={})
    repo.audit(trace_id="t1", actor="human", event="second", payload={"amount": D("1.50"), "d": dt.date(2026, 1, 1)})
    entries = repo.audit_for_trace("t1")
    assert [e.event for e in entries] == ["first", "second"]
    assert entries[0].payload == {"a": 1} and entries[1].actor == "human"
    assert entries[1].payload["amount"] == "1.50" and entries[0].ts.tzinfo is not None
    assert repo.audit_for_trace("zz") == []


def test_decimal_text_round_trip(repo):
    iid = new_invoice(repo, "d.json")
    repo.update_invoice(iid, InvoiceUpdate(amount=D("9975.00"), amount_usd=D("10000.10")))
    raw = repo._conn.execute("SELECT amount, amount_usd FROM invoices WHERE id=?", (iid,)).fetchone()
    assert (raw["amount"], raw["amount_usd"]) == ("9975.00", "10000.10")
    rec = repo.get_invoice(iid)
    assert rec.amount == D("9975.00") and str(rec.amount) == "9975.00"
    repo.record_payment(payment(iid, amount="9975.00", deltas={"WidgetA": -1}))
    raw = repo._conn.execute("SELECT amount, amount_usd FROM payments").fetchone()
    assert (raw["amount"], raw["amount_usd"]) == ("9975.00", "9975.00")
    assert str(repo.budget_entries("2026-01")[0][1]) == "9975.00"
    assert repo._conn.execute("SELECT unit_price FROM inventory WHERE item='WidgetA'").fetchone()[0] == "250.00"


def test_nested_transaction_joins_outer(repo):
    with pytest.raises(RuntimeError):
        with repo.transaction():
            repo.set_meta("a", "1")
            with repo.transaction():
                repo.set_meta("b", "2")
            raise RuntimeError("boom")
    assert repo.get_meta("a") is None and repo.get_meta("b") is None
    with repo.transaction():
        with repo.transaction():
            repo.set_meta("c", "3")
    assert repo.get_meta("c") == "3"
