import html
import threading
from decimal import Decimal as D

import pytest

from db.repo import PaymentWrite
from models import FlagCode
from service import Service
from tests.golden.golden_expected import POST_BATCH_STEPS
from ui.kit.runtime import UiConfig
from ui.kit.testing import app_test, make_runtime, open_page, widget


def pending(rt):
    with rt.reader(rt.default_config) as r:
        return r.queue("PENDING")


def item_id(rt, ref):
    return next(i.invoice_id for i in pending(rt) if i.invoice_ref == ref)


def kv(at):
    """The preview's label/value rows, from the html blocks."""
    out = {}
    for el in at.get("html"):
        body = el.proto.body
        if 'class="k-kv"' in body:
            import re
            for k, v in re.findall(r"<dt>(.*?)</dt><dd>(.*?)</dd>", body):
                out[html.unescape(k)] = html.unescape(v)
    return out


def texts(at):
    out = []
    for kind in ("markdown", "caption", "success", "warning", "error", "info", "header", "subheader", "title", "badge"):
        out += [e.value for e in getattr(at, kind, [])]
    return "\n".join(out).replace("\\", "")


def go(rt, ref=None, **kw):
    at = app_test(rt)
    if ref is not None:
        kw["item"] = str(item_id(rt, ref))
    open_page(at, "queue", **kw)
    assert not at.exception, [e.value for e in at.exception]
    return at


def fill(at, note="Checked with the vendor", **kw):
    widget(at, "queue.note").set_value(note)
    for key, value in kw.items():
        widget(at, "queue." + key).set_value(value)
    at.run()
    assert not at.exception, [e.value for e in at.exception]


def submit(at):
    btn = widget(at, "queue.submit")
    assert not btn.disabled
    btn.click()
    at.run()
    assert not at.exception, [e.value for e in at.exception]


def spend_budget(rt, usd, stock=None):
    with Service(rt.settings, provider=None, env={}) as svc:
        iid = svc.repo.create_invoice(trace_id="t-spend", file_path="p", file_name="spend.json", file_hash="hh-spend")
        svc.repo.record_payment(PaymentWrite(
            idempotency_key="k-spend", invoice_id=iid, trace_id="t-spend", vendor="v", amount=usd, currency="USD",
            amount_usd=usd, attempts=1, month="2026-01", stock_deltas=stock or {}, bank_response={}))


def test_three_items_in_order(ui_rt_batch):
    at = go(ui_rt_batch)
    assert [o.replace("\\", "") for o in at.radio[0].options] == [
        "INV-1004 R1: Revision of a paid invoice", "INV-1012: Vendor renamed", "INV-1014: Foreign currency"]
    assert [c.replace("\\", "") for c in at.radio[0].proto.captions] == [
        "Precision Parts Ltd., $5,940.00", "QuickShip Distributers, $9,975.00", "TechParts International, €4,125.00"]
    assert "3 invoices need a decision." in texts(at)
    assert at.radio[0].value == pending(ui_rt_batch)[0].invoice_id


def test_1012_detail_shows_master_name_and_stock(ui_rt_batch):
    at = go(ui_rt_batch, "INV-1012")
    text = texts(at)
    assert "INV-1012: vendor renamed" in text
    assert "was formerly FastShip Ltd." in text
    rows = kv(at)
    assert rows["On file as"] == "FastShip Ltd."
    assert rows["Pays"] == "$9,975.00 to QuickShip Distributers"
    assert rows["January budget"] == "$19,140.00 → $29,115.00 of $50,000.00"
    assert rows["Stock"] == "WidgetA 56 → 44, WidgetB 32 → 25, GadgetX 11 → 7"
    assert rows["Also"] == "QuickShip Distributers is recorded as a new name for FastShip Ltd."
    assert "VP and Critic" in text and "Leave it to a person" in text
    assert "VP's reasoning, as written" in text
    assert "Other flags:" in text
    assert widget(at, "queue.note").placeholder.startswith("Called FastShip Ltd. on the number in our records")


def test_revision_preview_shows_only_the_added_stock(ui_rt_batch):
    at = go(ui_rt_batch, "INV-1004 R1")
    rows = kv(at)
    assert rows["Stock"] == "GadgetX 11 → 6"
    assert "WidgetA" not in rows["Stock"] and "," not in rows["Stock"]
    assert rows["Pays"].startswith("only the difference: $4,050.00 (INV-1004 was paid $1,890.00)")


