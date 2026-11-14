"""The Critic: independent review of the VP's proposal with a wider toolset."""

from typing import Any

from agents._loop import run_tool_loop
from agents.vp import _ctx, _fold, build_invoice_brief
from deps import Deps
from llm.base import load_prompt
from models import AgentTurn, CriticOutput, InvoiceState, Message
from tools.toolsets import critic_tools


def critic_review(state: InvoiceState, deps: Deps) -> dict[str, Any]:
    prompt = load_prompt("critic", deps.settings.prompts_dir)
    start = state.llm_calls.get("critic", 0)
    parts = [build_invoice_brief(state), "\nThe VP's decision (round %d):" % state.round]
    parts.append(state.vp_output.model_dump_json() if state.vp_output is not None else "(none)")
    if state.round >= 1 and state.critic_output is not None and state.critic_output.objections:
        parts.append("\nYour earlier objections:")
        parts.append(state.critic_output.model_dump_json())
    result = run_tool_loop(
        provider=deps.provider, ctx=_ctx(state, "critic", prompt.version, start), system=prompt.text,
        messages=[Message(role="user", content="\n".join(parts))], tools=critic_tools(deps, state),
        schema=CriticOutput, max_tool_calls=deps.settings.max_tool_calls_per_turn, log=deps.log,
    )
    out = result.output
    turn = AgentTurn(
        actor="critic", kind="review", round=state.round, tool_calls=result.tool_traces,
        output=out.model_dump(mode="json"),
    )
    deps.repo.audit(
        trace_id=state.trace_id, actor="critic", event="critic_review",
        payload={"round": state.round, "verdict": out.verdict.value, "proposed": out.proposed_decision.value,
                 "objections": [o.model_dump(mode="json") for o in out.objections],
                 "tools": [t.name for t in result.tool_traces]},
    )
    return {
        "critic_output": out,
        "round": state.round + 1,
        "transcript": [*state.transcript, turn],
        "llm_calls": {**state.llm_calls, "critic": start + result.calls},
        "usage": _fold(state.usage, result.usages),
    }
