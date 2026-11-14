import datetime as dt
from decimal import Decimal as D

import pytest

from agents.clamp import decide
from agents.human import apply_resolution, assess_resolution, await_human, prepare_human
from core.invoice_math import recompute_totals
from core.text import vendor_key
from llm.fake import critic, vp
from models import (
    Ceiling, Decision, DedupeKind, DedupeResult, FlagCode, HumanResolution, Status, Verdict, VendorMatch, make_flag,
)

NOW = dt.datetime(2026, 1, 31, tzinfo=dt.timezone.utc)
H = Ceiling.HUMAN


def res(action="approve", note="checked", **kw):
    return HumanResolution(action=action, note=note, resolved_at=NOW, **kw)


def queued(make_state, make_invoice, make_deps, lines, codes, *, vmatch=None, dedupe=None, **inv):
    invoice = make_invoice(lines, **inv)
    flags = [make_flag(c, "m") for c in codes]
    st = make_state(invoice, flags=flags, ceiling=H, vp_output=vp(H, "person please"),
                    critic_output=critic(Verdict.AGREE, H), totals=recompute_totals(invoice),
                    amount_usd=None if invoice.currency != "USD" else invoice.total,
                    vendor=vmatch, dedupe=dedupe)
    deps = make_deps()
    st = st.model_copy(update={"decision": decide(st, deps.settings)[0]})
    st = st.model_copy(update=prepare_human(st, deps))
    return st, deps


def spend(repo, month, usd):
    # push the budget ledger to a given spent amount via a payment record
    from models import PaymentWrite
    iid = repo.create_invoice(trace_id="t-spend", file_path="p", file_name="spend.json", file_hash="hh")
    repo.record_payment(PaymentWrite(
        idempotency_key="k-spend", invoice_id=iid, trace_id="t-spend", vendor="v", amount=usd, currency="USD",
        amount_usd=usd, attempts=1, month=month, stock_deltas={}, bank_response={}))


def test_prepare_human_priority_queue_and_status(make_state, make_invoice, make_deps, repo):
    st, deps = queued(make_state, make_invoice, make_deps, [("WidgetA", 1, "250.00")],
                      [FlagCode.OVER_BUDGET, FlagCode.FOREIGN_CURRENCY, FlagCode.VENDOR_RENAMED])
    h = st.human
    assert h.reason == FlagCode.VENDOR_RENAMED
    assert h.reasons == [FlagCode.VENDOR_RENAMED, FlagCode.FOREIGN_CURRENCY, FlagCode.OVER_BUDGET]
    assert h.recommendation == H and h.recommendation_reasoning == "person please"
    assert h.payment_amount == D("250.00") and h.currency == "USD"
    assert st.status == Status.PENDING_HUMAN
    q = repo.get_queue_item(st.invoice_id)
    assert q.status == "PENDING" and q.reason == FlagCode.VENDOR_RENAMED
    assert repo.get_invoice(st.invoice_id).status == Status.PENDING_HUMAN
    assert any(a.event == "human_queued" for a in repo.audit_for_trace(st.trace_id))


def test_prepare_human_unreadable_has_no_recommendation(make_state, make_deps, repo):
    st = make_state(None, flags=[make_flag(FlagCode.UNREADABLE_DOCUMENT, "bad")], vp_output=vp(H, "x"))
    out = prepare_human(st, make_deps())
    assert out["human"].reason == FlagCode.UNREADABLE_DOCUMENT
    assert out["human"].recommendation is None and out["human"].recommendation_reasoning is None
    assert repo.get_queue_item(st.invoice_id).invoice_ref == st.file_name


def test_prepare_human_reopens_resolved_row(make_state, make_invoice, make_deps, repo):
    st, deps = queued(make_state, make_invoice, make_deps, [("WidgetA", 1, "250.00")], [FlagCode.FOREIGN_CURRENCY],
                      currency="EUR")
    repo.resolve_queue_item(st.invoice_id, resolution="approve", note="n")
    st2 = st.model_copy(update={"flags": st.flags + [make_flag(FlagCode.PAYMENT_FAILED, "bank")]})
    out = prepare_human(st2, deps)
    assert out["human"].reason == FlagCode.PAYMENT_FAILED
    assert repo.get_queue_item(st.invoice_id).status == "PENDING"


