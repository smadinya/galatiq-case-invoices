from decimal import Decimal as D

import pytest

from db.repo import PaymentWrite
from models import Ceiling, FlagCode, ResolutionPreview, Status
from service import (
    AmbiguousReference, ApproveNotAllowed, FxRateRequired, NotFound, NoteRequired,
    OverspendConfirmationRequired, ResolutionRefused, Service,
)
from ui.kit import queue_view as qv
from ui.kit.runtime import UiConfig


def load(rt, ref):
    """(item, record, state, vendor, aliases, payments, budget) for a pending item."""
    with rt.reader(rt.default_config) as r:
        item = next(i for i in r.queue("PENDING") if i.invoice_ref == ref)
        record = r.invoice(item.invoice_id)
        vendor = r.vendor(record.vendor_id) if record.vendor_id else None
        aliases = r.vendor_aliases(vendor.id) if vendor else []
        return (item, record, r.invoice_state(item.trace_id), vendor, aliases, r.payments(), r.budget_status())


def evidence(rt, ref, **kw):
    item, record, state, vendor, aliases, payments, budget = load(rt, ref)
    kw.setdefault("budget", budget)
    kw.setdefault("aliases", aliases)
    return item, qv.reason_evidence(item, record, state, vendor, payments, **kw)


def preview_of(rt, ref, fx=None):
    return rt.preview(rt.default_config, load(rt, ref)[0].trace_id, fx)


def pv(**kw):
    base = dict(
        invoice_ref="INV-1", reason=FlagCode.FOREIGN_CURRENCY, can_approve=True, needs_fx_rate=False,
        budget={"month": "2026-01", "limit": D("50000"), "spent": D("0"), "remaining": D("50000")},
        approve_outcome="pay", payment_amount=D("100"), currency="USD", payment_amount_usd=D("100"),
    )
    base.update(kw)
    return ResolutionPreview(**base)


# ------------------------------------------------------------------ form_state


def test_a_note_is_required():
    fs = qv.form_state(pv(), action="approve", note="  ", fx_text="", confirm=False)
    assert not fs.can_submit and fs.problems == ("note",)
    assert qv.form_state(pv(), action="approve", note="ok", fx_text="", confirm=False).can_submit


def test_approve_is_blocked_for_an_unreadable_file():
    p = pv(can_approve=False, approve_outcome="blocked", payment_amount=None)
    assert not qv.form_state(p, action="approve", note="n", fx_text="", confirm=False).can_submit
    fs = qv.form_state(p, action="reject", note="n", fx_text="", confirm=False)
    assert fs.can_submit and not fs.approve_allowed


def test_fx_rate_is_needed_and_must_be_valid():
    p = pv(
        needs_fx_rate=True, currency="EUR", approve_outcome="blocked", payment_amount_usd=None)
    for bad in ("", "abc", "0", "-1", "nan", "1,08"):
        fs = qv.form_state(p, action="approve", note="n", fx_text=bad, confirm=False)
        assert not fs.can_submit and fs.problems == ("rate",) and fs.fx_rate is None
    fs = qv.form_state(p, action="approve", note="n", fx_text=" 1.08 ", confirm=False)
    assert fs.can_submit and fs.fx_rate == D("1.08") and fs.needs_fx


def test_overspend_needs_the_box():
    p = pv(needs_overspend_confirmation=True, overspend_usd=D("5"))
    fs = qv.form_state(p, action="approve", note="n", fx_text="", confirm=False)
    assert not fs.can_submit and fs.problems == ("confirm",) and fs.needs_confirm
    assert qv.form_state(p, action="approve", note="n", fx_text="", confirm=True).can_submit
    forced = qv.form_state(pv(), action="approve", note="n", fx_text="", confirm=False, force_confirm=True)
    assert forced.problems == ("confirm",)


def test_reject_never_needs_a_rate_or_the_box():
    p = pv(needs_fx_rate=True, needs_overspend_confirmation=True, overspend_usd=D("5"))
    fs = qv.form_state(p, action="reject", note="n", fx_text="", confirm=False)
    assert fs.can_submit and fs.problems == ()


def test_no_preview_blocks_everything():
    fs = qv.form_state(None, action="reject", note="n", fx_text="1.1", confirm=True)
    assert not fs.can_submit and fs.fx_rate == D("1.1")


