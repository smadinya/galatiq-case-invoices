from decimal import Decimal

from core.invoice_math import aggregate_quantities, fingerprint, payable_amount, recompute_totals, to_usd

D = Decimal


def test_invoice_1001_clean(make_invoice):
    inv = make_invoice(
        [("WidgetA", 10, "250.00"), ("WidgetB", 5, "500.00")], number="INV-1001", subtotal="5000.00",
        tax_rate="0", tax_amount="0.00", total="5000.00",
    )
    t = recompute_totals(inv)
    assert t.ok and t.total_ok and t.difference == D("0")
    assert t.lines_sum == D("5000.00") and t.subtotal_ok is True and t.tax_ok is True
    assert t.tax_source == "stated"


def test_invoice_1007_total_off_by_110(make_invoice):
    inv = make_invoice(
        [("WidgetA", 20, "250.00"), ("WidgetB", 15, "500.00"), ("GadgetX", 3, "750.00")], number="INV-1007",
        subtotal="14750.00", tax_rate="0.06", tax_amount="885.00", total="15525.00",
    )
    t = recompute_totals(inv)
    assert t.lines_sum == D("14750.00")
    assert t.total_recomputed == D("15635.00")
    assert t.difference == D("-110.00")
    assert not t.total_ok and not t.ok
    assert t.subtotal_ok is True and t.tax_ok is True
    assert len(t.mismatches) == 1 and "total" in t.mismatches[0]


def test_invoice_1009_subtotal_mismatch_total_ok(make_invoice):
    inv = make_invoice(
        [("WidgetA", -5, "250.00"), ("WidgetB", 2, "500.00")], number="INV-1009", subtotal="1000.00",
        tax_rate="0.0", tax_amount="0.00", total="-250.00",
    )
    t = recompute_totals(inv)
    assert t.lines_sum == D("-250.00")
    assert t.subtotal_ok is False
    assert t.total_ok is True
    assert any("subtotal" in m for m in t.mismatches)
    assert not t.ok


def test_invoice_1013_real_50_dollar_error(make_invoice):
    lines = [
        ("WidgetA", 15, "250.00"), ("WidgetB", 10, "500.00"), ("GadgetX", 5, "750.00"),
        ("WidgetA", 5, "240.00"), ("WidgetB", 8, "480.00"), ("GadgetX", 3, "750.00"),
        ("WidgetA", 2, "250.00"), ("GadgetX", 1, "750.00"),
    ]
    inv = make_invoice(lines, number="INV-1013", subtotal="21040.00", tax_rate="0.07", tax_amount="1472.80",
                       total="22562.80")
    for li, amt in zip(inv.line_items, ["3750", "5000", "3750", "1200", "3840", "2250", "500", "750"]):
        li.amount = D(amt)
    t = recompute_totals(inv)
    assert all(lc.ok for lc in t.lines)
    assert t.lines_sum == D("21040.00") and t.subtotal_ok is True and t.tax_ok is True
    assert t.total_recomputed == D("22512.80")
    assert t.difference == D("50.00")
    assert not t.total_ok


def test_invoice_1010_shipping_and_stated_tax(make_invoice):
    inv = make_invoice(
        [("WidgetA", 8, "250.00"), ("WidgetB", 4, "500.00"), ("GadgetX", 2, "750.00"), ("WidgetA", 4, "300.00")],
        number="INV-1010", subtotal="6700.00", tax_amount="335.00", shipping="150.00", total="7185.00",
    )
    t = recompute_totals(inv)
    assert t.lines_sum == D("6700.00") and t.tax_used == D("335.00") and t.tax_source == "stated"
    assert t.tax_ok is None  # no rate printed
    assert t.shipping == D("150.00")
    assert t.total_recomputed == D("7185.00") and t.total_ok and t.ok


def test_tax_from_rate_and_none(make_invoice):
    inv = make_invoice([("WidgetA", 4, "250.00")], tax_rate="0.05", total="1050.00")
    t = recompute_totals(inv)
    assert t.tax_source == "rate" and t.tax_used == D("50.00") and t.tax_ok is None and t.ok
    inv = make_invoice([("WidgetA", 4, "250.00")], total="1000.00")
    t = recompute_totals(inv)
    assert t.tax_source == "none" and t.tax_used == D("0")


