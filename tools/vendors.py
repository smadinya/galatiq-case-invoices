"""Vendor resolution and vendor-related tools."""

import datetime as dt
from decimal import Decimal

from core.text import split_vendor_rename, vendor_key
from core.values import money_str
from db.repo import Repo
from deps import Deps
from models import InvoiceRecord, InvoiceState, VendorMatch
from tools.registry import tool


def _resolve(repo: Repo, name: str):
    """(vendor record, alias or None) for a name by exact master key, then alias; else None."""
    key = vendor_key(name)
    if not key:
        return None
    vendor = repo.find_vendor_by_key(key)
    if vendor is not None:
        return vendor, None
    hit = repo.find_vendor_by_alias(key)
    if hit is not None:
        return hit
    return None


def lookup_vendor(repo: Repo, name: str) -> VendorMatch:
    """missing -> exact -> alias -> renamed (inline former-name clause) -> unknown."""
    if not name or not name.strip():
        return VendorMatch(kind="missing")
    current, former = split_vendor_rename(name.strip())
    hit = _resolve(repo, current)
    if hit is not None:
        vendor, alias = hit
        if alias is None:
            return VendorMatch(kind="exact", vendor_id=vendor.id, master_name=vendor.name, matched_name=current)
        return VendorMatch(kind="alias", vendor_id=vendor.id, master_name=vendor.name, matched_name=alias.alias)
    if former:
        hit = _resolve(repo, former)
        if hit is not None:
            vendor, _ = hit
            return VendorMatch(kind="renamed", vendor_id=vendor.id, master_name=vendor.name, matched_name=former)
    return VendorMatch(kind="unknown")


def _record_row(r: InvoiceRecord) -> dict:
    return {
        "invoice_number": r.invoice_number,
        "revision": r.revision,
        "status": r.status,
        "total": money_str(r.amount) if r.amount is not None else None,
        "currency": r.currency,
        "invoice_date": r.invoice_date.isoformat() if r.invoice_date else None,
        "file": r.file_name,
    }


def _vendor_records(deps: Deps, state: InvoiceState, name: str, match: VendorMatch) -> list[InvoiceRecord]:
    return deps.repo.vendor_invoices(
        vendor_id=match.vendor_id,
        vendor_key=vendor_key(split_vendor_rename(name)[0]) or None,
        exclude_trace=state.trace_id,
    )


@tool()
def get_vendor_history(deps: Deps, state: InvoiceState, vendor: str) -> dict:
    """List earlier invoices from a vendor (excluding this invoice and failed reads)."""
    match = lookup_vendor(deps.repo, vendor)
    records = _vendor_records(deps, state, vendor, match) if match.kind != "missing" else []
    return {
        "vendor": vendor,
        "known": match.kind in ("exact", "alias", "renamed"),
        "master_name": match.master_name,
        "invoices": [_record_row(r) for r in records],
    }


@tool()
def find_similar_invoices(deps: Deps, state: InvoiceState, vendor: str, amount: Decimal, days: int) -> dict:
    """Find same-vendor invoices dated within the given number of days of this invoice, or within 10% of the amount, and check whether together they look like a split to dodge the $10,000 threshold."""
    match = lookup_vendor(deps.repo, vendor)
    records = _vendor_records(deps, state, vendor, match) if match.kind != "missing" else []
    inv = state.invoice
    center = (inv.invoice_date if inv and inv.invoice_date else None) or deps.as_of
    current_total = inv.total if inv is not None else amount
    amount = Decimal(str(amount))
    window = dt.timedelta(days=abs(days))
    threshold = deps.settings.approval_threshold_usd
    matches = []
    for r in records:
        near_date = r.invoice_date is not None and abs(r.invoice_date - center) <= window
        near_amount = (
            r.amount is not None and amount > 0 and abs(r.amount - amount) <= amount * Decimal("0.10")
        )
        if near_date or near_amount:
            matches.append(r)
    totals = [r.amount for r in matches if r.amount is not None]
    combined = current_total + sum(totals, Decimal("0"))
    suspect = (
        bool(matches)
        and len(totals) == len(matches)
        and current_total < threshold
        and all(t < threshold for t in totals)
        and combined > threshold
    )
    return {
        "matches": [_record_row(r) for r in matches],
        "combined_total_with_current": money_str(combined),
        "split_suspect": suspect,
    }


@tool()
def get_vendor_master(deps: Deps, vendor: str) -> dict:
    """Return the vendor master record (status, address, payment terms, aliases) for a vendor name."""
    match = lookup_vendor(deps.repo, vendor)
    record = deps.repo.get_vendor(match.vendor_id) if match.vendor_id is not None else None
    if record is None:
        return {"found": False, "name": vendor}
    return {
        "found": True,
        "name": record.name,
        "status": record.status,
        "address": record.address,
        "payment_terms": record.payment_terms,
        "aliases": [a.alias for a in deps.repo.list_aliases(record.id)],
    }
