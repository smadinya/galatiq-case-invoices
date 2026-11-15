"""Shared contracts for the Acme invoice pipeline. Package A owns this file.

Every other package imports these types unchanged; contract changes go through the
orchestrator. Money is always Decimal, never float.
"""

import datetime as dt
import enum
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

# =========================================================== enums and literals


class Severity(str, enum.Enum):
    ERROR = "error"
    HUMAN = "human"
    ESCALATION = "escalation"
    INFO = "info"


class FlagCode(str, enum.Enum):
    # error
    NON_POSITIVE_QUANTITY = "non_positive_quantity"
    MISSING_VENDOR = "missing_vendor"
    INVALID_DUE_DATE = "invalid_due_date"
    ZERO_STOCK_ITEM = "zero_stock_item"
    UNKNOWN_ITEM = "unknown_item"
    INSUFFICIENT_STOCK = "insufficient_stock"
    MISSING_INVOICE_NUMBER = "missing_invoice_number"  # DESIGN §18 default
    NO_LINE_ITEMS = "no_line_items"  # DESIGN §18 default
    HIGH_RISK_SCORE = "high_risk_score"  # tier 3: never raised in tier 1
    PROMPT_INJECTION = "prompt_injection"  # tier 3: never raised in tier 1
    # escalation
    MATH_MISMATCH = "math_mismatch"
    UNKNOWN_VENDOR = "unknown_vendor"
    PRICE_ABOVE_CATALOG = "price_above_catalog"
    # human
    FOREIGN_CURRENCY = "foreign_currency"
    REVISION_OF_PAID_INVOICE = "revision_of_paid_invoice"
    CONFLICTING_INVOICE_NUMBER = "conflicting_invoice_number"
    OVER_BUDGET = "over_budget"
    VENDOR_RENAMED = "vendor_renamed"
    PAYMENT_FAILED = "payment_failed"
    UNREADABLE_DOCUMENT = "unreadable_document"
    # info
    OVER_THRESHOLD = "over_threshold"
    NEAR_THRESHOLD = "near_threshold"
    PAST_DUE = "past_due"
    PRICE_BELOW_CATALOG = "price_below_catalog"
    VALUE_NORMALIZED = "value_normalized"
    TERMS_MISMATCH = "terms_mismatch"


FLAG_SEVERITY: dict[FlagCode, Severity] = {
    FlagCode.NON_POSITIVE_QUANTITY: Severity.ERROR,
    FlagCode.MISSING_VENDOR: Severity.ERROR,
    FlagCode.INVALID_DUE_DATE: Severity.ERROR,
    FlagCode.ZERO_STOCK_ITEM: Severity.ERROR,
    FlagCode.UNKNOWN_ITEM: Severity.ERROR,
    FlagCode.INSUFFICIENT_STOCK: Severity.ERROR,
    FlagCode.MISSING_INVOICE_NUMBER: Severity.ERROR,
    FlagCode.NO_LINE_ITEMS: Severity.ERROR,
    FlagCode.HIGH_RISK_SCORE: Severity.ERROR,
    FlagCode.PROMPT_INJECTION: Severity.ERROR,
    FlagCode.MATH_MISMATCH: Severity.ERROR,
    FlagCode.UNKNOWN_VENDOR: Severity.ESCALATION,
    FlagCode.PRICE_ABOVE_CATALOG: Severity.ESCALATION,
    FlagCode.FOREIGN_CURRENCY: Severity.HUMAN,
    FlagCode.REVISION_OF_PAID_INVOICE: Severity.HUMAN,
    FlagCode.CONFLICTING_INVOICE_NUMBER: Severity.HUMAN,
    FlagCode.OVER_BUDGET: Severity.HUMAN,
    FlagCode.VENDOR_RENAMED: Severity.HUMAN,
    FlagCode.PAYMENT_FAILED: Severity.HUMAN,
    FlagCode.UNREADABLE_DOCUMENT: Severity.HUMAN,
    FlagCode.OVER_THRESHOLD: Severity.INFO,
    FlagCode.NEAR_THRESHOLD: Severity.INFO,
    FlagCode.PAST_DUE: Severity.INFO,
    FlagCode.PRICE_BELOW_CATALOG: Severity.INFO,
    FlagCode.VALUE_NORMALIZED: Severity.INFO,
    FlagCode.TERMS_MISMATCH: Severity.INFO,
}

