"""Background jobs: one write lock, one worker thread per job, thread-safe snapshots for the page.

The script thread never runs a pipeline. It starts a job here and polls `Job.snapshot()`.
Nothing in this module imports Streamlit, so a worker thread can never call `st.*`.

A job's callbacks (`on_update`, `observe`) never raise, because an exception inside the
pipeline would turn the invoice into an ERROR. The one deliberate exception is
`BatchStopped` from `on_result`, which `Service.run_batch` lets propagate.
"""

import dataclasses
import datetime as dt
import itertools
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Literal, Mapping

from models import AgentTurn, Ceiling, Decision, Flag, InvoiceResult, Status
from obs import EventLogger
from ui.kit import copy

JobKind = Literal["invoice", "batch", "resolve", "reset"]
JobState = Literal["running", "done", "failed", "stopped"]


class Busy(Exception):
    """The write lock is held. `job` is the running Job, or None when something else holds it."""

    def __init__(self, job: "Job | None" = None) -> None:
        super().__init__("A run is in progress.")
        self.job = job


class BatchStopped(Exception):
    """Raised by `JobContext.on_result` after the current invoice when a stop was requested."""


@dataclass(frozen=True)
class JobEvent:
    seq: int
    at: float  # time.time()
    kind: str  # an obs event name, or "update" | "result"
    file: str | None
    node: str | None
    text: str
    data: Mapping[str, Any]


@dataclass(frozen=True)
class LiveInvoice:
    file: str
    trace_id: str | None = None
    ceiling: Ceiling | None = None
    flags: tuple[Flag, ...] = ()
    transcript: tuple[AgentTurn, ...] = ()
    decision: Decision | None = None
    status: Status | None = None


@dataclass(frozen=True)
class JobSnapshot:
    id: str
    kind: JobKind
    label: str
    state: JobState
    started_at: dt.datetime
    finished_at: dt.datetime | None
    total_files: int | None
    events: tuple[JobEvent, ...]
    live: Mapping[str, LiveInvoice]
    results: tuple[InvoiceResult, ...]
    value: Any
    error: BaseException | None
    stop_requested: bool

    @property
    def running(self) -> bool:
        return self.state == "running"

    @property
    def current_file(self) -> str | None:
        for event in reversed(self.events):
            if event.file:
                return event.file
        return None


# Nodes after which a paced (slowed) replay waits longer: the four model steps.
SLOW_NODES = frozenset({"extract_with_llm", "vp_decide", "vp_memo", "critic_review"})
PACE_STEP_S = 0.3
PACE_SLOW_S = 0.8

_LIVE_FIELDS = {
    "ceiling": lambda v: v,
    "flags": lambda v: tuple(v or ()),
    "transcript": lambda v: tuple(v or ()),
    "decision": lambda v: v,
    "status": lambda v: v,
}


