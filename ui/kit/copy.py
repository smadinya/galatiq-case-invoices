"""Every fixed string the dashboard shows, taken verbatim from design spec section 6.

The voice is a careful colleague: plain verbs, sentence case, no exclamation marks, no
apologies, no emoji. Pages and components never carry copy of their own; they import it
from here.

Functions return plain text. When they interpolate untrusted text (vendor names, notes,
model output) the caller escapes the result with `fmt.md` (Markdown) or `fmt.esc` (HTML).

TEXT ids (use `t(id, **fields)` to format; fields are named in the template)
----------------------------------------------------------------------------
titles       title.process, title.queue, title.ledger, title.batch
buttons      button.process, button.batch, button.process_again, button.download_original,
             button.show_raw, button.reset_open, button.reset_cancel, button.reset_confirm,
             button.queue_next {ref}, button.approve_pay {amount}, button.approve_credit,
             button.approve, button.reject
labels       label.uploader, help.uploader, label.bank_response, label.run_details,
             label.ceiling_badge {label}, help.ceiling, label.final_decision,
             label.proposes {ceiling}, label.round {actor} {n}, label.memo, label.tools_raw,
             label.flags_answered, label.objections_answered, label.requested_change {text}, label.critic_proposes {ceiling},
             label.person_approved, label.person_rejected,
             label.tool, label.asked_about, label.answer
links        link.decide, link.open_case, link.review_waiting {n}, link.review_waiting_one,
             link.ledger, link.open_file {file}, link.process, link.batch
sections     section.checks, section.document, section.approval, section.payment,
             section.next_step, section.extracted, section.source, section.read_as
empty        empty.process, empty.queue, empty.queue_done {approved} {rejected} {total},
             empty.ledger {month} {limit}, empty.ledger_next, empty.batch
case         case.no_flags, case.no_lines, case.stored {date}, case.stored_undated,
             case.unreadable_panel {error}, case.no_source, case.no_vendor, case.read_as_none, source.aria
address      address.verified, address.mismatch, address.not_on_invoice, address.no_record
ceiling      ceiling.APPROVE, ceiling.HUMAN {reason}, ceiling.REJECT {labels}
path         path.fast, path.critic {n}, path.critic_one, path.rules, path.deadlock, path.person
notices      notice.clamp_ceiling {proposed} {ceiling}, notice.human_to_reject,
             notice.over_threshold, notice.deadlock {final}
final        final.rules, final.vp, final.vp_critic, final.deadlock, final.person
no review    approval.none_unreadable, approval.none_duplicate
next step    next.pending {reason}, next.rejected {why}, next.duplicate {file} {original},
             next.duplicate_plain {file}, next.credit {less} {paid} {credit},
             next.credit_plain {credit}, next.superseded
payment      pay.paid_to, pay.amount, pay.rate, pay.rate_value {rate} {currency}, pay.in_dollars,
             pay.attempts, pay.attempts_one, pay.attempts_retried {n} {failed}, pay.paid_at,
             pay.receipt, pay.stock_used, pay.spending_after {month}, pay.credit_line {credit}
error        error.system_title, error.system_body, error.system_trace {trace_id},
             error.no_model_title, error.no_model_sidebar, error.system
info         info.stored, info.rerun_batch {n}, info.replay_slowed
warn         warn.folder_missing {path}, warn.folder_empty {path}, warn.no_model_process,
             warn.stopped {file}
run details  run.trace, run.file, run.format, run.provider, run.calls, run.tokens_in,
             run.tokens_out, run.model_time, run.model_time_replay, run.steps, run.log_label, run.log {trace_id},
             run.cli, run.none
source       source.text, source.pdf, source.file
field labels field.invoice_number, field.revision, field.vendor.name, field.vendor.former_name,
             field.vendor.address, field.date, field.due_date, field.currency, field.payment_terms,
             field.subtotal, field.tax_amount, field.shipping, field.total
reasons      reason.revision_credit {credit}, reason.revision_pays {difference}

REASON_COPY templates use these named fields (the amounts are preformatted strings):
  foreign_currency            why {currency}; approve_does {amount} {currency} {usd}
                              ({amount} is the bare number, e.g. "4,125.00")
  revision_of_paid_invoice    why {original_ref} {paid_to_date} {added}; approve_does {difference}
  conflicting_invoice_number  why {vendor} {number}
  over_budget                 why {amount} {month} {after} {over} {limit}
  vendor_renamed              why {vendor} {former}; note_placeholder {former}; approve_does {vendor} {former}
  payment_failed              why {error}
  unreadable_document         why {error}
"""

from dataclasses import dataclass
from typing import Any, Literal, Mapping, Sequence

from models import (
    Ceiling,
    FlagCode,
    Flag,
    InvoiceResult,
    Severity,
    Status,
    ToolTrace,
    Verdict,
)
from ui.kit import fmt
from ui.kit.theme import CEILING, STATUS

StageKey = Literal["read", "check", "approve", "finish"]

STAGES: tuple[tuple[StageKey, str], ...] = (
    ("read", "Read"),
    ("check", "Check"),
    ("approve", "Approve"),
    ("finish", "Finish"),
)

