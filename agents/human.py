"""Human review: queue preparation, interrupt, resolution preview and application."""

from decimal import Decimal
from typing import Any

from langgraph.types import interrupt

from agents.validation import required_quantities, stock_shortfalls
from core.invoice_math import payable_amount, recompute_totals, to_usd
from core.values import money_str
from deps import Deps
from models import (
    HUMAN_REASON_PRIORITY,
    BudgetStatus,
    Ceiling,
    Decision,
    DedupeKind,
    FlagCode,
    HumanResolution,
    HumanReview,
    InvoiceState,
    InvoiceUpdate,
    PaymentBasis,
    QueueWrite,
    ResolutionPreview,
    Severity,
    Status,
)

_ZERO = Decimal("0")


def _invoice_ref(state: InvoiceState) -> str:
    inv = state.invoice
    if inv is None or not inv.invoice_number:
        return state.file_name
    return f"{inv.invoice_number} {inv.revision}" if inv.revision else inv.invoice_number


def prepare_human(state: InvoiceState, deps: Deps) -> dict[str, Any]:
    codes = {f.code for f in state.flags if f.severity == Severity.HUMAN}
    reasons = [c for c in HUMAN_REASON_PRIORITY if c in codes]
    reasons += sorted((c for c in codes if c not in HUMAN_REASON_PRIORITY), key=lambda c: c.value)
    if not reasons:  # defensive: should not happen
        reasons = [FlagCode.PAYMENT_FAILED if state.payment_error else FlagCode.UNREADABLE_DOCUMENT]
    reason = reasons[0]
    vp = state.vp_output
    has_rec = vp is not None and reason != FlagCode.UNREADABLE_DOCUMENT
    review = HumanReview(
        reason=reason,
        reasons=reasons,
        recommendation=vp.decision if has_rec else None,
        recommendation_reasoning=vp.reasoning if has_rec else None,
        payment_amount=state.decision.payment_amount if state.decision else None,
        currency=state.invoice.currency if state.invoice else None,
    )
    deps.repo.upsert_queue_item(
        QueueWrite(invoice_id=state.invoice_id, trace_id=state.trace_id, reason=reason, review=review)
    )
    deps.repo.update_invoice(state.invoice_id, InvoiceUpdate(status=Status.PENDING_HUMAN))
    deps.repo.audit(
        trace_id=state.trace_id, actor="system", event="human_queued",
        payload={"reason": reason.value, "reasons": [r.value for r in reasons],
                 "recommendation": review.recommendation.value if review.recommendation else None,
                 "payment_amount": str(review.payment_amount) if review.payment_amount is not None else None},
    )
    return {"human": review, "status": Status.PENDING_HUMAN}


def _payment(state: InvoiceState) -> tuple[Decimal | None, PaymentBasis | None]:
    if state.decision is not None and state.decision.payment_amount is not None:
        return state.decision.payment_amount, state.decision.payment_basis
    inv = state.invoice
    if inv is None:
        return None, None
    totals = state.totals or recompute_totals(inv)
    paid = state.dedupe.paid_to_date if state.dedupe and state.dedupe.kind == DedupeKind.REVISION_OF_PAID else None
    return payable_amount(inv, totals, revision_paid_to_date=paid)


def assess_resolution(state: InvoiceState, deps: Deps, fx_rate: Decimal | None) -> ResolutionPreview:
    s = deps.settings
    budget: BudgetStatus = deps.repo.budget_status(deps.budget_month, s.monthly_budget_usd)
    reason = state.human.reason if state.human else FlagCode.UNREADABLE_DOCUMENT
    inv = state.invoice
    if inv is None:
        return ResolutionPreview(
            invoice_ref=_invoice_ref(state), reason=reason, can_approve=False, needs_fx_rate=False,
            fx_rate=fx_rate, budget=budget, approve_outcome="blocked",
        )
    amount, _ = _payment(state)
    assert amount is not None
    needs_fx = inv.currency.upper() != "USD"
    usd = to_usd(amount, inv.currency, fx_rate)
    needed = required_quantities(inv, state.dedupe)
    shortfalls = stock_shortfalls(needed, deps.repo)
    stock_needed: dict[str, int] = {}
    stock_levels: dict[str, int] = {}
    for key, qty in needed.items():  # required_quantities is keyed by item_key; the ledger uses item names
        item = deps.repo.get_item_by_key(key)
        stock_needed[item.item if item else key] = qty
        if item is not None:
            stock_levels[item.item] = item.stock
    overspend = max(_ZERO, budget.spent + usd - budget.limit) if usd is not None else _ZERO
    if needs_fx and fx_rate is None:
        outcome = "blocked"
    elif shortfalls:
        outcome = "reject_stock"
    elif amount <= 0:
        outcome = "credit_due"
    else:
        outcome = "pay"
    return ResolutionPreview(
        invoice_ref=_invoice_ref(state), reason=reason, can_approve=True, needs_fx_rate=needs_fx,
        fx_rate=fx_rate, payment_amount=amount, currency=inv.currency, payment_amount_usd=usd,
        budget=budget, overspend_usd=overspend,
        needs_overspend_confirmation=outcome == "pay" and overspend > 0,
        stock_shortfalls=shortfalls, approve_outcome=outcome,
        stock_needed=stock_needed, stock_levels=stock_levels,
    )


