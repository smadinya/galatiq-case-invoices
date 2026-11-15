"""DESIGN §14.2 demo story through the dashboard: batch, review queue, ledger, process, all offline.

One AppTest session on one runtime (temp databases, scripted model, inline jobs), driven page by page
the way a person would. The expected numbers come from `golden_expected`, not from this file.
"""

from decimal import Decimal as D

import pytest

from golden_expected import BUDGET_LIMIT, GOLDEN_LEDGER, GOLDEN_TOTALS, POST_BATCH_STEPS
from models import Status
from ui.kit import ledger_view as lv
from ui.kit.testing import app_test, make_runtime, nav_titles, open_page, page_text, widget

TIMEOUT = 60
ITEMS = ("WidgetA", "WidgetB", "GadgetX", "FakeItem")


def ok(at):
    assert not at.exception, [e.value for e in at.exception]
    return at


def plain(at) -> str:
    """Rendered text of the page and sidebar with Markdown escapes removed."""
    return page_text(at).replace("\\\\", "\\").replace("\\$", "$")


def sidebar_text(at) -> str:
    return "\n".join(str(n.proto) for n in at.sidebar if getattr(n, "proto", None) is not None).replace("\\", "")


def main_text(at) -> str:
    out = []
    for kind in ("markdown", "caption", "success", "warning", "error", "info", "header", "subheader", "title"):
        out += [e.value for e in getattr(at, kind, [])]
    return "\n".join(out).replace("\\", "")


def kv(at) -> dict[str, str]:
    import html
    import re

    rows = {}
    for el in at.get("html"):
        if 'class="k-kv"' in el.proto.body:
            for k, v in re.findall(r"<dt>(.*?)</dt><dd>(.*?)</dd>", el.proto.body):
                rows[html.unescape(k)] = html.unescape(v)
    return rows


def queue_item(rt, ref):
    with rt.reader(rt.default_config) as r:
        return next(i for i in r.queue("PENDING") if i.invoice_ref == ref)


def facts(rt):
    """Stock levels (in ITEMS order), January spend and the invoice's status after a step."""
    with rt.reader(rt.default_config) as r:
        stock = {i.item: i.stock for i in r.inventory()}
        return tuple(stock[i] for i in ITEMS), r.budget_status().spent, {i.file_name: i.status for i in r.invoices()}


@pytest.fixture(scope="module")
def story(tmp_path_factory):
    """Run the whole story once; the tests below read what it recorded."""
    rt = make_runtime(tmp_path_factory.mktemp("walkthrough"))
    at = app_test(rt, timeout=TIMEOUT)
    log: dict = {"rt": rt, "at": at, "steps": []}

    # 1. Batch page: run data/invoices
    open_page(at, "batch")
    ok(at)
    widget(at, "batch.run").click()
    at.run()
    ok(at)
    log["batch_headers"] = [h.value for h in at.header]
    log["batch_metrics"] = {m.label: m.value for m in at.metric}
    log["batch_statuses"] = [s[0] for s in at.dataframe[0].value["Status"]]
    log["batch_text"] = main_text(at)
    at.run()  # a rerun, so the sidebar and navigation read the finished batch
    ok(at)
    log["after_batch_sidebar"] = sidebar_text(at)
    log["after_batch_nav"] = nav_titles(at)

    # 2. Review queue: each POST_BATCH_STEPS step
    for ref, action, kwargs, expect in POST_BATCH_STEPS:
        ref = "INV-1004 R1" if ref == "INV-1004" else ref
        item = queue_item(rt, ref)
        open_page(at, "queue", item=str(item.invoice_id))
        ok(at)
        assert at.radio[0].value == item.invoice_id
        widget(at, "queue.action").set_value(action)
        widget(at, "queue.note").set_value(kwargs["note"])
        at.run()
        ok(at)
        if "raises" in expect:  # FxRateRequired: the page refuses before anything is submitted
            assert expect["raises"] == "FxRateRequired"
            assert widget(at, "queue.submit").disabled
            assert "Add an exchange rate to approve." in main_text(at)
            log["fx_gate_seen"] = True
            continue
        if "fx_rate" in kwargs:
            assert widget(at, "queue.submit").disabled  # still no rate typed
            widget(at, "queue.fx_rate").set_value(str(kwargs["fx_rate"]))
            at.run()
            ok(at)
        assert not widget(at, "queue.submit").disabled
        widget(at, "queue.submit").click()
        at.run()
        ok(at)
        stock, spent, statuses = facts(rt)
        with rt.reader(rt.default_config) as r:
            pay = [p for p in r.payments() if p.invoice_id == item.invoice_id]
            record = r.invoice(item.invoice_id)
        log["steps"].append((ref, expect, stock, spent, record.status, pay, main_text(at)))
    log["queue_final"] = main_text(at)
    log["queue_pending"] = widget_free_pending(rt)

    # 3. Ledger page
    open_page(at, "ledger")
    ok(at)
    log["ledger_text"] = main_text(at)
    log["ledger_tables"] = [df.value for df in at.get("dataframe")]
    with rt.reader(rt.default_config) as r:
        log["ledger_steps"] = lv.ledger_steps(r.stock_ledger(), r.budget_ledger(), r.invoices(), [i.item for i in r.inventory()])
        log["trace_1012"] = next(i.trace_id for i in r.invoices() if i.file_name == "invoice_1012.pdf")
    log["ledger_sidebar"] = sidebar_text(at)

    # 4. Process page for the 1012 trace
    open_page(at, "process", trace=log["trace_1012"])
    ok(at)
    log["process_text"] = plain(at)
    log["process_headers"] = [h.value for h in at.header]
    return log


