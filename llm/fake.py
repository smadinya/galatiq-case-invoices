"""ScriptedFake: deterministic provider for tests and the golden run (plan §5)."""

from dataclasses import dataclass
from typing import Mapping

from pydantic import BaseModel

from llm.base import ScriptError
from models import (
    Ceiling,
    CallContext,
    Completion,
    CriticOutput,
    FlagCode,
    FlagResponse,
    Message,
    Objection,
    ObjectionAnswer,
    ToolCall,
    ToolSpec,
    Verdict,
    VPMemoOutput,
    VPOutput,
)

ScriptStep = BaseModel | list[ToolCall] | BaseException
Script = dict[tuple[str, str], list[ScriptStep]]


@dataclass
class FakeCall:
    ctx: CallContext
    system: str
    messages: list[Message]
    tool_names: list[str]
    schema_name: str | None


class ScriptedFake:
    name = "fake"

    def __init__(self, script: Script | None = None, *, fallback: Mapping[str, ScriptStep] | None = None) -> None:
        self.script: Script = script or {}
        self.fallback = fallback
        self.calls: list[FakeCall] = []

    def calls_for(self, file_name: str, stage: str | None = None) -> list[FakeCall]:
        return [c for c in self.calls if c.ctx.file_name == file_name and (stage is None or c.ctx.stage == stage)]

    def complete(
        self,
        *,
        ctx: CallContext,
        system: str,
        messages: list[Message],
        schema: type[BaseModel] | None = None,
        tools: list[ToolSpec] | None = None,
    ) -> Completion:
        names = [t.name for t in tools or []]
        self.calls.append(
            FakeCall(ctx, system, list(messages), names, schema.__name__ if schema is not None else None)
        )
        where = f"{ctx.file_name} {ctx.stage}#{ctx.call_index}"
        steps = self.script.get((ctx.file_name, ctx.stage))
        if steps is not None and ctx.call_index < len(steps):
            step = steps[ctx.call_index]
        elif self.fallback is not None and ctx.stage in self.fallback:
            step = self.fallback[ctx.stage]
        else:
            raise ScriptError(f"no scripted step for {where}")
        if isinstance(step, BaseException):
            raise step
        if isinstance(step, list):
            if not names:
                raise ScriptError(f"{where}: script asks for a tool call but no tools were offered")
            for call in step:
                if call.name not in names:
                    raise ScriptError(f"{where}: scripted tool {call.name!r} was not offered (offered: {names})")
            return Completion(tool_calls=list(step), provider="fake")
        if schema is None:
            raise ScriptError(f"{where}: script has a {type(step).__name__} answer but no schema was requested")
        if not isinstance(step, schema):
            raise ScriptError(f"{where}: scripted {type(step).__name__} does not match schema {schema.__name__}")
        return Completion(content=step.model_dump_json(), parsed=step, provider="fake", model="scripted")


def vp(
    decision: Ceiling,
    reasoning: str,
    flags: tuple[tuple[FlagCode, str], ...] | list[tuple[FlagCode, str]] = (),
    answers: tuple[tuple[str, str], ...] | list[tuple[str, str]] = (),
) -> VPOutput:
    return VPOutput(
        decision=decision,
        reasoning=reasoning,
        flags_addressed=[FlagResponse(code=c, response=r) for c, r in flags],
        objections_answered=[ObjectionAnswer(objection=o, answer=a) for o, a in answers],
    )


def critic(
    verdict: Verdict, proposed: Ceiling, objections: tuple[tuple[str, str, str], ...] | list[tuple[str, str, str]] = ()
) -> CriticOutput:
    return CriticOutput(
        verdict=verdict,
        proposed_decision=proposed,
        objections=[Objection(about=a, evidence=e, requested_change=r) for a, e, r in objections],
    )


def memo(text: str) -> VPMemoOutput:
    return VPMemoOutput(memo=text)


def tool_call(call_id: str, name: str, **arguments) -> list[ToolCall]:
    return [ToolCall(id=call_id, name=name, arguments=arguments)]
