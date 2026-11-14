"""Verbatim ExtractedInvoice -> typed Invoice. Records real value changes; never creates flags."""

import re
from decimal import Decimal

from core.text import clean_item_key, split_item_note, split_vendor_rename, vendor_key
from core.values import (
    normalize_currency,
    normalize_invoice_number,
    normalize_revision,
    parse_date,
    parse_money,
    parse_quantity,
    parse_rate,
    parse_terms_days,
    to_cents,
)
from models import ExtractedInvoice, Invoice, LineItem, NormalizedChange, NormalizeResult


class NormalizationError(Exception):
    """total missing/unparseable, or a line quantity/unit_price unparseable."""


_REVISED = re.compile(r"\brevis(ed|ion)\b", re.IGNORECASE)


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def normalize(extracted: ExtractedInvoice) -> NormalizeResult:
    changes: list[NormalizedChange] = []

    def record(field: str, raw: str, normalized: object) -> None:
        changes.append(NormalizedChange(field=field, raw=raw, normalized=str(normalized)))

    def money(field: str, raw: str | None) -> Decimal | None:
        if raw is None or not raw.strip():
            return None
        p = parse_money(raw)
        if p.value is not None and p.changed:
            record(field, raw, p.value)
        return p.value

    number = normalize_invoice_number(extracted.invoice_number)
    if number.value is not None and number.changed:
        record("invoice_number", extracted.invoice_number or "", number.value)
    revision = normalize_revision(extracted.revision)
    notes = _clean(extracted.notes)

    vendor = extracted.vendor
    name = _clean(vendor.name)
    former = _clean(vendor.former_name)
    if name:
        current, found = split_vendor_rename(name)
        name = current
        former = former or found

    def date_field(field: str, raw: str | None):
        if raw is None or not raw.strip():
            return None
        p = parse_date(raw)
        if p.value is not None and p.changed:
            record(field, raw, p.value.isoformat())
        return p.value

    invoice_date = date_field("date", extracted.date)
    due_raw = _clean(extracted.due_date)
    due_date = date_field("due_date", extracted.due_date)

    lines: list[LineItem] = []
    for i, ln in enumerate(extracted.line_items):
        path = f"line_items[{i}]"
        item, remark = split_item_note(ln.item or "")
        note = _clean(ln.note)
        if remark and (note is None or remark.lower() not in note.lower()):
            note = f"{note}; {remark}" if note else remark
        qty = parse_quantity(ln.quantity)
        if qty.value is None:
            raise NormalizationError(f"{path}.quantity is missing or unparseable: {ln.quantity!r}")
        if qty.changed:
            record(f"{path}.quantity", ln.quantity or "", qty.value)
        amount = money(f"{path}.amount", ln.amount)
        if ln.unit_price is None or not ln.unit_price.strip():
            if amount is None or qty.value == 0:
                raise NormalizationError(f"{path}.unit_price is missing and cannot be derived: {ln.unit_price!r}")
            unit_price = to_cents(amount / qty.value)
        else:
            unit_price = money(f"{path}.unit_price", ln.unit_price)
            if unit_price is None:
                raise NormalizationError(f"{path}.unit_price is unparseable: {ln.unit_price!r}")
        lines.append(
            LineItem(
                item=item, item_key=clean_item_key(item), quantity=qty.value,
                unit_price=unit_price, amount=amount, note=note,
            )
        )

    if extracted.total is None or not extracted.total.strip():
        raise NormalizationError("total is missing")
    total = money("total", extracted.total)
    if total is None:
        raise NormalizationError(f"total is unparseable: {extracted.total!r}")

    rate = None
    if extracted.tax_rate is not None and extracted.tax_rate.strip():
        pr = parse_rate(extracted.tax_rate)
        rate = pr.value
        if rate is not None and pr.changed:
            record("tax_rate", extracted.tax_rate, rate)

    invoice = Invoice(
        invoice_number=number.value,
        revision=revision,
        is_revision=bool(revision) or bool(notes and _REVISED.search(notes)),
        vendor_name=name,
        vendor_former_name=former,
        vendor_key=vendor_key(name) if name else None,
        vendor_address=_clean(vendor.address),
        invoice_date=invoice_date,
        due_date=due_date,
        due_date_raw=due_raw,
        currency=normalize_currency(extracted.currency),
        line_items=lines,
        subtotal=money("subtotal", extracted.subtotal),
        tax_rate=rate,
        tax_amount=money("tax_amount", extracted.tax_amount),
        shipping=money("shipping", extracted.shipping),
        total=total,
        payment_terms=_clean(extracted.payment_terms),
        terms_days=parse_terms_days(extracted.payment_terms),
        notes=notes,
    )
    return NormalizeResult(invoice=invoice, changes=changes)
