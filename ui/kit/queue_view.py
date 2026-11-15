"""Review queue: pure builders for the page (evidence, form rules, preview rows, messages).

Nothing here touches Streamlit or storage. Wording follows design 5.2 and 6.3; strings that are
not in `copy.TEXT` are constants below. Untrusted text (vendor, notes, errors) is returned raw: the
page escapes it with `fmt.md` or `fmt.esc` at the point of rendering.
"""

import calendar
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal, Sequence

from models import (
    BudgetStatus,
    FlagCode,
    InvoiceRecord,
    InvoiceResult,
    InvoiceState,
    PaymentRecord,
    QueueItem,
    ResolutionPreview,
    Status,
    VendorAlias,
    VendorRecord,
)
from service import (
    AmbiguousReference,
    ApproveNotAllowed,
    FxRateRequired,
    NotFound,
    NoteRequired,
    OverspendConfirmationRequired,
    ResolutionRefused,
)
from ui.kit import copy, fmt

# ------------------------------------------------------------------ wording

SUBTITLE = "{count} need a decision. Each is here for one of six reasons the system may not decide."
SUBTITLE_ONE = "1 invoice needs a decision. It is here for one of six reasons the system may not decide."
BUSY = "A run is in progress; resolving is available when it finishes."
GONE = "{ref} is no longer waiting. It may have been resolved in another tab. The list has been refreshed."
GONE_UNNAMED = "This invoice is no longer waiting. It may have been resolved in another tab. The list has been refreshed."
READ_ONLY = "This file couldn't be read reliably, so it can only be rejected. Fix the file and process it again."
BLOCKED = "Enter a rate to see the dollar amount and the effect on the budget."
RATE_HELP = "Units of USD per 1 {currency}. Recorded with the payment and used for the budget."
RATE_LABEL = "Exchange rate, {currency} to USD"
RATE_PLACEHOLDER = "for example 1.08"
RATE_INVALID = "Enter the rate as a plain number, for example 1.08."
NOTE_LABEL = "Note (required)"
OVERSPEND_LABEL = "Approve an overspend of {over}. {month} spending becomes {after} against the {limit} limit."
RENAME_NOTE = "{vendor} is recorded as a new name for {former}."
RENAME_DONE = "{vendor} is now recorded as a new name for {former}."
RETRY_NOTE = "The payment is tried again."
STOCK_ERROR = (
    "Approving will not pay this invoice. Stock has run short since it was queued: {items}. "
    "The queue doesn't reserve stock, so it is checked again now. Approving records a rejection with that memo."
)
CREDIT_ROW = "Nothing to pay. A credit of {credit} is due from the vendor."
RESOLVING = "Resolving {ref}"
REPLACED = "Replaced by its revision"
NEXT_STEP = "Open the full case"

PREVIEW_HEADING = "If you approve"
DECISION_HEADING = "Decision"
VP_HEADING = "VP and Critic"
REASONING_CAPTION = "VP's reasoning, as written"
OTHER_FLAGS = "Other flags: {labels}"

_NOTE_MISSING = {
    "note": "a note",
    "rate": "an exchange rate",
    "confirm": "the overspend box",
}


def month_name(month: str) -> str:
    """`2026-01` gives `January`."""
    try:
        return calendar.month_name[int(month[5:7])]
    except (ValueError, IndexError):
        return month


# ----------------------------------------------------------- reason evidence


@dataclass(frozen=True)
class ReasonEvidence:
    title: str
    explanation: str
    facts: tuple[tuple[str, str], ...]


def _invoice(record: InvoiceRecord | None, state: InvoiceState | None):
    if state is not None and state.invoice is not None:
        return state.invoice
    return record.result.invoice if record is not None and record.result is not None else None


def _added_text(state: InvoiceState | None, invoice) -> str:
    if state is None or state.dedupe is None or invoice is None:
        return ""
    names = {line.item_key: line.item for line in invoice.line_items}
    return ", ".join(f"{q} {names.get(k, k)}" for k, q in state.dedupe.added_quantities.items())


