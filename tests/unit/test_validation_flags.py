import datetime as dt
from decimal import Decimal

import pytest

from agents.validation import (
    required_quantities, resolve_vendor, stock_shortfalls, validate, validate_node,
)
from core.text import vendor_key
from models import (
    DedupeKind, DedupeResult, FlagCode, NormalizedChange, PaymentWrite, Severity,
)

D = Decimal
NEW = DedupeResult(kind=DedupeKind.NEW)


@pytest.fixture
def run(repo, settings, as_of):
    def _run(inv, dedupe=NEW, changes=(), **kw):
        return validate(inv, dedupe=dedupe, changes=list(changes), repo=repo, settings=settings,
                        as_of=kw.get("as_of", as_of))
    return _run


def codes(res):
    return [f.code.value for f in res.flags]


def flag(res, code):
    return next(f for f in res.flags if f.code.value == code)


def test_clean_invoice_has_no_flags(run, make_invoice):
    res = run(make_invoice())
    assert res.flags == []
    assert res.amount_usd == D("250.00")
    assert res.payable_amount == D("250.00")
    assert res.address_check == "not_on_invoice"
    assert res.vendor.kind == "exact"


def test_missing_invoice_number(run, make_invoice):
    res = run(make_invoice(number=None))
    f = flag(res, "missing_invoice_number")
    assert f.severity == Severity.ERROR and f.evidence == {}


def test_no_line_items(run, make_invoice):
    res = run(make_invoice(lines=(), total="0"))
    assert "no_line_items" in codes(res)
    assert flag(res, "no_line_items").evidence == {}


def test_non_positive_quantity_single_flag(run, make_invoice):
    res = run(make_invoice(lines=(("WidgetA", -5, "250"), ("WidgetB", 0, "500"), ("GadgetX", 1, "750"))))
    assert codes(res).count("non_positive_quantity") == 1
    assert flag(res, "non_positive_quantity").evidence == {"lines": [
        {"index": 0, "item": "WidgetA", "quantity": -5}, {"index": 1, "item": "WidgetB", "quantity": 0}]}
    # a negative quantity never counts as a stock request
    assert "insufficient_stock" not in codes(res) and "zero_stock_item" not in codes(res)


def test_missing_vendor_suppresses_unknown_and_renamed(run, make_invoice):
    res = run(make_invoice(vendor="", former_name="Widgets Inc."))
    assert "missing_vendor" in codes(res)
    assert "unknown_vendor" not in codes(res) and "vendor_renamed" not in codes(res)
    assert flag(res, "missing_vendor").evidence == {}
    assert res.vendor.kind == "missing"


def test_unknown_vendor(run, make_invoice):
    res = run(make_invoice(vendor="Fraudster LLC"))
    assert flag(res, "unknown_vendor").evidence == {"vendor": "Fraudster LLC"}
    assert flag(res, "unknown_vendor").severity == Severity.ESCALATION
    assert "missing_vendor" not in codes(res)


def test_vendor_renamed_via_former_name(run, repo, make_invoice):
    inv = make_invoice(vendor="FastShip Express", former_name="FastShip Ltd.")
    res = run(inv)
    f = flag(res, "vendor_renamed")
    assert "unknown_vendor" not in codes(res)
    master = repo.find_vendor_by_key("fastship")
    assert f.evidence == {"vendor": "FastShip Express", "former_name": "FastShip Ltd.",
                          "vendor_id": master.id, "master_name": "FastShip Ltd."}


def test_vendor_renamed_via_alias_former_name(run, repo, make_invoice):
    master = repo.find_vendor_by_key("gadgets")
    repo.add_vendor_alias(master.id, "Gizmos Co", "previous_name")
    res = run(make_invoice(vendor="Gadgetron", former_name="Gizmos Co"))
    assert flag(res, "vendor_renamed").evidence["master_name"] == "Gadgets Co."


def test_approved_rename_alias_suppresses_flag(run, repo, make_invoice):
    master = repo.find_vendor_by_key("fastship")
    repo.add_vendor_alias(master.id, "FastShip Express", "approved_rename")
    inv = make_invoice(vendor="FastShip Express", former_name="FastShip Ltd.")
    res = run(inv)
    assert res.vendor.kind == "alias" and res.vendor.vendor_id == master.id
    assert "vendor_renamed" not in codes(res) and "unknown_vendor" not in codes(res)


