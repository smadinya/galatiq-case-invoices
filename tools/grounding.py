"""Check that an extraction's items and numbers really appear in the source text."""

import re
from decimal import Decimal, InvalidOperation

from core.text import clean_item_key, split_item_note
from core.values import fix_ocr_digits, parse_money, parse_quantity
from models import ExtractedInvoice, GroundingResult

_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _numbers(text: str) -> set[Decimal]:
    out: set[Decimal] = set()
    for tok in _NUMBER.findall(fix_ocr_digits(text)[0]):
        try:
            out.add(abs(Decimal(tok.replace(",", "").rstrip("."))))
        except InvalidOperation:
            continue
    return out


def source_numbers(source_text: str) -> set[Decimal]:
    return _numbers(source_text)


def _norm_line(text: str) -> str:
    fixed, _ = fix_ocr_digits(text)
    return re.sub(r"\s+", " ", fixed.replace("$", "").replace(",", "")).strip().lower()


def _fields(ex: ExtractedInvoice) -> list[tuple[str, str | None, str]]:
    """(path, raw, kind) with kind in {'num', 'text'}."""
    out: list[tuple[str, str | None, str]] = [
        ("invoice_number", ex.invoice_number, "text"),
        ("vendor.name", ex.vendor.name, "text"),
        ("date", ex.date, "text"),
        ("due_date", ex.due_date, "text"),
    ]
    for i, ln in enumerate(ex.line_items):
        p = f"line_items[{i}]"
        out += [
            (f"{p}.item", ln.item, "text"),
            (f"{p}.quantity", ln.quantity, "num"),
            (f"{p}.unit_price", ln.unit_price, "num"),
            (f"{p}.amount", ln.amount, "num"),
        ]
    out += [
        ("subtotal", ex.subtotal, "num"),
        ("tax_amount", ex.tax_amount, "num"),
        ("shipping", ex.shipping, "num"),
        ("total", ex.total, "num"),
    ]
    return out


def _value(raw: str, field: str) -> Decimal | None:
    p = parse_quantity(raw) if field.endswith(".quantity") else parse_money(raw)
    return None if p.value is None else abs(Decimal(p.value))


def find_evidence_spans(extracted: ExtractedInvoice, source_text: str) -> dict[str, list[str]]:
    lines = [ln.strip() for ln in source_text.splitlines() if ln.strip()]
    normed = [_norm_line(ln) for ln in lines]
    line_numbers = [_numbers(ln) for ln in lines]
    spans: dict[str, list[str]] = {}
    for path, raw, kind in _fields(extracted):
        if raw is None or not raw.strip():
            continue
        if kind == "num":
            want = _value(raw, path)
            if want is None:
                continue
            hits = [ln for ln, nums in zip(lines, line_numbers) if want in nums]
        elif path.endswith(".item"):
            key = clean_item_key(split_item_note(raw)[0])
            hits = [ln for ln in lines if key and key in clean_item_key(ln)]
        else:
            needle = _norm_line(raw)
            hits = [ln for ln, n in zip(lines, normed) if needle and needle in n]
        if hits:
            spans[path] = hits
    return spans


def ground_check(extracted: ExtractedInvoice, source_text: str) -> GroundingResult:
    errors: list[str] = []
    if not extracted.line_items and (extracted.total is None or not extracted.total.strip()):
        errors.append("empty extraction: no line items and no total")
    source_key = clean_item_key(source_text)
    nums = source_numbers(source_text)
    for path, raw, kind in _fields(extracted):
        if raw is None or not raw.strip():
            continue
        if path.endswith(".item"):
            key = clean_item_key(split_item_note(raw)[0])
            if not key or key not in source_key:
                errors.append(f"{path}: item {raw!r} not found in the source text")
        elif kind == "num":
            value = _value(raw, path)
            if value is None:
                errors.append(f"{path}: value {raw!r} is not a number")
            elif value not in nums:
                errors.append(f"{path}: value {raw!r} not found in the source text")
    return GroundingResult(ok=not errors, errors=errors, spans=find_evidence_spans(extracted, source_text))