def test_approving_1012_pays_and_stores_the_alias(ui_rt_batch):
    at = go(ui_rt_batch, "INV-1012")
    assert widget(at, "queue.submit").disabled
    assert "Add a note to enable the button." in texts(at)
    assert widget(at, "queue.submit").label == "Approve and pay $9,975.00"
    fill(at)
    submit(at)
    text = texts(at)
    assert "Paid $9,975.00 to QuickShip Distributers." in text
    assert "January spending is now $29,115.00." in text
    assert "QuickShip Distributers is now recorded as a new name for FastShip Ltd." in text
    assert widget(at, "queue.next").label == "Next: INV-1004 R1"
    with ui_rt_batch.reader(ui_rt_batch.default_config) as r:
        names = {a.alias for a in r.vendor_aliases(r.invoice(item_id_all(r, "INV-1012")).vendor_id)}
        assert "QuickShip Distributers" in names
        assert [i.invoice_ref for i in r.queue("PENDING")] == ["INV-1004 R1", "INV-1014"]
    # Next moves on, Resolved lists it
    widget(at, "queue.next").click()
    at.run()
    assert not at.exception
    assert at.radio[0].value == item_id(ui_rt_batch, "INV-1004 R1")
    assert any("Resolved (1)" in e.label for e in at.expander)
    assert "INV-1012: Approved, Checked with the vendor" in texts(at)


def item_id_all(reader, ref):
    return next(i.invoice_id for i in reader.queue(None) if i.invoice_ref == ref)


def test_1014_needs_a_valid_rate(ui_rt_batch):
    at = go(ui_rt_batch, "INV-1014")
    fill(at)
    assert widget(at, "queue.submit").disabled
    assert "Add an exchange rate to approve." in texts(at)
    assert widget(at, "queue.fx_rate").label == "Exchange rate, EUR to USD"
    assert widget(at, "queue.fx_rate").placeholder == "for example 1.08"
    assert kv(at)["Outcome"] == "Enter a rate to see the dollar amount and the effect on the budget."
    fill(at, fx_rate="abc")
    assert widget(at, "queue.submit").disabled
    assert "Enter the rate as a plain number" in texts(at)
    fill(at, fx_rate="1.08")
    rows = kv(at)
    assert rows["Pays"] == "€4,125.00 = $4,455.00 at 1.08 to TechParts International"
    assert rows["January budget"] == "$19,140.00 → $23,595.00 of $50,000.00"
    assert widget(at, "queue.submit").label == "Approve and pay €4,125.00"
    submit(at)
    assert "Paid €4,125.00 to TechParts International." in texts(at)
    with ui_rt_batch.reader(ui_rt_batch.default_config) as r:
        pay = next(p for p in r.payments() if p.currency == "EUR")
        assert pay.amount == D("4125.00") and pay.amount_usd == D("4455.00") and pay.fx_rate == D("1.08")


def test_overspend_needs_the_checkbox(ui_rt_batch):
    spend_budget(ui_rt_batch, D("45000"))
    at = go(ui_rt_batch, "INV-1012")
    rows = kv(at)
    assert "Over budget by" in rows
    box = widget(at, "queue.confirm_overspend")
    assert box.label.startswith("Approve an overspend of")
    fill(at)
    assert widget(at, "queue.submit").disabled
    assert "Tick the overspend box to approve." in texts(at)
    box = widget(at, "queue.confirm_overspend")
    box.check()
    at.run()
    submit(at)
    assert "Paid $9,975.00 to QuickShip Distributers." in texts(at)


def test_a_refusal_reveals_the_overspend_box(ui_rt_batch, monkeypatch):
    """The service asks for confirmation although the preview did not (spending moved after it was read)."""
    real = ui_rt_batch.preview

    def stale(cfg, ref, fx):
        return real(cfg, ref, fx).model_copy(update={"needs_overspend_confirmation": False})

    monkeypatch.setattr(ui_rt_batch, "preview", stale)
    spend_budget(ui_rt_batch, D("45000"))
    at = go(ui_rt_batch, "INV-1012")
    assert "queue.confirm_overspend" not in [c.key for c in at.checkbox]
    fill(at)
    submit(at)
    assert not any("Paid" in s.value for s in at.success)
    assert "Tick the box to confirm the overspend." in texts(at)
    assert "Approving takes January spending to $74,115.00, $24,115.00 over the $50,000.00 limit." in texts(at)
    box = widget(at, "queue.confirm_overspend")
    assert widget(at, "queue.submit").disabled
    box.check()
    at.run()
    monkeypatch.undo()
    submit(at)
    assert "Paid $9,975.00 to QuickShip Distributers." in texts(at)


def test_reject_with_a_note(ui_rt_batch):
    at = go(ui_rt_batch, "INV-1012")
    widget(at, "queue.action").set_value("reject")
    at.run()
    assert widget(at, "queue.submit").label == "Reject invoice"
    fill(at, note="Vendor did not confirm")
    submit(at)
    text = texts(at)
    assert "Rejected. Your note: Vendor did not confirm" in text
    assert any("badge" in e.value and "Rejected" in e.value for e in at.markdown)
    with ui_rt_batch.reader(ui_rt_batch.default_config) as r:
        item = next(i for i in r.queue(None) if i.invoice_ref == "INV-1012")
        assert item.resolution == "reject"
        assert r.invoice(item.invoice_id).status.value == "REJECTED"


