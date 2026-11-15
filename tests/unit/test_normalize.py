import datetime as dt
from decimal import Decimal

import pytest

from config.settings import REPO_ROOT
from models import ExtractedInvoice
from tools.normalize import NormalizationError, normalize
from tools.parsers import detect_format, parse_structured

SAMPLES = REPO_ROOT / "data" / "invoices"


def x(**fields) -> ExtractedInvoice:
    return ExtractedInvoice.model_validate(fields)


def upd(ex: ExtractedInvoice, changes: dict) -> ExtractedInvoice:
    return ExtractedInvoice.model_validate({**ex.model_dump(), **changes})


EX_1012 = x(
    invoice_number="INV 1012",
    vendor={"name": "QuickShip Distributers", "former_name": "FastShip Ltd."},
    date="26-Jan-2O26",
    due_date="25-Feb-2026",
    line_items=[
        {"item": "Widget A", "quantity": "12", "unit_price": "$250", "amount": "$3,000.00"},
        {"item": "WidgetB", "quantity": "7", "unit_price": "$500", "amount": "$3,500.O0"},
        {"item": "Gadget X", "quantity": "4", "unit_price": "$750", "amount": "$3,000.00"},
    ],
    subtotal="$9,500.00", tax_rate="5%", tax_amount="$475.00", total="$9,975.00",
    payment_terms="Net 30",
    notes="Ref PO-20260115. Deliver to warehouse dock B.",
)
EX_1010 = x(
    invoice_number="INV-1010", vendor={"name": "Consolidated Materials Group"},
    date="January 27, 2026", due_date="February 26, 2026",
    line_items=[
        {"item": "WidgetA", "quantity": "8", "unit_price": "$250.00", "amount": "$2,000.00"},
        {"item": "WidgetA", "quantity": "4", "unit_price": "$300.00", "amount": "$1,200.00", "note": "rush order"},
    ],
    subtotal="$6,700.00", tax_amount="$335.00", shipping="$150.00", total="$7,185.00", payment_terms="Net 30",
)
EX_1002 = x(
    invoice_number="1002", vendor={"name": "Gadgets Co."}, date="Jan 30 2026", due_date="2026-01-30",
    line_items=[{"item": "GadgetX", "quantity": "20", "unit_price": "$750"}],
    total="$15,000.00", payment_terms="Net 30",
)


def test_1012_extraction():
    res = normalize(EX_1012)
    inv = res.invoice
    assert inv.invoice_number == "INV-1012"
    assert inv.invoice_date == dt.date(2026, 1, 26)
    assert inv.due_date == dt.date(2026, 2, 25)
    assert inv.line_items[1].amount == Decimal("3500.00")
    assert [ln.item_key for ln in inv.line_items] == ["widgeta", "widgetb", "gadgetx"]
    assert inv.vendor_name == "QuickShip Distributers"
    assert inv.vendor_former_name == "FastShip Ltd."
    assert inv.vendor_key == "quickshipdistributers"
    assert inv.tax_rate == Decimal("0.05")
    assert inv.terms_days == 30
    assert inv.currency == "USD"
    assert [c.field for c in res.changes] == ["invoice_number", "date", "line_items[1].amount"]
    assert res.changes[0].raw == "INV 1012" and res.changes[0].normalized == "INV-1012"
    assert res.changes[1].normalized == "2026-01-26"


@pytest.mark.parametrize("item,note", [("WidgetA (rush order)", None), ("WidgetA", "rush order")])
def test_rush_line_split(item, note):
    line = {"item": item, "quantity": "4", "unit_price": "$300.00", "amount": "$1,200.00"}
    if note:
        line["note"] = note
    inv = normalize(upd(EX_1010, {"line_items": [__import__("models").ExtractedLine(**line)]})).invoice
    assert inv.line_items[0].item == "WidgetA"
    assert inv.line_items[0].note == "rush order"
    assert inv.line_items[0].item_key == "widgeta"


