import datetime as dt
from decimal import Decimal as D
from pathlib import Path

import pytest

import graph
from config.settings import REPO_ROOT, load_settings
from llm.fake import ScriptedFake
from llm.fake_golden import golden_script
from models import (
    Ceiling, Decision, FlagCode, InvoiceResult, InvoiceState, Severity, Status, make_flag, Receipt,
    HumanReview, HumanResolution,
)
from obs import JsonlLogger
from service import Service
from ui.kit import case, copy, theme

SAMPLES = REPO_ROOT / "data" / "invoices"
FILES = ["invoice_1001.txt", "invoice_1007.csv", "invoice_1009.json", "invoice_1010.txt",
         "invoice_1012.pdf", "invoice_1014.xml"]


def build_cases(root: Path) -> dict[str, tuple[InvoiceResult, InvoiceState]]:
    base = load_settings(env={})
    settings = base.model_copy(update={
        "db_path": root / "acme.db", "checkpoint_path": root / "checkpoints.db",
        "log_dir": root / "logs", "replay_dir": root / "replay",
    })
    svc = Service(settings, provider=ScriptedFake(golden_script()), as_of=dt.date(2026, 1, 31),
                  sleep=lambda s: None, log=JsonlLogger(settings.log_dir))
    out = {}
    try:
        for name in FILES:
            r = svc.run_invoice(SAMPLES / name)
            vals = svc.graph.get_state({"configurable": {"thread_id": r.trace_id}}).values
            out[name] = (r, InvoiceState.model_validate(vals))
    finally:
        svc.close()
    return out


@pytest.fixture(scope="module")
def cases(tmp_path_factory):
    return build_cases(tmp_path_factory.mktemp("kit"))


def row(rows, path):
    return next(r for r in rows if r.path == path)


def codes(r):
    return {f.code for f in r.flags}


def test_1012_vendor_and_corrections(cases):
    res, st = cases["invoice_1012.pdf"]
    rows = case.field_rows(res, st)
    v = row(rows, "vendor.name")
    assert FlagCode.VENDOR_RENAMED in codes(v) and v.severity == Severity.HUMAN
    assert any("QuickShip" in e for e in v.evidence)
    assert row(rows, "date").corrected
    assert row(rows, "line_items[1].amount").corrected
    assert row(rows, "vendor.former_name").value == "FastShip Ltd."
    lines = case.source_lines(st.raw_text, rows)
    assert any(l.severity == Severity.HUMAN and "QuickShip" in l.text for l in lines)


def test_1010_price_and_chat(cases):
    res, st = cases["invoice_1010.txt"]
    rows = case.field_rows(res, st)
    assert FlagCode.PRICE_ABOVE_CATALOG in codes(row(rows, "line_items[3].unit_price"))
    turns = case.chat_turns(res.transcript, res.decision, res.human)
    assert [(t.actor, t.verdict) for t in turns] == [
        ("vp", "APPROVE"), ("critic", "OBJECT"), ("vp", "REJECT"), ("critic", "AGREE")]
    assert "Rush note is untrusted vendor text" in turns[1].points[0][1]
    assert turns[1].proposes == Ceiling.REJECT
    assert turns[2].answered
    lines = case.source_lines(st.raw_text, rows)
    flagged = [l for l in lines if l.severity is not None]
    assert len(flagged) == 1 and "rush order" in flagged[0].text and flagged[0].number == 14


def test_1007_total(cases):
    res, st = cases["invoice_1007.csv"]
    assert FlagCode.MATH_MISMATCH in codes(row(case.field_rows(res, st), "total"))


def test_1009_negative_and_missing_vendor(cases):
    res, st = cases["invoice_1009.json"]
    rows = case.field_rows(res, st)
    assert FlagCode.NON_POSITIVE_QUANTITY in codes(row(rows, "line_items[0].quantity"))
    vendor = row(rows, "vendor.name")
    assert FlagCode.MISSING_VENDOR in codes(vendor) and vendor.value is None
    assert FlagCode.INVALID_DUE_DATE in codes(row(rows, "due_date"))


def test_1014_currency(cases):
    res, st = cases["invoice_1014.xml"]
    assert FlagCode.FOREIGN_CURRENCY in codes(row(case.field_rows(res, st), "currency"))


def test_1001_clean(cases):
    res, st = cases["invoice_1001.txt"]
    rows = case.field_rows(res, st)
    assert all(r.severity is None for r in rows)
    lines = case.source_lines(st.raw_text, rows)
    assert any(l.paths for l in lines) and all(l.severity is None for l in lines)


