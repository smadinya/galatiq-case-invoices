from decimal import Decimal

import pytest

from agents.risk import score_risk
from agents.validation import compute_ceiling, compute_needs_critic
from models import Ceiling, FlagCode, make_flag

D = Decimal


def f(code):
    return make_flag(code, "x")


@pytest.mark.parametrize(
    "ceiling,flags,amount,expected",
    [
        (Ceiling.APPROVE, [], D("8999.99"), False),
        (Ceiling.APPROVE, [], D("9000"), True),
        (Ceiling.APPROVE, [], D("9000.01"), True),
        (Ceiling.APPROVE, [], None, True),
        (Ceiling.HUMAN, [f(FlagCode.FOREIGN_CURRENCY)], D("100"), True),
        (Ceiling.APPROVE, [f(FlagCode.MATH_MISMATCH)], D("100"), True),
        (Ceiling.APPROVE, [f(FlagCode.UNKNOWN_VENDOR)], D("100"), True),
        (Ceiling.APPROVE, [f(FlagCode.PRICE_ABOVE_CATALOG)], D("100"), True),
        (Ceiling.APPROVE, [f(FlagCode.PAST_DUE), f(FlagCode.PRICE_BELOW_CATALOG)], D("100"), False),
    ],
)
def test_needs_critic_table(settings, ceiling, flags, amount, expected):
    assert compute_needs_critic(ceiling, flags, amount, settings) is expected


def test_compute_ceiling_node_reject(make_state, make_deps, make_invoice):
    state = make_state(make_invoice(), flags=[f(FlagCode.ZERO_STOCK_ITEM), f(FlagCode.FOREIGN_CURRENCY)],
                       amount_usd=D("100"))
    deps = make_deps()
    out = compute_ceiling(state, deps)
    assert out["ceiling"] == Ceiling.REJECT
    assert out["needs_critic"] is False
    events = [a.event for a in deps.repo.audit_for_trace(state.trace_id)]
    assert events == ["ceiling"]
    assert deps.repo.get_invoice(state.invoice_id).ceiling == Ceiling.REJECT


def test_compute_ceiling_node_human_and_approve(make_state, make_deps, make_invoice):
    deps = make_deps()
    s1 = make_state(make_invoice(), flags=[f(FlagCode.FOREIGN_CURRENCY)], amount_usd=None)
    assert compute_ceiling(s1, deps) == {"ceiling": Ceiling.HUMAN, "needs_critic": True}
    s2 = make_state(make_invoice(), flags=[f(FlagCode.PAST_DUE)], amount_usd=D("500"))
    assert compute_ceiling(s2, deps) == {"ceiling": Ceiling.APPROVE, "needs_critic": False}


def test_compute_ceiling_does_not_mutate_state(make_state, make_deps, make_invoice):
    state = make_state(make_invoice(), flags=[f(FlagCode.PAST_DUE)], amount_usd=D("500"))
    before = state.model_copy(deep=True)
    compute_ceiling(state, make_deps())
    assert state == before


def test_score_risk_is_passthrough(make_state, make_deps, make_invoice):
    assert score_risk(make_state(make_invoice()), make_deps()) == {}