def test_former_name_that_is_unknown_stays_unknown(run, make_invoice):
    res = run(make_invoice(vendor="Newco", former_name="Oldco"))
    assert "unknown_vendor" in codes(res) and "vendor_renamed" not in codes(res)


def test_resolve_vendor_exact_ignores_legal_suffix(repo, make_invoice):
    m = resolve_vendor(make_invoice(vendor="widgets, inc"), repo)
    assert (m.kind, m.master_name) == ("exact", "Widgets Inc.")


def test_invalid_due_date(run, make_invoice):
    inv = make_invoice(due_date=None)
    inv = inv.model_copy(update={"due_date_raw": "yesterday"})
    res = run(inv)
    assert flag(res, "invalid_due_date").evidence == {"raw": "yesterday"}
    assert "past_due" not in codes(res) and "terms_mismatch" not in codes(res)


def test_unknown_item_with_hint(run, make_invoice):
    res = run(make_invoice(lines=(("WidgetC", 1, "100"),)))
    f = flag(res, "unknown_item")
    assert f.evidence["item"] == "WidgetC"
    # difflib ties WidgetA/WidgetB; any Widget* hint is acceptable and the item is never auto-corrected
    assert f.evidence["did_you_mean"] in {"WidgetA", "WidgetB"}
    assert set(f.evidence) == {"item", "did_you_mean"}
    assert f.evidence["item"] == "WidgetC"


def test_unknown_item_without_hint_and_per_item(run, make_invoice):
    res = run(make_invoice(lines=(("Zzzzzz", 1, "100"), ("Qqqqqq", 2, "100"), ("Zzzzzz", 3, "100"))))
    unknown = [f for f in res.flags if f.code == FlagCode.UNKNOWN_ITEM]
    assert [f.evidence["item"] for f in unknown] == ["Zzzzzz", "Qqqqqq"]
    assert unknown[0].evidence["did_you_mean"] is None
    assert not any(f.code == FlagCode.INSUFFICIENT_STOCK for f in res.flags)


def test_item_matching_ignores_case_and_spacing(run, make_invoice):
    res = run(make_invoice(lines=(("widget a", 1, "250"),)))
    assert res.flags == []


def test_zero_stock_only_zero_stock_flag(run, make_invoice):
    res = run(make_invoice(lines=(("FakeItem", 5, "1000"),)))
    assert codes(res).count("zero_stock_item") == 1
    assert "insufficient_stock" not in codes(res)
    assert flag(res, "zero_stock_item").evidence == {"item": "FakeItem", "requested": 5}


def test_insufficient_stock(run, make_invoice):
    res = run(make_invoice(lines=(("GadgetX", 14, "750"),)))
    assert flag(res, "insufficient_stock").evidence == {"item": "GadgetX", "requested": 14, "available": 13}
    assert "zero_stock_item" not in codes(res)


def test_stock_exactly_enough_passes(run, make_invoice):
    assert "insufficient_stock" not in codes(run(make_invoice(lines=(("GadgetX", 13, "750"),))))


def test_lines_fit_individually_but_not_in_total(run, repo, make_invoice):
    # 1013 shape: stock 34/18/8, GadgetX lines 5+3+1 each fit but total 9 does not
    repo._conn.execute("UPDATE inventory SET stock=34 WHERE item='WidgetA'")
    repo._conn.execute("UPDATE inventory SET stock=18 WHERE item='WidgetB'")
    repo._conn.execute("UPDATE inventory SET stock=8 WHERE item='GadgetX'")
    lines = (("WidgetA", 15, "250"), ("GadgetX", 5, "750"), ("WidgetB", 10, "500"), ("WidgetA", 5, "250"),
             ("GadgetX", 3, "750"), ("WidgetA", 2, "250"), ("WidgetB", 8, "500"), ("GadgetX", 1, "750"))
    res = run(make_invoice(lines=lines))
    stock_flags = [f for f in res.flags if f.code == FlagCode.INSUFFICIENT_STOCK]
    assert len(stock_flags) == 1
    assert stock_flags[0].evidence == {"item": "GadgetX", "requested": 9, "available": 8}


