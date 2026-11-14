import csv
import io
import re

from models import ExtractedInvoice, ExtractedLine, ExtractedVendor

_ALIASES = {
    "invoice number": "invoice_number", "invoice no": "invoice_number", "invoice #": "invoice_number",
    "invoice": "invoice_number", "invoice_number": "invoice_number",
    "vendor": "vendor", "date": "date", "invoice date": "date", "due date": "due_date", "due_date": "due_date",
    "currency": "currency", "payment terms": "payment_terms", "terms": "payment_terms",
    "revision": "revision", "notes": "notes",
    "item": "item", "description": "item", "qty": "quantity", "quantity": "quantity",
    "unit price": "unit_price", "price": "unit_price", "rate": "unit_price", "unit_price": "unit_price",
    "line total": "amount", "amount": "amount", "line_total": "amount",
    "note": "note",
}
_INVOICE_FIELDS = ("invoice_number", "date", "due_date", "currency", "payment_terms", "revision", "notes")
_LINE_FIELDS = ("item", "quantity", "unit_price", "amount", "note")


def parse_csv_table(text: str) -> ExtractedInvoice:
    from tools.parsers import ParseError

    rows = [r for r in csv.reader(io.StringIO(text)) if any(c.strip() for c in r)]
    if len(rows) < 2:
        raise ParseError("CSV table needs a header row and at least one data row")
    cols: dict[str, int] = {}
    for i, h in enumerate(rows[0]):
        name = _ALIASES.get(re.sub(r"\s+", " ", h.strip().lower()))
        if name and name not in cols:
            cols[name] = i
    if "item" not in cols or "quantity" not in cols:
        raise ParseError("CSV table has no recognisable item/quantity columns")

    def cell(row: list[str], name: str) -> str | None:
        i = cols.get(name)
        if i is None or i >= len(row):
            return None
        return row[i].strip()

    data = [r for r in rows[1:] if r[0].strip()]
    if not data:
        raise ParseError("CSV table has no data rows")
    first = data[0]
    top = {f: cell(first, f) for f in _INVOICE_FIELDS}
    lines = [ExtractedLine(**{f: cell(r, f) for f in _LINE_FIELDS}) for r in data]

    summary: dict[str, str] = {}
    for r in rows[1:]:
        if r[0].strip():
            continue
        cells = [c.strip() for c in r if c.strip()]
        if len(cells) < 2:
            continue
        label, value = cells[0].lower().rstrip(":").strip(), cells[-1]
        if label.startswith("subtotal"):
            summary["subtotal"] = value
        elif label.startswith("tax"):
            summary["tax_amount"] = value
            m = re.search(r"\(([^)]*)\)", cells[0])
            if m and m.group(1).strip():
                summary["tax_rate"] = m.group(1).strip()
        elif label.startswith("ship"):
            summary["shipping"] = value
        elif "total" in label:
            summary["total"] = value
    return ExtractedInvoice(
        vendor=ExtractedVendor(name=cell(first, "vendor")),
        line_items=lines,
        **top,
        **summary,
    )