def _payment_error(state: InvoiceState | None, payments: Sequence[PaymentRecord]) -> str:
    if state is not None and state.payment_error:
        return state.payment_error
    failed = [p for p in payments if p.status == "FAILED" and p.error]
    return failed[-1].error or "" if failed else ""


def _unreadable_error(item: QueueItem, state: InvoiceState | None, record: InvoiceRecord | None) -> str:
    if state is not None:
        if state.parse_error:
            return state.parse_error
        if state.grounding_errors:
            return "; ".join(state.grounding_errors)
    if record is not None and record.error:
        return record.error
    flag = None
    if record is not None and record.result is not None:
        flag = next((f for f in record.result.flags if f.code == FlagCode.UNREADABLE_DOCUMENT), None)
    return flag.message if flag else ""


def reason_evidence(
    item: QueueItem, record: InvoiceRecord | None, state: InvoiceState | None, vendor: VendorRecord | None,
    payments: Sequence[PaymentRecord], *, budget: BudgetStatus | None = None,
    aliases: Sequence[VendorAlias] = (),
) -> ReasonEvidence:
    """The reason's title, its explanation filled from the evidence, and the facts to check.

    When a template field is unavailable the explanation falls back to the flag's plain sentence.
    `budget` (the current month) lets `over_budget` quote the figures; `aliases` are the vendor
    master's existing alternative names.
    """
    code = item.reason
    title = copy.reason_label(code)
    invoice = _invoice(record, state)
    currency = (invoice.currency if invoice else None) or item.review.currency or (record.currency if record else None) or "USD"
    vendor_name = (invoice.vendor_name if invoice else None) or item.vendor or ""
    former = (invoice.vendor_former_name if invoice else None) or ""
    number = (invoice.invoice_number if invoice else None) or item.invoice_number or item.file_name
    facts: list[tuple[str, str]] = []
    fields: dict[str, str] = {}
    needed: tuple[str, ...] = ()

    if code == FlagCode.FOREIGN_CURRENCY:
        needed = ("currency",)
        fields["currency"] = currency
        total = invoice.total if invoice else (record.amount if record else None)
        if total is not None:
            facts.append(("Amount", fmt.money(total, currency)))
        facts.append(("Currency", currency))
    elif code == FlagCode.REVISION_OF_PAID_INVOICE:
        needed = ("original_ref", "paid_to_date", "added")
        dedupe = state.dedupe if state is not None else None
        fields["original_ref"] = number
        fields["paid_to_date"] = fmt.money(dedupe.paid_to_date, currency) if dedupe else ""
        fields["added"] = _added_text(state, invoice)
        if dedupe is not None and dedupe.original_file_name:
            facts.append(("Original file", dedupe.original_file_name))
        if dedupe is not None:
            facts.append(("Paid to date", fmt.money(dedupe.paid_to_date, currency)))
        if fields["added"]:
            facts.append(("Added quantities", fields["added"]))
        if item.review.payment_amount is not None:
            facts.append(("Difference payable", fmt.money(item.review.payment_amount, currency)))
    elif code == FlagCode.CONFLICTING_INVOICE_NUMBER:
        needed = ("vendor", "number")
        fields["vendor"], fields["number"] = vendor_name, number
        dedupe = state.dedupe if state is not None else None
        if dedupe is not None and dedupe.original_file_name:
            facts.append(("Other file", dedupe.original_file_name))
    elif code == FlagCode.OVER_BUDGET:
        needed = ("amount", "month", "after", "over", "limit")
        amount = item.review.payment_amount
        if budget is not None and amount is not None and currency == "USD":
            after = budget.spent + amount
            fields.update(
                amount=fmt.money(amount), month=month_name(budget.month), after=fmt.money(after),
                over=fmt.money(max(Decimal("0"), after - budget.limit)), limit=fmt.money(budget.limit),
            )
            facts += [("Spent", fmt.money(budget.spent)), ("Limit", fmt.money(budget.limit)),
                      ("Overspend", fields["over"])]
    elif code == FlagCode.VENDOR_RENAMED:
        needed = ("vendor", "former")
        fields["vendor"], fields["former"] = vendor_name, former
        if vendor is not None:
            facts.append(("On file as", vendor.name))
            if vendor.address:
                facts.append(("Address on file", vendor.address))
            if vendor.payment_terms:
                facts.append(("Terms on file", vendor.payment_terms))
            others = [a.alias for a in aliases]
            if others:
                facts.append(("Other names on file", ", ".join(others)))
    elif code == FlagCode.PAYMENT_FAILED:
        needed = ("error",)
        fields["error"] = _payment_error(state, payments)
        failed = [p for p in payments if p.invoice_id == item.invoice_id and p.status == "FAILED"]
        if failed:
            facts.append(("Attempts", str(failed[-1].attempts)))
        if fields["error"]:
            facts.append(("Error", fields["error"]))
    elif code == FlagCode.UNREADABLE_DOCUMENT:
        needed = ("error",)
        fields["error"] = _unreadable_error(item, state, record)
        if fields["error"]:
            facts.append(("Error", fields["error"]))

    if needed and all(fields.get(n) for n in needed):
        explanation = copy.REASON_COPY[code].why.format(**fields).replace("..", ".")
    else:
        explanation = copy.FLAG_COPY[code].sentence
    return ReasonEvidence(title, explanation, tuple(facts))


