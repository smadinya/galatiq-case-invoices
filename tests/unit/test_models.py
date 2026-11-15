import datetime as dt
import enum
import types
import typing
from decimal import Decimal

import pytest
from openai.lib._pydantic import to_strict_json_schema
from pydantic import BaseModel, ValidationError

import models
from models import (
    CHECKPOINT_TYPES, FLAG_SEVERITY, Ceiling, CriticOutput, ExtractedInvoice, Flag, FlagCode, Invoice,
    InvoiceState, Severity, VPMemoOutput, VPOutput, ceiling_for, make_flag, stricter,
)


def test_flag_severity_covers_every_code():
    assert set(FLAG_SEVERITY) == set(FlagCode)
    assert len(FLAG_SEVERITY) == 26


def test_ceiling_order():
    assert Ceiling.REJECT < Ceiling.HUMAN < Ceiling.APPROVE
    assert Ceiling.APPROVE > Ceiling.HUMAN >= Ceiling.HUMAN
    assert min(Ceiling.APPROVE, Ceiling.HUMAN) is Ceiling.HUMAN
    assert max(Ceiling.REJECT, Ceiling.HUMAN) is Ceiling.HUMAN
    assert sorted([Ceiling.APPROVE, Ceiling.REJECT, Ceiling.HUMAN]) == [
        Ceiling.REJECT, Ceiling.HUMAN, Ceiling.APPROVE,
    ]
    assert stricter(Ceiling.APPROVE, Ceiling.REJECT) is Ceiling.REJECT


def test_flag_severity_filled_when_omitted():
    f = Flag(code=FlagCode.MATH_MISMATCH, message="x")
    assert f.severity is Severity.ERROR
    assert Flag(code=FlagCode.PRICE_ABOVE_CATALOG, message="x").severity is Severity.ESCALATION
    f2 = Flag.model_validate({"code": "over_budget", "message": "x"})
    assert f2.severity is Severity.HUMAN
    assert make_flag(FlagCode.PAST_DUE, "late", days=3).evidence == {"days": 3}


def test_flag_severity_mismatch_raises():
    with pytest.raises(ValidationError):
        Flag(code=FlagCode.PRICE_ABOVE_CATALOG, severity=Severity.ERROR, message="x")


def test_ceiling_for():
    err = make_flag(FlagCode.UNKNOWN_ITEM, "e")
    hum = make_flag(FlagCode.OVER_BUDGET, "h")
    esc = make_flag(FlagCode.PRICE_ABOVE_CATALOG, "s")
    info = make_flag(FlagCode.PAST_DUE, "i")
    assert ceiling_for([]) is Ceiling.APPROVE
    assert ceiling_for([esc, info]) is Ceiling.APPROVE
    assert ceiling_for([hum, esc]) is Ceiling.HUMAN
    assert ceiling_for([hum, err]) is Ceiling.REJECT
    assert ceiling_for([make_flag(FlagCode.MATH_MISMATCH, "m")]) is Ceiling.REJECT


def _reachable(tp, seen: set[type]) -> None:
    for arg in typing.get_args(tp):
        _reachable(arg, seen)
    if isinstance(tp, type):
        if issubclass(tp, BaseModel):
            if tp in seen:
                return
            seen.add(tp)
            for field in tp.model_fields.values():
                _reachable(field.annotation, seen)
        elif issubclass(tp, enum.Enum):
            seen.add(tp)
    elif isinstance(tp, (types.UnionType, typing._GenericAlias)):  # pragma: no cover
        pass


def test_checkpoint_types_cover_everything_reachable_from_state():
    seen: set[type] = set()
    _reachable(InvoiceState, seen)
    assert Decimal not in seen
    missing = seen - set(CHECKPOINT_TYPES)
    assert not missing, f"missing from CHECKPOINT_TYPES: {sorted(t.__name__ for t in missing)}"


def test_invoice_state_json_round_trip():
    inv = Invoice(
        invoice_number="INV-1", vendor_name="Widgets Inc.", vendor_key="widgets",
        invoice_date=dt.date(2026, 1, 2), line_items=[
            models.LineItem(item="WidgetA", item_key="widgeta", quantity=2, unit_price=Decimal("250.00")),
        ],
        total=Decimal("500.00"),
    )
    state = InvoiceState(
        trace_id="t", invoice_id=1, file_path="a/b.json", file_name="b.json", file_hash="h",
        format="json", invoice=inv, ceiling=Ceiling.HUMAN,
        flags=[make_flag(FlagCode.OVER_BUDGET, "over", amount="1.00")],
        extracted=ExtractedInvoice(invoice_number="1"),
        decision=models.Decision(final=Ceiling.HUMAN, by="vp", reasoning="r", payment_amount=Decimal("9975.00")),
    )
    again = InvoiceState.model_validate_json(state.model_dump_json())
    assert again == state
    assert again.decision.payment_amount == Decimal("9975.00")
    assert str(again.invoice.total) == "500.00"


def _walk(node, path=""):
    if isinstance(node, dict):
        yield path, node
        for k, v in node.items():
            yield from _walk(v, f"{path}/{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _walk(v, f"{path}/{i}")


@pytest.mark.parametrize("cls", [ExtractedInvoice, VPOutput, VPMemoOutput, CriticOutput])
def test_strict_schemas_have_no_defaults_and_all_required(cls):
    schema = to_strict_json_schema(cls)
    for path, node in _walk(schema):
        assert "default" not in node, f"default at {path}"
        props = node.get("properties")
        if isinstance(props, dict) and node.get("type") == "object":
            assert set(node.get("required", [])) == set(props), f"not all required at {path}"