NODE_STAGE: dict[str, StageKey] = {
    "load_document": "read",
    "parse_structured": "read",
    "extract_with_llm": "read",
    "ground_check": "read",
    "normalize": "read",
    "dedupe": "check",
    "validate": "check",
    "score_risk": "check",
    "compute_ceiling": "check",
    "vp_memo": "approve",
    "vp_decide": "approve",
    "critic_review": "approve",
    "clamp_decision": "approve",
    "prepare_human": "finish",
    "await_human": "finish",
    "pay": "finish",
    "record_rejection": "finish",
    "record_duplicate": "finish",
    "finish": "finish",
}

# Tier 3 shows the risk score; until then its node produces nothing worth a line.
HIDDEN_NODES: frozenset[str] = frozenset({"score_risk"})

NODE_LABEL: dict[str, str] = {
    "load_document": "Read the file",
    "parse_structured": "Parsed the structured file",
    "extract_with_llm": "Extracted fields with the model",
    "ground_check": "Checked every extracted value against the text",
    "normalize": "Cleaned up values",
    "dedupe": "Checked for duplicates",
    "validate": "Checked inventory, vendor, prices, totals and budget",
    "score_risk": "Scored risk",
    "compute_ceiling": "Worked out the ceiling",
    "vp_memo": "VP wrote the rejection memo",
    "vp_decide": "VP decided",
    "critic_review": "Critic reviewed",
    "clamp_decision": "Applied the policy limits",
    "prepare_human": "Queued for a person",
    "await_human": "Waiting for a person",
    "pay": "Paid",
    "record_rejection": "Recorded the rejection",
    "record_duplicate": "Recorded the duplicate",
    "finish": "Done",
}

_ACTIVE: dict[str, str] = {
    "load_document": "Reading the file…",
    "parse_structured": "Parsing the file…",
    "extract_with_llm": "Extracting fields with the model…",
    "ground_check": "Checking extracted values against the text…",
    "normalize": "Cleaning up values…",
    "dedupe": "Checking for duplicates…",
    "validate": "Checking inventory, vendor, prices, totals and budget…",
    "score_risk": "Scoring risk…",
    "compute_ceiling": "Working out the ceiling…",
    "vp_memo": "VP is writing the rejection memo…",
    "vp_decide": "VP is deciding…",
    "critic_review": "Critic is reviewing…",
    "clamp_decision": "Applying the policy limits…",
    "prepare_human": "Queuing for a person…",
    "await_human": "Waiting for a person…",
    "pay": "Paying…",
    "record_rejection": "Recording the rejection…",
    "record_duplicate": "Recording the duplicate…",
    "finish": "Finishing…",
}


def active_label(node: str) -> str:
    """Running-status wording for a node, for example `VP is deciding…`."""
    return _ACTIVE.get(node, "Working…")


# ------------------------------------------------------------------ small helpers


def _get(obj: Any, key: str, default: Any = None) -> Any:
    """Read `key` from a mapping or an object (node updates carry either)."""
    if obj is None:
        return default
    if isinstance(obj, Mapping):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _val(x: Any) -> Any:
    return getattr(x, "value", x)


def _ceiling(x: Any) -> Ceiling | None:
    try:
        return Ceiling(_val(x)) if x is not None else None
    except ValueError:
        return None


def _status(x: Any) -> Status | None:
    try:
        return Status(_val(x)) if x is not None else None
    except ValueError:
        return None


def _code(x: Any) -> FlagCode | None:
    try:
        return FlagCode(_val(x)) if x is not None else None
    except ValueError:
        return None


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def snippet(text: str | None, limit: int = 40) -> str:
    """Whitespace-collapsed text cut to `limit` characters with an ellipsis."""
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def ceiling_label(ceiling: Ceiling | None) -> str:
    return CEILING[ceiling].label if ceiling in CEILING else "-"


# ----------------------------------------------------------------- flags (6.4)


@dataclass(frozen=True)
class FlagCopy:
    label: str  # short pill label
    sentence: str  # plain-English explanation


