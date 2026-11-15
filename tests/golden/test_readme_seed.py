"""DESIGN §6.1: with the README's seed (WidgetA 15, WidgetB 10, GadgetX 5, ...) the same batch changes shape."""

from collections import Counter

from config.settings import REPO_ROOT
from llm.fake import ScriptedFake, memo
from llm.fake_golden import golden_script

SAMPLES = REPO_ROOT / "data" / "invoices"


def test_readme_seed_outcomes(make_service):
    fake = ScriptedFake(golden_script(), fallback={"vp_memo": memo("Rejected by rule.")})
    svc = make_service(provider=fake)
    svc.reset("readme")
    assert svc.banner().seed == "readme"
    batch = svc.run_batch(SAMPLES)
    assert not [r for r in batch.results if r.error]
    counts = Counter(r.status.value for r in batch.results)
    assert counts == {"PAID": 2, "PENDING_HUMAN": 1, "REJECTED": 14, "DUPLICATE": 3}
    by = {r.file: r for r in batch.results}
    assert {n for n, r in by.items() if r.status.value == "PAID"} == {"invoice_1001.txt", "invoice_1004.json"}
    assert [r.file for r in batch.results if r.status.value == "PENDING_HUMAN"] == ["invoice_1004_revised.json"]
    assert [q.invoice_ref for q in svc.list_queue()] == ["INV-1004 R1"]
    r6 = by["invoice_1006.csv"]
    assert r6.status.value == "REJECTED"
    short = next(f for f in r6.flags if f.code.value == "insufficient_stock")
    assert short.evidence["item"] == "WidgetA" and short.evidence["requested"] == 5 and short.evidence["available"] == 2
