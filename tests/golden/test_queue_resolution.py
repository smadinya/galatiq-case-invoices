"""DESIGN §14.2 queue walk-through. Every step uses a NEW Service on the same files (separate CLI processes)."""

from decimal import Decimal as D

import pytest

import service as service_module
from config.settings import REPO_ROOT
from golden_expected import ALT_ORDER_STEPS, MONTH, POST_BATCH_STEPS
from llm.fake import ScriptedFake
from llm.fake_golden import golden_script
from models import StockShortfall, Status

SAMPLES = REPO_ROOT / "data" / "invoices"
ITEMS = ("WidgetA", "WidgetB", "GadgetX", "FakeItem")


def state_of(svc):
    stock = svc.repo.stock_levels()
    return (
        tuple(stock[i] for i in ITEMS), svc.repo.budget_spent(MONTH),
        [(q.invoice_id, q.status) for q in svc.repo.list_queue(None)],
        len(svc.repo.stock_ledger()), len(svc.repo.budget_entries(MONTH)),
    )


def run_steps(make_service, steps):
    fake = ScriptedFake(golden_script())
    make_service(provider=fake).run_batch(SAMPLES)
    for ref, action, kwargs, expect in steps:
        svc = make_service(provider=fake)  # fresh "process"
        before = state_of(svc)
        if "raises" in expect:
            with pytest.raises(getattr(service_module, expect["raises"])) as ei:
                svc.resolve(ref, action=action, **kwargs)
            if "overspend_usd" in expect:
                assert ei.value.overspend_usd == expect["overspend_usd"]
                assert ei.value.budget.spent == before[1]
            assert state_of(svc) == before, (ref, expect)
            continue
        preview = svc.preview_resolution(ref, fx_rate=kwargs.get("fx_rate"))
        if "shortfalls" in expect:
            assert preview.approve_outcome == "reject_stock"
            assert preview.stock_shortfalls == [StockShortfall(**s) for s in expect["shortfalls"]]
        res = svc.resolve(ref, action=action, **kwargs)
        assert res.status == Status(expect["status"]), (ref, res.status, res.error)
        if "paid" in expect:
            assert res.payment.amount == expect["paid"]
            assert res.payment.currency == expect.get("currency", "USD")
            if "fx_rate" in expect:
                assert res.payment.fx_rate == expect["fx_rate"]
                assert res.payment.amount_usd == expect["amount_usd"]
        else:
            assert res.payment is None
        if "human_outcome" in expect:
            assert res.human.outcome == expect["human_outcome"]
        assert res.decision.by == "human"
        after = make_service(provider=fake)
        stock, spent = state_of(after)[:2]
        assert stock == expect["stock"], (ref, stock)
        assert spent == expect["spent"], (ref, spent)
        if "alias" in expect:
            name, alias = expect["alias"]
            pairs = [(v.name, a.alias) for v in after.repo.list_vendors() for a in after.repo.list_aliases(v.id)]
            assert any(alias.lower() in (n.lower(), al.lower()) and name.lower() in (n.lower(), al.lower())
                       for n, al in pairs), pairs
    final = make_service(provider=fake)
    return final, fake


def test_post_batch_steps(make_service):
    final, _ = run_steps(make_service, POST_BATCH_STEPS)
    assert final.list_queue() == []
    assert final.banner().budget.spent == D("56280.00")
    inv = {i.file_name: i.status for i in final.repo.list_invoices()}
    assert inv["invoice_1012.pdf"] == Status.PAID and inv["invoice_1014.xml"] == Status.PAID
    assert inv["invoice_1004_revised.json"] == Status.REJECTED


def test_alternative_order(make_service):
    final, _ = run_steps(make_service, ALT_ORDER_STEPS)
    queue = final.list_queue()
    assert [q.invoice_ref for q in queue] == ["INV-1014"]
    inv = {i.file_name: i.status for i in final.repo.list_invoices()}
    assert inv["invoice_1004_revised.json"] == Status.PAID and inv["invoice_1012.pdf"] == Status.REJECTED
    assert final.banner().budget.spent == D("45900.00")