FLAG_COPY: dict[FlagCode, FlagCopy] = {
    FlagCode.NON_POSITIVE_QUANTITY: FlagCopy(
        "Zero or negative quantity", "A line bills zero or fewer units, so the invoice can't be right."
    ),
    FlagCode.MISSING_VENDOR: FlagCopy("No vendor", "The invoice doesn't say who to pay."),
    FlagCode.INVALID_DUE_DATE: FlagCopy(
        "No valid due date", 'The due date is missing or isn\'t a date, for example "yesterday".'
    ),
    FlagCode.ZERO_STOCK_ITEM: FlagCopy(
        "Item has no stock", "An item on the invoice has zero stock, a common sign of a fake invoice."
    ),
    FlagCode.UNKNOWN_ITEM: FlagCopy(
        "Unknown item", "An item isn't in our inventory. We don't pay for things we don't stock."
    ),
    FlagCode.INSUFFICIENT_STOCK: FlagCopy(
        "Not enough stock", "The invoice bills more units than we have, counting every line for the item."
    ),
    FlagCode.MISSING_INVOICE_NUMBER: FlagCopy(
        "No invoice number", "Without a number we can't check for duplicates or pay it safely."
    ),
    FlagCode.NO_LINE_ITEMS: FlagCopy("No line items", "There's nothing on the invoice to check or pay for."),
    FlagCode.HIGH_RISK_SCORE: FlagCopy("High risk score", "Fraud signals add up to 60 or more."),
    FlagCode.PROMPT_INJECTION: FlagCopy(
        "Instructions in the text",
        'The document contains text that tries to instruct the system, such as "ignore previous instructions".',
    ),
    FlagCode.MATH_MISMATCH: FlagCopy(
        "Totals don't add up", "Lines, tax and total don't agree. Any mismatch is rejected, over or under."
    ),
    FlagCode.UNKNOWN_VENDOR: FlagCopy(
        "Unknown vendor", "The vendor isn't in the vendor master. The VP must explain the decision in writing."
    ),
    FlagCode.PRICE_ABOVE_CATALOG: FlagCopy(
        "Price above catalog", "A unit price is more than 10% over catalog. The VP must justify it in writing."
    ),
    FlagCode.FOREIGN_CURRENCY: FlagCopy(
        "Foreign currency", "Not in US dollars. A person sets the exchange rate."
    ),
    FlagCode.REVISION_OF_PAID_INVOICE: FlagCopy(
        "Revises a paid invoice",
        "A revised version of an invoice we already paid. Approving pays only the difference.",
    ),
    FlagCode.CONFLICTING_INVOICE_NUMBER: FlagCopy(
        "Same number, different content",
        "Another invoice from this vendor has the same number but different content, "
        "and neither is marked as a revision.",
    ),
    FlagCode.OVER_BUDGET: FlagCopy(
        "Over monthly budget",
        "Paying it would take spending past the monthly limit. Only a person can authorise that.",
    ),
    FlagCode.VENDOR_RENAMED: FlagCopy(
        "Vendor renamed",
        "The vendor says it used to trade under another name. Someone must confirm that with the vendor.",
    ),
    FlagCode.PAYMENT_FAILED: FlagCopy("Payment failed", "The bank call still failed after 4 attempts."),
    FlagCode.UNREADABLE_DOCUMENT: FlagCopy(
        "Unreadable document", "The file couldn't be read reliably, so there's nothing safe to pay from."
    ),
    FlagCode.OVER_THRESHOLD: FlagCopy(
        "Over $10,000", "Over the approval threshold, so the Critic reviews and must agree to any approval."
    ),
    FlagCode.NEAR_THRESHOLD: FlagCopy(
        "Just under $10,000",
        "In the $9,000 to $10,000 band where invoices get split. The Critic looks for the other half.",
    ),
    FlagCode.PAST_DUE: FlagCopy(
        "Past due", "The due date has passed. This sets priority; it's never a reason to hold payment."
    ),
    FlagCode.PRICE_BELOW_CATALOG: FlagCopy(
        "Price below catalog", "A unit price is below catalog, for example a volume discount."
    ),
    FlagCode.VALUE_NORMALIZED: FlagCopy(
        "Values cleaned up", "Printed values were cleaned up before checking, for example 2O26 read as 2026."
    ),
    FlagCode.TERMS_MISMATCH: FlagCopy(
        "Due date off terms",
        "The due date is more than 3 days from the invoice date plus the payment terms.",
    ),
}


def flag_label(flag: Flag | FlagCode) -> str:
    code = flag.code if isinstance(flag, Flag) else flag
    return FLAG_COPY[code].label


def labels_for(flags: Sequence[Flag], severity: Severity, *, lower: bool = False) -> list[str]:
    seen: dict[str, None] = {}
    for f in flags:
        if f.severity == severity:
            label = flag_label(f)
            seen[label.lower() if lower else label] = None
    return list(seen)


# ------------------------------------------------------------ review reasons (6.3)


@dataclass(frozen=True)
class ReasonCopy:
    label: str  # queue label
    why: str  # str.format template, fields in the module docstring
    note_placeholder: str
    approve_does: str


REASON_COPY: dict[FlagCode, ReasonCopy] = {
    FlagCode.FOREIGN_CURRENCY: ReasonCopy(
        "Foreign currency",
        "The invoice is in {currency}. Exchange rates move, so a person sets the rate.",
        "Rate source and time, for example: Treasury rate 1.0800 at 09:00.",
        "Pays {amount} in {currency}, records the rate, and counts {usd} against the budget.",
    ),
    FlagCode.REVISION_OF_PAID_INVOICE: ReasonCopy(
        "Revision of a paid invoice",
        "{original_ref} was paid {paid_to_date}. This revision adds {added}. Approving pays only the difference.",
        "What confirms the change, for example: PO amendment 2291 adds 5 GadgetX.",
        "Pays the {difference} difference",
    ),
    FlagCode.CONFLICTING_INVOICE_NUMBER: ReasonCopy(
        "Same number, different content",
        "{vendor} sent another {number} with different content and no revision mark.",
        "Which one the vendor confirmed is genuine.",
        "Pays this invoice in full.",
    ),
    FlagCode.OVER_BUDGET: ReasonCopy(
        "Over monthly budget",
        "Paying {amount} takes {month} spending to {after}, {over} over the {limit} limit. "
        "Only a person can authorise that.",
        "Who authorised the overspend and why.",
        "Pays and records the overspend. Needs the confirmation box.",
    ),
    FlagCode.VENDOR_RENAMED: ReasonCopy(
        "Vendor renamed",
        "The invoice says {vendor} was formerly {former}. Confirm the change with {former} using contact "
        "details already on file, not ones printed on this invoice.",
        "Called {former} on the number in our records; rename confirmed by …",
        "Pays, and records {vendor} as a new name for {former}, so its next invoice isn't flagged.",
    ),
    FlagCode.PAYMENT_FAILED: ReasonCopy(
        "Payment failed",
        "The bank call failed 4 times: {error}.",
        "What you checked, for example: bank connection restored.",
        "Tries the payment again.",
    ),
    FlagCode.UNREADABLE_DOCUMENT: ReasonCopy(
        "Unreadable document",
        "The file couldn't be read reliably: {error}. There's nothing safe to pay from.",
        "Why it's being rejected, for example: scanned image with no text; asked vendor to resend.",
        "Can't approve; reject only.",
    ),
}