def _small(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


class JobContext:
    """What the worker function receives. Thread-safe; callbacks never raise (except on_result)."""

    def __init__(self, job: "Job") -> None:
        self._job = job
        self._pace: Callable[[float], None] | None = None

    def set_pace(self, sleep: Callable[[float], None] | None) -> None:
        """Slow `on_update` for viewing: 0.3 s after each update, 0.8 s after a model step."""
        self._pace = sleep

    def on_update(self, file_name: str, node: str, update: dict) -> None:
        try:
            self._job._add_update(file_name, node, update)
        except Exception:
            pass
        pace = self._pace
        if pace is not None:
            try:
                pace(PACE_SLOW_S if node in SLOW_NODES else PACE_STEP_S)
            except Exception:
                pass

    def on_result(self, result: InvoiceResult) -> None:
        self._job._add_result(result)
        if self._job.stop_requested:
            raise BatchStopped()

    def observe(self, record: dict[str, Any]) -> None:
        try:
            self._job._add_record(record)
        except Exception:
            pass


class Job:
    def __init__(
        self, kind: JobKind, label: str, *, total_files: int | None = None, keep_events: int = 5000
    ) -> None:
        self.id = uuid.uuid4().hex[:10]
        self.kind: JobKind = kind
        self.label = label
        self._total = total_files
        self._lock = threading.Lock()
        self._seq = itertools.count(1)
        self._events: list[JobEvent] = []
        self._keep_events = keep_events
        self._live: dict[str, LiveInvoice] = {}
        self._results: list[InvoiceResult] = []
        self._state: JobState = "running"
        self._value: Any = None
        self._error: BaseException | None = None
        self._stop = threading.Event()
        self._done = threading.Event()
        self._started = dt.datetime.now(dt.timezone.utc)
        self._finished: dt.datetime | None = None

    # ---------------------------------------------------------------- reading

    @property
    def stop_requested(self) -> bool:
        return self._stop.is_set()

    def snapshot(self) -> JobSnapshot:
        with self._lock:
            return JobSnapshot(
                id=self.id, kind=self.kind, label=self.label, state=self._state,
                started_at=self._started, finished_at=self._finished, total_files=self._total,
                events=tuple(self._events), live=dict(self._live), results=tuple(self._results),
                value=self._value, error=self._error, stop_requested=self._stop.is_set(),
            )

    @property
    def state(self) -> JobState:
        with self._lock:
            return self._state

    def request_stop(self) -> None:
        self._stop.set()

    def wait(self, timeout: float | None = None) -> bool:
        return self._done.wait(timeout)

    # ---------------------------------------------------------------- writing

    def _append(self, kind: str, file: str | None, node: str | None, text: str, data: Mapping[str, Any]) -> None:
        # caller holds self._lock
        self._events.append(JobEvent(next(self._seq), time.time(), kind, file, node, text, data))
        if len(self._events) > self._keep_events:
            del self._events[: len(self._events) - self._keep_events]

    def _live_for(self, file: str) -> LiveInvoice:
        return self._live.get(file) or LiveInvoice(file=file)

    def _add_update(self, file_name: str, node: str, update: Mapping[str, Any]) -> None:
        text = copy.node_line(node, update)
        with self._lock:
            changes = {k: conv(update[k]) for k, conv in _LIVE_FIELDS.items() if k in update}
            self._live[file_name] = dataclasses.replace(self._live_for(file_name), **changes)
            self._append("update", file_name, node, text, {"detail": copy.node_detail(node, update)})

    def _add_result(self, result: InvoiceResult) -> None:
        with self._lock:
            self._results.append(result)
            live = self._live_for(result.file)
            self._live[result.file] = dataclasses.replace(
                live, trace_id=result.trace_id, status=result.status, ceiling=result.ceiling or live.ceiling
            )
            self._append("result", result.file, None, result.status.value, {"status": result.status.value})

    def _add_record(self, record: Mapping[str, Any]) -> None:
        event = str(record.get("event") or "")
        node = record.get("node")
        file = record.get("invoice")
        data = record.get("data") or {}
        extra: dict[str, Any] = {}
        for key in ("duration_ms", "tokens_in", "tokens_out"):
            if record.get(key) is not None:
                extra[key] = record[key]
        for key in ("tool", "attempt", "outcome", "stage", "status"):
            if key in data:
                extra[key] = _small(data[key])
        if event in ("node_start", "node_end"):
            text = copy.NODE_LABEL.get(str(node), str(node))
        elif event == "tool_call":
            text = str(data.get("tool") or "tool")
        elif event == "retry":
            text = f"retry {data.get('stage', '')} attempt {data.get('attempt', '')}".strip()
        else:
            text = event.replace("_", " ")
        with self._lock:
            trace = record.get("trace_id")
            if file and trace and not str(trace).startswith("batch-"):
                live = self._live_for(file)
                if live.trace_id != trace:
                    self._live[file] = dataclasses.replace(live, trace_id=str(trace))
            self._append(event, file, str(node) if node else None, text, extra)

    def _finish(self, state: JobState, value: Any = None, error: BaseException | None = None) -> None:
        with self._lock:
            self._state = state
            self._value = value
            self._error = error
            self._finished = dt.datetime.now(dt.timezone.utc)


class JobRunner:
    """Runs jobs under ONE global lock. The lock is held for a job's whole run.

    `preview` and the provider probe acquire `lock` non-blocking, so they never wait behind a job.
    """

    def __init__(self, *, inline: bool = False, keep: int = 20) -> None:
        self.inline = inline
        self.keep = keep
        self.lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._jobs: list[Job] = []

    def submit(
        self, kind: JobKind, label: str, fn: Callable[[JobContext], Any], *, total_files: int | None = None
    ) -> Job:
        if not self.lock.acquire(blocking=False):
            raise Busy(self.current())
        job = Job(kind, label, total_files=total_files)
        with self._state_lock:
            self._jobs.append(job)
        ctx = JobContext(job)
        if self.inline:
            self._run(job, ctx, fn)
        else:
            threading.Thread(target=self._run, args=(job, ctx, fn), name=f"acme-job-{job.id}", daemon=True).start()
        return job

    def _run(self, job: Job, ctx: JobContext, fn: Callable[[JobContext], Any]) -> None:
        state: JobState = "failed"
        value: Any = None
        error: BaseException | None = None
        try:
            value = fn(ctx)
            state = "done"
        except BatchStopped:
            state = "stopped"
        except Exception as exc:
            error = exc
        finally:
            job._finish(state, value, error)
            self.lock.release()
            job._done.set()  # after the release, so a waiter can start the next job at once
            self._trim()

    def _trim(self) -> None:
        with self._state_lock:
            finished = [j for j in self._jobs if j.state != "running"]
            for old in finished[: max(0, len(finished) - self.keep)]:
                self._jobs.remove(old)

    def current(self) -> Job | None:
        with self._state_lock:
            for job in reversed(self._jobs):
                if job.state == "running":
                    return job
        return None

    def get(self, job_id: str) -> Job | None:
        with self._state_lock:
            return next((j for j in self._jobs if j.id == job_id), None)

    def latest(self, kind: JobKind | None = None) -> Job | None:
        with self._state_lock:
            for job in reversed(self._jobs):
                if kind is None or job.kind == kind:
                    return job
        return None

    def clear_history(self) -> None:
        with self._state_lock:
            self._jobs = [j for j in self._jobs if j.state == "running"]


class TeeLogger:
    """An `obs.EventLogger` that forwards to `inner`, then copies the record to `sink`.

    The record has the same shape as a JSONL line. A sink that raises never breaks `event()`.
    """

    def __init__(self, inner: EventLogger, sink: Callable[[dict[str, Any]], None]) -> None:
        self._inner = inner
        self._sink = sink

    def event(
        self,
        event: str,
        *,
        trace_id: str,
        node: str | None = None,
        invoice: str | None = None,
        duration_ms: int | None = None,
        tokens_in: int | None = None,
        tokens_out: int | None = None,
        provider: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        self._inner.event(
            event, trace_id=trace_id, node=node, invoice=invoice, duration_ms=duration_ms,
            tokens_in=tokens_in, tokens_out=tokens_out, provider=provider, data=data,
        )
        try:
            self._sink({
                "event": event, "trace_id": trace_id, "node": node, "invoice": invoice,
                "duration_ms": duration_ms, "tokens_in": tokens_in, "tokens_out": tokens_out,
                "provider": provider, "data": data,
            })
        except Exception:
            pass
