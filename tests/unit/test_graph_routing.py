import pytest

from graph import (
    NODES, ROUTES, build_graph, route_after_clamp, route_after_critic, route_after_dedupe, route_after_ceiling,
    route_after_extract, route_after_ground, route_after_human, route_after_load, route_after_normalize,
    route_after_parse, route_after_pay, route_after_prepare_human, route_after_vp_decide, route_after_vp_memo,
)
from models import (
    Ceiling, Decision, DedupeKind, DedupeResult, FlagCode, HumanReview, InvoiceState, Status, Verdict, make_flag,
)
from llm.fake import critic


def st(**kw) -> InvoiceState:
    base = dict(trace_id="t", invoice_id=1, file_path="p", file_name="f", file_hash="h")
    base.update(kw)
    return InvoiceState(**base)


UNREADABLE = [make_flag(FlagCode.UNREADABLE_DOCUMENT, "x")]


@pytest.mark.parametrize("fmt,expected", [
    ("json", "parse_structured"), ("csv", "parse_structured"), ("xml", "parse_structured"),
    ("txt", "extract_with_llm"), ("pdf", "extract_with_llm"),
])
def test_after_load(settings, fmt, expected):
    assert route_after_load(st(format=fmt), settings) == expected


def test_after_load_unreadable_first(settings):
    assert route_after_load(st(format="json", flags=UNREADABLE), settings) == "prepare_human"


def test_after_parse_and_normalize(settings):
    assert route_after_parse(st(), settings) == "normalize"
    assert route_after_parse(st(flags=UNREADABLE), settings) == "prepare_human"
    assert route_after_normalize(st(), settings) == "dedupe"
    assert route_after_normalize(st(flags=UNREADABLE), settings) == "prepare_human"


def test_after_extract_and_ground(settings):
    assert route_after_extract(st(), settings) == "ground_check"
    assert route_after_ground(st(), settings) == "normalize"
    assert route_after_ground(st(grounding_errors=["bad"]), settings) == "extract_with_llm"
    assert route_after_ground(st(grounding_errors=["bad"], flags=UNREADABLE), settings) == "prepare_human"


def test_after_dedupe(settings):
    dup = DedupeResult(kind=DedupeKind.DUPLICATE)
    assert route_after_dedupe(st(dedupe=dup), settings) == "record_duplicate"
    assert route_after_dedupe(st(dedupe=DedupeResult(kind=DedupeKind.NEW)), settings) == "validate"
    assert route_after_dedupe(st(), settings) == "validate"


def test_after_ceiling_and_vp(settings):
    assert route_after_ceiling(st(ceiling=Ceiling.REJECT), settings) == "vp_memo"
    assert route_after_ceiling(st(ceiling=Ceiling.HUMAN), settings) == "vp_decide"
    assert route_after_ceiling(st(ceiling=Ceiling.APPROVE), settings) == "vp_decide"
    assert route_after_vp_memo(st(), settings) == "record_rejection"
    assert route_after_vp_decide(st(needs_critic=True), settings) == "critic_review"
    assert route_after_vp_decide(st(needs_critic=False), settings) == "clamp_decision"


def test_after_critic(settings):
    obj = critic(Verdict.OBJECT, Ceiling.HUMAN)
    agree = critic(Verdict.AGREE, Ceiling.APPROVE)
    assert route_after_critic(st(critic_output=obj, round=1), settings) == "vp_decide"
    assert route_after_critic(st(critic_output=obj, round=settings.max_critic_rounds), settings) == "clamp_decision"
    assert route_after_critic(st(critic_output=agree, round=1), settings) == "clamp_decision"


@pytest.mark.parametrize("final,expected", [
    (Ceiling.APPROVE, "pay"), (Ceiling.REJECT, "record_rejection"), (Ceiling.HUMAN, "prepare_human"),
])
def test_after_clamp(settings, final, expected):
    d = Decision(final=final, by="vp", reasoning="r")
    assert route_after_clamp(st(decision=d), settings) == expected


def test_after_human(settings):
    def h(outcome):
        return st(human=HumanReview(reason=FlagCode.FOREIGN_CURRENCY, reasons=[FlagCode.FOREIGN_CURRENCY],
                                    outcome=outcome))
    assert route_after_prepare_human(st(), settings) == "await_human"
    assert route_after_human(h("approved"), settings) == "pay"
    assert route_after_human(h("rejected"), settings) == "record_rejection"
    assert route_after_human(h("rejected_stock"), settings) == "record_rejection"
    assert route_after_human(h("requeued"), settings) == "prepare_human"


def test_after_pay(settings):
    assert route_after_pay(st(status=Status.PAID), settings) == "finish"
    assert route_after_pay(st(status=Status.CREDIT_DUE), settings) == "finish"
    assert route_after_pay(st(payment_error="boom"), settings) == "prepare_human"


def test_graph_covers_node_table(make_deps):
    graph = build_graph(make_deps(), None)
    names = set(graph.get_graph().nodes) - {"__start__", "__end__"}
    assert names == set(NODES)
    assert set(ROUTES) <= set(NODES)
