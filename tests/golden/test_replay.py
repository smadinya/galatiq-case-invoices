"""Replay layer (DESIGN §14.1): the 20-file batch served from fixtures/replay, no network."""

import json
import os
from collections import Counter
from pathlib import Path

import pytest

from conftest import build_factory
from config.settings import REPO_ROOT
from golden_expected import AS_OF, GOLDEN_LEDGER, GOLDEN_ROWS, MONTH
from llm.base import StaleRecording
from llm.fake_golden import GOLDEN_EXTRACTIONS
from llm.replay import ReplayProvider
from models import CallContext, Status
from tools.normalize import normalize

REPLAY = REPO_ROOT / "fixtures" / "replay"
SAMPLES = REPO_ROOT / "data" / "invoices"
needs_fixtures = pytest.mark.skipif(
    not (REPLAY / "manifest.json").exists(),
    reason="no replay fixtures yet: run 'uv run python scripts/record.py' with XAI_API_KEY in .env",
)
FORBIDDEN = ("authorization", "bearer ", "api_key", "x-api-key")


def summarize(res) -> dict:
    out = {
        "status": res.status.value,
        "flags": sorted(f.code.value for f in res.flags),
        "ceiling": res.ceiling.value if res.ceiling else None,
        "paid": str(res.payment.amount) if res.payment else None,
        "by": res.decision.by if res.decision else None,
        "rounds": len([t for t in res.transcript if t.actor == "critic"]),
    }
    if res.status == Status.PENDING_HUMAN and res.human is not None:
        out["human"] = {
            "reason": res.human.reason.value,
            "recommendation": res.human.recommendation.value if res.human.recommendation else None,
        }
    if res.error:
        out["error"] = res.error
    return out


ITEMS = ("WidgetA", "WidgetB", "GadgetX", "FakeItem")
STATE_FLAGS = {"insufficient_stock", "zero_stock_item", "over_budget"}  # depend on carried-over state


def _snapshot(svc) -> tuple:
    levels = svc.repo.stock_levels()
    return tuple(levels[i] for i in ITEMS) + (svc.repo.budget_spent(MONTH),)


def _golden_state_before() -> dict[str, tuple]:
    """Stock and spend the golden run has just before each file is processed."""
    paid = {entry[0]: tuple(entry[1:]) for entry in GOLDEN_LEDGER[1:]}
    state, out = tuple(GOLDEN_LEDGER[0][1:]), {}
    for row in GOLDEN_ROWS:
        out[row.file] = state
        if row.file in paid:
            state = paid[row.file]
    return out


@pytest.fixture(scope="module")
def replayed(tmp_path_factory):
    """(BatchResult, final ledger, state before each file) for the replayed batch."""
    created: list = []
    factory = build_factory(tmp_path_factory.mktemp("replay"), created)
    svc = factory(provider=ReplayProvider(REPLAY))
    before: dict[str, tuple] = {}

    def on_update(file_name, node, update):
        if node == "load_document" and file_name not in before:
            before[file_name] = _snapshot(svc)

    batch = svc.run_batch(SAMPLES, on_update=on_update)
    ledger = (svc.repo.stock_levels(), str(svc.repo.budget_spent(MONTH)))
    yield batch, ledger, before
    for s in created:
        s.close()


@needs_fixtures
def test_rows_match_expected_results(replayed):
    batch, _, _ = replayed
    expected = json.loads((REPLAY / "expected_results.json").read_text(encoding="utf-8"))["files"]
    assert [r.file for r in batch.results] == [r.file for r in GOLDEN_ROWS]
    for res in batch.results:
        assert res.error is None, (res.file, res.error)
        assert summarize(res) == expected[res.file], res.file


@needs_fixtures
def test_final_ledger_matches(replayed):
    _, (stock, spent), _ = replayed
    expected = json.loads((REPLAY / "expected_results.json").read_text(encoding="utf-8"))
    assert stock == expected["stock"]
    assert spent == expected["spent"]


@needs_fixtures
@pytest.mark.parametrize("row", [r for r in GOLDEN_ROWS if not r.scripted], ids=lambda r: r.file)
def test_rule_determined_rows_equal_golden(replayed, row):
    """A non-† row is decided by rules, but only given the same stock and budget. If an earlier †
    judgement went differently (e.g. Grok rejected an invoice the golden script pays), stock carries
    over differently and this row may legitimately differ; that is reported by scripts/record.py."""
    batch, _, before = replayed
    res = {r.file: r for r in batch.results}[row.file]
    got = Counter(f.code.value for f in res.flags)
    want = Counter(row.flags)
    # Flags that don't depend on carried-over stock or budget must always match.
    assert {c: n for c, n in got.items() if c not in STATE_FLAGS} == {c: n for c, n in want.items() if c not in STATE_FLAGS}
    if before.get(row.file) != _golden_state_before()[row.file]:
        return  # outcome may legitimately differ; scripts/record.py reports it
    assert res.status == row.status
    assert got == want
    assert res.ceiling == row.ceiling
    assert (res.payment.amount if res.payment else None) == row.paid


LLM_FILES = sorted(GOLDEN_EXTRACTIONS)


def _shape(inv) -> dict:
    """The extraction facts every rule depends on (free-text notes may be worded differently)."""
    return {
        "invoice_number": inv.invoice_number, "revision": inv.revision, "vendor_key": inv.vendor_key,
        "vendor_former_name": inv.vendor_former_name, "currency": inv.currency,
        "invoice_date": inv.invoice_date, "due_date": inv.due_date, "terms_days": inv.terms_days,
        "total": inv.total, "subtotal": inv.subtotal, "tax_amount": inv.tax_amount, "shipping": inv.shipping,
        "lines": [(li.item_key, li.quantity, li.unit_price, li.amount) for li in inv.line_items],
    }


@needs_fixtures
@pytest.mark.parametrize("name", LLM_FILES)
def test_recorded_extraction_matches_golden_extraction(replayed, name):
    """Grok's recorded extraction normalizes to the same invoice as the hand-checked canned one."""
    res = {r.file: r for r in replayed[0].results}[name]
    assert res.invoice is not None, name
    assert _shape(res.invoice) == _shape(normalize(GOLDEN_EXTRACTIONS[name]).invoice), name


@needs_fixtures
def test_changed_prompt_version_raises_stale_recording():
    fixture = next(p for p in sorted(REPLAY.glob("*/*.json")))
    key = json.loads(fixture.read_text(encoding="utf-8"))["key"]
    ctx = CallContext(
        trace_id="t", file_hash=key["file_hash"], file_name=key["file_name"], stage=key["stage"],
        prompt_version=key["prompt_version"] + "-changed", call_index=key["call_index"],
    )
    with pytest.raises(StaleRecording) as exc:
        ReplayProvider(REPLAY).complete(ctx=ctx, system="", messages=[])
    assert "re-record" in str(exc.value)


def test_fixtures_contain_no_secrets():
    """Runs even without fixtures; an empty or missing directory passes trivially."""
    needles = list(FORBIDDEN)
    key = os.environ.get("XAI_API_KEY")
    files = [p for p in REPLAY.rglob("*") if p.is_file()] if REPLAY.exists() else []
    for path in files:
        text = path.read_text(encoding="utf-8", errors="ignore").lower()
        for needle in needles:
            assert needle not in text, f"{path} contains {needle!r}"
        if key:
            assert key.lower() not in text, f"{path} contains the API key"
