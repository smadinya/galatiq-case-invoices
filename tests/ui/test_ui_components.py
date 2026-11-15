import re

import pytest
from streamlit.testing.v1 import AppTest

from models import Ceiling, FlagCode, InvoiceResult, Severity, Status, make_flag
from test_ui_case import build_cases

EVIL = "![x](https://example.invalid/p.png) <img src=x onerror=1>"


@pytest.fixture(scope="module")
def cases(tmp_path_factory):
    return build_cases(tmp_path_factory.mktemp("comp"))


def js(r):
    return r.model_dump(mode="json")


def run(fn, *args, **kwargs):
    at = AppTest.from_function(fn, args=args, kwargs=kwargs, default_timeout=30).run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def _case_app(result, state):
    from models import InvoiceResult, InvoiceState
    from ui.kit.case import CaseDetail
    from ui.kit.components import render_case

    res = InvoiceResult.model_validate(result)
    st_ = InvoiceState.model_validate(state) if state else None
    render_case(res, CaseDetail(st_, st_.raw_text if st_ else None, "text", None), key="c", links=False)


def test_render_case_1010(cases):
    res, st = cases["invoice_1010.txt"]
    at = run(_case_app, js(res), js(st))
    assert len(at.chat_message) >= 4
    assert [h.value for h in at.header] == [r"Rejected by the VP and Critic\: price above catalog"]


def test_render_case_every_sample(cases):
    for res, st in cases.values():
        run(_case_app, js(res), js(st))
        run(_case_app, js(res), None)


def test_render_case_error():
    res = InvoiceResult(trace_id="inv-x-1", file="invoice_1007.csv", status=Status.ERROR,
                        error="TypeError: boom", provider="replay")
    at = run(_case_app, js(res), None)
    text = " ".join(m.value for m in at.markdown)
    assert "This is a bug, not a business decision" in text.replace("\\", "")
    assert [h.value for h in at.header] == [r"System error\: nothing was paid"]
    assert not at.button  # nothing unless the page passes after_error


def _error_after_app(result):
    import streamlit as st
    from models import InvoiceResult
    from ui.kit.components import render_case

    def again():
        st.button("Process again", key="process.again")
        st.caption("after-error marker")

    render_case(InvoiceResult.model_validate(result), None, key="c", links=False, after_error=again)


def test_render_case_error_calls_after_error_under_the_container():
    res = InvoiceResult(trace_id="inv-x-1", file="invoice_1007.csv", status=Status.ERROR,
                        error="TypeError: boom", provider="replay")
    at = run(_error_after_app, js(res))
    assert [b.key for b in at.button] == ["process.again"]
    assert "after-error marker" in [c.value for c in at.caption]


def test_after_error_is_ignored_for_a_normal_case(cases):
    res, st = cases["invoice_1010.txt"]
    def app(result, state):
        import streamlit as st_
        from models import InvoiceResult
        from ui.kit.components import render_case
        render_case(InvoiceResult.model_validate(result), None, key="c", links=False,
                    after_error=lambda: st_.button("nope", key="nope"))
    at = run(app, js(res), js(st))
    assert all(b.key != "nope" for b in at.button)


def _flags_app():
    from models import FlagCode, make_flag
    from ui.kit.components import flag_list

    flag_list([make_flag(FlagCode.PAST_DUE, "p"), make_flag(FlagCode.MISSING_VENDOR, "m"),
               make_flag(FlagCode.FOREIGN_CURRENCY, "f"), make_flag(FlagCode.UNKNOWN_VENDOR, "u")])


def test_flag_list_order():
    at = run(_flags_app)
    badges = [m.value for m in at.markdown if "-badge[" in m.value]
    assert [b.split("]")[0].split(" ", 1)[1] for b in badges] == [
        "Blocks payment (1)", "Needs a person (1)", "VP must answer (1)", "For information (1)"]
    assert at.markdown[1].value.startswith("**No vendor.**")


def _evil_app(result):
    from models import InvoiceResult
    from ui.kit.case import CaseDetail
    from ui.kit.components import render_case

    render_case(InvoiceResult.model_validate(result), CaseDetail(None, "line <b>$5</b>\n" + "![x](https://example.invalid/p.png)", "text", None),
                key="e", links=False)


def test_untrusted_text_is_inert(cases):
    res, _ = cases["invoice_1010.txt"]
    turns = [t.model_copy(update={"output": {**t.output, "reasoning": EVIL} if "reasoning" in t.output else t.output})
             for t in res.transcript]
    res = res.model_copy(update={"vendor": EVIL, "transcript": turns})
    res.decision.reasoning = EVIL
    at = run(_evil_app, js(res))
    seen = [m.value for m in at.markdown] + [c.value for c in at.caption]
    assert any("example" in s for s in seen)
    for s in seen:
        bare = re.sub(r"\\.", "", s)  # drop backslash-escaped characters: what Markdown would still act on
        assert "![" not in bare and "<img" not in bare
    for el in at.get("html"):
        body = el.proto.body
        assert "<img" not in body and "onerror" not in body.replace("&lt;img src=x onerror=1&gt;", "")
        assert "<b>" not in body


def _viz_app():
    from models import Status
    from ui.kit import components as c

    c.segmented_bar([(Status.PAID, 5), (Status.REJECTED, 9), (Status.PENDING_HUMAN, 3), (Status.DUPLICATE, 3)])
    c.meter_list([("WidgetA", 56, 90), ("FakeItem", 0, 0)])
    c.kv_block([("Pays", "$1 <b>x</b>")])
    c.status_badge(Status.PAID)
    c.ceiling_badge(__import__("models").Ceiling.HUMAN)


def test_bar_and_meters():
    at = run(_viz_app)
    bodies = [el.proto.body for el in at.get("html")]
    assert 'aria-label="5 paid, 9 rejected, 3 need a person, 3 duplicates"' in bodies[0]
    assert "56 of 90" in bodies[1] and "none in stock" in bodies[1] and 'width:62%' in bodies[1]
    assert "&lt;b&gt;" in bodies[2] and "&#36;1" in bodies[2]


def test_stylesheet_out_ranks_streamlits_list_padding_without_its_generated_classes():
    from ui.kit import components, theme

    assert "st-emotion" not in theme._STYLE and "st-emotion" not in components._BAR_STYLE
    for cls in (".k-kv.k-kv", ".k-meters.k-meters", ".k-legend.k-legend", ".k-src.k-src"):
        assert cls in theme._STYLE + components._BAR_STYLE
    assert len(theme._STYLE.strip().splitlines()) <= 40


def _line_cols(items, recomputed):
    from types import SimpleNamespace as N

    from ui.kit.components import _line_columns

    inv = N(currency="USD", line_items=[N(item=i, quantity=1, unit_price=5, amount=a, note=n) for i, a, n in items])
    checks = {i: N(recomputed_amount=r) for i, r in enumerate(recomputed)}
    return _line_columns(inv, checks)


def test_line_columns_drop_note_and_recomputed_when_empty_or_equal():
    cols = _line_cols([("a", 5, None), ("b", 10, "")], [5, 10])
    assert list(cols) == ["#", "Item", "Qty", "Unit price", "Amount"]


def test_line_columns_keep_recomputed_on_mismatch_and_fold_note_into_item():
    cols = _line_cols([("a", 5, "see <b>x</b>"), ("b", 10, None)], [5, 12])
    assert "Recomputed" in cols and "Note" not in cols
    assert cols["Item"] == ["a (see <b>x</b>)", "b"]


def test_line_columns_missing_stated_amount_counts_as_mismatch():
    assert "Recomputed" in _line_cols([("a", None, None)], [5])
