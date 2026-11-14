"""Parsing and normalizing raw document values. Package A owns this file.

Money is Decimal everywhere, never float.
"""

import datetime as dt
import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Generic, TypeVar

T = TypeVar("T")


@dataclass(frozen=True)
class Parsed(Generic[T]):
    value: T | None
    changed: bool  # True = a real value change normalize must record (OCR digit fix, invoice-number reformat)


_OCR_TOKEN = re.compile(r"(?<![A-Za-z])[0-9OoIl$][0-9OoIl$,.]*")


def fix_ocr_digits(text: str) -> tuple[str, bool]:
    """In digit-bearing numeric tokens, O/o -> 0 and I/l -> 1."""
    changed = False

    def repl(m: re.Match[str]) -> str:
        nonlocal changed
        tok = m.group(0)
        if not any("0" <= c <= "9" for c in tok):
            return tok
        fixed = tok.translate(str.maketrans("OoIl", "0011"))
        if fixed != tok:
            changed = True
        return fixed

    out = _OCR_TOKEN.sub(repl, text)
    return out, changed


def _to_decimal(raw: Any) -> Decimal | None:
    if isinstance(raw, bool):
        return None
    if isinstance(raw, Decimal):
        return raw if raw.is_finite() else None
    if isinstance(raw, int):
        return Decimal(raw)
    if isinstance(raw, float):
        return Decimal(str(raw)) if raw == raw and raw not in (float("inf"), float("-inf")) else None
    return None


_NUM = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)$")
_CURRENCY_NOISE = re.compile(r"USD|EUR|[$€£\s,]", re.IGNORECASE)


def _parse_number_text(raw: str) -> Parsed[Decimal]:
    fixed, changed = fix_ocr_digits(raw.strip())
    cleaned = _CURRENCY_NOISE.sub("", fixed)
    if not cleaned or not _NUM.match(cleaned):
        return Parsed(None, False)
    try:
        return Parsed(Decimal(cleaned), changed)
    except InvalidOperation:
        return Parsed(None, False)


def parse_money(raw: str | int | Decimal | None) -> Parsed[Decimal]:
    if raw is None:
        return Parsed(None, False)
    if isinstance(raw, str):
        return _parse_number_text(raw)
    d = _to_decimal(raw)
    return Parsed(d, False)


def parse_quantity(raw: Any) -> Parsed[int]:
    if raw is None:
        return Parsed(None, False)
    if isinstance(raw, str):
        text = re.sub(r"^\s*(?:qty\s*:?|[xX×])\s*", "", raw.strip(), flags=re.IGNORECASE)
        p = _parse_number_text(text)
        d, changed = p.value, p.changed
    else:
        d, changed = _to_decimal(raw), False
    if d is None or d != d.to_integral_value():
        return Parsed(None, False)
    return Parsed(int(d), changed)


def parse_rate(raw: Any) -> Parsed[Decimal]:
    """"5%" -> 0.05, "0.08" -> 0.08, "10" -> 0.10 (values > 1 are percent)."""
    if raw is None:
        return Parsed(None, False)
    percent = False
    if isinstance(raw, str):
        text = raw.strip()
        if text.endswith("%"):
            percent = True
            text = text[:-1]
        p = _parse_number_text(text)
        d, changed = p.value, p.changed
    else:
        d, changed = _to_decimal(raw), False
    if d is None:
        return Parsed(None, False)
    if percent or d > 1:
        d = d.scaleb(-2)
    return Parsed(d, changed)


_DATE_FORMATS = (
    "%Y-%m-%d", "%b %d %Y", "%b %d, %Y", "%B %d, %Y", "%B %d %Y",
    "%m/%d/%Y", "%d-%b-%Y", "%d %b %Y", "%d-%B-%Y",
)


def parse_date(raw: Any) -> Parsed[dt.date]:
    if raw is None:
        return Parsed(None, False)
    if isinstance(raw, dt.datetime):
        return Parsed(raw.date(), False)
    if isinstance(raw, dt.date):
        return Parsed(raw, False)
    if not isinstance(raw, str):
        return Parsed(None, False)
    fixed, changed = fix_ocr_digits(re.sub(r"\s+", " ", raw.strip()))
    for fmt in _DATE_FORMATS:
        try:
            return Parsed(dt.datetime.strptime(fixed, fmt).date(), changed)
        except ValueError:
            continue
    return Parsed(None, False)


_TERMS_NET = re.compile(r"\bnet\s*(\d+)\b", re.IGNORECASE)
_TERMS_DAYS = re.compile(r"^\s*(\d+)\s*days?\s*$", re.IGNORECASE)
_TERMS_IMMEDIATE = re.compile(r"^\s*(immediate(ly)?|due\s+on\s+receipt|(up)?on\s+receipt)\s*$", re.IGNORECASE)


def parse_terms_days(raw: Any) -> int | None:
    if raw is None or not isinstance(raw, str):
        return None
    if _TERMS_IMMEDIATE.match(raw):
        return 0
    m = _TERMS_NET.search(raw) or _TERMS_DAYS.match(raw)
    return int(m.group(1)) if m else None


_INV_NUMBER = re.compile(r"^#?\s*(?:INV(?:OICE)?)?[\s\-#:.]*(\d+)$", re.IGNORECASE)


def normalize_invoice_number(raw: Any) -> Parsed[str]:
    if raw is None:
        return Parsed(None, False)
    text = str(raw).strip()
    base = text.lstrip("#").strip()
    if not base:
        return Parsed(None, False)
    fixed, _ = fix_ocr_digits(text)
    m = _INV_NUMBER.match(fixed)
    if not m:
        return Parsed(base, False)
    result = f"INV-{m.group(1)}"
    return Parsed(result, result != base)


_REVISION = re.compile(r"^(?:R|REV(?:ISION)?)?[\s\-.]*(\d+)$", re.IGNORECASE)


def normalize_revision(raw: Any) -> str | None:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    m = _REVISION.match(text)
    return f"R{int(m.group(1))}" if m else text.upper()


def normalize_currency(raw: Any) -> str:
    if raw is None:
        return "USD"
    text = str(raw).strip()
    key = text.lower()
    if key in ("", "$", "usd", "us$"):
        return "USD"
    if key in ("€", "eur"):
        return "EUR"
    if key in ("£", "gbp"):
        return "GBP"
    return text.upper()


def to_cents(d: Decimal) -> Decimal:
    return d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def money_str(d: Decimal) -> str:
    return str(to_cents(d))
