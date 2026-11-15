"""Run metrics and the savings estimate (DESIGN 15). Pure: no I/O, no Streamlit, no `ui` import.

Measured figures come from `InvoiceResult.usage`. The savings estimate multiplies them by the
assumptions in `config/settings.toml` (`[assumptions]`), and says so. The model-cost term is a
seam for tier 3: `cost` returns None until prices exist, and `net_usd` stays None with it.
"""

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Callable, Sequence

from config.settings import Settings
from models import InvoiceRecord, InvoiceResult, Status, Usage

_CENT = Decimal("0.01")
_NOT_DISTINCT = frozenset({Status.DUPLICATE, Status.SUPERSEDED, Status.PROCESSING})
_DECIDED = frozenset({Status.PAID, Status.REJECTED, Status.CREDIT_DUE})


@dataclass(frozen=True)
class RunMetrics:
    documents: int
    distinct: int
    decided_without_person: int
    counts: dict[str, int]
    automation_rate: Decimal | None
    processed: int  # not short-circuited and not ERROR; duplicates count (they used the pipeline)
    reused: int  # short-circuited: the stored result was returned
    errors: int
    avg_processing_s: Decimal | None
    llm_calls: int
    tokens_in: int
    tokens_out: int
    llm_latency_ms: int
    llm_calls_per_doc: Decimal | None
    tokens_per_doc: Decimal | None
    avg_llm_latency_s: Decimal | None
    llm_cost_per_doc_usd: Decimal | None  # tier-3 seam: None until prices exist


def no_cost(usage: Usage) -> Decimal | None:
    """Tier 2 has no model prices."""
    return None


def _mean(total: Decimal | int, n: int) -> Decimal | None:
    return Decimal(total) / n if n else None


def compute_metrics(
    results: Sequence[InvoiceResult], *, cost: Callable[[Usage], Decimal | None] = no_cost
) -> RunMetrics:
    counts: dict[str, int] = {}
    for r in results:
        counts[r.status.value] = counts.get(r.status.value, 0) + 1
    distinct = [r for r in results if r.status not in _NOT_DISTINCT]
    # A person-resolved invoice still needed a person.
    decided = sum(1 for r in distinct if r.status in _DECIDED and r.human is None)
    processed = [r for r in results if not r.short_circuited and r.status != Status.ERROR]
    n = len(processed)
    node_ms = sum(sum(r.usage.node_ms.values()) for r in processed)
    calls = sum(r.usage.llm_calls for r in processed)
    tok_in = sum(r.usage.tokens_in for r in processed)
    tok_out = sum(r.usage.tokens_out for r in processed)
    latency = sum(r.usage.llm_latency_ms for r in processed)
    costs = [cost(r.usage) for r in processed]
    priced = n > 0 and all(c is not None for c in costs)
    return RunMetrics(
        documents=len(results),
        distinct=len(distinct),
        decided_without_person=decided,
        counts=counts,
        automation_rate=_mean(decided, len(distinct)),
        processed=n,
        reused=sum(1 for r in results if r.short_circuited),
        errors=counts.get(Status.ERROR.value, 0),
        avg_processing_s=_mean(Decimal(node_ms) / 1000, n),
        llm_calls=calls,
        tokens_in=tok_in,
        tokens_out=tok_out,
        llm_latency_ms=latency,
        llm_calls_per_doc=_mean(calls, n),
        tokens_per_doc=_mean(tok_in + tok_out, n),
        avg_llm_latency_s=_mean(Decimal(latency) / 1000, n),
        llm_cost_per_doc_usd=(sum((c for c in costs if c is not None), Decimal(0)) / n) if priced else None,
    )


@dataclass(frozen=True)
class SavingsEstimate:
    volume: int
    automation_rate: Decimal
    baseline_cost_usd: Decimal
    llm_cost_per_invoice_usd: Decimal | None
    gross_usd: Decimal
    llm_cost_usd: Decimal | None
    net_usd: Decimal | None
    formula: str
    assumptions: tuple[tuple[str, str], ...]  # (input, source), only the assumed inputs


FORMULA = "volume × automation rate × baseline cost per invoice − volume × model cost per invoice"


def savings_estimate(m: RunMetrics, settings: Settings) -> SavingsEstimate | None:
    """None when no distinct document was decided or routed (no automation rate to extrapolate)."""
    if m.automation_rate is None:
        return None
    volume = settings.annual_invoice_volume
    baseline = settings.baseline_cost_per_invoice_usd
    gross = (Decimal(volume) * m.automation_rate * baseline).quantize(_CENT, rounding=ROUND_HALF_UP)
    per_invoice = m.llm_cost_per_doc_usd
    llm_cost = (Decimal(volume) * per_invoice).quantize(_CENT, rounding=ROUND_HALF_UP) if per_invoice is not None else None
    net = gross - llm_cost if llm_cost is not None else None
    annual = (Decimal(volume) * baseline).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    return SavingsEstimate(
        volume=volume,
        automation_rate=m.automation_rate,
        baseline_cost_usd=baseline,
        llm_cost_per_invoice_usd=per_invoice,
        gross_usd=gross,
        llm_cost_usd=llm_cost,
        net_usd=net,
        formula=FORMULA,
        assumptions=(
            ("Annual invoice volume", "Assumption (config/settings.toml)"),
            ("Baseline cost per invoice", f"Assumption: ${annual:,} a year ÷ {volume:,} invoices"),
        ),
    )


def latest_per_file(records: Sequence[InvoiceRecord]) -> list[InvoiceRecord]:
    """The newest row per `file_hash` (a retry replaces an ERROR row), PROCESSING dropped, in file-name order."""
    newest: dict[str, InvoiceRecord] = {}
    for rec in records:
        current = newest.get(rec.file_hash)
        if current is None or (rec.updated_at, rec.id) > (current.updated_at, current.id):
            newest[rec.file_hash] = rec
    kept = [r for r in newest.values() if r.status != Status.PROCESSING]
    return sorted(kept, key=lambda r: (r.file_name, r.id))
