import random
from decimal import Decimal

from agents.payment import pay
from models import (
    Ceiling, Decision, DedupeKind, DedupeResult, Flag, FlagCode, HumanResolution, HumanReview, Status,
    VendorMatch, make_flag,
)
import datetime as dt

D = Decimal


def decision(amount, basis="stated_total"):
    return Decision(final=Ceiling.APPROVE, by="rules", reasoning="ok", payment_amount=D(amount), payment_basis=basis)


def setup(make_state, make_invoice, lines=(("WidgetA", 5, "250.00"),), amount="1250.00", **kw):
    inv = make_invoice(lines, **kw.pop("inv", {}))
    return make_state(
        inv, vendor=VendorMatch(kind="exact", vendor_id=1, master_name="Widgets Inc."),
        decision=decision(amount), **kw,
    )


def test_success_updates_stock_budget_receipt(make_state, make_invoice, make_deps, repo, memlog):
    st = setup(make_state, make_invoice)
    out = pay(st, make_deps())
    assert out["status"] == Status.PAID and out["payment_error"] is None
    r = out["payment"]
    assert r.idempotency_key == "1:INV-9001:R0"
    assert r.amount == D("1250.00") and r.amount_usd == D("1250.00") and r.attempts == 1
    assert r.bank_response == {"status": "success"}
    assert repo.stock_levels()["WidgetA"] == 85
    assert repo.budget_spent("2026-01") == D("1250.00")
    assert repo.get_invoice(st.invoice_id).status == Status.PAID
    assert repo.get_payment(r.idempotency_key).status == "SUCCESS"
    assert memlog.of("payment")
    assert "payment" in [a.event for a in repo.audit_for_trace(st.trace_id)]
    assert not any(e["event"] == "flag_raised" for e in memlog.events)


def test_idempotent_second_call(make_state, make_invoice, make_deps, repo):
    st = setup(make_state, make_invoice)
    deps = make_deps()
    first = pay(st, deps)
    second = pay(st, deps)
    assert second["payment"] == first["payment"] and second["status"] == Status.PAID
    assert repo.stock_levels()["WidgetA"] == 85
    assert repo.budget_spent("2026-01") == D("1250.00")


def test_all_attempts_fail(make_state, make_invoice, make_deps, repo, sleeps, memlog):
    st = setup(make_state, make_invoice)
    deps = make_deps(payment_fail_rate=1.0)
    out = pay(st, deps)
    assert sleeps == [0.5, 1.0, 2.0]
    assert len(memlog.of("retry")) == 3
    assert out["payment_error"] == "injected failure"
    assert [f.code for f in out["flags"]] == [FlagCode.PAYMENT_FAILED]
    assert "status" not in out and "payment" not in out
    assert repo.stock_levels()["WidgetA"] == 90
    assert repo.budget_spent("2026-01") == D("0")
    rec = repo.get_payment("1:INV-9001:R0")
    assert rec.status == "FAILED" and rec.attempts == 4
    assert repo.get_invoice(st.invoice_id).status != Status.PAID


def test_no_second_payment_failed_flag(make_state, make_invoice, make_deps):
    flag = make_flag(FlagCode.PAYMENT_FAILED, "earlier")
    st = setup(make_state, make_invoice, flags=[flag])
    out = pay(st, make_deps(payment_fail_rate=1.0))
    assert [f.code for f in out["flags"]] == [FlagCode.PAYMENT_FAILED]


class SeqRng:
    def __init__(self, values):
        self.values = list(values)

    def random(self):
        return self.values.pop(0)


def test_retry_then_success(make_state, make_invoice, make_deps, sleeps, repo):
    st = setup(make_state, make_invoice)
    out = pay(st, make_deps(payment_fail_rate=0.5, rng=SeqRng([0.1, 0.1, 0.9])))
    assert out["payment"].attempts == 3 and sleeps == [0.5, 1.0]
    assert repo.stock_levels()["WidgetA"] == 85


