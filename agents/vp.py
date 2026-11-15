"""VP of Finance: invoice brief, rejection memo and the tool-using decision."""

import json
from typing import Any

from agents._loop import LoopResult, run_structured, run_tool_loop
from core.text import quote_untrusted
from core.values import money_str
from deps import Deps
from llm.base import load_prompt
from models import (
    AgentTurn,
    CallContext,
    Ceiling,
    Decision,
    InvoiceState,
    Message,
    Severity,
    Usage,
    VPMemoOutput,
    VPOutput,
)
from tools.toolsets import vp_tools


def _money(value: Any) -> str:
    return "(none)" if value is None else money_str(value)


def build_invoice_brief(state: InvoiceState) -> str:
    """Text the VP and Critic see. Never raw_text or evidence_spans; free text is quoted."""
    out: list[str] = [f"File name: {state.file_name}"]
    inv = state.invoice
    if inv is None:
        out.append("No normalized invoice is available.")
    else:
        out += [
            f"Invoice number: {inv.invoice_number or '(none)'}",
            f"Revision: {inv.revision or '(none)'}",
            f"Is revision: {inv.is_revision}",
            f"Invoice date: {inv.invoice_date.isoformat() if inv.invoice_date else '(none)'}",
            f"Due date: {inv.due_date.isoformat() if inv.due_date else '(none)'}",
            f"Currency: {inv.currency}",
            quote_untrusted("Vendor name", inv.vendor_name),
            quote_untrusted("Vendor former name", inv.vendor_former_name),
            quote_untrusted("Vendor address", inv.vendor_address),
            quote_untrusted("Payment terms", inv.payment_terms),
            quote_untrusted("Notes", inv.notes),
            "Line items:",
        ]
        for i, li in enumerate(inv.line_items, 1):
            out.append(
                f"  {i}. {quote_untrusted('item', li.item)}; quantity {li.quantity}; "
                f"unit price {money_str(li.unit_price)}; stated amount {_money(li.amount)}; "
                f"{quote_untrusted('line note', li.note)}"
            )
        out += [
            f"Subtotal: {_money(inv.subtotal)}",
            f"Tax rate: {inv.tax_rate if inv.tax_rate is not None else '(none)'}",
            f"Tax amount: {_money(inv.tax_amount)}",
            f"Shipping: {_money(inv.shipping)}",
            f"Total stated: {money_str(inv.total)}",
        ]
    if state.totals is not None:
        t = state.totals
        out.append(
            f"Recomputed total: {money_str(t.total_recomputed)} (difference {money_str(t.difference)}); "
            f"totals ok: {t.total_ok}"
        )
    out.append("Flags:")
    if not state.flags:
        out.append("  (none)")
    for f in state.flags:
        out.append(f"  - {f.code.value} [{f.severity.value}] {quote_untrusted('message', f.message)}")
    out += [
        f"Ceiling: {state.ceiling.value if state.ceiling else '(none)'}",
        f"Address check: {state.address_check or '(none)'}",
        f"Amount USD: {_money(state.amount_usd)}",
        f"Payable amount: {_money(state.payable_amount)}",
        f"Round: {state.round}",
    ]
    return "\n".join(out)


def _ctx(state: InvoiceState, stage: str, version: str, index: int) -> CallContext:
    return CallContext(
        trace_id=state.trace_id, file_hash=state.file_hash, file_name=state.file_name,
        stage=stage, prompt_version=version, call_index=index,
    )


def _fold(usage: Usage, usages: list) -> Usage:
    for u in usages:
        usage = usage.plus_call(u)
    return usage