def test_stock_shortfalls_and_required_quantities(repo, make_invoice):
    inv = make_invoice(lines=(("GadgetX", 20, "750"), ("WidgetC", 5, "1"), ("WidgetA", 1, "250")))
    req = required_quantities(inv, None)
    assert req == {"gadgetx": 20, "widgetc": 5, "widgeta": 1}
    sf = stock_shortfalls(req, repo)
    assert [(s.item, s.needed, s.available) for s in sf] == [("GadgetX", 20, 13)]


def _paid_original(repo, make_invoice, lines):
    from core.invoice_math import fingerprint
    from models import InvoiceResult, InvoiceUpdate, Status
    orig = make_invoice(lines=lines)
    rid = repo.create_invoice(trace_id="t-orig", file_path="p", file_name="orig.json", file_hash="h-orig")
    res = InvoiceResult(trace_id="t-orig", file="orig.json", status=Status.PAID, provider="none", invoice=orig)
    repo.update_invoice(rid, InvoiceUpdate(fingerprint=fingerprint(orig), vendor_key=orig.vendor_key,
                                           invoice_number=orig.invoice_number, status=Status.PAID, result=res))
    repo.record_payment(PaymentWrite(
        idempotency_key="k-orig", invoice_id=rid, trace_id="t-orig", vendor="Widgets Inc.",
        amount=orig.total, currency="USD", amount_usd=orig.total, attempts=1, month="2026-01",
        stock_deltas={}, bank_response={}))
    return orig


@pytest.mark.parametrize("stock,fails", [(13, False), (2, True)])
def test_revision_checks_only_added_quantities(run, repo, make_invoice, make_state, make_deps, stock, fails):
    from agents.validation import dedupe_node
    repo._conn.execute("UPDATE inventory SET stock=? WHERE item='GadgetX'", (stock,))
    _paid_original(repo, make_invoice, (("GadgetX", 8, "750"),))
    rev = make_invoice(lines=(("GadgetX", 13, "750"),), revision="R1")
    state = make_state(rev)
    out = dedupe_node(state, make_deps())
    assert out["dedupe"].kind == DedupeKind.REVISION_OF_PAID
    assert out["dedupe"].added_quantities == {"gadgetx": 5}
    res = run(rev, dedupe=out["dedupe"])
    assert ("insufficient_stock" in codes(res)) is fails
    f = flag(res, "revision_of_paid_invoice")
    assert f.evidence == {"original_file": "orig.json", "paid_to_date": "6000.00",
                          "added_quantities": {"gadgetx": 5}}
    # payable is the difference: 9750 - 6000
    assert res.payable_amount == D("3750.00")


def test_math_mismatch_evidence(run, make_invoice):
    inv = make_invoice(lines=(("WidgetA", 2, "250"),), total="550.00")
    res = run(inv)
    f = flag(res, "math_mismatch")
    assert f.severity == Severity.ESCALATION
    assert f.evidence["stated_total"] == "550.00"
    assert f.evidence["recomputed_total"] == "500.00"
    assert f.evidence["difference"] == "50.00"
    assert f.evidence["mismatches"] and isinstance(f.evidence["mismatches"][0], str)
    assert "math_mismatch" not in codes(run(make_invoice(lines=(("WidgetA", 2, "250"),))))


def test_price_above_catalog_boundary(run, make_invoice):
    res = run(make_invoice(lines=(("WidgetA", 1, "250"), ("WidgetB", 1, "500"), ("WidgetA", 1, "300"))))
    assert flag(res, "price_above_catalog").evidence == {"lines": [
        {"index": 2, "item": "WidgetA", "unit_price": "300.00", "catalog": "250.00", "pct": "20.0"}]}
    assert "price_above_catalog" not in codes(run(make_invoice(lines=(("WidgetA", 1, "275"),))))  # exactly +10%
    assert "price_above_catalog" in codes(run(make_invoice(lines=(("WidgetA", 1, "275.01"),))))


def test_price_below_catalog_single_flag(run, make_invoice):
    res = run(make_invoice(lines=(("WidgetA", 1, "240"), ("WidgetB", 1, "480"), ("GadgetX", 1, "750"))))
    assert codes(res).count("price_below_catalog") == 1
    f = flag(res, "price_below_catalog")
    assert f.severity == Severity.INFO
    assert f.evidence["lines"][0] == {"index": 0, "item": "WidgetA", "unit_price": "240.00",
                                      "catalog": "250.00", "pct": "-4.0"}
    assert [r["index"] for r in f.evidence["lines"]] == [0, 1]


