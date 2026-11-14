"""Records every model call as a JSON fixture (our own Message objects only; never HTTP data)."""

import datetime as dt
import hashlib
import json
from pathlib import Path
from typing import Callable

from pydantic import BaseModel

from models import CallContext, Completion, Message, Provider, ToolSpec
from obs import utc_now


def fixture_path(root: Path, file_hash: str, stage: str, call_index: int) -> Path:
    return Path(root) / file_hash[:16] / f"{stage}-{call_index:03d}.json"


def request_digest(system: str, messages: list[Message], tool_names: list[str], schema_name: str | None) -> str:
    payload = {
        "system": system,
        "messages": [m.model_dump(mode="json") for m in messages],
        "tools": tool_names,
        "schema": schema_name,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


class RecordingProvider:
    def __init__(self, inner: Provider, out_dir: Path, *, clock: Callable[[], dt.datetime] = utc_now) -> None:
        self.inner = inner
        self.name = inner.name
        self.out_dir = Path(out_dir)
        self._clock = clock

    def complete(
        self,
        *,
        ctx: CallContext,
        system: str,
        messages: list[Message],
        schema: type[BaseModel] | None = None,
        tools: list[ToolSpec] | None = None,
    ) -> Completion:
        completion = self.inner.complete(ctx=ctx, system=system, messages=messages, schema=schema, tools=tools)
        tool_names = [t.name for t in tools or []]
        schema_name = schema.__name__ if schema is not None else None
        record = {
            "key": {
                "file_hash": ctx.file_hash,
                "file_name": ctx.file_name,
                "stage": ctx.stage,
                "prompt_version": ctx.prompt_version,
                "call_index": ctx.call_index,
            },
            "recorded_at": self._clock().astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "provider": completion.provider or self.inner.name,
            "model": completion.model,
            "request": {
                "system_sha256": hashlib.sha256(system.encode("utf-8")).hexdigest(),
                "messages": [m.model_dump(mode="json") for m in messages],
                "tools": tool_names,
                "schema": schema_name,
                "digest": request_digest(system, messages, tool_names, schema_name),
            },
            "response": {
                "content": completion.content,
                "tool_calls": [tc.model_dump(mode="json") for tc in completion.tool_calls],
                "usage": completion.usage.model_dump(mode="json"),
            },
        }
        path = fixture_path(self.out_dir, ctx.file_hash, ctx.stage, ctx.call_index)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return completion
