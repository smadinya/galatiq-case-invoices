import json
import shutil
from decimal import Decimal as D

import pytest

from config.settings import REPO_ROOT
from llm.fake import ScriptedFake
from llm.fake_golden import golden_script
from llm.select import NoProviderAvailable
from models import PaymentWrite, Status
from service import (
    AmbiguousReference, ApproveNotAllowed, FxRateRequired, NoteRequired, NotFound,
    OverspendConfirmationRequired, Service, build_result, new_trace_id,
)

SAMPLES = REPO_ROOT / "data" / "invoices"


@pytest.fixture
def fake():
    return ScriptedFake(golden_script())


@pytest.fixture
def svc(settings, fake):
    s = Service(settings, provider=fake, sleep=lambda x: None)
    yield s
    s.close()


def spend(repo, usd):
    iid = repo.create_invoice(trace_id="t-spend", file_path="p", file_name="spend.json", file_hash="hh")
    repo.record_payment(PaymentWrite(
        idempotency_key="k-spend", invoice_id=iid, trace_id="t-spend", vendor="v", amount=usd, currency="USD",
        amount_usd=usd, attempts=1, month="2026-01", stock_deltas={}, bank_response={}))


def test_new_trace_id():
    t = new_trace_id(SAMPLES / "invoice_1001.txt")
    assert t.startswith("inv-invoice-1001-") and len(t.rsplit("-", 1)[1]) == 8


def test_short_circuit(svc, fake):
    first = svc.run_invoice(SAMPLES / "invoice_1001.txt")
    assert first.status == Status.PAID and not first.short_circuited
    calls, spent = len(fake.calls), svc.banner().budget.spent
    second = svc.run_invoice(SAMPLES / "invoice_1001.txt")
    assert second.short_circuited and second.trace_id == first.trace_id
    assert len(fake.calls) == calls and svc.banner().budget.spent == spent
    assert len(svc.repo.list_invoices()) == 1


def test_error_is_contained_and_reprocessed(settings, fake):
    boom = {"on": True}

    def hook(name, state):
        if name == "validate" and boom["on"]:
            raise RuntimeError("boom")

    with Service(settings, provider=fake, node_hook=hook, sleep=lambda x: None) as s:
        r = s.run_invoice(SAMPLES / "invoice_1004.json")
        assert r.status == Status.ERROR and "boom" in r.error
        assert s.list_queue() == [] and s.banner().budget.spent == 0
        boom["on"] = False
        r2 = s.run_invoice(SAMPLES / "invoice_1004.json")
        assert r2.status == Status.PAID and not r2.short_circuited
        assert r2.trace_id != r.trace_id


def test_batch_continues_after_error_and_counts(settings, fake, tmp_path):
    d = tmp_path / "in"
    d.mkdir()
    for n in ("invoice_1001.txt", "invoice_1004.json", "invoice_1011.pdf", "invoice_1011.txt"):
        shutil.copy(SAMPLES / n, d / n)
    (d / ".hidden").write_text("x")

    def hook(name, state):
        if name == "validate" and state.file_name == "invoice_1004.json":
            raise RuntimeError("boom")

    seen = []
    with Service(settings, provider=fake, node_hook=hook, sleep=lambda x: None) as s:
        b = s.run_batch(d, on_result=seen.append)
    assert [r.file for r in b.results] == ["invoice_1001.txt", "invoice_1004.json", "invoice_1011.pdf",
                                           "invoice_1011.txt"]
    assert len(seen) == 4
    assert b.counts == {"PAID": 2, "ERROR": 1, "DUPLICATE": 1}
    assert b.distinct_documents == 3 and b.decided_without_person == 2


def test_no_provider_needed_for_queue_and_reset(settings):
    with Service(settings, env={}) as s:
        assert s.list_queue() == []
        s.reset()
        assert s.banner().provider in ("none", "replay")


def test_no_provider_propagates_and_marks_error(settings):
    with Service(settings, env={}) as s:
        with pytest.raises(NoProviderAvailable):
            s.run_invoice(SAMPLES / "invoice_1001.txt")
        assert [i.status for i in s.repo.list_invoices()] == [Status.ERROR]