def test_foreign_currency_has_no_threshold_or_price_flags(run, make_invoice):
    inv = make_invoice(lines=(("WidgetA", 100, "300"),), currency="EUR")  # 30,000 EUR, 20% over catalog
    res = run(inv)
    assert flag(res, "foreign_currency").evidence == {"currency": "EUR"}
    assert res.amount_usd is None
    assert not {"over_threshold", "near_threshold", "price_above_catalog", "price_below_catalog",
                "over_budget"} & set(codes(res))


def test_non_usd_currency_other_than_eur(run, make_invoice):
    assert "foreign_currency" in codes(run(make_invoice(currency="GBP")))


def test_over_threshold_and_near_threshold(run, make_invoice):
    over = run(make_invoice(lines=(("WidgetA", 41, "250"),)))  # 10,250
    assert flag(over, "over_threshold").evidence == {"amount_usd": "10250.00"}
    assert "near_threshold" not in codes(over)
    exact = run(make_invoice(lines=(("WidgetA", 40, "250"),)))  # 10,000 is not over
    assert not {"over_threshold", "near_threshold"} & set(codes(exact))
    near = run(make_invoice(lines=(("WidgetA", 36, "250"),)))  # 9,000
    assert flag(near, "near_threshold").evidence == {"amount_usd": "9000.00"}
    below = run(make_invoice(lines=(("WidgetA", 35, "250"),)))  # 8,750
    assert not {"over_threshold", "near_threshold"} & set(codes(below))


def test_threshold_uses_stated_total_not_payable(run, make_invoice):
    inv = make_invoice(lines=(("WidgetA", 1, "250"),), total="10500.00")
    res = run(inv)
    assert "over_threshold" in codes(res)
    assert res.payable_amount == D("250.00")  # lower of stated and recomputed


def test_negative_total_raises_no_threshold(run, make_invoice):
    res = run(make_invoice(lines=(("WidgetA", -1, "250"),)))
    assert res.amount_usd == D("-250.00")
    assert not {"over_threshold", "near_threshold"} & set(codes(res))


def test_over_budget_uses_payable_amount(run, repo, settings, make_invoice):
    repo._conn.execute("INSERT INTO budget_ledger(month, amount_usd, ts) VALUES ('2026-01','45000.00','t')")
    inv = make_invoice(lines=(("WidgetA", 1, "250"),), total="5500.00")  # stated 5,500 but payable 250
    assert "over_budget" not in codes(run(inv))
    inv2 = make_invoice(lines=(("WidgetA", 21, "250"),))  # 5,250
    f = flag(run(inv2), "over_budget")
    assert f.severity == Severity.HUMAN
    assert f.evidence == {"payable_usd": "5250.00", "spent": "45000.00", "limit": "50000.00",
                          "overspend": "250.00"}
    exactly = make_invoice(lines=(("WidgetA", 20, "250"),))  # lands exactly on the limit
    assert "over_budget" not in codes(run(exactly))


def test_over_budget_skipped_when_an_error_flag_fired(run, repo, make_invoice):
    repo._conn.execute("INSERT INTO budget_ledger(month, amount_usd, ts) VALUES ('2026-01','45000.00','t')")
    res = run(make_invoice(lines=(("FakeItem", 10, "1000"),)))  # zero stock (error) and 10,000 over budget
    assert "zero_stock_item" in codes(res)
    assert "over_budget" not in codes(res)


def test_over_budget_ignores_other_months(run, repo, make_invoice):
    repo._conn.execute("INSERT INTO budget_ledger(month, amount_usd, ts) VALUES ('2025-12','49999.00','t')")
    assert "over_budget" not in codes(run(make_invoice(lines=(("WidgetA", 21, "250"),))))


def test_over_budget_for_revision_uses_difference(run, repo, make_invoice):
    repo._conn.execute("INSERT INTO budget_ledger(month, amount_usd, ts) VALUES ('2026-01','49000.00','t')")
    rev = make_invoice(lines=(("WidgetA", 6, "250"),), revision="R1")  # total 1,500; paid 1,250 -> diff 250
    d = DedupeResult(kind=DedupeKind.REVISION_OF_PAID, paid_to_date=D("1250.00"),
                     added_quantities={"widgeta": 1}, original_file_name="o.json")
    res = run(rev, dedupe=d)
    assert res.payable_amount == D("250.00")
    assert "over_budget" not in codes(res)
    d2 = d.model_copy(update={"paid_to_date": D("0")})
    assert "over_budget" in codes(run(rev, dedupe=d2))