def test_assess_unreadable(make_state, make_deps):
    st = make_state(None, flags=[make_flag(FlagCode.UNREADABLE_DOCUMENT, "bad")])
    deps = make_deps()
    st = st.model_copy(update=prepare_human(st, deps))
    p = assess_resolution(st, deps, None)
    assert not p.can_approve and p.approve_outcome == "blocked"
    out = apply_resolution(st, deps, res())
    assert out["human"].outcome == "requeued"
    out = apply_resolution(st, deps, res("reject", "no good"))
    assert out["human"].outcome == "rejected"


def test_reject(make_state, make_invoice, make_deps, repo):
    st, deps = queued(make_state, make_invoice, make_deps, [("WidgetA", 1, "250.00")], [FlagCode.VENDOR_RENAMED])
    out = apply_resolution(st, deps, res("reject", "not legit"))
    assert out["human"].outcome == "rejected" and out["human"].resolution.note == "not legit"
    d = out["decision"]
    assert (d.final, d.by, d.reasoning) == (Ceiling.REJECT, "human", "not legit")
    q = repo.get_queue_item(st.invoice_id)
    assert (q.status, q.resolution, q.note) == ("RESOLVED", "reject", "not legit")
    ev = [a for a in repo.audit_for_trace(st.trace_id) if a.event == "human_resolved"]
    assert ev and ev[0].actor == "human"


def test_stock_short_rejects_without_llm(make_state, make_invoice, make_deps, repo):
    st, deps = queued(make_state, make_invoice, make_deps, [("GadgetX", 20, "750.00")], [FlagCode.VENDOR_RENAMED])
    p = assess_resolution(st, deps, None)
    assert p.approve_outcome == "reject_stock"
    assert [(s.item, s.needed, s.available) for s in p.stock_shortfalls] == [("GadgetX", 20, 13)]
    out = apply_resolution(st, deps, res())
    assert out["human"].outcome == "rejected_stock"
    assert out["decision"].final == Ceiling.REJECT and out["decision"].by == "human"
    assert "GadgetX" in out["decision"].reasoning and "needed 20" in out["decision"].reasoning
    q = repo.get_queue_item(st.invoice_id)
    assert q.status == "RESOLVED" and q.resolution == "approve"
    assert "amount_usd" not in out


def test_revision_stock_uses_added_quantities(make_state, make_invoice, make_deps):
    ded = DedupeResult(kind=DedupeKind.REVISION_OF_PAID, paid_to_date=D("100.00"), added_quantities={"gadgetx": 14})
    st, deps = queued(make_state, make_invoice, make_deps, [("GadgetX", 20, "750.00")],
                      [FlagCode.REVISION_OF_PAID_INVOICE], dedupe=ded)
    p = assess_resolution(st, deps, None)
    assert [(s.item, s.needed) for s in p.stock_shortfalls] == [("GadgetX", 14)]
    assert p.payment_amount == D("14900.00")


def test_overspend_requires_confirmation_and_alias_stored(make_state, make_invoice, make_deps, repo):
    vid = repo.find_vendor_by_key(vendor_key("FastShip Ltd.")).id
    spend(repo, "2026-01", D("42000.00"))
    st, deps = queued(make_state, make_invoice, make_deps, [("WidgetA", 39, "250.00"), ("WidgetB", 1, "225.00")],
                      [FlagCode.VENDOR_RENAMED], vmatch=VendorMatch(kind="renamed", vendor_id=vid),
                      vendor="QuickShip Distributers", former_name="FastShip Ltd.")
    p = assess_resolution(st, deps, None)
    assert p.approve_outcome == "pay" and p.payment_amount == D("9975.00")
    assert p.overspend_usd == D("1975.00")
    assert p.needs_overspend_confirmation
    out = apply_resolution(st, deps, res())
    assert out["human"].outcome == "requeued" and "decision" not in out
    assert repo.get_queue_item(st.invoice_id).status == "PENDING"
    assert not repo.list_aliases(vid)
    out = apply_resolution(st, deps, res(confirm_overspend=True))
    assert out["human"].outcome == "approved"
    d = out["decision"]
    assert (d.final, d.by, d.payment_amount, d.payment_basis) == (Ceiling.APPROVE, "human", D("9975.00"), "stated_total")
    assert out["amount_usd"] == D("9975.00")
    assert repo.get_queue_item(st.invoice_id).status == "RESOLVED"
    assert any(a.alias == "QuickShip Distributers" and a.kind == "approved_rename" for a in repo.list_aliases(vid))


