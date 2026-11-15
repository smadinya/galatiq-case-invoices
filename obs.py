"""Operational event logging (JSONL) and a test logger. Package A owns this file."""

import datetime as dt
import json
from pathlib import Path
from typing import Any, Callable, Protocol

EVENT_NAMES = frozenset(
    {
        "run_start", "run_end", "short_circuit", "node_start", "node_end", "llm_call",
        "tool_call", "flag_raised", "ceiling_clamped", "retry", "interrupt", "resume",
        "payment", "error",
    }
)
# values replaced by "[redacted]", at any depth
REDACT_KEYS = frozenset({"api_key", "xai_api_key", "authorization", "headers"})


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: ("[redacted]" if isinstance(k, str) and k.lower() in REDACT_KEYS else _redact(v))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact(v) for v in value]
    return value


class EventLogger(Protocol):
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
    ) -> None: ...


def _build(
    ts: dt.datetime,
    event: str,
    trace_id: str,
    node: str | None,
    invoice: str | None,
    duration_ms: int | None,
    tokens_in: int | None,
    tokens_out: int | None,
    provider: str | None,
    data: dict[str, Any] | None,
) -> dict[str, Any]:
    if not trace_id:
        raise ValueError("trace_id is required for every event")
    if event not in EVENT_NAMES:
        raise ValueError(f"unknown event name: {event!r}")
    return {
        "ts": ts.isoformat(),
        "trace_id": trace_id,
        "invoice": invoice,
        "node": node,
        "event": event,
        "duration_ms": duration_ms,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "cost_usd": None,  # always null in tier 1
        "provider": provider,
        "data": _redact(data) if data is not None else None,
    }


class JsonlLogger:
    """Appends one JSON object per line to <log_dir>/<YYYY-MM-DD from clock>.jsonl."""

    def __init__(self, log_dir: Path, *, clock: Callable[[], dt.datetime] = utc_now) -> None:
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._clock = clock

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
        now = self._clock()
        record = _build(now, event, trace_id, node, invoice, duration_ms, tokens_in, tokens_out, provider, data)
        line = json.dumps(record, default=str)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        with open(self.log_dir / f"{now.date().isoformat()}.jsonl", "a", encoding="utf-8") as fh:
            fh.write(line + "\n")


class MemoryLogger:
    """Collects events in memory (tests)."""

    def __init__(self, *, clock: Callable[[], dt.datetime] = utc_now) -> None:
        self.events: list[dict[str, Any]] = []
        self._clock = clock

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
        self.events.append(
            _build(self._clock(), event, trace_id, node, invoice, duration_ms, tokens_in, tokens_out, provider, data)
        )

    def of(self, event: str) -> list[dict[str, Any]]:
        return [e for e in self.events if e["event"] == event]
