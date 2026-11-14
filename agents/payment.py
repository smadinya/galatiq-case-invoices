"""Payment node: wraps the bank stub with retries, backoff, idempotency and a single atomic write."""

import contextlib
import io
from decimal import Decimal
from typing import Any

from agents.validation import required_quantities
from core.invoice_math import to_usd
from deps import Deps
from models import (
    FlagCode,
    InvoiceState,
    InvoiceUpdate,
    PaymentWrite,
    Status,
    make_flag,
)
from tools.bank import mock_payment


class PaymentError(Exception):
    """One failed payment attempt (injected or a non-success bank reply)."""


def _key(state: InvoiceState) -> str:
    inv = state.invoice
    vendor_id = state.vendor.vendor_id if state.vendor else None
    ident = vendor_id if vendor_id is not None else inv.vendor_key
    return f"{ident}:{inv.invoice_number}:{inv.revision or 'R0'}"


def _stock_deltas(state: InvoiceState, deps: Deps) -> dict[str, int]:
    deltas: dict[str, int] = {}
    for key, qty in required_quantities(state.invoice, state.dedupe).items():
        item = deps.repo.get_item_by_key(key)
        if item is None or qty == 0:
            continue
        deltas[item.item] = deltas.get(item.item, 0) - qty
    return deltas


def _fail(state: InvoiceState, deps: Deps, key: str, amount: Decimal, attempts: int, msg: str) -> dict[str, Any]:
    inv = state.invoice
    deps.repo.record_payment_failure(
        idempotency_key=key, invoice_id=state.invoice_id, amount=amount, currency=inv.currency,
        attempts=attempts, error=msg,
    )
    deps.repo.audit(
        trace_id=state.trace_id, actor="system", event="payment_failed",
        payload={"key": key, "attempts": attempts, "error": msg},
    )
    deps.log.event(
        "payment", trace_id=state.trace_id, node="pay", invoice=state.file_name,
        data={"outcome": "failed", "attempts": attempts, "error": msg},
    )
    flags = state.flags
    if not any(f.code == FlagCode.PAYMENT_FAILED for f in flags):
        flags = flags + [make_flag(FlagCode.PAYMENT_FAILED, f"Payment failed: {msg}", attempts=attempts, error=msg)]
    return {"payment_error": msg, "flags": flags}


def pay(state: InvoiceState, deps: Deps) -> dict[str, Any]:
    inv = state.invoice
    assert inv is not None, "pay needs a normalized invoice"
    settings = deps.settings
    key = _key(state)

    existing = deps.repo.get_payment(key)
    if existing is not None and existing.status == "SUCCESS" and existing.receipt is not None:
        return {"payment": existing.receipt, "status": Status.PAID, "payment_error": None}

    amount = state.decision.payment_amount if state.decision and state.decision.payment_amount is not None else None
    if amount is None:
        amount = state.payable_amount if state.payable_amount is not None else inv.total
    fx = state.human.resolution.fx_rate if state.human and state.human.resolution else None

    if amount <= 0:
        deps.repo.update_invoice(state.invoice_id, InvoiceUpdate(status=Status.CREDIT_DUE))
        deps.repo.audit(
            trace_id=state.trace_id, actor="system", event="credit_due",
            payload={"amount": str(amount), "currency": inv.currency, "key": key},
        )
        return {"status": Status.CREDIT_DUE, "payment_error": None}

    amount_usd = to_usd(amount, inv.currency, fx)
    if amount_usd is None:
        return _fail(state, deps, key, amount, 0, f"no FX rate for {inv.currency}")

    vendor_name = inv.vendor_name or ""
    attempts_max = 1 + settings.payment_retries
    last_error = ""
    for attempt in range(1, attempts_max + 1):
        if attempt > 1:
            i = attempt - 1
            deps.sleep(settings.payment_backoff_s * 2 ** (i - 1))
            deps.log.event(
                "retry", trace_id=state.trace_id, node="pay", invoice=state.file_name,
                data={"stage": "payment", "attempt": attempt, "error": last_error},
            )
        try:
            if deps.rng.random() < deps.payment_fail_rate:
                raise PaymentError("injected failure")
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                response = mock_payment(vendor_name, amount)
            if response != {"status": "success"}:
                raise PaymentError(f"bank replied {response!r}")
        except PaymentError as exc:
            last_error = str(exc)
            continue
        receipt = deps.repo.record_payment(
            PaymentWrite(
                idempotency_key=key, invoice_id=state.invoice_id, trace_id=state.trace_id, vendor=vendor_name,
                amount=amount, currency=inv.currency, fx_rate=fx, amount_usd=amount_usd, attempts=attempt,
                month=deps.budget_month, stock_deltas=_stock_deltas(state, deps), bank_response=response,
            )
        )
        deps.repo.audit(
            trace_id=state.trace_id, actor="system", event="payment",
            payload={
                "key": key, "amount": str(amount), "currency": inv.currency, "amount_usd": str(amount_usd),
                "attempts": attempt, "bank_line": buf.getvalue().strip(),
            },
        )
        deps.log.event(
            "payment", trace_id=state.trace_id, node="pay", invoice=state.file_name,
            data={"outcome": "success", "attempts": attempt, "amount_usd": str(amount_usd)},
        )
        return {"payment": receipt, "status": Status.PAID, "payment_error": None}
    return _fail(state, deps, key, amount, attempts_max, last_error)