def reason_label(code: FlagCode | None) -> str:
    if code is None:
        return "-"
    copy = REASON_COPY.get(code)
    return copy.label if copy else FLAG_COPY[code].label


def recommendation_text(rec: Ceiling | None) -> str:
    """`HumanReview.recommendation` in words (design 6.3)."""
    if rec is None:
        return "No recommendation: the document couldn't be read"
    return {
        Ceiling.HUMAN: "Leave it to a person",
        Ceiling.APPROVE: "Approve",
        Ceiling.REJECT: "Reject",
    }[rec]


# ------------------------------------------------------- headline and "Why" (6.2)


def _by_person(result: InvoiceResult) -> bool:
    d, h = result.decision, result.human
    return bool((d and d.by == "human") or (h and h.resolution and h.resolution.action == "reject"))


def _stock_recheck(result: InvoiceResult) -> bool:
    return bool(result.human and result.human.outcome == "rejected_stock")


def credit_amount(result: InvoiceResult) -> Any:
    d, h = result.decision, result.human
    for candidate in (d.payment_amount if d else None, h.payment_amount if h else None, result.amount):
        if candidate is not None:
            return abs(candidate)
    return None


def _paid_text(result: InvoiceResult) -> str:
    p = result.payment
    if p is not None:
        return fmt.money(p.amount, p.currency)
    return fmt.money(result.amount, result.currency)


def _single_item(flags: Sequence[Flag]) -> str | None:
    items = {
        str(f.evidence["item"])
        for f in flags
        if f.severity == Severity.ERROR
        and f.code in (FlagCode.UNKNOWN_ITEM, FlagCode.ZERO_STOCK_ITEM, FlagCode.INSUFFICIENT_STOCK)
        and f.evidence.get("item")
    }
    return next(iter(items)) if len(items) == 1 else None


def rate_text(rate: Any) -> str:
    text = format(rate.normalize(), "f")
    return text


def headline(result: InvoiceResult) -> str:
    """The h2 outcome sentence (design 6.2, `Headline` column). Plain text; escape before showing."""
    status, d, h = result.status, result.decision, result.human
    if status == Status.PAID:
        vendor = result.payment.vendor if result.payment else (result.vendor or "the vendor")
        return f"Paid {_paid_text(result)} to {vendor}"
    if status == Status.PENDING_HUMAN:
        code = h.reason if h else next((f.code for f in result.flags if f.severity == Severity.HUMAN), None)
        return f"Waiting for a person: {reason_label(code).lower()}"
    if status == Status.REJECTED:
        if _stock_recheck(result):
            return "Rejected: stock ran short before approval"
        if _by_person(result):
            return "Rejected by a person"
        if d is None or d.by == "rules":
            labels = ", ".join(labels_for(result.flags, Severity.ERROR, lower=True)) or "see memo"
            return f"Rejected: {labels}"
        if d.by == "deadlock":
            return "Rejected: VP and Critic didn't agree after 3 rounds"
        if d.clamped:
            return "Rejected: the rules capped the VP's proposal"
        labels = ", ".join(labels_for(result.flags, Severity.ESCALATION, lower=True)) or "see memo"
        who = "the VP" if d.by == "vp" else "the VP and Critic"
        return f"Rejected by {who}: {labels}"
    if status == Status.DUPLICATE:
        return f"Duplicate of {result.duplicate_of}. Not paid again." if result.duplicate_of else "Duplicate. Not paid again."
    if status == Status.CREDIT_DUE:
        return f"Approved revision: credit of {fmt.money(credit_amount(result), result.currency)} due"
    if status == Status.SUPERSEDED:
        return "Replaced by its revision"
    if status == Status.ERROR:
        return "System error: nothing was paid"
    return STATUS[status].label


def why(result: InvoiceResult) -> str:
    """The batch table's "Why" column (design 6.2). Plain text; escape before showing."""
    status, d, h = result.status, result.decision, result.human
    if status == Status.PAID:
        text = f"Paid {_paid_text(result)}"
        p = result.payment
        if p is not None:
            if p.fx_rate is not None and p.currency != "USD":
                text += f" at {rate_text(p.fx_rate)}"
            if p.attempts > 1:
                text += f", {p.attempts} attempts"
        return text
    if status == Status.PENDING_HUMAN:
        code = h.reason if h else next((f.code for f in result.flags if f.severity == Severity.HUMAN), None)
        return reason_label(code)
    if status == Status.REJECTED:
        if _stock_recheck(result):
            return "Person approved; stock short"
        if _by_person(result):
            note = (h.resolution.note if h and h.resolution else None) or (h.outcome_note if h else None) or (
                d.reasoning if d else None
            )
            return f"Person: {snippet(note, 40)}"
        if d is None or d.by == "rules":
            labels = ", ".join(labels_for(result.flags, Severity.ERROR, lower=True)) or "see memo"
            item = _single_item(result.flags)
            return f"Rules: {labels}" + (f" ({item})" if item else "")
        labels = ", ".join(labels_for(result.flags, Severity.ESCALATION, lower=True)) or "see memo"
        if d.by == "deadlock":
            return f"Deadlock: {labels}"
        if d.clamped:
            return f"Capped by rules: {labels}"
        return f"{'VP' if d.by == 'vp' else 'VP and Critic'}: {labels}"
    if status == Status.DUPLICATE:
        return f"Same invoice as {result.duplicate_of}" if result.duplicate_of else "Duplicate"
    if status == Status.CREDIT_DUE:
        return f"Credit due {fmt.money(credit_amount(result), result.currency)}"
    if status == Status.SUPERSEDED:
        return "Replaced by revision"
    if status == Status.ERROR:
        return (result.error or "System error")[:80]
    return STATUS[status].label


