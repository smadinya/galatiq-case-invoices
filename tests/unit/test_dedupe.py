from decimal import Decimal

import pytest

from agents.validation import dedupe, dedupe_node
from core.invoice_math import fingerprint
from models import (
    DedupeKind, InvoiceResult, InvoiceUpdate, PaymentWrite, Status,
)

D = Decimal


@pytest.fixture
def store(repo):
    """Persist an invoice the way the pipeline would; returns the record id."""
    counter = {"n": 0}

    def _store(inv, status=Status.PAID, *, file_name=None, pay=None):
        counter["n"] += 1
        n = counter["n"]
        file_name = file_name or f"orig_{n}.json"
        trace = f"trace-{n}"
        rid = repo.create_invoice(trace_id=trace, file_path=f"x/{file_name}", file_name=file_name,
                                  file_hash=f"hash{n}")
        result = InvoiceResult(trace_id=trace, file=file_name, status=status, provider="none", invoice=inv)
        repo.update_invoice(rid, InvoiceUpdate(
            fingerprint=fingerprint(inv), vendor_key=inv.vendor_key, vendor_name=inv.vendor_name,
            invoice_number=inv.invoice_number, status=status, result=result))
        if pay is not None:
            repo.record_payment(PaymentWrite(
                idempotency_key=f"k{n}", invoice_id=rid, trace_id=trace, vendor=inv.vendor_name or "",
                amount=pay, currency="USD", amount_usd=pay, attempts=1, month="2026-01",
                stock_deltas={}, bank_response={}))
        return rid

    return _store


def run(repo, inv, exclude=10_000):
    return dedupe(inv, fingerprint=fingerprint(inv), repo=repo, exclude_invoice_id=exclude)


def test_new_when_nothing_stored(repo, make_invoice):
    assert run(repo, make_invoice()).kind == DedupeKind.NEW


def test_new_for_revision_without_original(repo, make_invoice):
    assert run(repo, make_invoice(revision="R1")).kind == DedupeKind.NEW


def test_duplicate_by_fingerprint(repo, make_invoice, store):
    inv = make_invoice()
    rid = store(inv, Status.PAID, file_name="a.pdf")
    # different notes/address/terms do not matter
    again = make_invoice(notes="copy", address="1 Other St", terms="Net 60")
    res = run(repo, again)
    assert res.kind == DedupeKind.DUPLICATE
    assert (res.original_invoice_id, res.original_file_name, res.original_status) == (rid, "a.pdf", Status.PAID)


def test_duplicate_wins_over_number_rules(repo, make_invoice, store):
    store(make_invoice(), Status.REJECTED)
    assert run(repo, make_invoice()).kind == DedupeKind.DUPLICATE


def test_conflict_same_number_no_marker(repo, make_invoice, store):
    store(make_invoice(), Status.PAID, file_name="orig.json")
    res = run(repo, make_invoice(lines=(("WidgetB", 2, "500.00"),)))
    assert res.kind == DedupeKind.CONFLICT
    assert res.original_file_name == "orig.json"


def test_revision_of_paid(repo, make_invoice, store):
    orig = make_invoice(lines=(("WidgetA", 3, "250.00"), ("GadgetX", 2, "750.00")))
    store(orig, Status.PAID, file_name="o.json", pay=D("2250.00"))
    rev = make_invoice(lines=(("WidgetA", 3, "250.00"), ("GadgetX", 7, "750.00"), ("WidgetB", 1, "500.00")),
                       revision="R1")
    res = run(repo, rev)
    assert res.kind == DedupeKind.REVISION_OF_PAID
    assert res.paid_to_date == D("2250.00")
    assert res.added_quantities == {"gadgetx": 5, "widgetb": 1}
    assert res.original_file_name == "o.json"


def test_revision_marked_by_notes_flag(repo, make_invoice, store):
    store(make_invoice(), Status.PAID)
    res = run(repo, make_invoice(lines=(("WidgetA", 2, "250.00"),), is_revision=True))
    assert res.kind == DedupeKind.REVISION_OF_PAID
    assert res.added_quantities == {"widgeta": 1}


def test_revision_replaces_unpaid_original(repo, make_invoice, store):
    store(make_invoice(), Status.PENDING_HUMAN, file_name="o.json")
    res = run(repo, make_invoice(lines=(("WidgetA", 2, "250.00"),), revision="R1"))
    assert res.kind == DedupeKind.REVISION_REPLACES
    assert res.original_status == Status.PENDING_HUMAN


@pytest.mark.parametrize("status", [Status.ERROR, Status.DUPLICATE, Status.SUPERSEDED, Status.PROCESSING])
def test_non_live_originals_are_ignored(repo, make_invoice, store, status):
    store(make_invoice(), status)
    assert run(repo, make_invoice()).kind == DedupeKind.NEW
    assert run(repo, make_invoice(lines=(("WidgetB", 1, "500.00"),))).kind == DedupeKind.NEW


def test_dedupe_node_writes_identity_and_duplicate_of(make_state, make_deps, make_invoice, store, repo):
    store(make_invoice(), Status.PAID, file_name="first.pdf")
    inv = make_invoice()
    state = make_state(inv, file_name="second.txt")
    deps = make_deps()
    out = dedupe_node(state, deps)
    assert out["dedupe"].kind == DedupeKind.DUPLICATE
    assert out["duplicate_of"] == "first.pdf"
    assert out["fingerprint"] == fingerprint(inv)
    rec = repo.get_invoice(state.invoice_id)
    assert (rec.fingerprint, rec.vendor_key, rec.invoice_number, rec.amount, rec.currency) == (
        fingerprint(inv), "widgets", "INV-9001", D("250.00"), "USD")
    assert [a.event for a in repo.audit_for_trace(state.trace_id)] == ["dedupe"]


def test_dedupe_node_new_has_no_duplicate_of(make_state, make_deps, make_invoice):
    out = dedupe_node(make_state(make_invoice()), make_deps())
    assert out["dedupe"].kind == DedupeKind.NEW
    assert out["duplicate_of"] is None


@pytest.mark.parametrize("orig_status,superseded", [
    (Status.PENDING_HUMAN, True), (Status.REJECTED, True)])
def test_revision_replaces_supersedes_original(make_state, make_deps, make_invoice, store, repo,
                                               orig_status, superseded):
    rid = store(make_invoice(), orig_status)
    state = make_state(make_invoice(lines=(("WidgetA", 2, "250.00"),), revision="R1"))
    out = dedupe_node(state, make_deps())
    assert out["dedupe"].kind == DedupeKind.REVISION_REPLACES
    assert repo.get_invoice(rid).status == Status.SUPERSEDED


def test_revision_of_paid_does_not_supersede(make_state, make_deps, make_invoice, store, repo):
    rid = store(make_invoice(), Status.PAID, pay=D("250.00"))
    state = make_state(make_invoice(lines=(("WidgetA", 2, "250.00"),), revision="R1"))
    dedupe_node(state, make_deps())
    assert repo.get_invoice(rid).status == Status.PAID
