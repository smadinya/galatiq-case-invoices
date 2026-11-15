from agents.critic import critic_review
from llm.fake import ScriptedFake, critic, tool_call, vp
from core.invoice_math import recompute_totals
from models import Ceiling, Verdict

FILE = "invoice_9002.json"


def mk(make_state, make_invoice, **fields):
    inv = make_invoice([("WidgetA", 2, "250.00")])
    return make_state(inv, file_name=FILE, ceiling=Ceiling.APPROVE, totals=recompute_totals(inv),
                      vp_output=vp(Ceiling.APPROVE, "VP says fine"), **fields)


def test_round_increments_and_sees_nine_tools(make_state, make_invoice, make_deps, repo):
    st = mk(make_state, make_invoice)
    fake = ScriptedFake({(FILE, "critic"): [critic(Verdict.AGREE, Ceiling.APPROVE)]})
    out = critic_review(st, make_deps(fake))
    assert out["round"] == 1 and out["critic_output"].verdict == Verdict.AGREE
    assert out["llm_calls"] == {"critic": 1}
    assert len(fake.calls[0].tool_names) == 9
    assert {"get_source_evidence", "recompute_totals", "get_pipeline_trace"} <= set(fake.calls[0].tool_names)
    assert "VP says fine" in fake.calls[0].messages[0].content
    t = out["transcript"][-1]
    assert (t.actor, t.kind, t.round) == ("critic", "review", 0)
    assert any(a.event == "critic_review" and a.actor == "critic" for a in repo.audit_for_trace(st.trace_id))


def test_tool_call_then_second_round_indexes(make_state, make_invoice, make_deps):
    st = mk(make_state, make_invoice)
    obj = critic(Verdict.OBJECT, Ceiling.REJECT, objections=[("x", "e", "r")])
    fake = ScriptedFake({(FILE, "critic"): [tool_call("c", "recompute_totals"), obj, critic(Verdict.AGREE, Ceiling.APPROVE)]})
    deps = make_deps(fake)
    out = critic_review(st, deps)
    assert out["llm_calls"]["critic"] == 2 and out["transcript"][-1].tool_calls[0].name == "recompute_totals"
    st2 = st.model_copy(update=out)
    out2 = critic_review(st2, deps)
    assert out2["round"] == 2 and out2["critic_output"].verdict == Verdict.AGREE
    assert fake.calls[-1].ctx.call_index == 2
    assert "Your earlier objections" in fake.calls[-1].messages[0].content