def test_retry_after_failure_overwrites_failed_row(make_state, make_invoice, make_deps, repo):
    st = setup(make_state, make_invoice)
    pay(st, make_deps(payment_fail_rate=1.0))
    out = pay(st, make_deps())
    assert out["status"] == Status.PAID
    assert repo.get_payment("1:INV-9001:R0").status == "SUCCESS"


def test_credit_due(make_state, make_invoice, make_deps, repo, memlog):
    st = setup(make_state, make_invoice, amount="-100.00")
    out = pay(st, make_deps(payment_fail_rate=1.0))
    assert out == {"status": Status.CREDIT_DUE, "payment_error": None}
    assert repo.get_invoice(st.invoice_id).status == Status.CREDIT_DUE
    assert repo.stock_levels()["WidgetA"] == 90
    assert repo.budget_spent("2026-01") == D("0")
    assert repo.get_payment("1:INV-9001:R0") is None
    assert "credit_due" in [a.event for a in repo.audit_for_trace(st.trace_id)]


def test_zero_amount_is_credit_due(make_state, make_invoice, make_deps):
    st = setup(make_state, make_invoice, amount="0.00")
    assert pay(st, make_deps())["status"] == Status.CREDIT_DUE


def test_revision_consumes_only_added_quantities(make_state, make_invoice, make_deps, repo):
    st = setup(
        make_state, make_invoice, lines=(("WidgetA", 15, "250.00"),), amount="1000.00",
        inv={"number": "INV-1004", "revision": "R1"},
        dedupe=DedupeResult(kind=DedupeKind.REVISION_OF_PAID, paid_to_date=D("3750"), added_quantities={"widgeta": 4}),
    )
    out = pay(st, make_deps())
    assert out["payment"].idempotency_key == "1:INV-1004:R1"
    assert repo.stock_levels()["WidgetA"] == 86
    assert repo.budget_spent("2026-01") == D("1000.00")


def test_key_falls_back_to_vendor_key(make_state, make_invoice, make_deps):
    inv = make_invoice()
    st = make_state(inv, vendor=VendorMatch(kind="unknown"), decision=decision("250.00"))
    out = pay(st, make_deps())
    assert out["payment"].idempotency_key == f"{inv.vendor_key}:INV-9001:R0"


def test_nothing_printed_to_stdout(make_state, make_invoice, make_deps, capsys):
    st = setup(make_state, make_invoice)
    out = pay(st, make_deps())
    cap = capsys.readouterr()
    assert cap.out == "" and cap.err == ""
    assert out["status"] == Status.PAID


def test_fx_payment(make_state, make_invoice, make_deps, repo):
    human = HumanReview(
        reason=FlagCode.UNREADABLE_DOCUMENT, reasons=[FlagCode.UNREADABLE_DOCUMENT], outcome="approved",
        resolution=HumanResolution(action="approve", note="ok", fx_rate=D("1.08"),
                                   resolved_at=dt.datetime(2026, 1, 31, tzinfo=dt.timezone.utc)),
    )
    st = setup(make_state, make_invoice, lines=(("WidgetA", 1, "250.00"),), amount="4125.00",
               inv={"currency": "EUR", "total": "4125.00"}, human=human)
    out = pay(st, make_deps())
    r = out["payment"]
    assert r.amount == D("4125.00") and r.currency == "EUR"
    assert r.fx_rate == D("1.08") and r.amount_usd == D("4455.00")
    assert repo.budget_spent("2026-01") == D("4455.00")


def test_non_usd_without_rate_fails_without_bank_call(make_state, make_invoice, make_deps, repo):
    st = setup(make_state, make_invoice, amount="100.00", inv={"currency": "EUR"})
    out = pay(st, make_deps())
    assert "FX" in out["payment_error"]
    assert repo.stock_levels()["WidgetA"] == 90