def _review(state: InvoiceState) -> HumanReview:
    if state.human is not None:
        return state.human
    return HumanReview(reason=FlagCode.UNREADABLE_DOCUMENT, reasons=[FlagCode.UNREADABLE_DOCUMENT])


def _requeued(state: InvoiceState, resolution: HumanResolution | None, why: str) -> dict[str, Any]:
    review = _review(state).model_copy(update={"resolution": resolution, "outcome": "requeued", "outcome_note": why})
    return {"human": review}


def _stock_memo(preview: ResolutionPreview, state: InvoiceState) -> str:
    items = "; ".join(f"{s.item}: needed {s.needed}, available {s.available}" for s in preview.stock_shortfalls)
    return (
        f"Approval by a person could not be carried out for {preview.invoice_ref}: stock is insufficient "
        f"({items}). The invoice is rejected and no payment is made."
    )


def apply_resolution(state: InvoiceState, deps: Deps, resolution: HumanResolution) -> dict[str, Any]:
    try:
        return _apply(state, deps, resolution)
    except Exception as exc:  # never raise after interrupt() returns
        try:
            deps.log.event(
                "error", trace_id=state.trace_id, node="await_human", invoice=state.file_name,
                data={"kind": "apply_resolution_failed", "error": f"{type(exc).__name__}: {exc}"},
            )
        except Exception:
            pass
        return _requeued(state, resolution, f"resolution could not be applied: {type(exc).__name__}: {exc}")


def _audit(state: InvoiceState, deps: Deps, resolution: HumanResolution, outcome: str, note: str) -> None:
    deps.repo.audit(
        trace_id=state.trace_id, actor="human", event="human_resolved",
        payload={"action": resolution.action, "note": resolution.note, "outcome": outcome,
                 "outcome_note": note,
                 "fx_rate": str(resolution.fx_rate) if resolution.fx_rate is not None else None,
                 "confirm_overspend": resolution.confirm_overspend,
                 "resolved_at": resolution.resolved_at.isoformat()},
    )


def _apply(state: InvoiceState, deps: Deps, resolution: HumanResolution) -> dict[str, Any]:
    repo = deps.repo
    review = _review(state)
    if resolution.action == "reject":
        repo.resolve_queue_item(state.invoice_id, resolution="reject", note=resolution.note)
        decision = Decision(final=Ceiling.REJECT, by="human", reasoning=resolution.note)
        _audit(state, deps, resolution, "rejected", resolution.note)
        return {
            "human": review.model_copy(
                update={"resolution": resolution, "outcome": "rejected", "outcome_note": resolution.note}
            ),
            "decision": decision,
        }

    preview = assess_resolution(state, deps, resolution.fx_rate)
    if (
        not preview.can_approve
        or preview.approve_outcome == "blocked"
        or (preview.needs_overspend_confirmation and not resolution.confirm_overspend)
    ):
        why = "approval refused: " + (
            "nothing to pay" if not preview.can_approve
            else "exchange rate required" if preview.approve_outcome == "blocked"
            else f"budget overspend of {money_str(preview.overspend_usd)} USD not confirmed"
        )
        _audit(state, deps, resolution, "requeued", why)
        return _requeued(state, resolution, why)

    if preview.approve_outcome == "reject_stock":
        memo = _stock_memo(preview, state)
        repo.resolve_queue_item(state.invoice_id, resolution="approve", note=memo)
        _audit(state, deps, resolution, "rejected_stock", memo)
        return {
            "human": review.model_copy(
                update={"resolution": resolution, "outcome": "rejected_stock", "outcome_note": memo}
            ),
            "decision": Decision(final=Ceiling.REJECT, by="human", reasoning=memo),
        }

    inv = state.invoice
    assert inv is not None
    amount, basis = _payment(state)
    if FlagCode.VENDOR_RENAMED in review.reasons and state.vendor is not None and state.vendor.vendor_id is not None \
            and inv.vendor_name:
        repo.add_vendor_alias(state.vendor.vendor_id, inv.vendor_name, "approved_rename")
    repo.resolve_queue_item(state.invoice_id, resolution="approve", note=resolution.note)
    _audit(state, deps, resolution, "approved", resolution.note)
    return {
        "human": review.model_copy(
            update={"resolution": resolution, "outcome": "approved", "outcome_note": resolution.note}
        ),
        "decision": Decision(
            final=Ceiling.APPROVE, by="human", payment_amount=amount, payment_basis=basis,
            reasoning=resolution.note,
        ),
        "amount_usd": preview.payment_amount_usd,
    }


def await_human(state: InvoiceState, deps: Deps) -> dict[str, Any]:
    review = _review(state)
    payload = interrupt(
        {"invoice_id": state.invoice_id, "trace_id": state.trace_id, "review": review.model_dump(mode="json")}
    )
    try:
        resolution = HumanResolution.model_validate(payload)
    except Exception as exc:
        return _requeued(state, None, f"invalid resume payload: {type(exc).__name__}")
    return apply_resolution(state, deps, resolution)
