import datetime as dt
from decimal import Decimal

import pytest

from core.text import vendor_key
from models import InvoiceUpdate
from tools.vendors import find_similar_invoices, get_vendor_history, get_vendor_master, lookup_vendor


def add_invoice(repo, make_state, *, vendor="Widgets Inc.", number, amount, date, status="PAID", rev=None, file=None):
    state = make_state(file_name=file or f"invoice_{number}.json")
    rec = repo.find_vendor_by_key(vendor_key(vendor))
    repo.update_invoice(
        state.invoice_id,
        InvoiceUpdate(
            vendor_id=rec.id, vendor_key=rec.normalized_name, vendor_name=rec.name, invoice_number=number,
            revision=rev, status=status, amount=Decimal(amount), currency="USD", invoice_date=date,
        ),
    )
    return state


@pytest.mark.parametrize(
    "name,kind",
    [
        ("Widgets Inc.", "exact"), ("widgets inc", "exact"), ("WIDGETS, INC", "exact"),
        ("Nobody Corp", "unknown"), ("", "missing"), ("   ", "missing"),
    ],
)
def test_lookup_kinds(repo, name, kind):
    assert lookup_vendor(repo, name).kind == kind


def test_lookup_exact_fields(repo):
    m = lookup_vendor(repo, "Widgets Inc.")
    assert m.master_name == "Widgets Inc." and m.vendor_id == repo.find_vendor_by_key("widgets").id


def test_lookup_alias_and_inline_rename(repo):
    vid = repo.find_vendor_by_key("widgets").id
    repo.add_vendor_alias(vid, "Old Widgets LLC", "previous_name")
    m = lookup_vendor(repo, "Old Widgets")
    assert m.kind == "alias" and m.master_name == "Widgets Inc." and m.matched_name == "Old Widgets LLC"
    for text in ("Newco Ltd (formerly Widgets Inc.)", "Newco Ltd, formerly Widgets Inc", "Newco f/k/a Widgets Inc."):
        r = lookup_vendor(repo, text)
        assert r.kind == "renamed" and r.vendor_id == vid and r.matched_name.startswith("Widgets")
    assert lookup_vendor(repo, "Newco Ltd (formerly Ghost Corp)").kind == "unknown"
    # current part resolving beats the clause
    assert lookup_vendor(repo, "Widgets Inc. (formerly Ghost Corp)").kind == "exact"
    # former alias resolves too
    assert lookup_vendor(repo, "Newco (formerly Old Widgets)").kind == "renamed"


def test_history(repo, make_deps, make_state):
    deps = make_deps()
    add_invoice(repo, make_state, number="INV-1", amount="500.00", date=dt.date(2026, 1, 2))
    add_invoice(repo, make_state, number="INV-2", amount="600.00", date=dt.date(2026, 1, 3), status="ERROR")
    add_invoice(repo, make_state, vendor="Gadgets Co.", number="INV-3", amount="700.00", date=dt.date(2026, 1, 3))
    current = add_invoice(repo, make_state, number="INV-4", amount="800.00", date=dt.date(2026, 1, 4))
    out = get_vendor_history(deps, current, "widgets inc")
    assert out["known"] and out["master_name"] == "Widgets Inc."
    assert [i["invoice_number"] for i in out["invoices"]] == ["INV-1"]
    row = out["invoices"][0]
    assert row == {"invoice_number": "INV-1", "revision": None, "status": "PAID", "total": "500.00",
                   "currency": "USD", "invoice_date": "2026-01-02", "file": "invoice_INV-1.json"}


def test_history_unknown_vendor(make_deps, make_state):
    out = get_vendor_history(make_deps(), make_state(), "Nobody Corp")
    assert out == {"vendor": "Nobody Corp", "known": False, "master_name": None, "invoices": []}


def test_split_pair_flagged(repo, make_deps, make_state, make_invoice):
    day = dt.date(2026, 1, 20)
    add_invoice(repo, make_state, number="INV-101", amount="9600.00", date=day)
    inv = make_invoice(lines=(("WidgetB", 19, "500.00"), ("WidgetA", 1, "300.00")), number="INV-102",
                       invoice_date=day)
    assert inv.total == Decimal("9800.00")
    current = make_state(inv, file_name="invoice_102.json")
    out = find_similar_invoices(make_deps(), current, "Widgets Inc.", Decimal("9800"), 3)
    assert [m["invoice_number"] for m in out["matches"]] == ["INV-101"]
    assert out["combined_total_with_current"] == "19400.00"
    assert out["split_suspect"] is True


def test_similar_not_split_cases(repo, make_deps, make_state, make_invoice):
    day = dt.date(2026, 1, 20)
    add_invoice(repo, make_state, number="INV-201", amount="200.00", date=day)
    inv = make_invoice(lines=(("WidgetA", 2, "250.00"),), number="INV-202", invoice_date=day)
    current = make_state(inv)
    out = find_similar_invoices(make_deps(), current, "Widgets Inc.", Decimal("500"), 1)
    assert len(out["matches"]) == 1 and out["split_suspect"] is False
    assert out["combined_total_with_current"] == "700.00"
    # one match over $10k -> not a split
    add_invoice(repo, make_state, number="INV-203", amount="10500.00", date=day)
    out = find_similar_invoices(make_deps(), current, "Widgets Inc.", Decimal("500"), 1)
    assert out["split_suspect"] is False


def test_similar_window_and_amount_band(repo, make_deps, make_state, make_invoice):
    add_invoice(repo, make_state, number="INV-301", amount="1000.00", date=dt.date(2025, 6, 1))   # far, amount near
    add_invoice(repo, make_state, number="INV-302", amount="50.00", date=dt.date(2026, 1, 21))    # near date
    add_invoice(repo, make_state, number="INV-303", amount="50.00", date=dt.date(2025, 6, 1))     # neither
    inv = make_invoice(lines=(("WidgetA", 4, "250.00"),), invoice_date=dt.date(2026, 1, 20))
    current = make_state(inv)
    out = find_similar_invoices(make_deps(), current, "Widgets Inc.", "1050", 2)
    assert sorted(m["invoice_number"] for m in out["matches"]) == ["INV-301", "INV-302"]


def test_similar_no_current_invoice_uses_as_of(repo, make_deps, make_state):
    add_invoice(repo, make_state, number="INV-401", amount="40.00", date=dt.date(2026, 1, 30))
    out = find_similar_invoices(make_deps(), make_state(), "Widgets Inc.", Decimal("9000"), 3)
    assert [m["invoice_number"] for m in out["matches"]] == ["INV-401"]


def test_vendor_master(repo, make_deps):
    deps = make_deps()
    vid = repo.find_vendor_by_key("widgets").id
    repo.add_vendor_alias(vid, "Old Widgets", "previous_name")
    out = get_vendor_master(deps, "WIDGETS INC")
    assert out == {"found": True, "name": "Widgets Inc.", "status": "approved",
                   "address": "100 Main St, Chicago, IL 60601", "payment_terms": None, "aliases": ["Old Widgets"]}
    assert get_vendor_master(deps, "Ghost")["found"] is False
    assert get_vendor_master(deps, "")["found"] is False


def test_tool_via_bind_accepts_string_amount(repo, make_deps, make_state):
    from tools.vendors import find_similar_invoices as f
    bound = f.bind(deps=make_deps(), state=make_state())
    assert bound.fn(vendor="Widgets Inc.", amount="9600.00", days=3)["matches"] == []
