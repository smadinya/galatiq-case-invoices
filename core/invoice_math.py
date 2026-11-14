"""Totals recomputation, fingerprints and payable amounts. Package A owns this file."""

import hashlib
import json
from decimal import Decimal

from core.values import money_str, to_cents
from models import Invoice, LineCheck, LineItem, PaymentBasis, Totals

_ZERO = Decimal("0")


def recompute_totals(invoice: Invoice, tolerance: Decimal = Decimal("0.01")) -> Totals:
    mismatches: list[str] = []
    lines: list[LineCheck] = []
    for i, li in enumerate(invoice.line_items):
        recomputed = to_cents(li.quantity * li.unit_price)
        ok = li.amount is None or abs(li.amount - recomputed) <= tolerance
        lines.append(
            LineCheck(
                index=i, item=li.item, quantity=li.quantity, unit_price=li.unit_price,
                stated_amount=li.amount, recomputed_amount=recomputed, ok=ok,
            )
        )
        if not ok:
            mismatches.append(
                f"line {i + 1} ({li.item}): stated {money_str(li.amount)} but "
                f"{li.quantity} x {money_str(li.unit_price)} = {money_str(recomputed)}"
            )
    lines_sum = sum((lc.recomputed_amount for lc in lines), _ZERO)

    subtotal_ok: bool | None = None
    if invoice.subtotal is not None:
        subtotal_ok = abs(invoice.subtotal - lines_sum) <= tolerance
        if not subtotal_ok:
            mismatches.append(
                f"subtotal: stated {money_str(invoice.subtotal)} but lines sum to {money_str(lines_sum)}"
            )

    rate_tax = to_cents(lines_sum * invoice.tax_rate) if invoice.tax_rate is not None else None
    if invoice.tax_amount is not None:
        tax_used, tax_source = invoice.tax_amount, "stated"
    elif rate_tax is not None:
        tax_used, tax_source = rate_tax, "rate"
    else:
        tax_used, tax_source = _ZERO, "none"
    tax_ok: bool | None = None
    if rate_tax is not None and invoice.tax_amount is not None:
        tax_ok = abs(rate_tax - invoice.tax_amount) <= tolerance
        if not tax_ok:
            mismatches.append(
                f"tax: stated {money_str(invoice.tax_amount)} but rate gives {money_str(rate_tax)}"
            )

    shipping = invoice.shipping or _ZERO
    total_recomputed = lines_sum + tax_used + shipping
    difference = invoice.total - total_recomputed
    total_ok = abs(difference) <= tolerance
    if not total_ok:
        mismatches.append(
            f"total: stated {money_str(invoice.total)} but recomputed {money_str(total_recomputed)} "
            f"(difference {money_str(difference)})"
        )

    return Totals(
        lines=lines, lines_sum=lines_sum, subtotal_stated=invoice.subtotal, subtotal_ok=subtotal_ok,
        tax_used=tax_used, tax_source=tax_source, tax_ok=tax_ok, shipping=shipping,
        total_stated=invoice.total, total_recomputed=total_recomputed, total_ok=total_ok,
        difference=difference, mismatches=mismatches,
    )


def fingerprint(invoice: Invoice) -> str:
    payload = {
        "vendor": invoice.vendor_key or "",
        "number": invoice.invoice_number or "",
        "currency": invoice.currency,
        "total": money_str(invoice.total),
        "lines": sorted([li.item_key, li.quantity, money_str(li.unit_price)] for li in invoice.line_items),
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def aggregate_quantities(lines: list[LineItem]) -> dict[str, int]:
    out: dict[str, int] = {}
    for li in lines:
        out[li.item_key] = out.get(li.item_key, 0) + li.quantity
    return out


def payable_amount(
    invoice: Invoice, totals: Totals, *, revision_paid_to_date: Decimal | None
) -> tuple[Decimal, PaymentBasis]:
    if totals.total_ok:
        base, basis = totals.total_stated, "stated_total"
    else:
        base, basis = min(totals.total_stated, totals.total_recomputed), "lower_total"
    if revision_paid_to_date is not None:
        return to_cents(base - revision_paid_to_date), "revision_difference"
    return to_cents(base), basis


def to_usd(amount: Decimal, currency: str, fx_rate: Decimal | None) -> Decimal | None:
    if currency.upper() == "USD":
        return amount
    if fx_rate is None:
        return None
    return to_cents(amount * fx_rate)