TIER3_FLAGS: frozenset[FlagCode] = frozenset({FlagCode.HIGH_RISK_SCORE, FlagCode.PROMPT_INJECTION})

# One flag per offending item for these codes; every other code appears at most
# once per invoice, with the offending lines listed in its evidence.
PER_ITEM_FLAGS: frozenset[FlagCode] = frozenset(
    {FlagCode.UNKNOWN_ITEM, FlagCode.ZERO_STOCK_ITEM, FlagCode.INSUFFICIENT_STOCK}
)

# When several human flags are present, the first one in this order becomes
# HumanReview.reason (the queue's headline); all of them go in HumanReview.reasons.
HUMAN_REASON_PRIORITY: tuple[FlagCode, ...] = (
    FlagCode.UNREADABLE_DOCUMENT,
    FlagCode.PAYMENT_FAILED,
    FlagCode.REVISION_OF_PAID_INVOICE,
    FlagCode.CONFLICTING_INVOICE_NUMBER,
    FlagCode.VENDOR_RENAMED,
    FlagCode.FOREIGN_CURRENCY,
    FlagCode.OVER_BUDGET,
)


class Ceiling(str, enum.Enum):
    """Outcome scale for ceilings and every decision. Ordered REJECT < HUMAN < APPROVE.

    Comparison operators use that order (NOT alphabetical), so min()/max() work.
    """

    REJECT = "REJECT"
    HUMAN = "HUMAN"
    APPROVE = "APPROVE"

    @property
    def rank(self) -> int:
        return _CEILING_RANK[self.value]

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, Ceiling):
            return NotImplemented
        return self.rank < other.rank

    def __le__(self, other: object) -> bool:
        if not isinstance(other, Ceiling):
            return NotImplemented
        return self.rank <= other.rank

    def __gt__(self, other: object) -> bool:
        if not isinstance(other, Ceiling):
            return NotImplemented
        return self.rank > other.rank

    def __ge__(self, other: object) -> bool:
        if not isinstance(other, Ceiling):
            return NotImplemented
        return self.rank >= other.rank


_CEILING_RANK: dict[str, int] = {"REJECT": 0, "HUMAN": 1, "APPROVE": 2}


def stricter(a: Ceiling, b: Ceiling) -> Ceiling:
    """The stricter of two outcomes (used for deadlock and the ceiling clamp)."""
    return min(a, b)


def ceiling_for(flags: "list[Flag]") -> Ceiling:
    """error -> REJECT; else human -> HUMAN; else APPROVE (DESIGN §3.2)."""
    severities = {f.severity for f in flags}
    if Severity.ERROR in severities:
        return Ceiling.REJECT
    if Severity.HUMAN in severities:
        return Ceiling.HUMAN
    return Ceiling.APPROVE


class Status(str, enum.Enum):
    PROCESSING = "PROCESSING"  # row exists, graph running (never a final result)
    PAID = "PAID"
    REJECTED = "REJECTED"
    PENDING_HUMAN = "PENDING_HUMAN"
    DUPLICATE = "DUPLICATE"
    CREDIT_DUE = "CREDIT_DUE"
    SUPERSEDED = "SUPERSEDED"  # an unpaid original replaced by its revision (DESIGN §7)
    ERROR = "ERROR"


class Verdict(str, enum.Enum):
    AGREE = "AGREE"
    OBJECT = "OBJECT"


class DedupeKind(str, enum.Enum):
    NEW = "new"
    DUPLICATE = "duplicate"  # same fingerprint as an earlier invoice
    CONFLICT = "conflict"  # same vendor+number, different content, no revision marker
    REVISION_OF_PAID = "revision_of_paid"
    REVISION_REPLACES = "revision_replaces"  # marked revision, original not paid


DocFormat = Literal["json", "csv", "xml", "txt", "pdf"]
AddressCheck = Literal["verified", "mismatch", "not_on_invoice", "no_record"]
VendorMatchKind = Literal["exact", "alias", "renamed", "unknown", "missing"]
AliasKind = Literal["previous_name", "approved_rename"]
Actor = Literal["system", "vp", "critic", "human"]
Stage = Literal["extraction", "vp", "vp_memo", "critic"]
QueueStatus = Literal["PENDING", "RESOLVED", "SUPERSEDED"]
PaymentBasis = Literal["stated_total", "revision_difference"]

