"""Live progress: the grouped run log (design 5.1) built from a job's events.

`timeline` and `status_label` are pure. `render_timeline` is display-only (no widgets), so it can
run inside `follow`, which re-renders into one placeholder every `interval` seconds.
"""

import time
from dataclasses import dataclass
from typing import Callable, Literal, Sequence

import streamlit as st

from models import Status
from ui.kit import copy, fmt
from ui.kit.jobs import Job, JobEvent, JobSnapshot

_STAGE_ORDER = [key for key, _ in copy.STAGES]


@dataclass(frozen=True)
class NodeStep:
    node: str
    label: str
    state: Literal["active", "done"]
    detail: str
    ms: int | None
    visits: int


@dataclass(frozen=True)
class StageView:
    key: copy.StageKey
    label: str
    state: Literal["pending", "active", "done", "skipped"]
    steps: tuple[NodeStep, ...]
    notes: tuple[str, ...]  # retries and payment attempts


def _retry_note(event: JobEvent) -> str:
    attempt = event.data.get("attempt")
    if event.data.get("stage") == "payment":
        return f"Payment attempt {attempt} after a failure"
    return f"Asked the model again (attempt {attempt})"


def timeline(events: Sequence[JobEvent], file: str, *, finished: bool) -> list[StageView]:
    """Group `file`'s events by stage. Stages: read, check, approve, finish.

    A node is *active* from its `node_start` until its `node_end`; an `update` supplies the detail
    (and creates a finished step when no start was logged). An `interrupt` ends the paused node.
    A stage is *done* when all its steps ended, *skipped* when the run finished without visiting it,
    and *pending* otherwise (also after an error: the stages the run never reached).
    """
    order: list[str] = []
    state: dict[str, str] = {}
    detail: dict[str, str] = {}
    ms: dict[str, int] = {}
    visits: dict[str, int] = {}
    notes: dict[str, list[str]] = {k: [] for k in _STAGE_ORDER}
    interrupted = errored = False

    for e in events:
        if e.file not in (file, None):
            continue
        node = e.node
        if e.kind == "interrupt":
            interrupted = True
        elif e.kind == "error":
            errored = True
        if node is None or node in copy.HIDDEN_NODES or node not in copy.NODE_STAGE:
            continue
        if e.kind == "node_start":
            if node not in state:
                order.append(node)
            state[node] = "active"
            visits[node] = visits.get(node, 0) + 1
        elif e.kind == "node_end":
            if node not in state:
                order.append(node)
                visits[node] = 1
            state[node] = "done"
            ms[node] = ms.get(node, 0) + int(e.data.get("duration_ms") or 0)
        elif e.kind == "update":
            if node not in state:
                order.append(node)
                visits[node] = 1
                state[node] = "done"
            detail[node] = str(e.data.get("detail") or "")
        elif e.kind == "retry":
            notes[copy.NODE_STAGE[node]].append(_retry_note(e))

    if interrupted:
        for node, s in state.items():
            if s == "active":
                state[node] = "done"
    if finished and any(s == "active" for s in state.values()):
        errored = True  # the run ended inside a node

    by_stage: dict[str, list[NodeStep]] = {k: [] for k in _STAGE_ORDER}
    for node in order:
        by_stage[copy.NODE_STAGE[node]].append(
            NodeStep(node, copy.NODE_LABEL.get(node, node), state[node], detail.get(node, ""),  # type: ignore[arg-type]
                     ms.get(node), visits.get(node, 1))
        )

    views = []
    for key, label in copy.STAGES:
        steps = tuple(by_stage[key])
        if any(s.state == "active" for s in steps):
            stage_state = "active"
        elif steps:
            later = any(by_stage[k] for k in _STAGE_ORDER[_STAGE_ORDER.index(key) + 1:])
            stage_state = "done" if (finished or later) else "active"
        elif finished and not errored:
            stage_state = "skipped"
        else:
            stage_state = "pending"
        views.append(StageView(key, label, stage_state, steps, tuple(notes[key])))  # type: ignore[arg-type]
    return views


def render_timeline(stages: Sequence[StageView]) -> None:
    """The grouped run log: a heading per stage that ran, one line per step. Display-only."""
    for stage in stages:
        if not stage.steps:
            continue
        st.markdown(f"##### {fmt.md(stage.label)}")
        lines = []
        for step in stage.steps:
            icon = ":material/check:" if step.state == "done" else ":material/progress_activity:"
            if step.state == "active":
                text = copy.active_label(step.node)
            else:
                text = f"{step.label}: {step.detail}" if step.detail else step.label
            lines.append(f"{icon} {fmt.md(text)}")
        st.markdown("\n\n".join(lines))
        for note in stage.notes:
            st.caption(fmt.md(note))


def status_label(snapshot: JobSnapshot, file: str) -> str:
    """The running label ("VP is deciding…") or the completion label with seconds and model calls."""
    if snapshot.running:
        node = None
        for e in reversed(snapshot.events):
            if e.kind == "node_start" and e.file in (file, None):
                node = e.node
                break
        return copy.active_label(node) if node else f"Processing {file}"
    result = next((r for r in snapshot.results if r.file == file), None)
    if result is None and snapshot.value is not None and getattr(snapshot.value, "file", None) == file:
        result = snapshot.value
    if result is None or result.status == Status.ERROR:
        return copy.t("error.system", file=file)
    total_ms = sum(result.usage.node_ms.values())
    calls = result.usage.llm_calls
    return f"Processed {file} in {fmt.seconds(total_ms)}. {calls} model {'call' if calls == 1 else 'calls'}."


def follow(job: Job, render: Callable[[JobSnapshot], None], *, interval: float = 0.3) -> JobSnapshot:
    """Re-render `render(snapshot)` into one placeholder until the job ends; returns the final snapshot.

    `render` must be display-only: widgets created inside this loop would raise DuplicateWidgetID.
    """
    placeholder = st.empty()
    while True:
        snapshot = job.snapshot()
        with placeholder.container():
            render(snapshot)
        if not snapshot.running:
            return snapshot
        time.sleep(interval)