def widget_free_pending(rt):
    with rt.reader(rt.default_config) as r:
        return r.queue("PENDING"), r.queue(None)


def test_batch_page_matches_the_golden_totals(story):
    counts = GOLDEN_TOTALS["counts"]
    statuses = story["batch_statuses"]
    assert statuses.count("Paid") == counts["PAID"] == 5
    assert statuses.count("Rejected") == counts["REJECTED"] == 9
    assert statuses.count("Needs a person") == counts["PENDING_HUMAN"] == 3
    assert statuses.count("Duplicate") == counts["DUPLICATE"] == 3
    assert len(statuses) == 20
    assert story["batch_headers"] == ["20 files\\: 5 paid, 9 rejected, 3 need a person, 3 duplicates"]
    assert story["batch_metrics"]["Decided without a person"] == "82%"
    assert "14 of 17 distinct documents decided without a person" in story["batch_text"]


def test_sidebar_shows_the_golden_budget_after_the_batch(story):
    assert "$19,140.00 of $50,000.00 spent" in story["after_batch_sidebar"]
    assert "$30,860.00" in story["after_batch_sidebar"]
    assert story["after_batch_nav"] == ["Process", "Review queue (3)", "Ledger", "Batch"]
    assert GOLDEN_TOTALS["spent"] == D("19140.00")


def test_each_queue_step_resolves_as_expected(story):
    steps = story["steps"]
    # the FxRateRequired step is checked in the fixture (submit disabled); the three payments remain
    assert [s[0] for s in steps] == ["INV-1012", "INV-1004 R1", "INV-1014"]
    for ref, expect, stock, spent, status, pay, text in steps:
        assert status == Status(expect["status"]), ref
        assert stock == expect["stock"], ref
        assert spent == expect["spent"], ref
        [payment] = pay
        assert payment.amount == expect["paid"] and payment.currency == expect.get("currency", "USD")
        if "fx_rate" in expect:
            assert payment.fx_rate == expect["fx_rate"] and payment.amount_usd == expect["amount_usd"]
    assert steps[-1][3] == D("37620.00")


def test_the_queue_ends_empty_with_the_all_resolved_summary(story):
    pending, everything = story["queue_pending"]
    assert pending == [] and len(everything) == 3
    assert "All decisions made. You approved 3 and rejected 0; $18,480.00 was paid." in story["queue_final"]
    assert story["fx_gate_seen"], "the 1014 step was refused until an exchange rate was entered"


def test_ledger_steps_match_the_golden_ledger_plus_the_queue_payments(story):
    steps = story["ledger_steps"]
    expected = [row[1:5] + (row[5],) for row in GOLDEN_LEDGER]
    expected += [(*e["stock"], e["spent"]) for _, _, _, e in POST_BATCH_STEPS if "raises" not in e]
    got = [(*(s.stock[i] for i in ITEMS), s.spent) for s in steps]
    assert got == [(*row[:4], row[4]) for row in expected]
    assert steps[-1].spent == D("37620.00") < BUDGET_LIMIT
    assert [s.ref for s in steps][-3:] == ["INV-1012", "INV-1004 R1", "INV-1014"]
    text = story["ledger_text"]
    assert "$37,620.00 paid of the $50,000.00 monthly limit, $12,380.00 left." in text
    assert "8 payments, none failed." in text
    payments, movements = story["ledger_tables"][:2]
    assert len(payments) == 8
    assert sorted(payments["Currency"]) == ["EUR"] + ["USD"] * 7
    assert D(str(payments["USD"].iloc[-1])) == D("4455.00")
    assert D(str(payments["USD"].sum())) == D("37620.00")


def test_process_for_1012_shows_the_vendor_renamed_flag(story):
    text = story["process_text"]
    assert "Vendor renamed" in text
    assert "formerly FastShip Ltd." in text
    assert story["process_headers"], "the case header renders"