def queue_three(svc, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    svc.run_invoice(SAMPLES / "invoice_1012.pdf")
    svc.run_invoice(SAMPLES / "invoice_1014.xml")
    svc.run_invoice(bad)
    return svc


def snapshot(svc):
    return ([(q.invoice_id, q.status) for q in svc.repo.list_queue(None)],
            svc.repo.budget_spent("2026-01"), svc.repo.stock_levels())


def test_refusals_leave_state_unchanged(svc, tmp_path):
    queue_three(svc, tmp_path)
    assert len(svc.list_queue()) == 3
    before = snapshot(svc)

    with pytest.raises(NotFound):
        svc.resolve("INV-9999", action="reject", note="n")
    with pytest.raises(NoteRequired):
        svc.resolve("INV-1012", action="approve", note="  ")
    with pytest.raises(ApproveNotAllowed):
        svc.resolve("bad.json", action="approve", note="ok")
    with pytest.raises(FxRateRequired):
        svc.resolve("INV-1014", action="approve", note="ok")
    spend(svc.repo, D("45000"))
    before = snapshot(svc)
    with pytest.raises(OverspendConfirmationRequired) as ei:
        svc.resolve("INV-1012", action="approve", note="ok")
    assert ei.value.overspend_usd == D("9975.00") + D("45000") - D("50000")
    assert ei.value.budget.spent == D("45000")
    assert snapshot(svc) == before

    pending = svc.list_queue()
    svc.repo.find_pending = lambda ref: pending[:2]
    with pytest.raises(AmbiguousReference) as ai:
        svc.resolve("x", action="reject", note="n")
    assert len(ai.value.candidates) == 2
    assert snapshot(svc) == before
    assert len(svc.list_queue()) == 3


def test_resolve_paths_after_refusal(svc, tmp_path):
    queue_three(svc, tmp_path)
    p = svc.preview_resolution("INV-1014", fx_rate=D("1.1"))
    assert p.needs_fx_rate and p.can_approve and p.payment_amount_usd is not None
    assert not svc.preview_resolution("bad.json").can_approve
    r = svc.resolve("INV-1014", action="approve", note="rate ok", fx_rate=D("1.1"))
    assert r.status == Status.PAID and r.payment.fx_rate == D("1.1")
    r = svc.resolve("bad.json", action="reject", note="unreadable")
    assert r.status == Status.REJECTED
    assert [q.file_name for q in svc.list_queue()] == ["invoice_1012.pdf"]
    # retry after a refusal still works
    r = svc.resolve("INV-1012", action="approve", note="fine")
    assert r.status == Status.PAID


def test_payment_failure_reopens_queue_then_succeeds(settings, fake, tmp_path):
    with Service(settings, provider=fake, payment_fail_rate=1.0, sleep=lambda x: None) as s:
        r = s.run_invoice(SAMPLES / "invoice_1001.txt")
        assert r.status == Status.PENDING_HUMAN
        assert s.list_queue()[0].reason.value == "payment_failed"
    with Service(settings, provider=fake, sleep=lambda x: None) as s:
        r = s.resolve("INV-1001", action="approve", note="retry payment")
        assert r.status == Status.PAID


def test_reset_restores_seed(svc):
    svc.run_invoice(SAMPLES / "invoice_1001.txt")
    assert svc.banner().budget.spent == D("5000.00")
    svc.reset()
    b = svc.banner()
    assert b.stock == {"WidgetA": 90, "WidgetB": 50, "GadgetX": 13, "FakeItem": 0}
    assert b.budget.spent == 0
    assert svc.repo.list_invoices() == []
    again = svc.run_invoice(SAMPLES / "invoice_1001.txt")
    assert again.status == Status.PAID and not again.short_circuited
    svc.reset("readme")
    assert svc.banner().stock["WidgetA"] == 15 and svc.banner().seed == "readme"


def test_banner_fields(svc, settings):
    b = svc.banner()
    assert b.provider == "fake" and b.seed == "demo" and b.as_of == settings.as_of
    assert b.budget.limit == settings.monthly_budget_usd and b.stock["WidgetA"] == 90
    assert "--reset" in b.hint


def test_banner_provider_without_key_or_recordings(settings, tmp_path):
    s = settings.model_copy(update={"replay_dir": tmp_path / "no_recordings"})
    with Service(s, env={}) as bare:
        assert bare.banner().provider == "none"
    s2 = s.model_copy(update={"extraction_model": "m1", "reasoning_model": "m2"})
    with Service(s2, env={"XAI_API_KEY": "k"}) as keyed:
        b = keyed.banner()
        assert b.provider == "grok" and b.models == {"extraction": "m1", "reasoning": "m2"}


def test_build_result_and_jsonl_trace_ids(svc, settings, tmp_path):
    r = svc.run_invoice(SAMPLES / "invoice_1004.json")
    assert r.invoice_number == "INV-1004" and r.amount == D("1890.00") and r.provider == "fake"
    assert svc.repo.get_invoice_by_trace(r.trace_id).result == r
    queue_three(svc, tmp_path)
    svc.resolve("bad.json", action="reject", note="n")
    svc.run_invoice(SAMPLES / "invoice_1004.json")
    svc.run_batch(SAMPLES)
    lines = [json.loads(l) for f in settings.log_dir.glob("*.jsonl") for l in f.read_text().splitlines()]
    assert lines
    assert all(l["trace_id"] for l in lines)
    events = {l["event"] for l in lines}
    assert {"node_start", "node_end", "flag_raised", "interrupt", "resume", "short_circuit", "run_start",
            "run_end"} <= events
    assert any(l["trace_id"].startswith("batch-") for l in lines)
    assert all(l["duration_ms"] is not None for l in lines if l["event"] == "node_end")
