import logging
from decimal import Decimal
from pathlib import Path

from config.settings import REPO_ROOT
from graph import checkpoint_serde, make_checkpointer
from llm.fake import ScriptedFake
from llm.fake_golden import golden_script
from models import Ceiling, Flag, InvoiceState, Status
from service import Service

SAMPLES = REPO_ROOT / "data" / "invoices"


def svc(settings, **kw):
    return Service(settings, provider=ScriptedFake(golden_script()), sleep=lambda s: None, **kw)


def test_checkpoint_round_trip_across_new_service_then_resume(settings, caplog):
    caplog.set_level(logging.DEBUG)
    a = svc(settings)
    result = a.run_invoice(SAMPLES / "invoice_1012.pdf")
    assert result.status == Status.PENDING_HUMAN
    trace = result.trace_id
    a.close()

    b = svc(settings)
    snap = b.graph.get_state({"configurable": {"thread_id": trace}})
    assert snap.next == ("await_human",)
    state = InvoiceState.model_validate(snap.values)
    assert isinstance(state.invoice.total, Decimal)
    assert isinstance(state.amount_usd, Decimal)
    assert isinstance(state.ceiling, Ceiling) and state.ceiling == Ceiling.HUMAN
    assert state.flags and all(isinstance(f, Flag) for f in state.flags)
    assert state.human is not None and state.human.payment_amount == Decimal("9975.00")
    assert not [r for r in caplog.records if "Deserializing unregistered type" in r.getMessage()]

    done = b.resolve("INV-1012", action="approve", note="verified with vendor")
    assert done.status == Status.PAID
    assert done.payment is not None and done.payment.amount == Decimal("9975.00")
    assert b.list_queue() == []
    assert not [r for r in caplog.records if "Deserializing unregistered type" in r.getMessage()]
    b.close()


def test_make_checkpointer_uses_allowlist_serde(tmp_path):
    saver, conn = make_checkpointer(tmp_path / "c.db")
    assert saver.serde is not None
    assert checkpoint_serde() is not None
    conn.close()