def test_1010_no_changes_and_totals():
    res = normalize(EX_1010)
    assert res.changes == []
    assert res.invoice.shipping == Decimal("150.00")
    assert res.invoice.invoice_date == dt.date(2026, 1, 27)


def test_1002_one_change():
    res = normalize(EX_1002)
    assert [c.field for c in res.changes] == ["invoice_number"]
    assert res.invoice.invoice_number == "INV-1002"
    assert res.invoice.invoice_date == dt.date(2026, 1, 30)
    assert res.invoice.subtotal is None


def test_date_format_only_is_not_a_change():
    res = normalize(parse_structured("csv", (SAMPLES / "invoice_1007.csv").read_bytes()))
    assert res.changes == []
    assert res.invoice.invoice_date == dt.date(2026, 1, 28)
    assert res.invoice.tax_rate == Decimal("0.06")


def test_unparseable_due_date_kept_raw():
    inv = normalize(upd(EX_1002, {"due_date": "yesterday"})).invoice
    assert inv.due_date is None
    assert inv.due_date_raw == "yesterday"


def test_missing_or_bad_total_raises():
    with pytest.raises(NormalizationError):
        normalize(upd(EX_1002, {"total": None}))
    with pytest.raises(NormalizationError):
        normalize(upd(EX_1002, {"total": "lots"}))


def test_bad_line_values_raise():
    base = {"item": "A", "quantity": "1", "unit_price": "$5"}
    for bad in ({"quantity": "two"}, {"quantity": None}, {"unit_price": "free"}, {"unit_price": None}):
        ex = x(total="5", line_items=[{**base, **bad}])
        with pytest.raises(NormalizationError):
            normalize(ex)


def test_unit_price_derived_from_amount():
    inv = normalize(x(total="30", line_items=[{"item": "A", "quantity": "3", "amount": "$30.00"}])).invoice
    assert inv.line_items[0].unit_price == Decimal("10.00")


def test_revision_via_note_and_field():
    assert normalize(upd(EX_1002, {"notes": "Revised invoice - added items"})).invoice.is_revision
    assert normalize(upd(EX_1002, {"notes": "Revision 2 attached"})).invoice.is_revision
    assert not normalize(upd(EX_1002, {"notes": "Preview of next season"})).invoice.is_revision
    inv = normalize(upd(EX_1002, {"revision": "1"})).invoice
    assert inv.revision == "R1" and inv.is_revision


def test_vendor_rename_split_from_name_and_blank_vendor():
    inv = normalize(upd(EX_1002, {"vendor": {"name": " NewCo Inc. (formerly OldCo) "}})).invoice
    assert inv.vendor_name == "NewCo Inc."
    assert inv.vendor_former_name == "OldCo"
    assert inv.vendor_key == "newco"
    blank = normalize(upd(EX_1002, {"vendor": {"name": "  ", "address": " "}})).invoice
    assert blank.vendor_name is None and blank.vendor_key is None and blank.vendor_address is None


def test_currency_default_and_eur_not_a_change():
    res = normalize(parse_structured(detect_format(SAMPLES / "invoice_1014.xml"), (SAMPLES / "invoice_1014.xml").read_bytes()))
    assert res.invoice.currency == "EUR"
    assert res.invoice.tax_rate == Decimal("0.10")
    assert res.changes == []


def test_negative_quantity_and_empty_payment_terms_pass_through():
    res = normalize(parse_structured("json", (SAMPLES / "invoice_1009.json").read_bytes()))
    assert res.invoice.line_items[0].quantity == -5
    assert res.invoice.total == Decimal("-250.00")
    assert res.invoice.vendor_name is None
    assert res.invoice.terms_days is None
    assert res.invoice.payment_terms is None


def test_every_structured_sample_normalizes():
    for name in ("1004.json", "1004_revised.json", "1005.json", "1006.csv", "1009.json", "1013.json", "1015.csv", "1016.json"):
        path = SAMPLES / f"invoice_{name}"
        assert normalize(parse_structured(detect_format(path), path.read_bytes())).invoice.line_items