# ---------------------------------------------------------------- form rules


@dataclass(frozen=True)
class FormState:
    approve_allowed: bool
    needs_fx: bool
    needs_confirm: bool
    fx_rate: Decimal | None
    can_submit: bool
    problems: tuple[str, ...]  # subset of "note", "rate", "confirm", in that order


def form_state(
    preview: ResolutionPreview | None, *, action: Literal["approve", "reject"], note: str, fx_text: str,
    confirm: bool, force_confirm: bool = False,
) -> FormState:
    """What is enabled and what is still missing. `preview` None (busy, gone) blocks everything.

    Rejecting needs only a note. `force_confirm` keeps the overspend box required after the service
    asked for it, even if a fresh preview no longer says so.
    """
    rate = fmt.parse_fx_rate(fx_text)
    if preview is None:
        return FormState(False, False, False, rate, False, ())
    approving = action == "approve"
    needs_fx = preview.needs_fx_rate
    needs_confirm = preview.needs_overspend_confirmation or force_confirm
    problems: list[str] = []
    if not note or not note.strip():
        problems.append("note")
    if approving and needs_fx and rate is None:
        problems.append("rate")
    if approving and needs_confirm and not confirm:
        problems.append("confirm")
    can = not problems and (preview.can_approve or not approving)
    return FormState(preview.can_approve, needs_fx, needs_confirm, rate, can, tuple(problems))


def missing_caption(problems: Sequence[str], action: Literal["approve", "reject"] = "approve") -> str:
    """Names exactly what is missing: "Add a note to enable the button." and its combinations."""
    if not problems:
        return ""
    if action == "reject" or problems == ("note",):
        return "Add a note to enable the button."
    parts = [_NOTE_MISSING[p] for p in problems]
    if "confirm" in problems:
        lead = [p for p in problems if p != "confirm"]
        if not lead:
            return "Tick the overspend box to approve."
        return f"Add {' and '.join(_NOTE_MISSING[p] for p in lead)} and tick the overspend box to approve."
    return f"Add {' and '.join(parts)} to approve."


def submit_label(preview: ResolutionPreview | None, action: Literal["approve", "reject"]) -> str:
    if action == "reject":
        return copy.t("button.reject")
    if preview is None or preview.approve_outcome == "reject_stock" or preview.payment_amount is None:
        return copy.t("button.approve")
    if preview.approve_outcome == "credit_due":
        return copy.t("button.approve_credit")
    return copy.t("button.approve_pay", amount=fmt.money(preview.payment_amount, preview.currency))


# ------------------------------------------------------------------ preview


def _rate_text(rate: Decimal) -> str:
    return copy.rate_text(rate)


def stock_text(preview: ResolutionPreview) -> str:
    """`WidgetA 56 → 44, WidgetB 32 → 25`; items whose level is unknown are left out."""
    parts = []
    for name, needed in preview.stock_needed.items():
        level = preview.stock_levels.get(name)
        if level is not None:
            parts.append(f"{name} {level} → {level - needed}")
    return ", ".join(parts)


