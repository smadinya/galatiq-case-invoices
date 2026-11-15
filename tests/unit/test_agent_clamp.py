from decimal import Decimal as D

import pytest

from agents.clamp import clamp_decision, decide
from core.invoice_math import recompute_totals
from llm.fake import critic, vp
from models import Ceiling, DedupeKind, DedupeResult, Verdict

A, H, R = Ceiling.APPROVE, Ceiling.HUMAN, Ceiling.REJECT


def mk(make_state, make_invoice, ceiling, vp_dec, crit=None, lines=(("WidgetA", 2, "250.00"), ), usd=None, **inv):
    invoice = make_invoice(list(lines), **inv)
    return make_state(
        invoice, ceiling=ceiling, vp_output=vp(vp_dec, "because"), critic_output=crit,
        totals=recompute_totals(invoice), amount_usd=usd if usd is not None else invoice.total,
    )


def test_fast_path(make_state, make_invoice, settings):
    d, ev = decide(mk(make_state, make_invoice, A, A), settings)
    assert (d.final, d.by, d.clamped, d.payment_amount, d.payment_basis) == (A, "vp", False, D("500.00"), "stated_total")
    assert ev == [] and d.critic_verdict is None


def test_agree(make_state, make_invoice, settings):
    d, _ = decide(mk(make_state, make_invoice, A, A, critic(Verdict.AGREE, A)), settings)
    assert (d.final, d.by, d.critic_verdict) == (A, "vp+critic", Verdict.AGREE)


def test_deadlock_stricter(make_state, make_invoice, settings):
    obj = critic(Verdict.OBJECT, R, objections=[("a", "b", "c")])
    d, _ = decide(mk(make_state, make_invoice, A, A, obj), settings)
    assert (d.final, d.by, d.proposed, d.critic_proposed) == (R, "deadlock", A, R)
    assert d.payment_amount is None
    d2, _ = decide(mk(make_state, make_invoice, A, R, critic(Verdict.OBJECT, A)), settings)
    assert d2.final == R


def test_ceiling_clamp(make_state, make_invoice, settings):
    d, ev = decide(mk(make_state, make_invoice, H, A), settings)
    assert d.final == H and d.clamped and ev == ["ceiling_clamped"]


def test_human_without_human_ceiling_becomes_reject(make_state, make_invoice, settings):
    d, ev = decide(mk(make_state, make_invoice, A, H), settings)
    assert d.final == R and d.clamped and "human_not_allowed" in ev and d.payment_amount is None


def test_over_threshold_needs_agree(make_state, make_invoice, settings):
    lines = (("WidgetA", 50, "250.00"),)  # 12,500
    d, ev = decide(mk(make_state, make_invoice, A, A, lines=lines), settings)
    assert d.final == R and ev == ["critic_agreement_required"]
    d, ev = decide(mk(make_state, make_invoice, A, A, critic(Verdict.AGREE, A), lines=lines), settings)
    assert d.final == A and ev == []
    # deadlock where VP and Critic both say approve is impossible; object -> stricter handles it
    d, _ = decide(mk(make_state, make_invoice, A, A, critic(Verdict.OBJECT, A), lines=lines), settings)
    assert d.final == R


def test_payable_amounts(make_state, make_invoice, settings):
    # 1007: stated total 110 below recomputed -> lower total
    inv = make_invoice([("WidgetA", 50, "250.00"), ("WidgetB", 5, "500.00")], tax_rate="0.06", total="15525.00")
    st = make_state(inv, ceiling=A, vp_output=vp(A, "x"), critic_output=critic(Verdict.AGREE, A),
                    totals=recompute_totals(inv), amount_usd=D("15525.00"))
    d, _ = decide(st, settings)
    assert (d.payment_amount, d.payment_basis) == (D("15525.00"), "lower_total")
    # 1010: stated total 7185.00 is used
    inv = make_invoice([("WidgetA", 1, "250.00")], total="7185.00", subtotal="250.00", tax_amount="6935.00")
    st = make_state(inv, ceiling=A, vp_output=vp(A, "x"), totals=recompute_totals(inv), amount_usd=D("7185"))
    st = st.model_copy(update={"critic_output": critic(Verdict.AGREE, A)})
    d, _ = decide(st, settings)
    assert d.payment_amount == D("7185.00") and d.payment_basis == "stated_total"
    # 1004 R1 revision difference 4050.00 (R1 total 5940, paid 1890)
    inv = make_invoice([("WidgetA", 1, "250.00")], total="5940.00", subtotal="250.00", tax_amount="5690.00")
    st = make_state(inv, ceiling=H, vp_output=vp(H, "x"), critic_output=critic(Verdict.AGREE, H),
                    totals=recompute_totals(inv), amount_usd=D("5940"),
                    dedupe=DedupeResult(kind=DedupeKind.REVISION_OF_PAID, paid_to_date=D("1890.00")))
    d, _ = decide(st, settings)
    assert (d.final, d.payment_amount, d.payment_basis) == (H, D("4050.00"), "revision_difference")
    # 1014: EUR 4125.00
    inv = make_invoice([("WidgetA", 1, "250.00")], currency="EUR", total="4125.00", subtotal="250.00",
                       tax_amount="3875.00")
    st = make_state(inv, ceiling=H, vp_output=vp(H, "x"), critic_output=critic(Verdict.AGREE, H),
                    totals=recompute_totals(inv), amount_usd=None)
    d, _ = decide(st, settings)
    assert d.payment_amount == D("4125.00")


def test_clamp_node_logs_and_audits(make_state, make_invoice, make_deps, repo):
    st = mk(make_state, make_invoice, A, H)
    deps = make_deps()
    out = clamp_decision(st, deps)
    assert out["decision"].final == R
    ev = deps.log.of("ceiling_clamped")
    assert [e["data"]["kind"] for e in ev] == ["human_not_allowed"]
    assert any(a.event == "decision_final" for a in repo.audit_for_trace(st.trace_id))