def test_line_amount_mismatch_reported(make_invoice):
    inv = make_invoice([("WidgetA", 2, "250.00")], total="500.00")
    inv.line_items[0].amount = D("499.00")
    t = recompute_totals(inv)
    assert not t.lines[0].ok and any("line 1" in m for m in t.mismatches)


def test_tolerance(make_invoice):
    inv = make_invoice([("WidgetA", 1, "250.00")], total="250.01")
    assert recompute_totals(inv).total_ok
    inv = make_invoice([("WidgetA", 1, "250.00")], total="250.02")
    assert not recompute_totals(inv).total_ok
    assert recompute_totals(inv, D("0.05")).total_ok


def test_fingerprint_pdf_style_equals_txt_style(make_invoice):
    # 1011: PDF has no subtotal/tax/terms; TXT does.
    lines = [("WidgetA", 6, "250.00"), ("WidgetB", 3, "500.00")]
    txt = make_invoice(lines, number="INV-1011", vendor="Summit Manufacturing Co.", subtotal="3000.00",
                       tax_rate="0", tax_amount="0.00", terms="Net 30", total="3000.00")
    pdf = make_invoice(lines, number="INV-1011", vendor="Summit Manufacturing Co.", terms=None, total="3000.00")
    assert fingerprint(txt) == fingerprint(pdf)


def test_fingerprint_1013_json_equals_pdf_style(make_invoice):
    lines = [("WidgetA", 15, "250.00"), ("GadgetX", 5, "750.00"), ("WidgetA", 5, "240.00")]
    js = make_invoice(lines, number="INV-1013", vendor="Atlas Industrial Supply",
                      address="500 Commerce Blvd, Detroit, MI 48201", currency="USD", total="22562.80")
    pdf = make_invoice(list(reversed(lines)), number="INV-1013", vendor="Atlas Industrial Supply", address=None,
                       total="22562.80")
    assert fingerprint(js) == fingerprint(pdf)


def test_fingerprint_changes_with_content(make_invoice):
    base = make_invoice([("WidgetA", 4, "250.00")], total="1000.00")
    assert len(fingerprint(base)) == 64
    assert fingerprint(base) != fingerprint(make_invoice([("WidgetA", 5, "250.00")], total="1000.00"))
    assert fingerprint(base) != fingerprint(make_invoice([("WidgetA", 4, "250.00")], total="1001.00"))
    assert fingerprint(base) != fingerprint(make_invoice([("WidgetA", 4, "250.00")], total="1000.00", number="INV-1"))
    assert fingerprint(base) != fingerprint(make_invoice([("WidgetA", 4, "250.00")], total="1000.00", currency="EUR"))


def test_aggregate_quantities(make_invoice):
    inv = make_invoice([("WidgetA", 15, "250"), ("Widget A", 5, "240"), ("GadgetX", 1, "750")])
    assert aggregate_quantities(inv.line_items) == {"widgeta": 20, "gadgetx": 1}


def test_payable_amount_stated_and_revision(make_invoice):
    ok = make_invoice([("WidgetA", 4, "250.00")], total="1000.00")
    assert payable_amount(ok, recompute_totals(ok), revision_paid_to_date=None) == (D("1000.00"), "stated_total")

    # stated total below recomputed (underbilled): the stated total is still the base
    low = make_invoice([("WidgetA", 4, "250.00")], total="900.00")
    assert payable_amount(low, recompute_totals(low), revision_paid_to_date=None)[0] == D("900.00")

    rev = make_invoice(
        [("WidgetA", 3, "250"), ("WidgetB", 2, "500"), ("GadgetX", 5, "750")], revision="R1",
        subtotal="5500.00", tax_rate="0.08", tax_amount="440.00", total="5940.00",
    )
    assert payable_amount(rev, recompute_totals(rev), revision_paid_to_date=D("1890.00")) == (
        D("4050.00"), "revision_difference",
    )
    assert payable_amount(rev, recompute_totals(rev), revision_paid_to_date=D("6000.00"))[0] == D("-60.00")


def test_to_usd():
    assert to_usd(D("4125"), "EUR", D("1.08")) == D("4455.00")
    assert to_usd(D("4125"), "EUR", None) is None
    assert to_usd(D("100.00"), "USD", None) == D("100.00")
    assert to_usd(D("100.00"), "usd", D("2")) == D("100.00")
