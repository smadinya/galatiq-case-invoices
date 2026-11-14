import pytest

from config.settings import REPO_ROOT
from tools.parsers import ParseError, decode_text, detect_format, extract_pdf_text, parse_structured

SAMPLES = REPO_ROOT / "data" / "invoices"
STRUCTURED = [
    "invoice_1004.json", "invoice_1004_revised.json", "invoice_1005.json", "invoice_1006.csv",
    "invoice_1007.csv", "invoice_1009.json", "invoice_1013.json", "invoice_1014.xml",
    "invoice_1015.csv", "invoice_1016.json",
]


def parse(name: str):
    path = SAMPLES / name
    return parse_structured(detect_format(path), path.read_bytes())


@pytest.mark.parametrize("name", STRUCTURED)
def test_every_structured_sample_parses(name):
    ex = parse(name)
    assert ex.invoice_number
    assert ex.line_items
    assert ex.total


def test_json_values_are_verbatim_strings():
    ex = parse("invoice_1004.json")
    assert ex.invoice_number == "INV-1004"
    assert ex.vendor.name == "Precision Parts Ltd."
    assert ex.vendor.address.startswith("742 Evergreen")
    assert ex.line_items[0].quantity == "3"
    assert ex.line_items[0].unit_price == "250.00"
    assert ex.tax_rate == "0.08"
    assert ex.total == "1890.00"
    assert parse("invoice_1004_revised.json").revision == "R1"
    assert "Revised" in parse("invoice_1004_revised.json").notes
    assert parse("invoice_1013.json").line_items[3].note == "Volume discount"
    assert parse("invoice_1013.json").line_items[0].amount == "3750.00"


def test_1006_key_value_csv():
    ex = parse("invoice_1006.csv")
    assert ex.invoice_number == "INV-1006"
    assert ex.vendor.name == "Acme Industrial Supplies"
    assert [(ln.item, ln.quantity, ln.unit_price) for ln in ex.line_items] == [
        ("WidgetA", "5", "250.00"), ("WidgetB", "3", "500.00")]
    assert ex.subtotal == "2750.00"
    assert ex.tax_amount == "0.00"
    assert ex.total == "2750.00"
    assert ex.payment_terms == "Net 15"
    assert ex.date == "2026-01-25" and ex.due_date == "2026-02-10"


def test_1007_table_csv():
    ex = parse("invoice_1007.csv")
    assert ex.invoice_number == "INV-1007"
    assert ex.vendor.name == "MegaWidgets Corp"
    assert ex.date == "01/28/2026" and ex.due_date == "02/28/2026"
    assert len(ex.line_items) == 3
    assert (ex.line_items[2].item, ex.line_items[2].quantity, ex.line_items[2].amount) == ("GadgetX", "3", "2250.00")
    assert ex.subtotal == "14750.00"
    assert ex.tax_rate == "6%"
    assert ex.tax_amount == "885.00"
    assert ex.total == "15525.00"


def test_1009_empty_vendor_negative_quantity():
    ex = parse("invoice_1009.json")
    assert ex.vendor.name == ""
    assert ex.vendor.address is None
    assert ex.line_items[0].quantity == "-5"
    assert ex.due_date is None
    assert ex.total == "-250.00"


def test_1014_xml():
    ex = parse("invoice_1014.xml")
    assert ex.currency == "EUR"
    assert ex.tax_rate == "0.10"
    assert ex.vendor.name == "TechParts International"
    assert [ln.item for ln in ex.line_items] == ["WidgetA", "WidgetB"]
    assert ex.line_items[1].unit_price == "475.00"
    assert ex.payment_terms == "Net 30"
    assert ex.total == "4125.00"


def test_csv_layout_detection_and_shipping():
    ex = parse_structured("csv", b"Invoice Number,Vendor,Item,Qty,Price\nINV-9,V,A,2,5\n,,,,\n,,Shipping:,3\n,,Total:,13\n")
    assert ex.shipping == "3" and ex.total == "13"
    assert ex.line_items[0].unit_price == "5"


@pytest.mark.parametrize(
    "fmt,data",
    [
        ("json", b'{"invoice_number": '),
        ("json", b"[1, 2]"),
        ("csv", b"just some words\nwithout structure\n"),
        ("csv", b""),
        ("xml", b"<invoice><header>"),
        ("xml", b"<invoice/>"),
    ],
)
def test_corrupt_documents_raise_parse_error(fmt, data):
    with pytest.raises(ParseError):
        parse_structured(fmt, data)


def test_pdf_text():
    t11 = extract_pdf_text((SAMPLES / "invoice_1011.pdf").read_bytes())
    t12 = extract_pdf_text((SAMPLES / "invoice_1012.pdf").read_bytes())
    t13 = extract_pdf_text((SAMPLES / "invoice_1013.pdf").read_bytes())
    assert "Total: $3,000.00" in t11
    assert "INV NO: INV 1012" in t12
    assert "Grand Total: $22,562.80" in t13


def test_pdf_garbage_and_unsupported_suffix():
    with pytest.raises(ParseError):
        extract_pdf_text(b"this is not a pdf")
    with pytest.raises(ParseError):
        detect_format(SAMPLES / "invoice.docx")


def test_detect_format_and_decode_text():
    assert [detect_format(SAMPLES / n) for n in ("a.JSON", "a.csv", "a.xml", "a.txt", "a.pdf")] == [
        "json", "csv", "xml", "txt", "pdf"]
    assert decode_text("﻿hi".encode("utf-8")) == "hi"
    assert decode_text("café".encode("latin-1")) == "café"