def test_state_none_falls_back(cases):
    res, _ = cases["invoice_1012.pdf"]
    rows = case.field_rows(res, None)
    v = row(rows, "vendor.name")
    assert v.value == "QuickShip Distributers" and v.evidence == () and v.raw is None
    assert FlagCode.VENDOR_RENAMED in codes(v)


def test_sorted_flags_stable():
    flags = [make_flag(FlagCode.PAST_DUE, "a"), make_flag(FlagCode.FOREIGN_CURRENCY, "b"),
             make_flag(FlagCode.MISSING_VENDOR, "c"), make_flag(FlagCode.UNKNOWN_VENDOR, "d"),
             make_flag(FlagCode.NEAR_THRESHOLD, "e")]
    assert [f.message for f in case.sorted_flags(flags)] == ["c", "b", "d", "a", "e"]


def test_policy_notices_and_sentences(cases):
    res, _ = cases["invoice_1010.txt"]
    assert case.policy_notices(res) == []
    assert case.path_sentence(res) == "VP and Critic, 2 rounds."
    clamped = res.model_copy(update={
        "ceiling": Ceiling.HUMAN,
        "decision": Decision(proposed=Ceiling.APPROVE, final=Ceiling.HUMAN, by="vp", reasoning="x", clamped=True)})
    assert case.policy_notices(clamped) == [
        "Capped by the rules: the VP proposed Approve, but the ceiling is Person decides."]
    dead = res.model_copy(update={"decision": Decision(final=Ceiling.REJECT, by="deadlock", reasoning="x")})
    assert "No agreement after 3 rounds" in case.policy_notices(dead)[0]


def test_reason_text(cases):
    res, st = cases["invoice_1012.pdf"]
    assert "formerly FastShip Ltd." in case.reason_text(res, st)
    res, st = cases["invoice_1014.xml"]
    assert case.reason_text(res, st).startswith("The invoice is in EUR")


# ------------------------------------------------------------------ copy and theme


def test_copy_coverage():
    assert set(copy.FLAG_COPY) == set(FlagCode)
    from models import FLAG_SEVERITY
    human = {c for c, s in FLAG_SEVERITY.items() if s == Severity.HUMAN}
    assert set(copy.REASON_COPY) == human
    assert set(copy.NODE_STAGE) == set(graph.NODES) == set(copy.NODE_LABEL)
    assert copy.HIDDEN_NODES <= set(graph.NODES)
    assert {n.name for n in SAMPLES.iterdir()} == set(copy.SAMPLE_GUIDE)
    assert set(theme.STATUS) == set(Status)
    assert set(theme.SEVERITY) == set(Severity)
    assert set(theme.CEILING) == set(Ceiling)
    for k in ("title.process", "button.process", "empty.process", "empty.queue", "empty.queue_done",
              "empty.ledger", "empty.batch", "case.no_flags", "case.no_lines", "error.no_model_sidebar",
              "error.system", "info.stored", "info.rerun_batch", "warn.folder_missing", "warn.folder_empty"):
        assert k in copy.TEXT


def test_text_ids_documented_and_formattable():
    doc = copy.__doc__
    for key in copy.TEXT:
        assert key.split(" ")[0] in doc, key
    for key in copy.TEXT:
        assert "!" not in copy.TEXT[key], key
        assert all(ord(c) < 0x2200 or c in "−" for c in copy.TEXT[key]), key  # no emoji


def test_reason_templates_format():
    fields = dict(currency="EUR", amount="1.00", usd="$1.08", original_ref="INV-1", paid_to_date="$1.00",
                  added="5 X", difference="$2.00", vendor="V", number="INV-2", month="January",
                  after="$2", over="$1", limit="$3", former="F", error="e")
    for rc in copy.REASON_COPY.values():
        rc.why.format(**fields), rc.approve_does.format(**fields), rc.note_placeholder.format(**fields)


def _base(**kw):
    d = dict(trace_id="t", file="invoice_9.txt", invoice_number="INV-9", vendor="Acme Inc.",
             status=Status.PAID, provider="fake", amount=D("100"), currency="USD")
    d.update(kw)
    return InvoiceResult(**d)


