"""DESIGN §14.2: all 20 sample invoices through the real pipeline with the scripted fake."""

from collections import Counter
from decimal import Decimal as D

import pytest

from conftest import build_factory
from config.settings import REPO_ROOT
from golden_expected import (
    BUDGET_LIMIT, GOLDEN_LEDGER, GOLDEN_ROWS, GOLDEN_TOTALS, MONTH,
)
from llm.fake import ScriptedFake
from llm.fake_golden import golden_script
from models import TIER3_FLAGS, Status

SAMPLES = REPO_ROOT / "data" / "invoices"
ITEMS = ("WidgetA", "WidgetB", "GadgetX", "FakeItem")
ROWS = {r.file: r for r in GOLDEN_ROWS}
RAW_ONLY = ("Attn: Accounts Payble", "Thank you for your business!", "Please process at your earliest convenience.")


def snap(svc):
    stock = svc.repo.stock_levels()
    return tuple(stock[i] for i in ITEMS) + (svc.repo.budget_spent(MONTH),)


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    created: list = []
    factory = build_factory(tmp_path_factory.mktemp("golden"), created)
    fake = ScriptedFake(golden_script())
    svc = factory(provider=fake)
    out = []
    for path in sorted(SAMPLES.iterdir()):
        before = snap(svc)
        result = svc.run_invoice(path)
        out.append((path.name, before, snap(svc), result))
    yield svc, fake, out
    for s in created:
        s.close()


def by_file(run):
    return {name: (before, after, res) for name, before, after, res in run[2]}


def test_file_order():
    assert [p.name for p in sorted(SAMPLES.iterdir())] == [r.file for r in GOLDEN_ROWS]


@pytest.mark.parametrize("row", GOLDEN_ROWS, ids=lambda r: r.file)
def test_row(run, row):
    _, _, res = by_file(run)[row.file]
    assert res.error is None
    assert res.status == row.status
    assert Counter(f.code.value for f in res.flags) == Counter(row.flags)
    assert res.ceiling == row.ceiling
    assert (getattr(res.address_check, 'value', res.address_check)) == row.address_check
    assert res.duplicate_of == row.duplicate_of
    if row.paid is None:
        assert res.payment is None
    else:
        assert res.payment is not None and res.payment.amount == row.paid
    assert (res.decision.by if res.decision else None) == row.decided_by
    critic_turns = [t for t in res.transcript if t.actor == "critic"]
    assert len(critic_turns) == row.critic_rounds
    assert not any(f.code in TIER3_FLAGS for f in res.flags)
    if row.status == Status.PENDING_HUMAN:
        assert res.human is not None
        assert res.human.reason.value == row.human_reason
        assert res.human.recommendation == row.human_recommendation
        assert res.decision.payment_amount == row.human_payment_amount
    for code, expected in row.evidence.items():
        flag = next(f for f in res.flags if f.code.value == code)
        for key, value in expected.items():
            assert flag.evidence[key] == value, (code, key, flag.evidence)


def test_ledger_snapshots(run):
    ledger = iter(GOLDEN_LEDGER)
    start = next(ledger)
    assert start[0] == "start"
    expected = tuple(start[1:])
    for name, before, after, res in run[2]:
        assert before == expected, name
        if res.status == Status.PAID:
            entry = next(ledger)
            assert entry[0] == name
            expected = tuple(entry[1:])
        assert after == expected, name
    assert next(ledger, None) is None
    assert snap(run[0]) == tuple(GOLDEN_LEDGER[-1][1:])


