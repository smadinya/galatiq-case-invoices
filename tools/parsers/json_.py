import json
from decimal import Decimal
from typing import Any

from models import ExtractedInvoice, ExtractedLine, ExtractedVendor


def _s(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (dict, list)):
        return json.dumps(value, default=str)
    return str(value)


def parse_json(text: str) -> ExtractedInvoice:
    from tools.parsers import ParseError

    try:
        doc = json.loads(text, parse_float=Decimal)
    except (ValueError, RecursionError) as exc:
        raise ParseError(f"invalid JSON: {exc}") from exc
    if not isinstance(doc, dict):
        raise ParseError("JSON invoice must be an object")

    vendor = doc.get("vendor")
    if isinstance(vendor, dict):
        v = ExtractedVendor(
            name=_s(vendor.get("name")),
            address=_s(vendor.get("address")),
            former_name=_s(vendor.get("former_name")),
        )
    else:
        v = ExtractedVendor(name=_s(vendor), address=_s(doc.get("vendor_address")))

    raw_lines = doc.get("line_items") or []
    if not isinstance(raw_lines, list):
        raise ParseError("line_items must be a list")
    lines = []
    for row in raw_lines:
        if not isinstance(row, dict):
            raise ParseError("each line item must be an object")
        lines.append(
            ExtractedLine(
                item=_s(row.get("item", row.get("name"))),
                quantity=_s(row.get("quantity")),
                unit_price=_s(row.get("unit_price")),
                amount=_s(row.get("amount")),
                note=_s(row.get("note")),
            )
        )
    return ExtractedInvoice(
        invoice_number=_s(doc.get("invoice_number")),
        revision=_s(doc.get("revision")),
        vendor=v,
        date=_s(doc.get("date")),
        due_date=_s(doc.get("due_date")),
        currency=_s(doc.get("currency")),
        line_items=lines,
        subtotal=_s(doc.get("subtotal")),
        tax_rate=_s(doc.get("tax_rate")),
        tax_amount=_s(doc.get("tax_amount")),
        shipping=_s(doc.get("shipping")),
        total=_s(doc.get("total")),
        payment_terms=_s(doc.get("payment_terms")),
        notes=_s(doc.get("notes")),
    )
