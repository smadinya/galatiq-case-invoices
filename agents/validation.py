"""Validation: dedupe, vendor resolution, address check, flag rules, ceiling and needs_critic.

Deterministic Python only. Nodes return changed fields as full replacement values.
"""

import datetime as dt
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from config.settings import Settings
from core.invoice_math import (
    aggregate_quantities,
    fingerprint as compute_fingerprint,
    payable_amount,
    recompute_totals,
    to_usd,
)
from core.text import address_key, did_you_mean, vendor_key as make_vendor_key
from core.values import money_str
from db.repo import Repo
from deps import Deps
from models import (
    AddressCheck,
    Ceiling,
    DedupeKind,
    DedupeResult,
    Flag,
    FlagCode,
    Invoice,
    InvoiceState,
    InvoiceUpdate,
    NormalizedChange,
    Severity,
    Status,
    StockShortfall,
    ValidationResult,
    VendorMatch,
    ceiling_for,
    make_flag,
)

_ZERO = Decimal("0")
_TENTH = Decimal("0.1")


# ================================================================== dedupe


def _added_quantities(invoice: Invoice, original) -> dict[str, int]:
    original_lines = original.result.invoice.line_items if original.result and original.result.invoice else []
    before = aggregate_quantities(original_lines)
    now = aggregate_quantities(invoice.line_items)
    return {k: q - before.get(k, 0) for k, q in now.items() if q - before.get(k, 0) > 0}


def _original_fields(rec) -> dict[str, Any]:
    return dict(
        original_invoice_id=rec.id,
        original_trace_id=rec.trace_id,
        original_file_name=rec.file_name,
        original_status=rec.status,
    )


def dedupe(invoice: Invoice, *, fingerprint: str, repo: Repo, exclude_invoice_id: int) -> DedupeResult:
    same_fp = repo.find_by_fingerprint(fingerprint, exclude_id=exclude_invoice_id)
    if same_fp is not None:
        return DedupeResult(kind=DedupeKind.DUPLICATE, **_original_fields(same_fp))

    if invoice.vendor_key and invoice.invoice_number:
        originals = repo.find_by_vendor_number(
            invoice.vendor_key, invoice.invoice_number, exclude_id=exclude_invoice_id
        )
        if originals:
            if not invoice.is_revision:
                return DedupeResult(kind=DedupeKind.CONFLICT, **_original_fields(originals[0]))
            paid = [r for r in originals if r.status == Status.PAID]
            if paid:
                original = paid[0]
                return DedupeResult(
                    kind=DedupeKind.REVISION_OF_PAID,
                    paid_to_date=repo.paid_to_date(invoice.vendor_key, invoice.invoice_number),
                    added_quantities=_added_quantities(invoice, original),
                    **_original_fields(original),
                )
            return DedupeResult(kind=DedupeKind.REVISION_REPLACES, **_original_fields(originals[0]))
    return DedupeResult(kind=DedupeKind.NEW)


# ================================================================== vendor


def resolve_vendor(invoice: Invoice, repo: Repo) -> VendorMatch:
    name = (invoice.vendor_name or "").strip()
    key = invoice.vendor_key or (make_vendor_key(name) if name else "")
    if not name or not key:
        return VendorMatch(kind="missing")
    rec = repo.find_vendor_by_key(key)
    if rec is not None:
        return VendorMatch(kind="exact", vendor_id=rec.id, master_name=rec.name, matched_name=name)
    hit = repo.find_vendor_by_alias(key)
    if hit is not None:
        rec, alias = hit
        return VendorMatch(kind="alias", vendor_id=rec.id, master_name=rec.name, matched_name=alias.alias)
    former = (invoice.vendor_former_name or "").strip()
    if former:
        fkey = make_vendor_key(former)
        rec = repo.find_vendor_by_key(fkey) if fkey else None
        if rec is None and fkey:
            hit = repo.find_vendor_by_alias(fkey)
            rec = hit[0] if hit else None
        if rec is not None:
            return VendorMatch(kind="renamed", vendor_id=rec.id, master_name=rec.name, matched_name=former)
    return VendorMatch(kind="unknown")


def check_address(invoice: Invoice, vendor: VendorMatch, repo: Repo) -> AddressCheck:
    if vendor.kind in ("unknown", "missing") or vendor.vendor_id is None:
        return "no_record"
    record = repo.get_vendor(vendor.vendor_id)
    if record is None or not (record.address or "").strip():
        return "no_record"
    if not (invoice.vendor_address or "").strip():
        return "not_on_invoice"
    if address_key(invoice.vendor_address) == address_key(record.address):
        return "verified"
    return "mismatch"


