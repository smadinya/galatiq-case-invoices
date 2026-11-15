"""LangGraph wiring: node wrapper, routing functions, checkpointer."""

import sqlite3
import time
from pathlib import Path
from typing import Any, Callable

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, StateGraph

import agents.clamp as clamp
import agents.critic as critic
import agents.human as human
import agents.ingestion as ingestion
import agents.outcome as outcome
import agents.payment as payment
import agents.risk as risk
import agents.validation as validation
import agents.vp as vp
from config.settings import Settings
from deps import Deps
from models import (
    CHECKPOINT_TYPES,
    Ceiling,
    DedupeKind,
    FlagCode,
    InvoiceState,
    Status,
    Verdict,
)

NodeFn = Callable[[InvoiceState, Deps], dict]

NODES: dict[str, NodeFn] = {
    "load_document": ingestion.load_document,
    "parse_structured": ingestion.parse_structured,
    "extract_with_llm": ingestion.extract_with_llm,
    "ground_check": ingestion.ground_check,
    "normalize": ingestion.normalize,
    "dedupe": validation.dedupe_node,
    "validate": validation.validate_node,
    "score_risk": risk.score_risk,
    "compute_ceiling": validation.compute_ceiling,
    "vp_memo": vp.vp_memo,
    "vp_decide": vp.vp_decide,
    "critic_review": critic.critic_review,
    "clamp_decision": clamp.clamp_decision,
    "prepare_human": human.prepare_human,
    "await_human": human.await_human,
    "pay": payment.pay,
    "record_rejection": outcome.record_rejection,
    "record_duplicate": outcome.record_duplicate,
    "finish": outcome.finish,
}


# ---------------------------------------------------------------- checkpointer


def checkpoint_serde() -> JsonPlusSerializer:
    return JsonPlusSerializer(allowed_msgpack_modules=CHECKPOINT_TYPES)


def make_checkpointer(path: Path) -> tuple[SqliteSaver, sqlite3.Connection]:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    return SqliteSaver(conn, serde=checkpoint_serde()), conn


# --------------------------------------------------------------------- routing


def _unreadable(state: InvoiceState) -> bool:
    return any(f.code == FlagCode.UNREADABLE_DOCUMENT for f in state.flags)


def route_after_load(state: InvoiceState, settings: Settings) -> str:
    if _unreadable(state):
        return "prepare_human"
    return "parse_structured" if state.format in ("json", "csv", "xml") else "extract_with_llm"


def route_after_parse(state: InvoiceState, settings: Settings) -> str:
    return "prepare_human" if _unreadable(state) else "normalize"


def route_after_extract(state: InvoiceState, settings: Settings) -> str:
    return "ground_check"


def route_after_ground(state: InvoiceState, settings: Settings) -> str:
    if _unreadable(state):
        return "prepare_human"
    return "extract_with_llm" if state.grounding_errors else "normalize"


def route_after_normalize(state: InvoiceState, settings: Settings) -> str:
    return "prepare_human" if _unreadable(state) else "dedupe"


def route_after_dedupe(state: InvoiceState, settings: Settings) -> str:
    if state.dedupe is not None and state.dedupe.kind == DedupeKind.DUPLICATE:
        return "record_duplicate"
    return "validate"


def route_after_ceiling(state: InvoiceState, settings: Settings) -> str:
    return "vp_memo" if state.ceiling == Ceiling.REJECT else "vp_decide"


def route_after_vp_memo(state: InvoiceState, settings: Settings) -> str:
    return "record_rejection"


def route_after_vp_decide(state: InvoiceState, settings: Settings) -> str:
    return "critic_review" if state.needs_critic else "clamp_decision"


def route_after_critic(state: InvoiceState, settings: Settings) -> str:
    if (
        state.critic_output is not None
        and state.critic_output.verdict == Verdict.OBJECT
        and state.round < settings.max_critic_rounds
    ):
        return "vp_decide"
    return "clamp_decision"