def test_exact_overspend_figure(make_state, make_invoice, make_deps, repo):
    spend(repo, "2026-01", D("41850.00"))
    st, deps = queued(make_state, make_invoice, make_deps, [("WidgetA", 1, "250.00")], [FlagCode.VENDOR_RENAMED],
                      total="9975.00", subtotal="250.00", tax_amount="9725.00")
    p = assess_resolution(st, deps, None)
    assert p.payment_amount == D("9975.00") and p.overspend_usd == D("1825.00")


def test_fx_required_then_rate(make_state, make_invoice, make_deps, repo):
    spend(repo, "2026-01", D("51825.00"))
    st, deps = queued(make_state, make_invoice, make_deps, [("WidgetA", 1, "250.00")], [FlagCode.FOREIGN_CURRENCY],
                      currency="EUR", total="4125.00", subtotal="250.00", tax_amount="3875.00")
    p = assess_resolution(st, deps, None)
    assert p.needs_fx_rate and p.approve_outcome == "blocked" and p.payment_amount_usd is None
    out = apply_resolution(st, deps, res())
    assert out["human"].outcome == "requeued"
    rate = D("1.08")
    p = assess_resolution(st, deps, rate)
    assert p.payment_amount_usd == D("4455.00") and p.overspend_usd == D("6280.00")
    assert p.needs_overspend_confirmation
    assert apply_resolution(st, deps, res(fx_rate=rate))["human"].outcome == "requeued"
    out = apply_resolution(st, deps, res(fx_rate=rate, confirm_overspend=True))
    assert out["human"].outcome == "approved" and out["amount_usd"] == D("4455.00")
    assert out["human"].resolution.fx_rate == rate
    assert out["decision"].payment_amount == D("4125.00")


def test_credit_due(make_state, make_invoice, make_deps):
    ded = DedupeResult(kind=DedupeKind.REVISION_OF_PAID, paid_to_date=D("600.00"))
    st, deps = queued(make_state, make_invoice, make_deps, [("WidgetA", 2, "250.00")],
                      [FlagCode.REVISION_OF_PAID_INVOICE], dedupe=ded)
    p = assess_resolution(st, deps, None)
    assert p.payment_amount == D("-100.00") and p.approve_outcome == "credit_due"
    assert not p.needs_overspend_confirmation
    out = apply_resolution(st, deps, res())
    assert out["human"].outcome == "approved" and out["decision"].payment_amount == D("-100.00")


def test_requeued_is_total_when_queue_row_missing(make_state, make_invoice, make_deps, repo):
    st, deps = queued(make_state, make_invoice, make_deps, [("WidgetA", 1, "250.00")], [FlagCode.VENDOR_RENAMED])
    repo.discard_queue_item(st.invoice_id)
    out = apply_resolution(st, deps, res())  # resolve_queue_item raises ValueError; must not propagate
    assert out["human"].outcome == "requeued"
    assert deps.log.of("error")


def test_await_human_interrupts_then_applies(make_state, make_invoice, make_deps, monkeypatch):
    import agents.human as mod
    st, deps = queued(make_state, make_invoice, make_deps, [("WidgetA", 1, "250.00")], [FlagCode.VENDOR_RENAMED])
    seen = {}

    def fake_interrupt(value):
        seen.update(value)
        return res().model_dump(mode="json")

    monkeypatch.setattr(mod, "interrupt", fake_interrupt)
    out = await_human(st, deps)
    assert seen["invoice_id"] == st.invoice_id and seen["review"]["reason"] == "vendor_renamed"
    assert out["human"].outcome == "approved"
    monkeypatch.setattr(mod, "interrupt", lambda v: {"garbage": 1})
    assert await_human(st, deps)["human"].outcome == "requeued"