# ================================================================== stock


def required_quantities(invoice: Invoice, dedupe: DedupeResult | None) -> dict[str, int]:
    if dedupe is not None and dedupe.kind == DedupeKind.REVISION_OF_PAID:
        return dict(dedupe.added_quantities)
    return aggregate_quantities(invoice.line_items)


def stock_shortfalls(required: dict[str, int], repo: Repo) -> list[StockShortfall]:
    out: list[StockShortfall] = []
    for key, needed in required.items():
        item = repo.get_item_by_key(key)
        if item is not None and needed > item.stock:
            out.append(StockShortfall(item=item.item, needed=needed, available=item.stock))
    return out


# ================================================================== validate


def _pct(unit: Decimal, catalog: Decimal) -> str:
    return str((((unit / catalog) - 1) * 100).quantize(_TENTH, rounding=ROUND_HALF_UP))


def validate(
    invoice: Invoice,
    *,
    dedupe: DedupeResult,
    changes: list[NormalizedChange],
    repo: Repo,
    settings: Settings,
    as_of: dt.date,
) -> ValidationResult:
    flags: list[Flag] = []

    def raise_(code: FlagCode, message: str, **evidence: Any) -> None:
        flags.append(make_flag(code, message, **evidence))

    totals = recompute_totals(invoice, settings.math_tolerance_usd)
    amount_usd = to_usd(invoice.total, invoice.currency, None)
    vendor = resolve_vendor(invoice, repo)
    address = check_address(invoice, vendor, repo)
    is_usd = invoice.currency == "USD"

    # structure
    if invoice.invoice_number is None:
        raise_(FlagCode.MISSING_INVOICE_NUMBER, "The invoice has no invoice number.")
    if not invoice.line_items:
        raise_(FlagCode.NO_LINE_ITEMS, "The invoice has no line items.")
    bad = [
        {"index": i, "item": li.item, "quantity": li.quantity}
        for i, li in enumerate(invoice.line_items)
        if li.quantity <= 0
    ]
    if bad:
        raise_(FlagCode.NON_POSITIVE_QUANTITY, f"{len(bad)} line(s) have a quantity of zero or less.", lines=bad)

    # vendor
    if vendor.kind == "missing":
        raise_(FlagCode.MISSING_VENDOR, "The invoice names no vendor.")
    elif vendor.kind == "unknown":
        raise_(
            FlagCode.UNKNOWN_VENDOR,
            f"Vendor {invoice.vendor_name!r} is not in the vendor master.",
            vendor=invoice.vendor_name,
        )
    elif vendor.kind == "renamed":
        raise_(
            FlagCode.VENDOR_RENAMED,
            f"Vendor {invoice.vendor_name!r} says it was formerly {vendor.matched_name!r}, "
            f"which is {vendor.master_name!r} in the vendor master.",
            vendor=invoice.vendor_name,
            former_name=vendor.matched_name,
            vendor_id=vendor.vendor_id,
            master_name=vendor.master_name,
        )

    if invoice.due_date is None:
        raise_(FlagCode.INVALID_DUE_DATE, "The due date is missing or not a date.", raw=invoice.due_date_raw)

    # items and stock
    inventory = {i.item_key: i for i in repo.list_inventory()}
    names = {key: i.item for key, i in inventory.items()}
    printed: dict[str, str] = {}
    for li in invoice.line_items:
        printed.setdefault(li.item_key, li.item)
    for key, as_printed in printed.items():
        if key not in inventory:
            raise_(
                FlagCode.UNKNOWN_ITEM,
                f"Item {as_printed!r} is not in inventory.",
                item=as_printed,
                did_you_mean=did_you_mean(key, names),
            )
    required = required_quantities(invoice, dedupe)
    for key, needed in required.items():
        item = inventory.get(key)
        if item is None or needed <= 0:
            continue
        if item.stock == 0:
            raise_(
                FlagCode.ZERO_STOCK_ITEM,
                f"{item.item} is out of stock (requested {needed}).",
                item=item.item,
                requested=needed,
            )
        elif needed > item.stock:
            raise_(
                FlagCode.INSUFFICIENT_STOCK,
                f"{item.item}: {needed} requested but only {item.stock} in stock.",
                item=item.item,
                requested=needed,
                available=item.stock,
            )

    # totals
    if totals.mismatches:
        raise_(
            FlagCode.MATH_MISMATCH,
            "The invoice arithmetic does not add up: " + "; ".join(totals.mismatches),
            stated_total=money_str(totals.total_stated),
            recomputed_total=money_str(totals.total_recomputed),
            difference=money_str(totals.difference),
            mismatches=list(totals.mismatches),
        )

    # catalog prices (USD only)
    if is_usd:
        factor = 1 + settings.price_above_catalog_pct / 100
        above: list[dict[str, Any]] = []
        below: list[dict[str, Any]] = []
        for i, li in enumerate(invoice.line_items):
            item = inventory.get(li.item_key)
            if item is None or item.unit_price <= 0:
                continue
            row = {
                "index": i,
                "item": li.item,
                "unit_price": money_str(li.unit_price),
                "catalog": money_str(item.unit_price),
                "pct": _pct(li.unit_price, item.unit_price),
            }
            if li.unit_price > item.unit_price * factor:
                above.append(row)
            elif li.unit_price < item.unit_price:
                below.append(row)
        if above:
            raise_(
                FlagCode.PRICE_ABOVE_CATALOG,
                f"{len(above)} line(s) are priced more than {settings.price_above_catalog_pct}% above catalog.",
                lines=above,
            )
        if below:
            raise_(FlagCode.PRICE_BELOW_CATALOG, f"{len(below)} line(s) are priced below catalog.", lines=below)

    if not is_usd:
        raise_(FlagCode.FOREIGN_CURRENCY, f"The invoice is in {invoice.currency}.", currency=invoice.currency)

    # dedupe outcomes
    if dedupe.kind == DedupeKind.REVISION_OF_PAID:
        raise_(
            FlagCode.REVISION_OF_PAID_INVOICE,
            f"This revises {dedupe.original_file_name}, which was already paid.",
            original_file=dedupe.original_file_name,
            paid_to_date=money_str(dedupe.paid_to_date),
            added_quantities=dict(dedupe.added_quantities),
        )
    elif dedupe.kind == DedupeKind.CONFLICT:
        raise_(
            FlagCode.CONFLICTING_INVOICE_NUMBER,
            f"Another invoice ({dedupe.original_file_name}) has this vendor and number but different content.",
            original_file=dedupe.original_file_name,
        )

    # payable amount and budget. over_budget is only checked when no error flag fired:
    # an invoice the hard rules reject is never paid, so it can't overspend (golden 1003, 1013).
    revision_paid = dedupe.paid_to_date if dedupe.kind == DedupeKind.REVISION_OF_PAID else None
    payable, _basis = payable_amount(invoice, totals, revision_paid_to_date=revision_paid)
    payable_usd = to_usd(payable, invoice.currency, None)
    has_error = any(f.severity == Severity.ERROR for f in flags)
    if payable_usd is not None and not has_error:
        month = as_of.strftime("%Y-%m")
        spent = repo.budget_spent(month)
        if spent + payable_usd > settings.monthly_budget_usd:
            overspend = spent + payable_usd - settings.monthly_budget_usd
            raise_(
                FlagCode.OVER_BUDGET,
                f"Paying {money_str(payable_usd)} would take {month} spending past the "
                f"{money_str(settings.monthly_budget_usd)} budget.",
                payable_usd=money_str(payable_usd),
                spent=money_str(spent),
                limit=money_str(settings.monthly_budget_usd),
                overspend=money_str(overspend),
            )

    # thresholds (stated total)
    if amount_usd is not None:
        if amount_usd > settings.approval_threshold_usd:
            raise_(
                FlagCode.OVER_THRESHOLD,
                f"The total {money_str(amount_usd)} is over the approval threshold.",
                amount_usd=money_str(amount_usd),
            )
        elif settings.near_threshold_min_usd <= amount_usd < settings.approval_threshold_usd:
            raise_(
                FlagCode.NEAR_THRESHOLD,
                f"The total {money_str(amount_usd)} is just under the approval threshold.",
                amount_usd=money_str(amount_usd),
            )

    # dates
    if invoice.due_date is not None and invoice.due_date < as_of:
        days = (as_of - invoice.due_date).days
        raise_(
            FlagCode.PAST_DUE,
            f"The invoice was due {invoice.due_date.isoformat()}, {days} day(s) before {as_of.isoformat()}.",
            due_date=invoice.due_date.isoformat(),
            as_of=as_of.isoformat(),
            days=days,
        )
    if invoice.due_date is not None and invoice.invoice_date is not None and invoice.terms_days is not None:
        expected = invoice.invoice_date + dt.timedelta(days=invoice.terms_days)
        off = abs((invoice.due_date - expected).days)
        if off > settings.terms_tolerance_days:
            raise_(
                FlagCode.TERMS_MISMATCH,
                f"Due date {invoice.due_date.isoformat()} is {off} day(s) from the {invoice.terms_days}-day "
                f"terms (expected {expected.isoformat()}).",
                expected_due=expected.isoformat(),
                due_date=invoice.due_date.isoformat(),
                terms_days=invoice.terms_days,
                days_off=off,
            )

    if changes:
        raise_(
            FlagCode.VALUE_NORMALIZED,
            f"{len(changes)} value(s) were normalized from the printed text.",
            changes=[c.model_dump(mode="json") for c in changes],
        )

    return ValidationResult(
        flags=flags,
        vendor=vendor,
        address_check=address,
        amount_usd=amount_usd,
        totals=totals,
        payable_amount=payable,
    )