# ------------------------------------------------------- run-log details (6.5)


_FORMAT_LABEL = {"pdf": "PDF, text layer extracted", "txt": "Text", "json": "JSON", "csv": "CSV", "xml": "XML"}


def _n_flags(flags: Sequence[Any]) -> str:
    total = len(flags)
    if total == 0:
        return "no flags"
    blocking = sum(1 for f in flags if _val(_get(f, "severity")) == Severity.ERROR.value)
    return f"{_plural(total, 'flag', 'flags')}: {blocking} block payment"


def _detail(node: str, update: Mapping[str, Any]) -> str:
    if node == "load_document":
        if _get(update, "parse_error"):
            return "could not be read"
        return _FORMAT_LABEL.get(str(_val(_get(update, "format"))), "")
    if node == "extract_with_llm":
        attempts = int(_get(update, "extraction_attempts", 1) or 1)
        return {1: "", 2: "second attempt"}.get(attempts, f"attempt {attempts}")
    if node == "ground_check":
        errors = _get(update, "grounding_errors") or []
        if not errors:
            return "all found"
        if _get(update, "parse_error"):
            return "still not found; sending to a person"
        return f"{len(errors)} not found; asking the model again"
    if node == "normalize":
        n = len(_get(update, "normalized_changes") or [])
        return _plural(n, "value", "values") if n else "no changes"
    if node == "dedupe":
        d = _get(update, "dedupe")
        kind = str(_val(_get(d, "kind", "new")))
        if kind == "duplicate":
            return f"same invoice as {_get(d, 'original_file_name') or _get(update, 'duplicate_of') or 'an earlier file'}"
        return {
            "new": "new invoice",
            "conflict": "same number, different content",
            "revision_of_paid": "revises a paid invoice",
            "revision_replaces": "replaces an unpaid original",
        }.get(kind, "")
    if node == "validate":
        return _n_flags(_get(update, "flags") or [])
    if node == "compute_ceiling":
        c = _ceiling(_get(update, "ceiling"))
        return ceiling_label(c) if c else ""
    if node == "vp_decide":
        out = _get(update, "vp_output")
        proposed = _ceiling(_get(out, "decision"))
        turns = _get(update, "transcript") or []
        calls = len(_get(turns[-1], "tool_calls") or []) if turns else 0
        if proposed is None:
            return ""
        text = f"proposes {ceiling_label(proposed)}"
        return text + (f" (looked up {_plural(calls, 'thing', 'things')})" if calls else "")
    if node == "critic_review":
        out = _get(update, "critic_output")
        verdict = _val(_get(out, "verdict"))
        if verdict == Verdict.AGREE.value:
            return "agrees"
        if verdict == Verdict.OBJECT.value:
            n = len(_get(out, "objections") or [])
            return f"objects ({_plural(n, 'objection', 'objections')})"
        return ""
    if node == "clamp_decision":
        d = _get(update, "decision")
        final = _ceiling(_get(d, "final"))
        if final is None:
            return ""
        return f"final: {ceiling_label(final)}" + (", capped" if _get(d, "clamped") else "")
    if node == "prepare_human":
        code = _code(_get(_get(update, "human"), "reason"))
        return reason_label(code) if code else ""
    if node == "pay":
        receipt = _get(update, "payment")
        if receipt is not None:
            n = int(_get(receipt, "attempts", 1) or 1)
            return f"{fmt.money(_get(receipt, 'amount'), _get(receipt, 'currency'))}, {_plural(n, 'attempt', 'attempts')}"
        if _get(update, "payment_error"):
            return "failed after 4 attempts"
        status = _status(_get(update, "status"))
        return STATUS[status].label if status == Status.CREDIT_DUE else ""
    if node == "finish":
        status = _status(_get(update, "status"))
        return STATUS[status].label if status else ""
    return ""


def node_detail(node: str, update: Mapping[str, Any]) -> str:
    """The detail after a node's run-log label (design 6.5), from the node's state update.

    The update may hold pydantic objects (live streaming) or plain dicts (stored). Never raises.
    """
    try:
        return _detail(node, update or {})
    except Exception:
        return ""


def node_line(node: str, update: Mapping[str, Any]) -> str:
    """`Label: detail`, or just the label when there is no detail."""
    label = NODE_LABEL.get(node, node.replace("_", " ").capitalize())
    detail = node_detail(node, update)
    return f"{label}: {detail}" if detail else label


# --------------------------------------------------------------- tools (6.5)

TOOL_NAMES: dict[str, str] = {
    "get_vendor_history": "Vendor history",
    "get_stock_level": "Stock level",
    "get_budget_status": "Budget",
    "get_policy": "Policy",
    "get_source_evidence": "Source lines",
    "recompute_totals": "Recomputed totals",
    "find_similar_invoices": "Similar invoices",
    "get_vendor_master": "Vendor master",
    "get_pipeline_trace": "Pipeline trace",
}