def route_after_clamp(state: InvoiceState, settings: Settings) -> str:
    final = state.decision.final if state.decision else Ceiling.HUMAN
    if final == Ceiling.APPROVE:
        return "pay"
    if final == Ceiling.REJECT:
        return "record_rejection"
    return "prepare_human"


def route_after_prepare_human(state: InvoiceState, settings: Settings) -> str:
    return "await_human"


def route_after_human(state: InvoiceState, settings: Settings) -> str:
    out = state.human.outcome if state.human else None
    if out == "approved":
        return "pay"
    if out in ("rejected", "rejected_stock"):
        return "record_rejection"
    return "prepare_human"


def route_after_pay(state: InvoiceState, settings: Settings) -> str:
    if state.status in (Status.PAID, Status.CREDIT_DUE):
        return "finish"
    return "prepare_human" if state.payment_error else "finish"


ROUTES: dict[str, tuple[Callable[[InvoiceState, Settings], str], tuple[str, ...]]] = {
    "load_document": (route_after_load, ("prepare_human", "parse_structured", "extract_with_llm")),
    "parse_structured": (route_after_parse, ("prepare_human", "normalize")),
    "extract_with_llm": (route_after_extract, ("ground_check",)),
    "ground_check": (route_after_ground, ("prepare_human", "extract_with_llm", "normalize")),
    "normalize": (route_after_normalize, ("prepare_human", "dedupe")),
    "dedupe": (route_after_dedupe, ("record_duplicate", "validate")),
    "compute_ceiling": (route_after_ceiling, ("vp_memo", "vp_decide")),
    "vp_memo": (route_after_vp_memo, ("record_rejection",)),
    "vp_decide": (route_after_vp_decide, ("critic_review", "clamp_decision")),
    "critic_review": (route_after_critic, ("vp_decide", "clamp_decision")),
    "clamp_decision": (route_after_clamp, ("pay", "record_rejection", "prepare_human")),
    "prepare_human": (route_after_prepare_human, ("await_human",)),
    "await_human": (route_after_human, ("pay", "record_rejection", "prepare_human")),
    "pay": (route_after_pay, ("finish", "prepare_human")),
}
FIXED_EDGES = (
    ("validate", "score_risk"),
    ("score_risk", "compute_ceiling"),
    ("record_rejection", "finish"),
    ("record_duplicate", "finish"),
)


# ------------------------------------------------------------------ the graph


def _flag_keys(flags) -> set[tuple[str, str]]:
    return {(f.code.value, f.message) for f in flags}


def _wrap(name: str, fn: NodeFn, deps: Deps) -> Callable[[InvoiceState], dict]:
    def node(state: InvoiceState) -> dict[str, Any]:
        deps.log.event("node_start", trace_id=state.trace_id, node=name, invoice=state.file_name)
        start = time.perf_counter()
        if deps.node_hook is not None:
            deps.node_hook(name, state)
        update = dict(fn(state, deps) or {})
        ms = int((time.perf_counter() - start) * 1000)
        update["usage"] = (update.get("usage") or state.usage).plus_node(name, ms)
        if "flags" in update:
            before = _flag_keys(state.flags)
            for flag in update["flags"]:
                if (flag.code.value, flag.message) not in before:
                    deps.log.event(
                        "flag_raised", trace_id=state.trace_id, node=name, invoice=state.file_name,
                        data={"code": flag.code.value, "severity": flag.severity.value, "message": flag.message},
                    )
        deps.log.event(
            "node_end", trace_id=state.trace_id, node=name, invoice=state.file_name, duration_ms=ms
        )
        return update

    node.__name__ = name
    return node


def build_graph(deps: Deps, checkpointer: BaseCheckpointSaver | None):
    g = StateGraph(InvoiceState)
    for name, fn in NODES.items():
        g.add_node(name, _wrap(name, fn, deps))
    g.set_entry_point("load_document")
    for src, (route, targets) in ROUTES.items():
        g.add_conditional_edges(
            src, (lambda s, _r=route: _r(s, deps.settings)), {t: t for t in targets}
        )
    for a, b in FIXED_EDGES:
        g.add_edge(a, b)
    g.add_edge("finish", END)
    return g.compile(checkpointer=checkpointer)
