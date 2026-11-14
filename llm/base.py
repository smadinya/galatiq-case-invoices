"""Shared LLM-layer types: prompt loading, error classes, and the null provider."""

import re
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel

from models import CallContext, Completion, Message, ToolSpec

_HEADER = re.compile(r"^<!--\s*prompt_version:\s*([A-Za-z0-9._-]+)\s*-->\s*$")


@dataclass(frozen=True)
class Prompt:
    name: str
    version: str  # e.g. "vp-v1"; goes into the replay key
    text: str  # the prompt body without the header line


def load_prompt(name: str, prompts_dir: Path) -> Prompt:
    raw = (Path(prompts_dir) / f"{name}.md").read_text(encoding="utf-8")
    first, _, rest = raw.partition("\n")
    match = _HEADER.match(first.strip())
    if not match:
        raise ValueError(f"prompt {name!r} must start with '<!-- prompt_version: {name}-vN -->'")
    return Prompt(name=name, version=match.group(1), text=rest.strip() + "\n")


class ProviderError(Exception):
    """A model call failed (never carries credentials)."""


class StructuredOutputError(ProviderError):
    """The model's output did not validate against the requested schema."""


class StaleRecording(ProviderError):
    """Replay cannot serve a call. The message always ends with the re-record command."""


class ScriptError(AssertionError):
    """ScriptedFake: no step for this call, or a step of the wrong type."""


class NullProvider:
    name = "none"

    def complete(
        self,
        *,
        ctx: CallContext,
        system: str,
        messages: list[Message],
        schema: type[BaseModel] | None = None,
        tools: list[ToolSpec] | None = None,
    ) -> Completion:
        raise ProviderError("no model provider configured")