def _money_text(raw: Any) -> str:
    from decimal import Decimal, InvalidOperation

    try:
        return fmt.money(Decimal(str(raw)))
    except (InvalidOperation, ValueError):
        return str(raw)


def tool_asked(arguments: Mapping[str, Any]) -> str:
    """The "Asked about" cell: the argument values, comma-joined; `-` when there are none."""
    try:
        parts = [str(v) for v in (arguments or {}).values() if v not in (None, "")]
    except Exception:
        return "-"
    return ", ".join(parts) if parts else "-"


def tool_summary(name: str, arguments: Mapping[str, Any], result: Mapping[str, Any]) -> str:
    """One-line answer for the tool-call table (design 6.5). Never raises."""
    try:
        r = result or {}
        if name == "get_vendor_history":
            n = len(r.get("invoices") or [])
            past = _plural(n, "past invoice", "past invoices") if n else "no past invoices"
            return f"{'known' if r.get('known') else 'not known'}, {past}"
        if name == "get_stock_level":
            item = r.get("item") or (arguments or {}).get("item", "item")
            if r.get("found"):
                return f"{item}: {r.get('stock')}"
            hint = r.get("did_you_mean")
            return f"{item}: not in inventory" + (f", did you mean {hint}?" if hint else "")
        if name == "get_budget_status":
            return f"{_money_text(r['remaining'])} left of {_money_text(r['limit'])}"
        if name == "get_policy":
            if "topic" in r:
                return f"section: {r['topic']}"
            return "unknown section"
        if name == "get_source_evidence":
            lines = r.get("lines") or []
            field = r.get("field") or (arguments or {}).get("field", "field")
            return f"{field}: {' '.join(str(lines[0]).split())}" if lines else f"{field}: no source line"
        if name == "recompute_totals":
            return f"total {_money_text(r['total_recomputed'])} vs stated {_money_text(r['total_stated'])}"
        if name == "find_similar_invoices":
            return f"{len(r.get('matches') or [])} found"
        if name == "get_vendor_master":
            if not r.get("found"):
                return "not found"
            terms = f", terms {r['payment_terms']}" if r.get("payment_terms") else ""
            return f"found, {r.get('status', 'status unknown')}{terms}"
        if name == "get_pipeline_trace":
            return _plural(len(r.get("audit") or []), "event", "events")
    except Exception:
        pass
    keys = list((result or {}).keys()) if isinstance(result, Mapping) else []
    return _plural(len(keys), "field", "fields") if keys else "no answer"


def tools_label(calls: Sequence[ToolTrace]) -> str:
    """`Looked up 9 things: vendor history ×2, stock level ×3, budget, policy ×3`; empty for no calls."""
    if not calls:
        return ""
    counts: dict[str, int] = {}
    for c in calls:
        label = TOOL_NAMES.get(c.name, c.name.replace("_", " ")).lower()
        counts[label] = counts.get(label, 0) + 1
    parts = [f"{label} ×{n}" if n > 1 else label for label, n in counts.items()]
    return f"Looked up {_plural(len(calls), 'thing', 'things')}: {', '.join(parts)}"


# ------------------------------------------------------------ sample guide (6.8)

SAMPLE_GUIDE: dict[str, str] = {
    "invoice_1001.txt": "Clean text invoice from a known vendor, $5,000",
    "invoice_1002.txt": '20 GadgetX; due the same day it was issued despite Net 30; number printed without "INV-"',
    "invoice_1003.txt": 'Unknown vendor, FakeItem, due "yesterday", $100,000, "URGENT… wire transfer"',
    "invoice_1004.json": "Known vendor with a Springfield address, $1,890",
    "invoice_1004_revised.json": 'Revision R1 of 1004 adding 5 GadgetX "per PO amendment"',
    "invoice_1005.json": "Unknown vendor at 1600 Pennsylvania Ave, $15,225",
    "invoice_1006.csv": "Field-and-value CSV layout, $2,750",
    "invoice_1007.csv": "Table CSV; the 6% tax total doesn't match the lines",
    "invoice_1008.txt": "Email-style text; unknown vendor and items, $9,900",
    "invoice_1009.json": "Negative quantity, no vendor, no due date",
    "invoice_1010.txt": 'WidgetA on two lines, one a $300 "rush order", plus shipping',
    "invoice_1011.pdf": "The same invoice as PDF and text",
    "invoice_1011.txt": "The same invoice as PDF and text",
    "invoice_1012.pdf": 'OCR-style typos; vendor "formerly FastShip Ltd."; $9,975',
    "invoice_1012.txt": 'OCR-style typos; vendor "formerly FastShip Ltd."; $9,975',
    "invoice_1013.json": "Lines within stock one by one; grand total off by $50",
    "invoice_1013.pdf": "Lines within stock one by one; grand total off by $50",
    "invoice_1014.xml": "XML invoice in euros",
    "invoice_1015.csv": "Table CSV, $6,500",
    "invoice_1016.json": "Known vendor billing WidgetC",
}

# ------------------------------------------------------------- fixed strings

