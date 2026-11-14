"""Serves recorded completions keyed by (file_hash, stage, prompt_version, call_index)."""

import json
from pathlib import Path

from pydantic import BaseModel

from llm.base import StaleRecording
from llm.recorder import fixture_path, request_digest
from models import CallContext, CallUsage, Completion, Message, ToolCall, ToolSpec
from obs import EventLogger

RERECORD = "Re-record: uv run python scripts/record.py"


class ReplayProvider:
    name = "replay"

    def __init__(self, replay_dir: Path, *, log: EventLogger | None = None) -> None:
        self.replay_dir = Path(replay_dir)
        self._log = log
        self.drifted: list[str] = []  # fixtures whose request digest differed (a warning, not a failure)

    def has_recording(self, file_hash: str) -> bool:
        directory = self.replay_dir / file_hash[:16]
        return directory.is_dir() and any(directory.glob("*.json"))

    def _manifest(self) -> dict:
        path = self.replay_dir / "manifest.json"
        if not path.is_file():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            return {}

    def recording_date(self) -> str | None:
        return self._manifest().get("recorded_at")

    def models(self) -> dict[str, str]:
        return dict(self._manifest().get("models") or {})

    def complete(
        self,
        *,
        ctx: CallContext,
        system: str,
        messages: list[Message],
        schema: type[BaseModel] | None = None,
        tools: list[ToolSpec] | None = None,
    ) -> Completion:
        path = fixture_path(self.replay_dir, ctx.file_hash, ctx.stage, ctx.call_index)
        if not path.is_file():
            raise StaleRecording(
                f"No recording for {ctx.file_name} {ctx.stage}#{ctx.call_index} "
                f"(prompt {ctx.prompt_version}). {RERECORD}"
            )
        record = json.loads(path.read_text(encoding="utf-8"))
        recorded_version = record["key"]["prompt_version"]
        if recorded_version != ctx.prompt_version:
            raise StaleRecording(
                f"prompt {ctx.stage} changed {recorded_version} -> {ctx.prompt_version}; "
                f"re-record with: uv run python scripts/record.py"
            )
        tool_names = [t.name for t in tools or []]
        schema_name = schema.__name__ if schema is not None else None
        if request_digest(system, messages, tool_names, schema_name) != record["request"]["digest"]:
            self.drifted.append(str(path))
            if self._log is not None:
                self._log.event(
                    "error",
                    trace_id=ctx.trace_id,
                    node=ctx.stage,
                    provider=self.name,
                    data={"kind": "replay_request_drift", "fixture": path.name, "file": ctx.file_name},
                )
        response = record["response"]
        tool_calls = [ToolCall.model_validate(tc) for tc in response.get("tool_calls") or []]
        content = response.get("content")
        parsed = None
        if schema is not None and not tool_calls:
            parsed = schema.model_validate_json(content or "")
        return Completion(
            content=content,
            parsed=parsed,
            tool_calls=tool_calls,
            usage=CallUsage.model_validate(response.get("usage") or {}),
            model=record.get("model"),
            provider=self.name,
        )