def test_stock_and_budget_ledger_rows(run):
    svc, _, out = run
    repo = svc.repo
    paid = [(n, r) for n, _, _, r in out if r.status == Status.PAID]
    invoices = {i.file_name: i for i in repo.list_invoices()}
    prev = tuple(GOLDEN_LEDGER[0][1:])
    entries = repo.budget_entries(MONTH)
    assert len(entries) == len(paid) == len(GOLDEN_LEDGER) - 1
    for idx, (entry, (name, res)) in enumerate(zip(GOLDEN_LEDGER[1:], paid)):
        assert entry[0] == name
        iid = invoices[name].id
        cur = tuple(entry[1:])
        deltas = {e.item: e.delta for e in repo.stock_ledger(iid)}
        assert all(e.reason == "payment" for e in repo.stock_ledger(iid))
        want = {item: cur[k] - prev[k] for k, item in enumerate(ITEMS) if cur[k] != prev[k]}
        assert deltas == want, name
        assert entries[idx] == (iid, cur[4] - prev[4]) == (iid, res.payment.amount_usd)
        prev = cur
    # nothing else touched the ledgers
    assert {e.invoice_id for e in repo.stock_ledger() if e.reason == 'payment'} == {invoices[n].id for n, _ in paid}
    # rejected / pending / duplicate invoices paid nothing
    for name, _, _, res in out:
        if res.status != Status.PAID:
            assert repo.stock_ledger(invoices[name].id) == []


def test_run_batch_totals(tmp_path):
    created: list = []
    factory = build_factory(tmp_path, created)
    svc = factory()
    seen = []
    batch = svc.run_batch(SAMPLES, on_result=seen.append)
    assert [r.file for r in batch.results] == [r.file for r in GOLDEN_ROWS]
    assert len(seen) == len(GOLDEN_ROWS)
    assert batch.counts == GOLDEN_TOTALS["counts"]
    assert batch.distinct_documents == GOLDEN_TOTALS["distinct_documents"]
    assert batch.decided_without_person == GOLDEN_TOTALS["decided_without_person"]
    assert batch.budget.spent == GOLDEN_TOTALS["spent"]
    assert batch.budget.limit == BUDGET_LIMIT and batch.budget.month == MONTH
    assert batch.budget.remaining == BUDGET_LIMIT - GOLDEN_TOTALS["spent"]
    assert tuple(q.invoice_ref for q in svc.list_queue()) == GOLDEN_TOTALS["queue_refs"]
    last = GOLDEN_LEDGER[-1]
    assert batch.stock == dict(zip(ITEMS, last[1:5]))
    for s in created:
        s.close()


def test_no_extraction_call_for_structured_files(run):
    fake = run[1]
    for p in SAMPLES.iterdir():
        if p.suffix in (".json", ".csv", ".xml"):
            assert fake.calls_for(p.name, "extraction") == [], p.name
    for name in ("invoice_1001.txt", "invoice_1011.pdf", "invoice_1012.pdf"):
        assert len(fake.calls_for(name, "extraction")) == 1


def test_no_vp_critic_for_duplicates_or_memo_rows(run):
    fake = run[1]
    for row in GOLDEN_ROWS:
        if row.path == "dup":
            for stage in ("vp", "critic", "vp_memo"):
                assert fake.calls_for(row.file, stage) == [], (row.file, stage)
        if row.path == "memo":
            assert fake.calls_for(row.file, "vp") == [] and fake.calls_for(row.file, "critic") == []
            assert len(fake.calls_for(row.file, "vp_memo")) == 1
        if row.path == "fast":
            assert fake.calls_for(row.file, "critic") == []
            assert len(fake.calls_for(row.file, "vp")) == 1


def test_raw_only_markers_never_reach_vp_or_critic(run):
    fake = run[1]
    checked = 0
    for call in fake.calls:
        if call.ctx.stage in ("vp", "critic", "vp_memo"):
            checked += 1
            for m in call.messages:
                for marker in RAW_ONLY:
                    assert marker not in (m.content or ""), (call.ctx.file_name, marker)
    assert checked > 10
    # sanity: the markers do exist in the raw sources, so the check can fail
    corpus = "".join(p.read_text(errors="ignore") for p in SAMPLES.glob("*.txt"))
    assert any(m in corpus for m in RAW_ONLY)


def test_1005_vp_trace_has_vendor_history_unknown(run):
    res = by_file(run)["invoice_1005.json"][2]
    vp_turns = [t for t in res.transcript if t.actor == "vp"]
    traces = [tc for t in vp_turns for tc in t.tool_calls if tc.name == "get_vendor_history"]
    assert traces and traces[0].result["known"] is False


def test_1010_has_round_two(run):
    res = by_file(run)["invoice_1010.txt"][2]
    assert len([t for t in res.transcript if t.actor == "critic"]) == 2
    assert [t.round for t in res.transcript if t.actor == "critic"] == [0, 1]
