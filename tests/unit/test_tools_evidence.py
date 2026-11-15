import json
from decimal import Decimal

from models import Correction, Flag, NormalizedChange
from tools.evidence import get_pipeline_trace, get_source_evidence, recompute_totals


def test_source_evidence(make_state):
    state = make_state(evidence_spans={"total": ["Total: $100"], "invoice_number": ["Invoice # 1"]})
    assert get_source_evidence(state, "total") == {"field": "total", "lines": ["Total: $100"]}
    out = get_source_evidence(state, "vendor")
    assert out["error"] and out["fields"] == ["invoice_number", "total"]


def test_recompute_totals(make_state, make_invoice):
    inv = make_invoice(lines=(("WidgetA", 2, "250.00"),), total="600.00")
    out = recompute_totals(make_state(inv))
    json.dumps(out)
    assert out["lines_sum"] == "500.00" and out["total_ok"] is False
    assert Decimal(out["difference"]) == Decimal("100.00")
    assert out["mismatches"]


def test_recompute_totals_without_invoice(make_state):
    assert "error" in recompute_totals(make_state())


def test_pipeline_trace(repo, make_deps, make_state):
    deps = make_deps()
    flag = Flag(code="unknown_vendor", message="m", evidence={"vendor": "X"})
    state = make_state(
        corrections=[Correction(field="date", raw="2O26", interpreted="2026")],
        extraction_attempts=2,
        grounding_errors=["total not in source"],
        normalized_changes=[NormalizedChange(field="invoice_number", raw="1002", normalized="INV-1002")],
        flags=[flag],
    )
    repo.audit(trace_id=state.trace_id, actor="system", event="ingested", payload={"a": 1})
    repo.audit(trace_id="other-trace", actor="system", event="ignored", payload={})
    out = get_pipeline_trace(deps, state)
    json.dumps(out)
    assert [(a["actor"], a["event"], a["payload"]) for a in out["audit"]] == [("system", "ingested", {"a": 1})]
    assert out["extraction_attempts"] == 2
    assert out["grounding_errors"] == ["total not in source"]
    assert out["corrections"][0]["raw"] == "2O26"
    assert out["normalized_changes"][0]["normalized"] == "INV-1002"
    assert out["flags"][0]["code"] == "unknown_vendor"


def test_bound_evidence_tools_hide_context(make_state, make_deps):
    state = make_state(evidence_spans={"total": ["x"]})
    bound = get_source_evidence.bind(state=state, deps=make_deps())
    assert bound.fn(field="total")["lines"] == ["x"]
    assert recompute_totals.spec.parameters["properties"] == {}
