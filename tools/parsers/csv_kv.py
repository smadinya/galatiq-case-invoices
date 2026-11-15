import csv
import io

from models import ExtractedInvoice, ExtractedLine, ExtractedVendor

_TOP = {
    "invoice_number": "invoice_number", "revision": "revision", "date": "date", "due_date": "due_date",
    "currency": "currency", "subtotal": "subtotal", "tax": "tax_amount", "tax_amount": "tax_amount",
    "tax_rate": "tax_rate", "shipping": "shipping", "total": "total", "payment_terms": "payment_terms",
    "notes": "notes",
}
_LINE = {"quantity": "quantity", "unit_price": "unit_price", "amount": "amount", "note": "note"}


def parse_csv_kv(text: str) -> ExtractedInvoice:
    from tools.parsers import ParseError

    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        raise ParseError("empty CSV")
    top: dict[str, str] = {}
    vendor: dict[str, str] = {}
    lines: list[dict[str, str]] = []
    for row in rows[1:]:
        if not row or not any(c.strip() for c in row):
            continue
        key = row[0].strip().lower().replace(" ", "_")
        value = row[1].strip() if len(row) > 1 else ""
        if key == "item":
            lines.append({"item": value})
        elif key in _LINE and lines:
            lines[-1][_LINE[key]] = value
        elif key == "vendor":
            vendor["name"] = value
        elif key in ("vendor_address", "address"):
            vendor["address"] = value
        elif key == "former_name":
            vendor["former_name"] = value
        elif key in _TOP:
            top[_TOP[key]] = value
    if not lines and "total" not in top:
        raise ParseError("CSV has no line items and no total")
    return ExtractedInvoice(
        vendor=ExtractedVendor(**vendor),
        line_items=[ExtractedLine(**ln) for ln in lines],
        **top,
    )
