"""Terminal nodes: record a rejection or duplicate, and finish."""

from typing import Any

from deps import Deps
from models import InvoiceState, InvoiceUpdate, Status


def record_rejection(state: InvoiceState, deps: Deps) -> dict[str, Any]:
    deps.repo.update_invoice(state.invoice_id, InvoiceUpdate(status=Status.REJECTED))
    reasoning = None
    if state.human is not None and state.human.outcome in ("rejected", "rejected_stock"):
        reasoning = state.human.outcome_note
    elif state.decision is not None:
        reasoning = state.decision.reasoning
    deps.repo.audit(
        trace_id=state.trace_id,
        actor="human" if state.human is not None and state.human.outcome in ("rejected", "rejected_stock") else "system",
        event="rejected",
        payload={
            "by": state.decision.by if state.decision else None,
            "reasoning": reasoning,
            "flags": [f.code.value for f in state.flags],
        },
    )
    return {"status": Status.REJECTED}


def record_duplicate(state: InvoiceState, deps: Deps) -> dict[str, Any]:
    original = state.dedupe.original_file_name if state.dedupe else None
    deps.repo.update_invoice(state.invoice_id, InvoiceUpdate(status=Status.DUPLICATE))
    deps.repo.audit(
        trace_id=state.trace_id, actor="system", event="duplicate", payload={"duplicate_of": original}
    )
    return {"status": Status.DUPLICATE, "duplicate_of": original}


def finish(state: InvoiceState, deps: Deps) -> dict[str, Any]:
    return {}