def stock_error(preview: ResolutionPreview) -> str:
    items = "; ".join(f"{s.item} needs {s.needed}, {s.available} left" for s in preview.stock_shortfalls)
    return STOCK_ERROR.format(items=items)


def overspend_label(preview: ResolutionPreview) -> str:
    after = preview.budget.spent + (preview.payment_amount_usd or Decimal("0"))
    return OVERSPEND_LABEL.format(
        over=fmt.money(preview.overspend_usd), month=month_name(preview.budget.month),
        after=fmt.money(after), limit=fmt.money(preview.budget.limit),
    )


def preview_rows(
    preview: ResolutionPreview, item: QueueItem, state: InvoiceState | None = None,
) -> list[tuple[str, str]]:
    """The "If you approve" rows. Empty when approving is impossible or will be a stock rejection
    (the page shows the error block for that)."""
    if not preview.can_approve or preview.approve_outcome == "reject_stock" or preview.payment_amount is None:
        return []
    rows: list[tuple[str, str]] = []
    amount = preview.payment_amount
    vendor = item.vendor or copy.t("case.no_vendor")
    invoice = state.invoice if state is not None else None
    former = (invoice.vendor_former_name if invoice else None) or ""
    dedupe = state.dedupe if state is not None else None

    if preview.approve_outcome == "credit_due":
        rows.append(("Outcome", CREDIT_ROW.format(credit=fmt.money(abs(amount), preview.currency))))
    else:
        text = fmt.money(amount, preview.currency)
        if item.reason == FlagCode.REVISION_OF_PAID_INVOICE and dedupe is not None and dedupe.paid_to_date:
            ref = (invoice.invoice_number if invoice else None) or item.invoice_number or item.file_name
            text = (f"only the difference: {text} ({ref} was paid {fmt.money(dedupe.paid_to_date, preview.currency)})")
        elif preview.needs_fx_rate and preview.payment_amount_usd is not None and preview.fx_rate is not None:
            text += f" = {fmt.money(preview.payment_amount_usd)} at {_rate_text(preview.fx_rate)}"
        rows.append(("Pays", f"{text} to {vendor}"))

    if preview.approve_outcome == "blocked":
        rows.append(("Outcome", BLOCKED))
        return rows

    budget = preview.budget
    if preview.approve_outcome == "pay" and preview.payment_amount_usd is not None:
        after = budget.spent + preview.payment_amount_usd
        rows.append((
            f"{month_name(budget.month)} budget",
            f"{fmt.money(budget.spent)} → {fmt.money(after)} of {fmt.money(budget.limit)}",
        ))
        if preview.needs_overspend_confirmation:
            rows.append(("Over budget by", fmt.money(preview.overspend_usd)))
    stock = stock_text(preview)
    if stock:
        rows.append(("Stock", stock))
    if item.reason == FlagCode.VENDOR_RENAMED and former and item.vendor:
        rows.append(("Also", RENAME_NOTE.format(vendor=item.vendor, former=former).replace("..", ".")))
    elif item.reason == FlagCode.PAYMENT_FAILED:
        rows.append(("Also", RETRY_NOTE))
    return rows


def note_placeholder(item: QueueItem, state: InvoiceState | None = None) -> str:
    text = copy.REASON_COPY[item.reason].note_placeholder
    invoice = state.invoice if state is not None else None
    former = (invoice.vendor_former_name if invoice else None) or "the vendor"
    return text.replace("{former}", former)


# ---------------------------------------------------------------- messages


