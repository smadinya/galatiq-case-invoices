import datetime as dt
from decimal import Decimal

import pytest

from core.values import (
    Parsed, fix_ocr_digits, money_str, normalize_currency, normalize_invoice_number, normalize_revision,
    parse_date, parse_money, parse_quantity, parse_rate, parse_terms_days, to_cents,
)

D = Decimal


def test_fix_ocr_digits():
    assert fix_ocr_digits("2O26") == ("2026", True)
    assert fix_ocr_digits("$3,500.O0") == ("$3,500.00", True)
    assert fix_ocr_digits("INVOICE") == ("INVOICE", False)
    assert fix_ocr_digits("Illinois IL 60601") == ("Illinois IL 60601", False)
    assert fix_ocr_digits("1l") == ("11", True)
    assert fix_ocr_digits("26-Jan-2O26") == ("26-Jan-2026", True)


@pytest.mark.parametrize(
    "raw, value, changed",
    [
        ("$3,500.O0", D("3500.00"), True),
        ("$15,000.00", D("15000.00"), False),
        ("$250", D("250"), False),
        ("-250.00", D("-250.00"), False),
        ("$-250.00", D("-250.00"), False),
        ("USD 1,234.50", D("1234.50"), False),
        ("€ 99", D("99"), False),
        ("0.00", D("0.00"), False),
        ("abc", None, False),
        ("", None, False),
        (None, None, False),
        (250, D("250"), False),
        (D("12.50"), D("12.50"), False),
    ],
)
def test_parse_money(raw, value, changed):
    assert parse_money(raw) == Parsed(value, changed)


def test_parse_money_float_goes_through_str():
    assert parse_money(250.5).value == D("250.5")


@pytest.mark.parametrize(
    "raw, value",
    [("12", 12), ("-5", -5), ("x12", 12), ("X 12", 12), ("12.0", 12), ("12.5", None), ("abc", None),
     (7, 7), (None, None), ("", None)],
)
def test_parse_quantity(raw, value):
    assert parse_quantity(raw).value == value


@pytest.mark.parametrize(
    "raw, value",
    [("5%", D("0.05")), ("0.08", D("0.08")), ("10", D("0.10")), ("0", D("0")), ("0.0", D("0.0")),
     ("", None), (None, None), ("abc", None), (0.07, D("0.07")), (D("6"), D("0.06")), ("8.5%", D("0.085"))],
)
def test_parse_rate(raw, value):
    assert parse_rate(raw).value == value


@pytest.mark.parametrize(
    "raw, value, changed",
    [
        ("2026-01-15", dt.date(2026, 1, 15), False),
        ("Jan 30 2026", dt.date(2026, 1, 30), False),
        ("01/28/2026", dt.date(2026, 1, 28), False),
        ("January 27, 2026", dt.date(2026, 1, 27), False),
        ("26-Jan-2O26", dt.date(2026, 1, 26), True),
        ("25-Feb-2026", dt.date(2026, 2, 25), False),
        ("Jan 30, 2026", dt.date(2026, 1, 30), False),
        ("30 Jan 2026", dt.date(2026, 1, 30), False),
        ("yesterday", None, False),
        ("", None, False),
        (None, None, False),
        ("2026-13-45", None, False),
    ],
)
def test_parse_date(raw, value, changed):
    assert parse_date(raw) == Parsed(value, changed)


@pytest.mark.parametrize(
    "raw, days",
    [("Net 30", 30), ("Net 15", 15), ("net60", 60), ("Immediate", 0), ("Due on receipt", 0),
     ("Upon receipt", 0), ("30 days", 30), ("", None), (None, None), ("whenever", None)],
)
def test_parse_terms_days(raw, days):
    assert parse_terms_days(raw) == days


@pytest.mark.parametrize(
    "raw, value, changed",
    [
        ("INV-1001", "INV-1001", False),
        ("1002", "INV-1002", True),
        ("INV 1012", "INV-1012", True),
        ("#INV-1010", "INV-1010", False),
        ("INV-1O12", "INV-1012", True),
        ("Invoice #55", "INV-55", True),
        ("PO-ABC", "PO-ABC", False),
        ("  INV-7  ", "INV-7", False),
        ("", None, False),
        (None, None, False),
    ],
)
def test_normalize_invoice_number(raw, value, changed):
    assert normalize_invoice_number(raw) == Parsed(value, changed)


def test_normalize_revision():
    assert normalize_revision("R1") == "R1"
    assert normalize_revision("1") == "R1"
    assert normalize_revision("r2") == "R2"
    assert normalize_revision("") is None
    assert normalize_revision(None) is None


@pytest.mark.parametrize(
    "raw, code",
    [(None, "USD"), ("", "USD"), ("$", "USD"), ("usd", "USD"), ("US$", "USD"), ("€", "EUR"),
     ("eur", "EUR"), ("EUR", "EUR"), ("gbp", "GBP"), ("cad", "CAD")],
)
def test_normalize_currency(raw, code):
    assert normalize_currency(raw) == code


def test_cents_rounding_is_half_up():
    assert to_cents(D("1.005")) == D("1.01")
    assert to_cents(D("2.675")) == D("2.68")
    assert to_cents(D("-1.005")) == D("-1.01")
    assert money_str(D("9975")) == "9975.00"
    assert money_str(D("1472.8")) == "1472.80"