def compute_needs_critic(
    ceiling: Ceiling, flags: list[Flag], amount_usd: Decimal | None, settings: Settings
) -> bool:
    return (
        ceiling == Ceiling.HUMAN
        or any(f.severity == Severity.ESCALATION for f in flags)
        or amount_usd is None
        or amount_usd >= settings.near_threshold_min_usd
    )


# ================================================================== nodes


def dedupe_node(state: InvoiceState, deps: Deps) -> dict[str, Any]:
    invoice = state.invoice
    assert invoice is not None, "dedupe_node needs a normalized invoice"
    fp = compute_fingerprint(invoice)
    result = dedupe(invoice, fingerprint=fp, repo=deps.repo, exclude_invoice_id=state.invoice_id)
    deps.repo.update_invoice(
        state.invoice_id,
        InvoiceUpdate(
            fingerprint=fp,
            vendor_key=invoice.vendor_key,
            vendor_name=invoice.vendor_name,
            invoice_number=invoice.invoice_number,
            revision=invoice.revision,
            currency=invoice.currency,
            amount=invoice.total,
            invoice_date=invoice.invoice_date,
        ),
    )
    if (
        result.kind == DedupeKind.REVISION_REPLACES
        and result.original_invoice_id is not None
        and result.original_status in (Status.PENDING_HUMAN, Status.REJECTED)
    ):
        deps.repo.supersede_invoice(result.original_invoice_id)
    deps.repo.audit(
        trace_id=state.trace_id,
        actor="system",
        event="dedupe",
        payload={
            "kind": result.kind.value,
            "fingerprint": fp,
            "original_file": result.original_file_name,
            "original_status": result.original_status.value if result.original_status else None,
        },
    )
    return {
        "fingerprint": fp,
        "dedupe": result,
        "duplicate_of": result.original_file_name if result.kind == DedupeKind.DUPLICATE else None,
    }