def refusal_message(
    exc: ResolutionRefused, *, ref: str | None = None, currency: str | None = None,
) -> str:
    """The inline message for a refused resolution (design 5.2 table).

    `ref` names the invoice for the "no longer waiting" message; `currency` fills the rate message.
    """
    if isinstance(exc, NoteRequired):
        return "Add a note saying what you checked. It goes in the audit trail."
    if isinstance(exc, FxRateRequired):
        lead = f"Enter the {currency} to USD rate" if currency else "Enter the exchange rate to USD"
        return f"{lead} to approve. Rejecting doesn't need one."
    if isinstance(exc, OverspendConfirmationRequired):
        limit = exc.budget.limit
        return (
            f"Approving takes {month_name(exc.budget.month)} spending to {fmt.money(limit + exc.overspend_usd)}, "
            f"{fmt.money(exc.overspend_usd)} over the {fmt.money(limit)} limit. "
            "Tick the box to confirm the overspend."
        )
    if isinstance(exc, ApproveNotAllowed):
        return "This file couldn't be read reliably, so it can only be rejected."
    if isinstance(exc, (NotFound, AmbiguousReference)):
        return GONE.format(ref=ref) if ref else GONE_UNNAMED
    return str(exc) or "The resolution was refused."


Kind = Literal["success", "info", "warning", "error"]


def outcome_message(
    result: InvoiceResult, *, spent: Decimal | None = None, month: str | None = None,
) -> tuple[Kind, str]:
    """What the page says after `resolve()` returned `result` (design 5.2 "After resolving").

    `spent` and `month` (the budget after the resolution) complete the "Paid" sentence.
    "info" is the plain panel under a Rejected badge; the others map to `st.success`,
    `st.warning` and `st.error`.
    """
    human = result.human
    if result.status == Status.ERROR:
        return "error", f"{copy.t('error.system_title')}: nothing was paid. {result.error or ''}".strip()
    if human is not None and human.outcome == "requeued":
        note = (human.outcome_note or "").rstrip(".")
        return "warning", f"Not applied: {note}. The invoice is still waiting."
    if result.status == Status.PENDING_HUMAN:
        error = result.error or (human.outcome_note if human else None) or ""
        tail = f": {error.rstrip('.')}" if error else ""
        return "warning", f"The payment failed again{tail}. The invoice is still waiting."
    if result.status == Status.PAID and result.payment is not None:
        pay = result.payment
        text = f"Paid {fmt.money(pay.amount, pay.currency)} to {pay.vendor}. Receipt {pay.idempotency_key}.".replace("..", ".")
        if spent is not None:
            text += f" {month_name(month) if month else 'Monthly'} spending is now {fmt.money(spent)}."
        former = result.invoice.vendor_former_name if result.invoice else None
        if former and human is not None and FlagCode.VENDOR_RENAMED in human.reasons:
            text += " " + RENAME_DONE.format(vendor=pay.vendor, former=former).replace("..", ".")
        return "success", text
    if result.status == Status.CREDIT_DUE:
        credit = fmt.money(copy.credit_amount(result), result.currency)
        return "success", f"Approved. Nothing to pay; a credit of {credit} is due from {result.vendor or 'the vendor'}.".replace("..", ".")
    if human is not None and human.outcome == "rejected_stock":
        return "info", f"Not paid. {human.outcome_note or ''}".strip()
    if result.status == Status.REJECTED:
        note = human.outcome_note if human and human.outcome_note else (
            result.decision.reasoning if result.decision else "")
        return "info", f"Rejected. Your note: {note}"
    return "info", copy.headline(result)


def resolved_line(item: QueueItem) -> str:
    """`INV-1012: Approved, Called FastShip…` for the Resolved expander (raw text)."""
    if item.status == "SUPERSEDED":
        return f"{item.invoice_ref}: {REPLACED}"
    verdict = {"approve": "Approved", "reject": "Rejected"}.get(item.resolution or "", "Resolved")
    note = copy.snippet(item.note) if item.note else ""
    return f"{item.invoice_ref}: {verdict}" + (f", {note}" if note else "")


def queue_summary(
    resolved: Sequence[QueueItem], payments: Sequence[PaymentRecord],
) -> tuple[int, int, Decimal]:
    """Approved count, rejected count and dollars paid through approvals, from the resolved items."""
    approved = [i for i in resolved if i.status == "RESOLVED" and i.resolution == "approve"]
    rejected = [i for i in resolved if i.status == "RESOLVED" and i.resolution == "reject"]
    ids = {i.invoice_id for i in approved}
    paid = sum(
        (p.amount_usd or Decimal("0") for p in payments if p.invoice_id in ids and p.status == "SUCCESS"),
        Decimal("0"),
    )
    return len(approved), len(rejected), paid
