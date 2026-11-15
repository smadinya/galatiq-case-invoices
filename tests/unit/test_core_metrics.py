import datetime as dt
from decimal import Decimal

import pytest

from config.settings import load_settings
from core.metrics import compute_metrics, latest_per_file, no_cost, savings_estimate
from models import FlagCode, HumanReview, InvoiceRecord, InvoiceResult, Status, Usage
from ui.kit.testing import make_runtime


def result(status=Status.PAID, *, file="a.txt", node_ms=None, calls=0, tin=0, tout=0, lat=0, short=False, human=None):
    return InvoiceResult(
        trace_id=f"t-{file}-{status.value}", file=file, status=status, provider="fake", short_circuited=short,
        human=human,
        usage=Usage(llm_calls=calls, tokens_in=tin, tokens_out=tout, llm_latency_ms=lat, node_ms=node_ms or {}),
    )


def record(file_hash, status, *, id, minute=0, name="a.txt"):
    when = dt.datetime(2026, 1, 31, 12, minute)
    return InvoiceRecord(
        id=id, trace_id=f"t{id}", file_path=f"/x/{name}", file_name=name, file_hash=file_hash, status=status,
        created_at=when, updated_at=when,
    )


@pytest.fixture(scope="module")
def golden_results(tmp_path_factory):
    rt = make_runtime(tmp_path_factory.mktemp("metrics-golden"))
    job = rt.start_batch(rt.default_config, rt.samples_dir)
    snap = job.snapshot()
    assert snap.error is None
    return list(snap.value.results)


def test_distinct_excludes_duplicates_superseded_and_processing():
    results = [
        result(Status.PAID, file="1"), result(Status.DUPLICATE, file="2"), result(Status.SUPERSEDED, file="3"),
        result(Status.PROCESSING, file="4"), result(Status.PENDING_HUMAN, file="5"), result(Status.ERROR, file="6"),
        result(Status.REJECTED, file="7"), result(Status.CREDIT_DUE, file="8"),
    ]
    m = compute_metrics(results)
    assert m.documents == 8
    assert m.distinct == 5  # paid, pending, error, rejected, credit due
    assert m.decided_without_person == 3  # paid, rejected, credit due
    assert m.automation_rate == Decimal(3) / Decimal(5)
    assert m.errors == 1
    assert m.counts["DUPLICATE"] == 1


def test_a_person_resolved_invoice_counts_as_needing_a_person():
    human = HumanReview(reason=FlagCode.FOREIGN_CURRENCY, reasons=[FlagCode.FOREIGN_CURRENCY], outcome="approved")
    m = compute_metrics([result(Status.PAID, file="1"), result(Status.PAID, file="2", human=human)])
    assert (m.distinct, m.decided_without_person) == (2, 1)


def test_processed_excludes_short_circuited_and_errors_but_keeps_duplicates():
    results = [
        result(Status.PAID, file="1", node_ms={"a": 1000, "b": 500}, calls=2, tin=100, tout=50, lat=2000),
        result(Status.DUPLICATE, file="2", node_ms={"a": 500}, calls=1, tin=40, tout=10, lat=1000),
        result(Status.PAID, file="3", node_ms={"a": 9000}, calls=9, tin=9999, short=True),
        result(Status.ERROR, file="4", node_ms={"a": 7000}, calls=5, tin=5000),
    ]
    m = compute_metrics(results)
    assert (m.processed, m.reused, m.errors) == (2, 1, 1)
    assert m.avg_processing_s == Decimal("1.0")  # (1.5 + 0.5) / 2
    assert (m.llm_calls, m.tokens_in, m.tokens_out, m.llm_latency_ms) == (3, 140, 60, 3000)
    assert m.llm_calls_per_doc == Decimal("1.5")
    assert m.tokens_per_doc == Decimal(100)
    assert m.avg_llm_latency_s == Decimal("1.5")
    assert m.llm_cost_per_doc_usd is None


def test_nothing_processed_gives_no_means():
    m = compute_metrics([result(Status.PAID, file="1", short=True, node_ms={"a": 10})])
    assert m.processed == 0 and m.reused == 1
    assert m.avg_processing_s is None and m.llm_calls_per_doc is None and m.tokens_per_doc is None


def test_no_results_and_no_distinct_documents():
    m = compute_metrics([])
    assert (m.documents, m.distinct, m.automation_rate) == (0, 0, None)
    assert savings_estimate(m, load_settings(env={})) is None
    only_dup = compute_metrics([result(Status.DUPLICATE)])
    assert only_dup.automation_rate is None


def test_a_cost_function_prices_the_run():
    results = [result(file="1", node_ms={"a": 1}, tin=1000), result(file="2", node_ms={"a": 1}, tin=3000)]
    m = compute_metrics(results, cost=lambda u: Decimal(u.tokens_in) / 1000)
    assert m.llm_cost_per_doc_usd == Decimal(2)
    assert no_cost(Usage()) is None
    est = savings_estimate(m, load_settings(env={}))
    assert est.llm_cost_usd == Decimal("40000.00")
    assert est.net_usd == est.gross_usd - est.llm_cost_usd


def test_golden_batch_figures(golden_results):
    m = compute_metrics(golden_results)
    assert (m.documents, m.distinct, m.decided_without_person) == (20, 17, 14)
    assert m.automation_rate == Decimal(14) / Decimal(17)
    assert m.counts == {"PAID": 5, "REJECTED": 9, "PENDING_HUMAN": 3, "DUPLICATE": 3}
    from ui.kit import fmt

    assert fmt.pct(m.decided_without_person, m.distinct) == "82%"
    assert (m.processed, m.reused, m.errors) == (20, 0, 0)
    assert m.llm_calls > 0


def test_golden_savings(golden_results):
    est = savings_estimate(compute_metrics(golden_results), load_settings(env={}))
    assert est.gross_usd == (Decimal(20000) * 14 / 17 * 100).quantize(Decimal("0.01")) == Decimal("1647058.82")
    assert (est.volume, est.baseline_cost_usd) == (20000, Decimal(100))
    assert est.net_usd is None and est.llm_cost_usd is None and est.llm_cost_per_invoice_usd is None
    assert "volume × automation rate" in est.formula
    assert all(source.startswith("Assumption") for _, source in est.assumptions)


def test_a_rerun_of_stored_results_processes_nothing(golden_results):
    again = [r.model_copy(update={"short_circuited": True}) for r in golden_results]
    m = compute_metrics(again)
    assert (m.processed, m.reused, m.documents) == (0, 20, 20)
    assert m.avg_processing_s is None
    assert m.decided_without_person == 14


def test_latest_per_file_keeps_the_newest_row_and_drops_processing():
    rows = [
        record("h1", Status.ERROR, id=1, minute=1),
        record("h1", Status.PAID, id=2, minute=5),  # the retry
        record("h2", Status.PROCESSING, id=3, minute=2, name="b.txt"),
        record("h3", Status.REJECTED, id=4, minute=3, name="c.txt"),
        record("h4", Status.PAID, id=5, minute=4, name="0.txt"),
    ]
    kept = latest_per_file(rows)
    assert [(r.file_name, r.status) for r in kept] == [
        ("0.txt", Status.PAID), ("a.txt", Status.PAID), ("c.txt", Status.REJECTED),
    ]
    assert latest_per_file([record("h", Status.ERROR, id=1)])[0].status == Status.ERROR
    assert latest_per_file([]) == []