def validate_node(state: InvoiceState, deps: Deps) -> dict[str, Any]:
    invoice = state.invoice
    assert invoice is not None, "validate_node needs a normalized invoice"
    dedupe_result = state.dedupe or DedupeResult(kind=DedupeKind.NEW)
    result = validate(
        invoice,
        dedupe=dedupe_result,
        changes=state.normalized_changes,
        repo=deps.repo,
        settings=deps.settings,
        as_of=deps.as_of,
    )
    update = InvoiceUpdate(vendor_id=result.vendor.vendor_id, amount_usd=result.amount_usd)
    deps.repo.update_invoice(state.invoice_id, update)
    codes = [f.code.value for f in result.flags]
    deps.repo.audit(
        trace_id=state.trace_id, actor="system", event="flags_raised",
        payload={"codes": codes, "address_check": result.address_check},
    )
    return {
        "flags": result.flags,
        "vendor": result.vendor,
        "address_check": result.address_check,
        "amount_usd": result.amount_usd,
        "totals": result.totals,
        "payable_amount": result.payable_amount,
    }


def compute_ceiling(state: InvoiceState, deps: Deps) -> dict[str, Any]:
    ceiling = ceiling_for(state.flags)
    needs_critic = compute_needs_critic(ceiling, state.flags, state.amount_usd, deps.settings)
    deps.repo.update_invoice(state.invoice_id, InvoiceUpdate(ceiling=ceiling))
    deps.repo.audit(
        trace_id=state.trace_id, actor="system", event="ceiling",
        payload={"ceiling": ceiling.value, "needs_critic": needs_critic,
                 "flags": [f.code.value for f in state.flags]},
    )
    return {"ceiling": ceiling, "needs_critic": needs_critic}
