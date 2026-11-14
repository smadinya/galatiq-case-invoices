"""Deterministic clamp: turns the VP/Critic outcome into the final decision."""

from typing import Any

from config.settings import Settings
from core.invoice_math import payable_amount
from deps import Deps
from models import Ceiling, Decision, DedupeKind, InvoiceState, Verdict, stricter


def decide(state: InvoiceState, settings: Settings) -> tuple[Decision, list[str]]:
    events: list[str] = []
    vp, cr = state.vp_output, state.critic_output
    vp_dec = vp.decision if vp is not None else Ceiling.REJECT
    reasoning = vp.reasoning if vp is not None else "No VP decision was produced."
    if cr is None:
        proposed, by = vp_dec, "vp"
    elif cr.verdict == Verdict.AGREE:
        proposed, by = vp_dec, "vp+critic"
    else:
        proposed, by = stricter(vp_dec, cr.proposed_decision), "deadlock"
        reasoning += " | Critic objections unresolved: " + "; ".join(
            f"{o.about}: {o.requested_change}" for o in cr.objections
        )
    ceiling = state.ceiling if state.ceiling is not None else Ceiling.REJECT
    final = min(proposed, ceiling)
    clamped = False
    if final < proposed:
        events.append("ceiling_clamped")
        clamped = True
    if final == Ceiling.HUMAN and ceiling != Ceiling.HUMAN:
        final = Ceiling.REJECT
        events.append("human_not_allowed")
        clamped = True
    if (
        final == Ceiling.APPROVE
        and state.amount_usd is not None
        and state.amount_usd > settings.approval_threshold_usd
        and not (cr is not None and cr.verdict == Verdict.AGREE)
    ):
        final = Ceiling.REJECT
        events.append("critic_agreement_required")
        clamped = True
    amount = basis = None
    if final in (Ceiling.APPROVE, Ceiling.HUMAN) and state.invoice is not None and state.totals is not None:
        paid = state.dedupe.paid_to_date if state.dedupe and state.dedupe.kind == DedupeKind.REVISION_OF_PAID else None
        amount, basis = payable_amount(state.invoice, state.totals, revision_paid_to_date=paid)
    decision = Decision(
        proposed=vp_dec if vp is not None else None,
        critic_verdict=cr.verdict if cr else None,
        critic_proposed=cr.proposed_decision if cr else None,
        final=final, by=by, reasoning=reasoning, clamped=clamped,
        payment_amount=amount, payment_basis=basis,
    )
    return decision, events


def clamp_decision(state: InvoiceState, deps: Deps) -> dict[str, Any]:
    decision, events = decide(state, deps.settings)
    for kind in events:
        deps.log.event(
            "ceiling_clamped", trace_id=state.trace_id, node="clamp_decision", invoice=state.file_name,
            data={"kind": kind},
        )
    deps.repo.audit(
        trace_id=state.trace_id, actor="system", event="decision_final",
        payload={"final": decision.final.value, "by": decision.by, "clamped": decision.clamped,
                 "events": events,
                 "payment_amount": str(decision.payment_amount) if decision.payment_amount is not None else None,
                 "payment_basis": decision.payment_basis},
    )
    return {"decision": decision}