def test_headline_and_why(cases):
    paid = cases["invoice_1001.txt"][0]
    assert copy.headline(paid) == "Paid $5,000.00 to Widgets Inc."
    assert copy.why(paid) == "Paid $5,000.00"
    rules = cases["invoice_1009.json"][0]
    assert copy.headline(rules).startswith("Rejected: zero or negative quantity")
    assert copy.why(rules).startswith("Rules: zero or negative quantity")
    vp = cases["invoice_1010.txt"][0]
    assert copy.headline(vp) == "Rejected by the VP and Critic: price above catalog"
    assert copy.why(vp) == "VP and Critic: price above catalog"
    pend = cases["invoice_1012.pdf"][0]
    assert copy.headline(pend) == "Waiting for a person: vendor renamed"
    assert copy.why(pend) == "Vendor renamed"
    dup = _base(status=Status.DUPLICATE, duplicate_of="invoice_1011.pdf")
    assert copy.headline(dup) == "Duplicate of invoice_1011.pdf. Not paid again."
    assert copy.why(dup) == "Same invoice as invoice_1011.pdf"
    err = _base(status=Status.ERROR, error="E" * 100)
    assert copy.headline(err) == "System error: nothing was paid"
    assert copy.why(err) == "E" * 80
    fx = _base(payment=Receipt(idempotency_key="k", vendor="V", amount=D("4125"), currency="EUR", fx_rate=D("1.0800"),
                               amount_usd=D("4455"), attempts=3, paid_at=dt.datetime(2026, 1, 1), bank_response={}))
    assert copy.why(fx) == "Paid €4,125.00 at 1.08, 3 attempts"
    person = _base(status=Status.REJECTED, decision=Decision(final=Ceiling.REJECT, by="human", reasoning="n"),
                   human=HumanReview(reason=FlagCode.FOREIGN_CURRENCY, reasons=[FlagCode.FOREIGN_CURRENCY],
                                     resolution=HumanResolution(action="reject", note="Vendor could not confirm",
                                                                resolved_at=dt.datetime(2026, 1, 1)),
                                     outcome="rejected"))
    assert copy.headline(person) == "Rejected by a person"
    assert copy.why(person) == "Person: Vendor could not confirm"
    stock = person.model_copy(update={"human": person.human.model_copy(update={"outcome": "rejected_stock"})})
    assert copy.headline(stock) == "Rejected: stock ran short before approval"
    clamp = _base(status=Status.REJECTED, decision=Decision(final=Ceiling.REJECT, by="vp", reasoning="x", clamped=True))
    assert copy.headline(clamp) == "Rejected: the rules capped the VP's proposal"
    dead = _base(status=Status.REJECTED, decision=Decision(final=Ceiling.REJECT, by="deadlock", reasoning="x"))
    assert "didn't agree after 3 rounds" in copy.headline(dead)
    assert copy.counts_sentence([(Status.PAID, 5), (Status.REJECTED, 9), (Status.PENDING_HUMAN, 3),
                                 (Status.DUPLICATE, 3)]) == "5 paid, 9 rejected, 3 need a person, 3 duplicates"


def test_node_detail_never_raises(cases):
    odd = [{}, None, {"flags": None}, {"flags": [object()]}, {"transcript": [1]}, {"vp_output": "x"},
           {"critic_output": {"verdict": "OBJECT", "objections": None}}, {"decision": 5}, {"payment": 3},
           {"dedupe": {"kind": "weird"}}, {"human": {"reason": "nope"}}, {"extraction_attempts": "x"}]
    for node in graph.NODES:
        for upd in odd:
            assert isinstance(copy.node_detail(node, upd), str)
            assert isinstance(copy.node_line(node, upd), str)
    res, st = cases["invoice_1010.txt"]
    assert copy.node_detail("critic_review", {"critic_output": st.critic_output}) == "agrees"
    assert copy.node_detail("validate", {"flags": st.flags}) == "1 flag: 0 block payment"
    assert copy.node_detail("compute_ceiling", {"ceiling": "HUMAN"}) == "Person decides"
    assert copy.node_line("finish", {}) == "Done"


def test_tools():
    assert copy.tool_summary("get_vendor_history", {}, {"known": True, "invoices": [{}, {}]}) == "known, 2 past invoices"
    assert copy.tool_summary("get_stock_level", {}, {"item": "WidgetA", "found": True, "stock": 56}) == "WidgetA: 56"
    assert copy.tool_summary("get_budget_status", {}, {"remaining": "30860.00", "limit": "50000.00"}) == \
        "$30,860.00 left of $50,000.00"
    assert copy.tool_summary("nope", {}, {"a": 1}) == "1 field"
    from models import ToolTrace
    calls = [ToolTrace(call_id=str(i), name=n, arguments={}, result={}) for i, n in enumerate(
        ["get_vendor_history"] * 2 + ["get_stock_level"] * 3 + ["get_budget_status"] + ["get_policy"] * 3)]
    assert copy.tools_label(calls) == "Looked up 9 things: vendor history ×2, stock level ×3, budget, policy ×3"
    assert copy.tools_label([]) == ""