TEXT: dict[str, str] = {
    # titles and buttons (6.1)
    "title.process": "Process an invoice",
    "title.queue": "Review queue",
    "title.ledger": "Ledger",
    "title.batch": "Batch",
    "button.process": "Process",
    "button.batch": "Run batch",
    "button.process_again": "Process again",
    "button.download_original": "Download original",
    "button.show_raw": "Show raw results",
    "button.reset_open": "Reset demo data…",
    "button.reset_cancel": "Cancel",
    "button.reset_confirm": "Reset and clear history",
    "button.queue_next": "Next: {ref}",
    "button.approve_pay": "Approve and pay {amount}",
    "button.approve_credit": "Approve (credit due, nothing to pay)",
    "button.approve": "Approve",
    "button.reject": "Reject invoice",
    "label.uploader": "Invoice file",
    "help.uploader": "PDF with a text layer, TXT, JSON, CSV or XML",
    "label.bank_response": "Bank response",
    "label.run_details": "Run details",
    # case view labels
    "label.ceiling_badge": "Ceiling: {label}",
    "help.ceiling": "The best outcome the rules allow. The VP may choose it or something stricter, never better.",
    "label.final_decision": "Final decision",
    "label.proposes": "Proposes {ceiling}",
    "label.round": "{actor}, round {n}",
    "label.memo": "VP memo",
    "label.tools_raw": "Show raw results",
    "label.flags_answered": "Flags answered",
    "label.objections_answered": "Objections answered",
    "label.requested_change": "Requested change: {text}",
    "label.critic_proposes": "proposes {ceiling}",
    "label.person_approved": "Person approved",
    "label.person_rejected": "Person rejected",
    "case.no_vendor": "No vendor",
    "case.read_as_none": "-",
    "label.tool": "Tool",
    "label.asked_about": "Asked about",
    "label.answer": "Answer",
    # links (6.1)
    "link.decide": "Decide in the review queue",
    "link.open_case": "Open the full case",
    "link.review_waiting": "Review the {n} invoices waiting for a person",
    "link.review_waiting_one": "Review the 1 invoice waiting for a person",
    "link.ledger": "See the payments in the ledger",
    "link.open_file": "Open {file}",
    "link.process": "Process an invoice",
    "link.batch": "Run a batch",
    # sections
    "section.checks": "Checks",
    "section.document": "Document",
    "section.approval": "Approval",
    "section.payment": "Payment",
    "section.next_step": "Next step",
    "section.extracted": "Extracted",
    "section.source": "Source",
    "section.read_as": "Read as",
    # empty states (6.6)
    "empty.process": "Pick a sample or upload an invoice. Each file runs through four steps: read, check, "
    "approve, then pay or route to a person.",
    "empty.queue": "Nothing is waiting for a person. Invoices come here for six reasons only: foreign currency, "
    "a revision or a reused invoice number, over budget, a renamed vendor, a failed payment, or an "
    "unreadable document.",
    "empty.queue_done": "All decisions made. You approved {approved} and rejected {rejected}; {total} was paid.",
    "empty.ledger": "{month}: nothing paid yet. {limit} of the monthly limit is available.",
    "empty.ledger_next": "Run the batch or process an invoice to see payments here.",
    "empty.batch": "Run the folder to process every file in filename order. A revision runs after its original, "
    "and the same invoice in two formats is caught and paid once.",
    "case.no_flags": "All checks passed: known vendor, every item in stock, prices at catalog, totals add up.",
    "case.no_lines": "No line items found.",
    "case.stored": "Already processed on {date}. Showing the stored result. Nothing was run again or paid twice.",
    "case.unreadable_panel": "The file couldn't be read, so there are no fields to show: {error}",
    "case.no_source": "The original file is no longer available.",
    "case.stored_undated": "Already processed. Showing the stored result. Nothing was run again or paid twice.",
    "source.aria": "Source document with line numbers",
    # address check (5.0 B)
    "address.verified": "Address matches the one on file.",
    "address.mismatch": "Address differs from the one on file. The VP had to mention it.",
    "address.not_on_invoice": "The invoice gives no vendor address.",
    "address.no_record": "No address on file for this vendor.",
    # approval (5.0 D)
    "ceiling.APPROVE": "Ceiling: Approve. No rule blocks payment, so the VP may approve.",
    "ceiling.HUMAN": "Ceiling: Person decides. {reason} needs a person; the VP and Critic write a recommendation.",
    "ceiling.REJECT": "Ceiling: Reject. A rule blocks payment ({labels}); the VP only writes the memo.",
    "path.fast": "Fast path: the VP decided alone.",
    "path.critic": "VP and Critic, {n} rounds.",
    "path.critic_one": "VP and Critic, 1 round.",
    "path.rules": "Rules decided; the VP wrote the memo.",
    "path.deadlock": "No agreement after 3 rounds. The stricter decision applies.",
    "path.person": "Decided by a person.",
    "notice.clamp_ceiling": "Capped by the rules: the VP proposed {proposed}, but the ceiling is {ceiling}.",
    "notice.human_to_reject": "Capped by the rules: a person can only be asked for one of the six review "
    "reasons, so this becomes Reject.",
    "notice.over_threshold": "Capped by the rules: approvals over $10,000 need the Critic to agree.",
    "notice.deadlock": "No agreement after 3 rounds. The stricter of the two decisions applies: {final}.",
    "final.rules": "by the rules",
    "final.vp": "by the VP",
    "final.vp_critic": "by the VP, Critic agreed",
    "final.deadlock": "by the deadlock rule",
    "final.person": "by a person",
    "approval.none_unreadable": "No VP review: there was no reliable data to reason about.",
    "approval.none_duplicate": "No review needed: this invoice was already handled.",
    # payment / next step (5.0 E)
    "next.pending": "Not paid yet. {reason}",
    "next.rejected": "Not paid. {why}",
    "next.duplicate": "Not paid. Same invoice as {file}, which was {original}.",
    "next.duplicate_plain": "Not paid. Same invoice as {file}.",
    "next.credit": "Nothing to pay. The revision totals {less} less than the {paid} already paid, so a credit "
    "of {credit} is due from the vendor.",
    "next.credit_plain": "Nothing to pay. The revision totals less than what was already paid, so a credit "
    "of {credit} is due from the vendor.",
    "next.superseded": "Not paid. Replaced by its revision, which was processed instead.",
    "pay.paid_to": "Paid to",
    "pay.amount": "Amount",
    "pay.rate": "Rate",
    "pay.rate_value": "{rate} {currency} to USD",
    "pay.in_dollars": "In dollars",
    "pay.attempts": "Attempts",
    "pay.attempts_one": "1",
    "pay.attempts_retried": "{n}: {failed} failed, then retried",
    "pay.paid_at": "Paid at",
    "pay.receipt": "Receipt",
    "pay.stock_used": "Stock used",
    "pay.spending_after": "{month} spending after this payment",
    "pay.credit_line": "Credit due from the vendor: {credit}",
    # errors, info, warnings (6.7)
    "error.system_title": "System error",
    "error.system_body": "Nothing was paid, and the invoice is not in the review queue. This is a bug, "
    "not a business decision.",
    "error.system_trace": "Trace {trace_id}. Details in the JSONL logs under logs/.",
    "error.no_model_title": "No model available",
    "error.no_model_sidebar": "No model available. Add XAI_API_KEY and the two model names to .env, or record "
    "once and use replay.",
    "error.system": "System error while processing {file}",
    "info.stored": "Already processed on {date}. Showing the stored result. Nothing was run again or paid twice.",
    "info.rerun_batch": "{n} of these files were processed before. Their stored results will be shown, and "
    "nothing will be paid twice. Reset demo data to run them from scratch.",
    "info.replay_slowed": "replay, slowed for viewing",
    "warn.folder_missing": "No folder at {path}. Paths are relative to the project folder.",
    "warn.folder_empty": "{path} has no files to process.",
    "warn.no_model_process": "No model available. New invoices can't be processed. Files processed before "
    "still show their stored result.",
    "warn.stopped": "Stopped at {file}: no model available.",
    # run details (5.0 F)
    "run.trace": "Trace ID",
    "run.file": "File",
    "run.format": "Format",
    "run.provider": "Provider",
    "run.calls": "Model calls",
    "run.tokens_in": "Tokens in",
    "run.tokens_out": "Tokens out",
    "run.model_time": "Model time",
    "run.model_time_replay": "Model time (as recorded)",
    "run.steps": "Time per step",
    "run.log_label": "Log",
    "run.log": "logs/, search for {trace_id}",
    "run.cli": "Command line",
    "run.none": "-",
    # source captions (5.0 C)
    "source.text": "Text as printed",
    "source.pdf": "Text layer of the PDF",
    "source.file": "File as received",
    # field labels
    "field.invoice_number": "Invoice number",
    "field.revision": "Revision",
    "field.vendor.name": "Vendor",
    "field.vendor.former_name": "Formerly",
    "field.vendor.address": "Vendor address",
    "field.date": "Invoice date",
    "field.due_date": "Due date",
    "field.currency": "Currency",
    "field.payment_terms": "Terms",
    "field.subtotal": "Subtotal",
    "field.tax_amount": "Tax",
    "field.shipping": "Shipping",
    "field.total": "Total",
    # revision approval wording
    "reason.revision_credit": "Records a credit of {credit} due.",
    "reason.revision_pays": "Pays the {difference} difference.",
}


