from agents.outcome import finish, record_duplicate, record_rejection
from models import Ceiling, Decision, DedupeKind, DedupeResult, FlagCode, Status, make_flag


def test_record_rejection(make_state, make_invoice, make_deps, repo):
    st = make_state(
        make_invoice(),
        decision=Decision(final=Ceiling.REJECT, by="rules", reasoning="blocked vendor"),
        flags=[make_flag(FlagCode.UNREADABLE_DOCUMENT, "x")],
    )
    out = record_rejection(st, make_deps())
    assert out == {"status": Status.REJECTED}
    assert repo.get_invoice(st.invoice_id).status == Status.REJECTED
    entry = repo.audit_for_trace(st.trace_id)[-1]
    assert entry.event == "rejected" and entry.payload["reasoning"] == "blocked vendor"


def test_record_rejection_without_decision(make_state, make_deps, repo):
    st = make_state()
    assert record_rejection(st, make_deps()) == {"status": Status.REJECTED}
    assert repo.audit_for_trace(st.trace_id)[-1].event == "rejected"


def test_record_duplicate(make_state, make_invoice, make_deps, repo):
    st = make_state(
        make_invoice(),
        dedupe=DedupeResult(kind=DedupeKind.DUPLICATE, original_file_name="invoice_1001.txt"),
    )
    out = record_duplicate(st, make_deps())
    assert out == {"status": Status.DUPLICATE, "duplicate_of": "invoice_1001.txt"}
    assert repo.get_invoice(st.invoice_id).status == Status.DUPLICATE
    entry = repo.audit_for_trace(st.trace_id)[-1]
    assert entry.event == "duplicate" and entry.payload == {"duplicate_of": "invoice_1001.txt"}


def test_finish_returns_nothing(make_state, make_deps):
    assert finish(make_state(), make_deps()) == {}