def test_conflicting_invoice_number(run, make_invoice):
    d = DedupeResult(kind=DedupeKind.CONFLICT, original_file_name="first.json")
    f = flag(run(make_invoice(), dedupe=d), "conflicting_invoice_number")
    assert f.evidence == {"original_file": "first.json"}


def test_past_due(run, make_invoice):
    res = run(make_invoice(due_date=dt.date(2026, 1, 20), terms=None))
    assert flag(res, "past_due").evidence == {"due_date": "2026-01-20", "as_of": "2026-01-31", "days": 11}
    assert "past_due" not in codes(run(make_invoice(due_date=dt.date(2026, 1, 31), terms=None)))


@pytest.mark.parametrize("delta,fires", [(0, False), (3, False), (-3, False), (4, True), (-4, True)])
def test_terms_mismatch_boundary(run, make_invoice, delta, fires):
    date = dt.date(2026, 1, 10)
    due = date + dt.timedelta(days=30 + delta)
    res = run(make_invoice(invoice_date=date, due_date=due, terms="Net 30"))
    assert ("terms_mismatch" in codes(res)) is fires
    if fires:
        assert flag(res, "terms_mismatch").evidence == {
            "expected_due": "2026-02-09", "due_date": due.isoformat(), "terms_days": 30, "days_off": 4}


def test_terms_mismatch_needs_all_inputs(run, make_invoice):
    assert "terms_mismatch" not in codes(run(make_invoice(terms=None)))
    assert "terms_mismatch" not in codes(run(make_invoice(invoice_date=None, terms="Net 5")))
    assert "terms_mismatch" not in codes(run(make_invoice(due_date=None, terms="Net 5")))


def test_value_normalized_single_flag_with_all_changes(run, make_invoice):
    changes = [NormalizedChange(field="invoice_number", raw="1002", normalized="INV-1002"),
               NormalizedChange(field="date", raw="2O26-01-15", normalized="2026-01-15")]
    res = run(make_invoice(), changes=changes)
    assert codes(res) == ["value_normalized"]
    assert flag(res, "value_normalized").evidence == {"changes": [c.model_dump(mode="json") for c in changes]}
    assert run(make_invoice()).flags == []


def test_tier3_flags_never_raised(run, make_invoice):
    res = run(make_invoice(lines=(("WidgetC", -1, "1"),), vendor="", due_date=None, number=None))
    assert not {"high_risk_score", "prompt_injection"} & set(codes(res))


def test_validate_node_outputs_and_audit(make_state, make_deps, make_invoice, repo):
    changes = [NormalizedChange(field="invoice_number", raw="9001", normalized="INV-9001")]
    inv = make_invoice(lines=(("GadgetX", 14, "750"),))
    state = make_state(inv, dedupe=NEW, normalized_changes=changes)
    deps = make_deps()
    before = state.model_copy(deep=True)
    out = validate_node(state, deps)
    assert state == before
    assert set(out) == {"flags", "vendor", "address_check", "amount_usd", "totals", "payable_amount"}
    assert [f.code.value for f in out["flags"]] == ["insufficient_stock", "over_threshold", "value_normalized"]
    assert out["amount_usd"] == D("10500.00") and out["vendor"].kind == "exact"
    entries = repo.audit_for_trace(state.trace_id)
    assert [e.event for e in entries] == ["flags_raised"]
    assert entries[0].payload["codes"] == ["insufficient_stock", "over_threshold", "value_normalized"]
    assert deps.log.of("flag_raised") == []  # the graph's node wrapper logs flag_raised, not the node
    assert repo.get_invoice(state.invoice_id).amount_usd == D("10500.00")


def test_invoice_1013_shape_all_flags(run, repo, make_invoice):
    repo._conn.execute("UPDATE inventory SET stock=8 WHERE item='GadgetX'")
    lines = (("WidgetA", 22, "240"), ("WidgetB", 18, "480"), ("GadgetX", 9, "750"))
    inv = make_invoice(lines=lines, tax_rate="0.07", total="22562.80",
                       address="100 Main St, Chicago, IL 60601")
    res = run(inv)
    assert {"insufficient_stock", "over_threshold", "price_below_catalog", "math_mismatch"} <= set(codes(res))
    assert res.address_check == "verified"
