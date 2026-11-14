import xml.etree.ElementTree as ET

from models import ExtractedInvoice, ExtractedLine, ExtractedVendor


def _t(el: ET.Element | None) -> str | None:
    if el is None or el.text is None:
        return None
    return el.text.strip()


def parse_xml(text: str) -> ExtractedInvoice:
    from tools.parsers import ParseError

    if "<!ENTITY" in text or "<!DOCTYPE" in text:
        raise ParseError("XML DTDs and entities are not accepted")
    try:
        root = ET.fromstring(text.lstrip("﻿").encode("utf-8"))
    except ET.ParseError as exc:
        raise ParseError(f"invalid XML: {exc}") from exc

    header, items, totals = root.find("header"), root.find("line_items"), root.find("totals")
    if header is None and items is None and totals is None:
        raise ParseError("XML has no header, line_items or totals")

    def h(name: str) -> str | None:
        return _t(header.find(name)) if header is not None else None

    vendor_el = header.find("vendor") if header is not None else None
    if vendor_el is not None and len(vendor_el):
        vendor = ExtractedVendor(
            name=_t(vendor_el.find("name")),
            address=_t(vendor_el.find("address")),
            former_name=_t(vendor_el.find("former_name")),
        )
    else:
        vendor = ExtractedVendor(name=_t(vendor_el), address=h("address"))

    lines = []
    for item in items.findall("item") if items is not None else []:
        lines.append(
            ExtractedLine(
                item=_t(item.find("name")) if item.find("name") is not None else _t(item.find("item")),
                quantity=_t(item.find("quantity")),
                unit_price=_t(item.find("unit_price")),
                amount=_t(item.find("amount")),
                note=_t(item.find("note")),
            )
        )

    def t(name: str) -> str | None:
        return _t(totals.find(name)) if totals is not None else None

    return ExtractedInvoice(
        invoice_number=h("invoice_number"),
        revision=h("revision"),
        vendor=vendor,
        date=h("date"),
        due_date=h("due_date"),
        currency=h("currency"),
        line_items=lines,
        subtotal=t("subtotal"),
        tax_rate=t("tax_rate"),
        tax_amount=t("tax_amount"),
        shipping=t("shipping"),
        total=t("total"),
        payment_terms=_t(root.find("payment_terms")),
        notes=_t(root.find("notes")),
    )
