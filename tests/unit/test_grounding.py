from decimal import Decimal

import pytest

from config.settings import REPO_ROOT
from models import ExtractedInvoice
from tools.grounding import find_evidence_spans, ground_check, source_numbers
from tools.parsers import extract_pdf_text

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
)
EX_1010 = x(
    invoice_number="INV-1010", vendor={"name": "Consolidated Materials Group"},
    date="January 27, 2026", due_date="February 26, 2026",
    line_items=[
        {"item": "WidgetA", "quantity": "8", "unit_price": "$250.00", "amount": "$2,000.00"},
        {"item": "WidgetB", "quantity": "4", "unit_price": "$500.00", "amount": "$2,000.00"},
        {"item": "GadgetX", "quantity": "2", "unit_price": "$750.00", "amount": "$1,500.00"},
        {"item": "WidgetA", "quantity": "4", "unit_price": "$300.00", "amount": "$1,200.00", "note": "rush order"},
    ],
    subtotal="$6,700.00", tax_amount="$335.00", shipping="$150.00", total="$7,185.00", payment_terms="Net 30",
)


@pytest.fixture
def pdf_1012() -> str:
    return extract_pdf_text((SAMPLES / "invoice_1012.pdf").read_bytes())


@pytest.fixture
def txt_1012() -> str:
    return (SAMPLES / "invoice_1012.txt").read_text()


def test_1012_grounds_against_pdf_and_txt(pdf_1012, txt_1012):
    for source in (pdf_1012, txt_1012):
        res = ground_check(EX_1012, source)
        assert res.ok, res.errors


def test_1010_grounds_against_txt():
    res = ground_check(EX_1010, (SAMPLES / "invoice_1010.txt").read_text())
    assert res.ok, res.errors


def test_inline_item_note_still_grounds():
    ex = upd(EX_1010, {"line_items": [{"item": "WidgetA (rush order)", "quantity": "4", "unit_price": "$300.00"}]})
    assert ground_check(ex, (SAMPLES / "invoice_1010.txt").read_text()).ok


def test_wrong_total_fails_and_names_total(txt_1012):
    res = ground_check(EX_1012.model_copy(update={"total": "$9,999.00"}), txt_1012)
    assert not res.ok
    assert any(e.startswith("total") and "$9,999.00" in e for e in res.errors)


def test_invented_item_fails(txt_1012):
    lines = [dict(ln.model_dump(), item="SuperWidget") if i == 0 else ln for i, ln in enumerate(EX_1012.line_items)]
    res = ground_check(upd(EX_1012, {"line_items": lines}), txt_1012)
    assert not res.ok
    assert any("line_items[0].item" in e and "SuperWidget" in e for e in res.errors)


def test_unparseable_number_fails(txt_1012):
    res = ground_check(EX_1012.model_copy(update={"subtotal": "n/a"}), txt_1012)
    assert any(e.startswith("subtotal") for e in res.errors)


def test_empty_extraction_is_an_error():
    res = ground_check(x(invoice_number="INV-1"), "INV-1 nothing else")
    assert not res.ok and any("empty" in e for e in res.errors)


def test_spans_contain_total_line(txt_1012):
    spans = ground_check(EX_1012, txt_1012).spans
    assert any(ln.startswith("TOTAL:") for ln in spans["total"])
    assert any("Widget A" in ln for ln in spans["line_items[0].item"])
    assert any("3,500.O0" in ln for ln in spans["line_items[1].amount"])
    assert any("26-Jan-2O26" in ln for ln in spans["date"])


def test_find_evidence_spans_direct(pdf_1012):
    spans = find_evidence_spans(EX_1012, pdf_1012)
    assert spans["invoice_number"] == ["INV NO: INV 1012"]


def test_source_numbers_normalises():
    nums = source_numbers("Total $3,500.O0 and (-250.00), qty 12, ref 2O26")
    assert Decimal("3500.00") in nums and Decimal("250") in nums and Decimal("12") in nums
    assert Decimal("2026") in nums
    assert Decimal("-250") not in nums


def test_numbers_compare_as_decimals():
    res = ground_check(x(line_items=[{"item": "Widget", "quantity": "-5", "unit_price": "250.0"}], total="250"),
                       "Widget 5 $250.00 total 250.00")
    assert res.ok, res.errors