def vp_memo(state: InvoiceState, deps: Deps) -> dict[str, Any]:
    prompt = load_prompt("vp_memo", deps.settings.prompts_dir)
    start = state.llm_calls.get("vp_memo", 0)
    user = (
        build_invoice_brief(state)
        + "\n\nThe rules have fixed the outcome as REJECT. Write the memo explaining it."
    )
    out, usage = run_structured(
        provider=deps.provider,
        ctx=_ctx(state, "vp_memo", prompt.version, start),
        system=prompt.text,
        messages=[Message(role="user", content=user)],
        schema=VPMemoOutput,
        log=deps.log,
    )
    turn = AgentTurn(actor="vp", kind="memo", round=state.round, output=out.model_dump(mode="json"))
    deps.repo.audit(trace_id=state.trace_id, actor="vp", event="vp_memo", payload={"memo": out.memo})
    return {
        "decision": Decision(final=Ceiling.REJECT, by="rules", reasoning=out.memo),
        "transcript": [*state.transcript, turn],
        "llm_calls": {**state.llm_calls, "vp_memo": start + 1},
        "usage": state.usage.plus_call(usage),
    }


def _norm(s: str) -> str:
    return " ".join(s.lower().split())


def _missing(state: InvoiceState, out: VPOutput) -> list[str]:
    problems: list[str] = []
    addressed = {r.code for r in out.flags_addressed}
    for f in state.flags:
        if f.severity == Severity.ESCALATION and f.code not in addressed:
            problems.append(f"flags_addressed is missing escalation flag {f.code.value}")
    co = state.critic_output
    if co is not None and co.objections:
        answers = [_norm(a.objection) for a in out.objections_answered]
        unanswered = []
        for ob in co.objections:
            keys = [_norm(k) for k in (ob.about, ob.requested_change, ob.evidence) if k]
            if not any(a and (a in k or k in a) for a in answers for k in keys):
                unanswered.append(ob)
        if unanswered and len(out.objections_answered) < len(co.objections):
            for ob in unanswered:
                problems.append(f"objections_answered does not answer the Critic objection: {ob.requested_change}")
    return problems


def _round_context(state: InvoiceState) -> str:
    if state.round < 1:
        return ""
    parts = ["\n\nThis is a later round. Your previous decision:"]
    if state.vp_output is not None:
        parts.append(state.vp_output.model_dump_json())
    if state.critic_output is not None:
        parts.append("The Critic's review (answer every objection in objections_answered):")
        parts.append(state.critic_output.model_dump_json())
    return "\n".join(parts)


def vp_decide(state: InvoiceState, deps: Deps) -> dict[str, Any]:
    prompt = load_prompt("vp", deps.settings.prompts_dir)
    tools = vp_tools(deps, state)
    start = state.llm_calls.get("vp", 0)
    user = build_invoice_brief(state) + _round_context(state)
    messages = [Message(role="user", content=user)]

    def loop(msgs: list[Message], index: int) -> LoopResult[VPOutput]:
        return run_tool_loop(
            provider=deps.provider, ctx=_ctx(state, "vp", prompt.version, index), system=prompt.text,
            messages=msgs, tools=tools, schema=VPOutput,
            max_tool_calls=deps.settings.max_tool_calls_per_turn, log=deps.log,
        )

    result = loop(messages, start)
    traces, usages, calls = list(result.tool_traces), list(result.usages), result.calls
    problems = _missing(state, result.output)
    if problems:
        reask = messages + [
            Message(role="assistant", content=result.output.model_dump_json()),
            Message(role="user", content="Your answer is incomplete: " + "; ".join(problems) + ". Answer again, completely."),
        ]
        second = loop(reask, start + calls)
        traces += second.tool_traces
        usages += second.usages
        calls += second.calls
        result = second
        problems = _missing(state, result.output)
        if problems:
            deps.log.event(
                "error", trace_id=state.trace_id, node="vp_decide", invoice=state.file_name,
                data={"kind": "vp_incomplete", "problems": problems},
            )
    out = result.output
    turn = AgentTurn(
        actor="vp", kind="decision", round=state.round, tool_calls=traces, output=out.model_dump(mode="json")
    )
    deps.repo.audit(
        trace_id=state.trace_id, actor="vp", event="vp_decision",
        payload={"round": state.round, "decision": out.decision.value, "reasoning": out.reasoning,
                 "tools": [t.name for t in traces]},
    )
    return {
        "vp_output": out,
        "transcript": [*state.transcript, turn],
        "llm_calls": {**state.llm_calls, "vp": start + calls},
        "usage": _fold(state.usage, usages),
    }