def make_unreadable(rt, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{")
    rt.start_invoice(rt.default_config, bad)


def test_unreadable_item_can_only_be_rejected(ui_rt, tmp_path):
    make_unreadable(ui_rt, tmp_path)
    at = go(ui_rt)
    text = texts(at)
    assert "bad.json: unreadable document" in text
    assert "This file couldn't be read reliably, so it can only be rejected." in text
    assert "No recommendation: the document couldn't be read" in text
    with pytest.raises(KeyError):
        widget(at, "queue.action")
    assert kv(at) == {}
    assert widget(at, "queue.submit").label == "Reject invoice"
    fill(at, note="Asked the vendor to resend")
    submit(at)
    assert "Rejected. Your note: Asked the vendor to resend" in texts(at)
    assert "All decisions made. You approved 0 and rejected 1; $0.00 was paid." in texts(at)


def test_empty_state_before_anything_was_queued(ui_rt):
    at = go(ui_rt)
    text = texts(at)
    assert "Nothing is waiting for a person. Invoices come here for six reasons only" in text
    assert [p.proto.label for p in at.get("page_link")] == ["Process an invoice", "Run a batch"]


def test_busy_blocks_the_form(tmp_path, golden_db):
    from ui.kit.testing import runtime_from_snapshot
    rt = runtime_from_snapshot(tmp_path / "busy", golden_db, inline=False)
    at = go(rt, "INV-1012")
    gate = threading.Event()
    job = rt.jobs.submit("invoice", "blocked", lambda ctx: gate.wait(20))
    try:
        at.run()
        assert not at.exception, [e.value for e in at.exception]
        assert "A run is in progress; resolving is available when it finishes." in texts(at)
        assert widget(at, "queue.submit").disabled and widget(at, "queue.note").disabled
    finally:
        gate.set()
        job.wait(20)


def test_item_resolved_in_the_background_is_no_longer_waiting(ui_rt_batch):
    at = go(ui_rt_batch, "INV-1012")
    with Service(ui_rt_batch.settings, provider=None, env={}) as svc:
        svc.resolve("INV-1012", action="reject", note="resolved elsewhere")
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert "INV-1012 is no longer waiting. It may have been resolved in another tab." in texts(at)
    assert at.radio[0].value == item_id(ui_rt_batch, "INV-1004 R1")


def test_stock_recheck_shows_the_error_and_records_a_rejection(ui_rt_batch):
    spend_budget(ui_rt_batch, D("1"), stock={"GadgetX": -8})  # 11 -> 3 left, 1004 R1 needs 5 more
    at = go(ui_rt_batch, "INV-1004 R1")
    assert any("Stock has run short since it was queued: GadgetX needs 5, 3 left." in e.value.replace("\\", "") for e in at.error)
    assert any("Approving records a rejection with that memo." in e.value.replace("\\", "") for e in at.error)
    assert widget(at, "queue.submit").label == "Approve"
    fill(at, note="PO amendment confirmed")
    submit(at)
    text = texts(at)
    assert "Not paid. Approval by a person could not be carried out" in text
    assert any("badge" in e.value and "Rejected" in e.value for e in at.markdown)
    with ui_rt_batch.reader(ui_rt_batch.default_config) as r:
        rec = next(i for i in r.queue(None) if i.invoice_ref == "INV-1004 R1")
        assert r.invoice(rec.invoice_id).status.value == "REJECTED"


def test_payment_failed_item_retries(ui_rt):
    cfg = UiConfig(ui_rt.default_config.as_of, payment_fail_rate=1.0)
    ui_rt.start_invoice(cfg, ui_rt.samples_dir / "invoice_1001.txt")
    at = go(ui_rt)
    assert "The bank call failed 4 times" in texts(at)
    assert kv(at)["Attempts"] == "4"
    assert kv(at)["Also"] == "The payment is tried again."


def test_post_batch_walk_through_ends_all_resolved(ui_rt_batch):
    at = go(ui_rt_batch)
    for ref, action, kwargs, expect in POST_BATCH_STEPS:
        if "raises" in expect:
            continue
        ref = ref.replace("INV-1004", "INV-1004 R1")
        pick = item_id(ui_rt_batch, ref)
        at.query_params["item"] = str(pick)
        at.run()
        assert at.radio[0].value == pick
        extra = {}
        if "fx_rate" in kwargs:
            extra["fx_rate"] = str(kwargs["fx_rate"])
        fill(at, note=kwargs["note"], **extra)
        submit(at)
        assert "Paid " in texts(at) or "Approved" in texts(at)
    text = texts(at)
    assert "All decisions made. You approved 3 and rejected 0; $18,480.00 was paid." in text
    assert "See the payments in the ledger" in [p.proto.label for p in at.get("page_link")]
    assert not pending(ui_rt_batch)
    assert any("Resolved (3)" in e.label for e in at.expander)


def test_a_fresh_visit_to_a_finished_queue_still_lists_the_resolved_items(ui_rt_batch):
    with Service(ui_rt_batch.settings, provider=None, env={}) as svc:
        for ref in ("INV-1004 R1", "INV-1012"):
            svc.resolve(ref, action="reject", note="not needed")
        svc.resolve("INV-1014", action="reject", note="not needed")
    at = go(ui_rt_batch)
    assert "All decisions made. You approved 0 and rejected 3" in texts(at)
    [exp] = [e for e in at.expander if e.label.startswith("Resolved")]
    assert exp.label == "Resolved (3)"