def test_missing_captions_name_what_is_missing():
    assert qv.missing_caption(("note",)) == "Add a note to enable the button."
    assert qv.missing_caption(("note", "rate")) == "Add a note and an exchange rate to approve."
    assert qv.missing_caption(("rate",)) == "Add an exchange rate to approve."
    assert qv.missing_caption(("confirm",)) == "Tick the overspend box to approve."
    assert qv.missing_caption(("note", "confirm")) == "Add a note and tick the overspend box to approve."
    assert qv.missing_caption(("note", "rate", "confirm")) == (
        "Add a note and an exchange rate and tick the overspend box to approve.")
    assert qv.missing_caption(("note",), "reject") == "Add a note to enable the button."
    assert qv.missing_caption(()) == ""


def test_submit_labels():
    assert qv.submit_label(pv(), "approve") == "Approve and pay $100.00"
    assert qv.submit_label(pv(currency="EUR", payment_amount=D("4125")), "approve") == "Approve and pay €4,125.00"
    assert qv.submit_label(pv(approve_outcome="credit_due", payment_amount=D("-50")), "approve") == (
        "Approve (credit due, nothing to pay)")
    assert qv.submit_label(pv(approve_outcome="reject_stock"), "approve") == "Approve"
    assert qv.submit_label(pv(), "reject") == "Reject invoice"
    assert qv.submit_label(None, "approve") == "Approve"


# --------------------------------------------------------------- reason evidence


def test_vendor_renamed_evidence(ui_rt_batch):
    item, ev = evidence(ui_rt_batch, "INV-1012")
    assert ev.title == "Vendor renamed"
    assert ev.explanation == (
        "The invoice says QuickShip Distributers was formerly FastShip Ltd. Confirm the change with "
        "FastShip Ltd. using contact details already on file, not ones printed on this invoice.")
    assert dict(ev.facts)["On file as"] == "FastShip Ltd."
    assert dict(ev.facts)["Terms on file"] == "Net 30"


def test_revision_evidence(ui_rt_batch):
    item, ev = evidence(ui_rt_batch, "INV-1004 R1")
    assert ev.explanation == (
        "INV-1004 was paid $1,890.00. This revision adds 5 GadgetX. Approving pays only the difference.")
    facts = dict(ev.facts)
    assert facts["Original file"] == "invoice_1004.json"
    assert facts["Paid to date"] == "$1,890.00" and facts["Added quantities"] == "5 GadgetX"
    assert facts["Difference payable"] == "$4,050.00"


def test_foreign_currency_evidence(ui_rt_batch):
    item, ev = evidence(ui_rt_batch, "INV-1014")
    assert ev.explanation == "The invoice is in EUR. Exchange rates move, so a person sets the rate."
    assert dict(ev.facts) == {"Amount": "€4,125.00", "Currency": "EUR"}


def test_conflicting_number_and_over_budget_evidence(ui_rt_batch):
    item, record, state, vendor, aliases, payments, budget = load(ui_rt_batch, "INV-1012")
    conflict = item.model_copy(update={"reason": FlagCode.CONFLICTING_INVOICE_NUMBER})
    ev = qv.reason_evidence(conflict, record, state, vendor, payments)
    assert ev.title == "Same number, different content"
    assert ev.explanation == "QuickShip Distributers sent another INV-1012 with different content and no revision mark."
    over = item.model_copy(update={"reason": FlagCode.OVER_BUDGET})
    ev = qv.reason_evidence(over, record, state, vendor, payments, budget=budget)
    assert ev.title == "Over monthly budget"
    assert ev.explanation == (
        "Paying $9,975.00 takes January spending to $29,115.00, $0.00 over the $50,000.00 limit. "
        "Only a person can authorise that.")
    assert dict(ev.facts) == {"Spent": "$19,140.00", "Limit": "$50,000.00", "Overspend": "$0.00"}
    # without the month's figures it falls back to the plain flag sentence
    assert qv.reason_evidence(over, record, state, vendor, payments).explanation


def test_payment_failed_evidence(ui_rt):
    cfg = UiConfig(ui_rt.default_config.as_of, payment_fail_rate=1.0)
    ui_rt.start_invoice(cfg, ui_rt.samples_dir / "invoice_1001.txt")
    item, ev = evidence(ui_rt, "INV-1001")
    assert ev.title == "Payment failed"
    assert ev.explanation.startswith("The bank call failed 4 times: ") and ev.explanation.endswith(".")
    facts = dict(ev.facts)
    assert facts["Attempts"] == "4" and facts["Error"]


