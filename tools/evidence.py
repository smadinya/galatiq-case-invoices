"""Tools that expose this invoice's own evidence to the Critic."""

import core.invoice_math as invoice_math
from deps import Deps
from models import InvoiceState
from tools.registry import tool


@tool(context=("state",))
def get_source_evidence(state: InvoiceState, field: str) -> dict:
    """Return the source lines the document supports for one extracted field."""
    spans = state.evidence_spans
    if field in spans:
        return {"field": field, "lines": list(spans[field])}
    return {"error": f"no evidence recorded for field {field!r}", "fields": sorted(spans)}


@tool(context=("state",))
def recompute_totals(state: InvoiceState) -> dict:
    """Recompute the invoice's line amounts, subtotal, tax and total from its line items."""
    if state.invoice is None:
        return {"error": "no invoice has been extracted"}
    return invoice_math.recompute_totals(state.invoice).model_dump(mode="json")


@tool()
def get_pipeline_trace(deps: Deps, state: InvoiceState) -> dict:
    """Return what the pipeline did to this invoice: audit events, corrections, extraction attempts, grounding errors, normalizations and flags."""
    return {
        "audit": [
            {"ts": e.ts.isoformat(), "actor": e.actor, "event": e.event, "payload": e.payload}
            for e in deps.repo.audit_for_trace(state.trace_id)
        ],
        "corrections": [c.model_dump(mode="json") for c in state.corrections],
        "extraction_attempts": state.extraction_attempts,
        "grounding_errors": list(state.grounding_errors),
        "normalized_changes": [c.model_dump(mode="json") for c in state.normalized_changes],
        "flags": [f.model_dump(mode="json") for f in state.flags],
    }