# --------------------------------------------------------- outcome counts (5.4)

_COUNT_PHRASE: dict[Status, tuple[str, str]] = {
    Status.PAID: ("paid", "paid"),
    Status.CREDIT_DUE: ("credit due", "credit due"),
    Status.REJECTED: ("rejected", "rejected"),
    Status.PENDING_HUMAN: ("needs a person", "need a person"),
    Status.DUPLICATE: ("duplicate", "duplicates"),
    Status.SUPERSEDED: ("replaced by its revision", "replaced by their revisions"),
    Status.ERROR: ("system error", "system errors"),
    Status.PROCESSING: ("processing", "processing"),
}

# Display order of the outcome split: green is never next to amber (design 3.4).
OUTCOME_ORDER: tuple[Status, ...] = (
    Status.PAID, Status.CREDIT_DUE, Status.REJECTED, Status.PENDING_HUMAN,
    Status.DUPLICATE, Status.SUPERSEDED, Status.ERROR,
)


def count_phrase(status: Status, n: int) -> str:
    """`3 need a person`, `1 needs a person`, `3 duplicates`, `5 paid`."""
    one, many = _COUNT_PHRASE[status]
    return f"{n} {one if n == 1 else many}"


def counts_sentence(counts: Sequence[tuple[Status, int]]) -> str:
    """`5 paid, 9 rejected, 3 need a person, 3 duplicates` in display order, zero counts left out."""
    by_status = {s: n for s, n in counts}
    return ", ".join(count_phrase(s, by_status[s]) for s in OUTCOME_ORDER if by_status.get(s, 0) > 0)


def t(key: str, **fields: Any) -> str:
    """Look up a fixed string and fill its named fields. Callers escape untrusted field values first."""
    text = TEXT[key]
    return text.format(**fields) if fields else text