def test_unreadable_evidence(ui_rt, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{")
    ui_rt.start_invoice(ui_rt.default_config, bad)
    item, ev = evidence(ui_rt, "bad.json")
    assert ev.title == "Unreadable document"
    assert ev.explanation.startswith("The file couldn't be read reliably: ")
    assert ev.explanation.endswith("There's nothing safe to pay from.")
    assert dict(ev.facts)["Error"]


# ----------------------------------------------------------------------- preview


def test_preview_rows_for_the_three_demo_items(ui_rt_batch):
    item = load(ui_rt_batch, "INV-1012")[0]
    state = load(ui_rt_batch, "INV-1012")[2]
    rows = dict(qv.preview_rows(preview_of(ui_rt_batch, "INV-1012"), item, state))
    assert list(rows) == ["Pays", "January budget", "Stock", "Also"]
    assert rows["Stock"] == "WidgetA 56 → 44, WidgetB 32 → 25, GadgetX 11 → 7"

    item, _, state, *_ = load(ui_rt_batch, "INV-1004 R1")
    rows = dict(qv.preview_rows(preview_of(ui_rt_batch, "INV-1004 R1"), item, state))
    assert rows["Pays"] == "only the difference: $4,050.00 (INV-1004 was paid $1,890.00) to Precision Parts Ltd."
    assert rows["Stock"] == "GadgetX 11 → 6"

    item, _, state, *_ = load(ui_rt_batch, "INV-1014")
    rows = qv.preview_rows(preview_of(ui_rt_batch, "INV-1014"), item, state)
    assert rows[-1] == ("Outcome", qv.BLOCKED)
    rows = dict(qv.preview_rows(preview_of(ui_rt_batch, "INV-1014", D("1.08")), item, state))
    assert rows["Pays"] == "€4,125.00 = $4,455.00 at 1.08 to TechParts International"
    assert rows["January budget"] == "$19,140.00 → $23,595.00 of $50,000.00"


def test_preview_rows_credit_and_unreadable():
    item = None
    base = pv(approve_outcome="credit_due", payment_amount=D("-250"))
    from models import QueueItem, HumanReview
    import datetime as dt
    item = QueueItem(
        invoice_id=1, trace_id="t", invoice_ref="INV-1", invoice_number="INV-1", revision=None, file_name="f.json",
        vendor="Acme", reason=FlagCode.FOREIGN_CURRENCY, review=HumanReview(
            reason=FlagCode.FOREIGN_CURRENCY, reasons=[FlagCode.FOREIGN_CURRENCY]), status="PENDING",
        created_at=dt.datetime(2026, 1, 1))
    assert qv.preview_rows(base, item) == [("Outcome", "Nothing to pay. A credit of $250.00 is due from the vendor.")]
    assert qv.preview_rows(pv(can_approve=False), item) == []
    assert qv.preview_rows(pv(approve_outcome="reject_stock"), item) == []


def test_over_budget_row_and_label():
    p = pv(
        budget={"month": "2026-01", "limit": D("50000"), "spent": D("45000"), "remaining": D("5000")},
        payment_amount=D("9975"), payment_amount_usd=D("9975"), overspend_usd=D("4975"),
        needs_overspend_confirmation=True)
    import datetime as dt
    from models import QueueItem, HumanReview
    item = QueueItem(
        invoice_id=1, trace_id="t", invoice_ref="INV-1", invoice_number="INV-1", revision=None, file_name="f.json",
        vendor="Acme", reason=FlagCode.OVER_BUDGET, review=HumanReview(
            reason=FlagCode.OVER_BUDGET, reasons=[FlagCode.OVER_BUDGET]), status="PENDING",
        created_at=dt.datetime(2026, 1, 1))
    rows = dict(qv.preview_rows(p, item))
    assert rows["Over budget by"] == "$4,975.00"
    assert rows["January budget"] == "$45,000.00 → $54,975.00 of $50,000.00"
    assert qv.overspend_label(p) == (
        "Approve an overspend of $4,975.00. January spending becomes $54,975.00 against the $50,000.00 limit.")


# --------------------------------------------------------------------- messages


def test_refusal_messages_cover_every_subclass(ui_rt_batch):
    budget = load(ui_rt_batch, "INV-1012")[6]
    assert qv.refusal_message(NoteRequired("x")) == "Add a note saying what you checked. It goes in the audit trail."
    assert qv.refusal_message(FxRateRequired("x"), currency="EUR") == (
        "Enter the EUR to USD rate to approve. Rejecting doesn't need one.")
    assert qv.refusal_message(FxRateRequired("x")).startswith("Enter the exchange rate to USD to approve.")
    assert qv.refusal_message(OverspendConfirmationRequired("x", D("4975"), budget)) == (
        "Approving takes January spending to $54,975.00, $4,975.00 over the $50,000.00 limit. "
        "Tick the box to confirm the overspend.")
    assert qv.refusal_message(ApproveNotAllowed("x")) == (
        "This file couldn't be read reliably, so it can only be rejected.")
    gone = "INV-1012 is no longer waiting. It may have been resolved in another tab. The list has been refreshed."
    assert qv.refusal_message(NotFound("x"), ref="INV-1012") == gone
    assert qv.refusal_message(AmbiguousReference("x", ["a", "b"]), ref="INV-1012") == gone
    assert "no longer waiting" in qv.refusal_message(NotFound("x"))
    assert qv.refusal_message(ResolutionRefused("plain")) == "plain"


def resolve(rt, ref, **kw):
    with Service(rt.settings, provider=None, env={}) as svc:
        return svc.resolve(ref, **kw)


def test_outcome_messages(ui_rt_batch):
    paid = resolve(ui_rt_batch, "INV-1012", action="approve", note="n")
    kind, text = qv.outcome_message(paid, spent=D("29115"), month="2026-01")
    assert kind == "success"
    assert text == (
        "Paid $9,975.00 to QuickShip Distributers. Receipt 8:INV-1012:R0. January spending is now $29,115.00. "
        "QuickShip Distributers is now recorded as a new name for FastShip Ltd.")

    eur = resolve(ui_rt_batch, "INV-1014", action="approve", note="n", fx_rate=D("1.08"))
    assert qv.outcome_message(eur)[1].startswith("Paid €4,125.00 to TechParts International. Receipt ")

    rejected = resolve(ui_rt_batch, "INV-1004", action="reject", note="Not confirmed")
    assert qv.outcome_message(rejected) == ("info", "Rejected. Your note: Not confirmed")

    credit = paid.model_copy(update={"status": Status.CREDIT_DUE, "payment": None, "vendor": "Precision Parts Ltd.",
                                     "currency": "USD"})
    credit = credit.model_copy(update={"decision": paid.decision.model_copy(update={"payment_amount": D("-250")})})
    assert qv.outcome_message(credit) == (
        "success", "Approved. Nothing to pay; a credit of $250.00 is due from Precision Parts Ltd.")

    stock = rejected.model_copy(update={"human": rejected.human.model_copy(
        update={"outcome": "rejected_stock", "outcome_note": "stock is insufficient (GadgetX: needed 5, available 3)"})})
    assert qv.outcome_message(stock) == ("info", "Not paid. stock is insufficient (GadgetX: needed 5, available 3)")

    requeued = rejected.model_copy(update={"status": Status.PENDING_HUMAN, "human": rejected.human.model_copy(
        update={"outcome": "requeued", "outcome_note": "resolution could not be applied: KeyError"})})
    assert qv.outcome_message(requeued) == (
        "warning", "Not applied: resolution could not be applied: KeyError. The invoice is still waiting.")

    again = requeued.model_copy(update={"human": requeued.human.model_copy(update={"outcome": None}),
                                        "error": "bank unreachable"})
    assert qv.outcome_message(again) == ("warning", "The payment failed again: bank unreachable. The invoice is still waiting.")

    error = paid.model_copy(update={"status": Status.ERROR, "error": "TypeError: boom"})
    assert qv.outcome_message(error) == ("error", "System error: nothing was paid. TypeError: boom")


def test_resolved_lines_and_summary(ui_rt_batch):
    resolve(ui_rt_batch, "INV-1012", action="approve", note="Called FastShip on the number on file; rename confirmed")
    resolve(ui_rt_batch, "INV-1004", action="reject", note="no")
    with ui_rt_batch.reader(ui_rt_batch.default_config) as r:
        done = [i for i in r.queue(None) if i.status != "PENDING"]
        lines = sorted(qv.resolved_line(i) for i in done)
        assert lines[0] == "INV-1004 R1: Rejected, no"
        assert lines[1].startswith("INV-1012: Approved, Called FastShip on the number")
        assert qv.queue_summary(done, r.payments()) == (1, 1, D("9975.00"))