# ======================================================= provider-layer types


class ToolSpec(BaseModel):
    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema object; additionalProperties false, all required


class ToolCall(BaseModel):
    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class Message(BaseModel):
    """One chat message. The system prompt is passed separately to Provider.complete."""

    role: Literal["user", "assistant", "tool"]
    content: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)  # assistant only
    tool_call_id: str | None = None  # tool only


class CallContext(BaseModel):
    """Identifies one model call. Replay key = (file_hash, stage, prompt_version, call_index)."""

    trace_id: str
    file_hash: str
    file_name: str  # basename, e.g. "invoice_1012.pdf"; ScriptedFake keys on it
    stage: Stage
    prompt_version: str
    call_index: int  # 0-based per (invoice, stage); taken from InvoiceState.llm_calls


class CallUsage(BaseModel):
    tokens_in: int = 0
    tokens_out: int = 0
    latency_ms: int = 0


class Completion(BaseModel):
    """Either tool_calls (non-empty) or parsed (an instance of the requested schema)."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    content: str | None = None  # raw text; the JSON document when a schema was requested
    parsed: Any = None  # instance of `schema`, or None when the model asked for tools
    tool_calls: list[ToolCall] = Field(default_factory=list)
    usage: CallUsage = Field(default_factory=CallUsage)
    model: str | None = None
    provider: str = ""


class Provider(Protocol):
    name: str

    def complete(
        self,
        *,
        ctx: CallContext,
        system: str,
        messages: list[Message],
        schema: type[BaseModel] | None = None,
        tools: list[ToolSpec] | None = None,
    ) -> Completion: ...


@dataclass(frozen=True)
class BoundTool:
    """A tool ready for the loop: spec for the model, fn(**arguments) -> JSON-safe dict."""

    spec: ToolSpec
    fn: Callable[..., dict[str, Any]]


# ============================================================ ingestion types


class Correction(BaseModel):
    field: str  # e.g. "invoice_number", "date", "line_items[1].amount"
    raw: str
    interpreted: str


class ExtractedVendor(BaseModel):
    name: str | None = Field(
        None, description="Vendor name exactly as printed, without any 'formerly ...' clause."
    )
    address: str | None = Field(
        None, description="The vendor's own address if printed; never the bill-to/ship-to address."
    )
    former_name: str | None = Field(
        None,
        description="If the document says formerly / previously / f/k/a NAME, that NAME exactly as printed.",
    )


class ExtractedLine(BaseModel):
    item: str | None = Field(
        None,
        description="Item name exactly as printed, minus any parenthetical remark, which goes in note.",
    )
    quantity: str | None = Field(None, description="Quantity exactly as printed, digits only (no 'x', 'qty').")
    unit_price: str | None = Field(None, description="Unit price exactly as printed, e.g. '$250', '$3,500.O0'.")
    amount: str | None = Field(None, description="Line total exactly as printed, if any.")
    note: str | None = Field(None, description="Remark attached to the line, e.g. 'rush order'.")


class ExtractedInvoice(BaseModel):
    """Common shape for every parser and the LLM. Values are raw strings, copied verbatim."""

    invoice_number: str | None = Field(
        None, description="Exactly as printed without the label, e.g. 'INV 1012', '1002'. Do not reformat."
    )
    revision: str | None = Field(None, description="Revision marker if printed, e.g. 'R1'.")
    vendor: ExtractedVendor = Field(default_factory=ExtractedVendor)
    date: str | None = Field(None, description="Invoice date exactly as printed. Do not fix or reformat.")
    due_date: str | None = Field(None, description="Due date exactly as printed, even if not a date.")
    currency: str | None = Field(None, description="Currency code only if the document states one.")
    line_items: list[ExtractedLine] = Field(default_factory=list)
    subtotal: str | None = None
    tax_rate: str | None = Field(None, description="As printed, e.g. '5%' or '0.08'.")
    tax_amount: str | None = None
    shipping: str | None = None
    total: str | None = Field(None, description="Total amount due exactly as printed.")
    payment_terms: str | None = None
    notes: str | None = None
    corrections: list[Correction] = Field(
        default_factory=list,
        description="Every value you believe is misprinted: the raw text and what you think it means.",
    )


class GroundingResult(BaseModel):
    ok: bool
    errors: list[str] = Field(default_factory=list)
    spans: dict[str, list[str]] = Field(default_factory=dict)  # field path -> source lines


class LineItem(BaseModel):
    item: str  # as printed, trimmed; trailing "(...)" moved to note
    item_key: str  # core.text.clean_item_key(item); inventory matching key
    quantity: int
    unit_price: Decimal
    amount: Decimal | None = None  # stated line amount, if any
    note: str | None = None


class Invoice(BaseModel):
    """Normalized invoice. Built only by tools.normalize.normalize()."""

    invoice_number: str | None  # "INV-NNNN"
    revision: str | None = None  # "R1"
    is_revision: bool = False  # revision set, or notes mention "revised"
    vendor_name: str | None  # as printed, rename clause removed
    vendor_former_name: str | None = None
    vendor_key: str | None  # core.text.vendor_key(vendor_name)
    vendor_address: str | None = None
    invoice_date: dt.date | None = None
    due_date: dt.date | None = None  # None when missing or unparseable
    due_date_raw: str | None = None
    currency: str = "USD"
    line_items: list[LineItem]
    subtotal: Decimal | None = None
    tax_rate: Decimal | None = None  # fraction: 0.08
    tax_amount: Decimal | None = None
    shipping: Decimal | None = None
    total: Decimal
    payment_terms: str | None = None
    terms_days: int | None = None  # "Net 30" -> 30, "Immediate" -> 0
    notes: str | None = None


class NormalizedChange(BaseModel):
    field: str
    raw: str
    normalized: str


class NormalizeResult(BaseModel):
    invoice: Invoice
    changes: list[NormalizedChange] = Field(default_factory=list)  # validation turns these into value_normalized


class Flag(BaseModel):
    code: FlagCode
    severity: Severity  # filled from FLAG_SEVERITY when omitted; must match it
    message: str
    evidence: dict[str, Any] = Field(default_factory=dict)  # JSON-safe; money as str

    @model_validator(mode="before")
    @classmethod
    def _fill_severity(cls, data: Any) -> Any:
        if isinstance(data, dict) and data.get("severity") is None and "code" in data:
            data = {**data, "severity": FLAG_SEVERITY[FlagCode(data["code"])]}
        return data

    @model_validator(mode="after")
    def _check_severity(self) -> "Flag":
        if self.severity != FLAG_SEVERITY[self.code]:
            raise ValueError(f"{self.code.value} must have severity {FLAG_SEVERITY[self.code].value}")
        return self


def make_flag(code: FlagCode, message: str, **evidence: Any) -> Flag:
    return Flag(code=code, severity=FLAG_SEVERITY[code], message=message, evidence=evidence)


# =========================================================== validation types


class LineCheck(BaseModel):
    index: int
    item: str
    quantity: int
    unit_price: Decimal
    stated_amount: Decimal | None
    recomputed_amount: Decimal
    ok: bool


class Totals(BaseModel):
    lines: list[LineCheck]
    lines_sum: Decimal
    subtotal_stated: Decimal | None
    subtotal_ok: bool | None  # None when no subtotal printed
    tax_used: Decimal
    tax_source: Literal["stated", "rate", "none"]
    tax_ok: bool | None  # None unless both rate and amount are printed
    shipping: Decimal
    total_stated: Decimal
    total_recomputed: Decimal
    total_ok: bool
    difference: Decimal  # total_stated - total_recomputed
    mismatches: list[str] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.mismatches


class DedupeResult(BaseModel):
    kind: DedupeKind
    original_invoice_id: int | None = None
    original_trace_id: str | None = None
    original_file_name: str | None = None
    original_status: Status | None = None
    paid_to_date: Decimal = Decimal("0")
    added_quantities: dict[str, int] = Field(default_factory=dict)  # item_key -> qty (revision_of_paid)


class VendorMatch(BaseModel):
    kind: VendorMatchKind
    vendor_id: int | None = None
    master_name: str | None = None
    matched_name: str | None = None  # the alias / former name that matched


class RiskScore(BaseModel):  # tier 3; tier 1 leaves InvoiceState.risk = None
    score: int = 0
    signals: list[str] = Field(default_factory=list)


class ValidationResult(BaseModel):
    flags: list[Flag]
    vendor: VendorMatch
    address_check: AddressCheck
    amount_usd: Decimal | None  # None for non-USD until a person sets the rate
    totals: Totals
    payable_amount: Decimal  # invoice currency; what an approval would pay


# ========================================================= approval types (LLM)
# These are structured-output schemas: no defaults, every field required.


class FlagResponse(BaseModel):
    code: FlagCode
    response: str


class ObjectionAnswer(BaseModel):
    objection: str
    answer: str


class VPOutput(BaseModel):
    decision: Ceiling
    reasoning: str
    flags_addressed: list[FlagResponse]  # must cover every escalation flag
    objections_answered: list[ObjectionAnswer]  # must answer every open Critic objection


class VPMemoOutput(BaseModel):
    memo: str


class Objection(BaseModel):
    about: str
    evidence: str
    requested_change: str


class CriticOutput(BaseModel):
    verdict: Verdict
    proposed_decision: Ceiling
    objections: list[Objection]


class ToolTrace(BaseModel):
    call_id: str
    name: str
    arguments: dict[str, Any]
    result: dict[str, Any]


class AgentTurn(BaseModel):
    actor: Literal["vp", "critic"]
    kind: Literal["memo", "decision", "review"]
    round: int  # critic reviews completed before this turn
    tool_calls: list[ToolTrace] = Field(default_factory=list)
    output: dict[str, Any]  # VPOutput / VPMemoOutput / CriticOutput .model_dump(mode="json")


class Decision(BaseModel):
    proposed: Ceiling | None = None  # VP's last decision; None for ceiling REJECT
    critic_verdict: Verdict | None = None
    critic_proposed: Ceiling | None = None
    final: Ceiling
    by: Literal["rules", "vp", "vp+critic", "deadlock", "human"]
    reasoning: str
    clamped: bool = False  # proposal was above the ceiling, HUMAN without a human reason, or failed the $10K AGREE rule
    payment_amount: Decimal | None = None  # invoice currency; set when final is APPROVE or HUMAN
    payment_basis: PaymentBasis | None = None


# ================================================================ human + pay


class HumanResolution(BaseModel):
    """Resume payload for await_human. Sent as model_dump(mode="json")."""

    action: Literal["approve", "reject"]
    note: str = Field(min_length=1)
    fx_rate: Decimal | None = None  # invoice currency -> USD
    confirm_overspend: bool = False
    resolved_at: dt.datetime


class HumanReview(BaseModel):
    reason: FlagCode  # first of HUMAN_REASON_PRIORITY present
    reasons: list[FlagCode]
    recommendation: Ceiling | None = None  # VP's decision; None for unreadable_document
    recommendation_reasoning: str | None = None
    payment_amount: Decimal | None = None  # what approval would pay, invoice currency
    currency: str | None = None
    resolution: HumanResolution | None = None
    outcome: Literal["approved", "rejected", "rejected_stock", "requeued"] | None = None
    outcome_note: str | None = None


class Receipt(BaseModel):
    idempotency_key: str  # f"{vendor_id or vendor_key}:{invoice_number}:{revision or 'R0'}"
    vendor: str
    amount: Decimal  # invoice currency
    currency: str
    fx_rate: Decimal | None = None
    amount_usd: Decimal
    attempts: int
    paid_at: dt.datetime
    bank_response: dict[str, Any]  # mock_payment's return value


class Usage(BaseModel):
    llm_calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    llm_latency_ms: int = 0
    node_ms: dict[str, int] = Field(default_factory=dict)

    def plus_call(self, u: CallUsage) -> "Usage":
        return self.model_copy(
            update={
                "llm_calls": self.llm_calls + 1,
                "tokens_in": self.tokens_in + u.tokens_in,
                "tokens_out": self.tokens_out + u.tokens_out,
                "llm_latency_ms": self.llm_latency_ms + u.latency_ms,
            }
        )

    def plus_node(self, node: str, ms: int) -> "Usage":
        return self.model_copy(update={"node_ms": {**self.node_ms, node: self.node_ms.get(node, 0) + ms}})


# ===================================================================== state


class InvoiceState(BaseModel):
    """The one object passed through the graph and checkpointed. Nodes return
    dicts of FULL replacement values (no reducers: send the whole new list)."""

    # identity (set by service before the graph starts)
    trace_id: str
    invoice_id: int  # invoices.id in acme.db
    file_path: str
    file_name: str
    file_hash: str  # sha256 hex of the file bytes
    format: DocFormat | None = None

    # ingestion
    raw_text: str | None = None  # TXT/PDF text only. NEVER sent to the VP.
    parse_error: str | None = None
    extracted: ExtractedInvoice | None = None
    corrections: list[Correction] = Field(default_factory=list)
    extraction_attempts: int = 0
    grounding_errors: list[str] = Field(default_factory=list)
    evidence_spans: dict[str, list[str]] = Field(default_factory=dict)
    invoice: Invoice | None = None
    normalized_changes: list[NormalizedChange] = Field(default_factory=list)

    # validation
    fingerprint: str | None = None
    dedupe: DedupeResult | None = None
    duplicate_of: str | None = None  # original's file name
    vendor: VendorMatch | None = None
    flags: list[Flag] = Field(default_factory=list)
    totals: Totals | None = None
    payable_amount: Decimal | None = None
    risk: RiskScore | None = None
    amount_usd: Decimal | None = None
    address_check: AddressCheck | None = None
    ceiling: Ceiling | None = None
    needs_critic: bool = False

    # approval
    transcript: list[AgentTurn] = Field(default_factory=list)
    round: int = 0  # Critic reviews completed
    vp_output: VPOutput | None = None
    critic_output: CriticOutput | None = None
    llm_calls: dict[str, int] = Field(default_factory=dict)  # stage -> calls made (call_index source)
    decision: Decision | None = None

    # human + payment
    human: HumanReview | None = None
    payment: Receipt | None = None
    payment_error: str | None = None

    # outcome
    status: Status = Status.PROCESSING
    usage: Usage = Field(default_factory=Usage)


# ============================================================ storage records


class InventoryItem(BaseModel):
    item: str
    item_key: str
    stock: int
    unit_price: Decimal
    category: str | None = None


class VendorRecord(BaseModel):
    id: int
    name: str
    normalized_name: str
    status: Literal["approved", "blocked"]
    address: str | None = None
    payment_terms: str | None = None


class VendorAlias(BaseModel):
    vendor_id: int
    alias: str
    normalized_alias: str
    kind: AliasKind


class BudgetStatus(BaseModel):
    month: str  # "2026-01"
    limit: Decimal
    spent: Decimal
    remaining: Decimal


class StockLedgerEntry(BaseModel):
    id: int
    item: str
    delta: int
    invoice_id: int | None
    reason: str
    ts: dt.datetime


class AuditEntry(BaseModel):
    id: int
    trace_id: str
    ts: dt.datetime
    actor: Actor
    event: str
    payload: dict[str, Any]


class InvoiceUpdate(BaseModel):
    """Partial update for an invoices row; only fields that are set get written."""

    fingerprint: str | None = None
    vendor_id: int | None = None
    vendor_key: str | None = None
    vendor_name: str | None = None
    invoice_number: str | None = None
    revision: str | None = None
    status: Status | None = None
    ceiling: Ceiling | None = None
    amount: Decimal | None = None
    currency: str | None = None
    amount_usd: Decimal | None = None
    invoice_date: dt.date | None = None
    result: "InvoiceResult | None" = None


class InvoiceRecord(BaseModel):
    id: int
    trace_id: str
    file_path: str
    file_name: str
    file_hash: str
    fingerprint: str | None = None
    vendor_id: int | None = None
    vendor_key: str | None = None
    vendor_name: str | None = None
    invoice_number: str | None = None
    revision: str | None = None
    status: Status
    ceiling: Ceiling | None = None
    amount: Decimal | None = None
    currency: str | None = None
    amount_usd: Decimal | None = None
    invoice_date: dt.date | None = None
    error: str | None = None
    result: "InvoiceResult | None" = None
    created_at: dt.datetime
    updated_at: dt.datetime


class PaymentWrite(BaseModel):
    idempotency_key: str
    invoice_id: int
    trace_id: str
    vendor: str
    amount: Decimal
    currency: str
    fx_rate: Decimal | None = None
    amount_usd: Decimal
    attempts: int
    month: str  # budget month, "2026-01"
    stock_deltas: dict[str, int]  # inventory item name -> negative delta
    final_status: Literal["PAID", "CREDIT_DUE"] = "PAID"
    bank_response: dict[str, Any]


class PaymentRecord(BaseModel):
    idempotency_key: str
    invoice_id: int
    amount: Decimal
    currency: str
    fx_rate: Decimal | None = None
    amount_usd: Decimal | None = None
    status: Literal["SUCCESS", "FAILED"]
    attempts: int
    receipt: Receipt | None = None
    error: str | None = None


class QueueWrite(BaseModel):
    invoice_id: int
    trace_id: str
    reason: FlagCode
    review: HumanReview


class QueueItem(BaseModel):
    invoice_id: int
    trace_id: str
    invoice_ref: str  # "INV-1004 R1", "INV-1012", or the file name when no number
    invoice_number: str | None
    revision: str | None
    file_name: str
    vendor: str | None
    reason: FlagCode
    review: HumanReview
    status: QueueStatus
    resolution: Literal["approve", "reject"] | None = None
    note: str | None = None
    created_at: dt.datetime
    resolved_at: dt.datetime | None = None


# ============================================================ service outputs


class InvoiceResult(BaseModel):
    """The --json object for one invoice, and invoices.result_json."""

    trace_id: str
    file: str  # file name
    invoice_number: str | None = None
    revision: str | None = None
    vendor: str | None = None
    status: Status
    ceiling: Ceiling | None = None
    flags: list[Flag] = Field(default_factory=list)
    address_check: AddressCheck | None = None
    amount: Decimal | None = None
    currency: str | None = None
    amount_usd: Decimal | None = None
    decision: Decision | None = None
    transcript: list[AgentTurn] = Field(default_factory=list)
    human: HumanReview | None = None
    payment: Receipt | None = None
    duplicate_of: str | None = None
    invoice: Invoice | None = None
    error: str | None = None
    short_circuited: bool = False  # exact same bytes seen before; stored result returned
    provider: str
    usage: Usage = Field(default_factory=Usage)


class BatchResult(BaseModel):
    results: list[InvoiceResult]
    counts: dict[str, int]  # Status value -> count
    distinct_documents: int  # results minus DUPLICATE
    decided_without_person: int  # PAID + REJECTED + CREDIT_DUE among distinct
    budget: BudgetStatus
    stock: dict[str, int]


class StockShortfall(BaseModel):
    item: str
    needed: int
    available: int


class ResolutionPreview(BaseModel):
    invoice_ref: str
    reason: FlagCode
    can_approve: bool  # False for unreadable_document
    needs_fx_rate: bool
    fx_rate: Decimal | None = None
    payment_amount: Decimal | None = None  # invoice currency
    currency: str | None = None
    payment_amount_usd: Decimal | None = None
    budget: BudgetStatus
    overspend_usd: Decimal = Decimal("0")  # max(0, spent + payment_usd - limit)
    needs_overspend_confirmation: bool = False
    stock_shortfalls: list[StockShortfall] = Field(default_factory=list)
    approve_outcome: Literal["pay", "credit_due", "reject_stock", "blocked"]


class Banner(BaseModel):
    provider: str
    models: dict[str, str] = Field(default_factory=dict)
    recording_date: str | None = None
    seed: str | None = None
    as_of: dt.date
    stock: dict[str, int]
    budget: BudgetStatus
    hint: str = "State carries over between runs. Use --reset to start fresh."


InvoiceUpdate.model_rebuild()
InvoiceRecord.model_rebuild()

# Classes that may appear inside a checkpointed InvoiceState. graph.py passes this
# to JsonPlusSerializer(allowed_msgpack_modules=...) so LangGraph deserializes
# them without "unregistered type" warnings (and keeps working in strict mode).
# tests/unit/test_models.py asserts it covers every model/enum reachable from InvoiceState.
CHECKPOINT_TYPES: tuple[type, ...] = (
    InvoiceState, Severity, FlagCode, Ceiling, Status, Verdict, DedupeKind,
    Correction, ExtractedVendor, ExtractedLine, ExtractedInvoice, LineItem, Invoice,
    NormalizedChange, Flag, LineCheck, Totals, DedupeResult, VendorMatch, RiskScore,
    FlagResponse, ObjectionAnswer, VPOutput, Objection, CriticOutput, ToolTrace,
    AgentTurn, Decision, HumanResolution, HumanReview, Receipt, Usage,
)
