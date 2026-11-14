"""Chat Completions provider for Grok (xAI) through the OpenAI SDK."""

import json
import time
from typing import Any

from pydantic import BaseModel, ValidationError

from llm.base import ProviderError, StructuredOutputError
from models import CallContext, CallUsage, Completion, Message, ToolCall, ToolSpec


def _strict_schema(schema: type[BaseModel]) -> dict[str, Any]:
    from openai.lib._pydantic import to_strict_json_schema  # private but pinned (plan §9 R5)

    return to_strict_json_schema(schema)


def _map_messages(system: str, messages: list[Message]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = [{"role": "system", "content": system}]
    for m in messages:
        if m.role == "assistant" and m.tool_calls:
            out.append(
                {
                    "role": "assistant",
                    "content": m.content,
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)},
                        }
                        for tc in m.tool_calls
                    ],
                }
            )
        elif m.role == "tool":
            out.append({"role": "tool", "tool_call_id": m.tool_call_id, "content": m.content or ""})
        else:
            out.append({"role": m.role, "content": m.content or ""})
    return out


class OpenAICompatProvider:
    name = "grok"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        extraction_model: str,
        reasoning_model: str,
        client: Any | None = None,
        timeout_s: float = 120.0,
    ) -> None:
        if client is None:
            from openai import OpenAI

            client = OpenAI(api_key=api_key, base_url=base_url, max_retries=2, timeout=timeout_s)
        self._client = client
        self._extraction_model = extraction_model
        self._reasoning_model = reasoning_model
        # The key lives only inside the client and this closure; never in an attribute or repr.
        self._scrub = (lambda text: text.replace(api_key, "[redacted]")) if api_key else (lambda text: text)

    def __repr__(self) -> str:
        return f"OpenAICompatProvider(models={self._extraction_model!r}/{self._reasoning_model!r})"

    def _create(self, kwargs: dict[str, Any]) -> Any:
        try:
            return self._client.chat.completions.create(**kwargs)
        except Exception as exc:  # SDK errors are wrapped; the message is scrubbed of the key
            raise ProviderError(self._scrub(f"{type(exc).__name__}: {exc}")) from None

    def complete(
        self,
        *,
        ctx: CallContext,
        system: str,
        messages: list[Message],
        schema: type[BaseModel] | None = None,
        tools: list[ToolSpec] | None = None,
    ) -> Completion:
        extraction = ctx.stage == "extraction"
        kwargs: dict[str, Any] = {
            "model": self._extraction_model if extraction else self._reasoning_model,
            "messages": _map_messages(system, messages),
        }
        if extraction:
            kwargs["temperature"] = 0
        if tools:
            kwargs["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters,
                        "strict": True,
                    },
                }
                for t in tools
            ]
        if schema is not None:
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": schema.__name__, "schema": _strict_schema(schema), "strict": True},
            }

        tokens_in = tokens_out = 0
        started = time.perf_counter()
        last_error = ""
        for attempt in range(2):
            response = self._create(kwargs)
            usage = getattr(response, "usage", None)
            tokens_in += int(getattr(usage, "prompt_tokens", 0) or 0)
            tokens_out += int(getattr(usage, "completion_tokens", 0) or 0)
            try:
                message = response.choices[0].message
            except (AttributeError, IndexError, TypeError):
                raise ProviderError("provider response had no choices") from None
            call_usage = CallUsage(
                tokens_in=tokens_in, tokens_out=tokens_out, latency_ms=int((time.perf_counter() - started) * 1000)
            )
            model = getattr(response, "model", None)
            content = getattr(message, "content", None)
            raw_calls = getattr(message, "tool_calls", None) or []
            if raw_calls:
                try:
                    calls = [
                        ToolCall(
                            id=tc.id,
                            name=tc.function.name,
                            arguments=json.loads(tc.function.arguments or "{}"),
                        )
                        for tc in raw_calls
                    ]
                except (ValueError, TypeError) as exc:
                    raise ProviderError(f"unparseable tool call arguments: {exc}") from None
                return Completion(
                    content=content, tool_calls=calls, usage=call_usage, model=model, provider=self.name
                )
            if schema is None:
                return Completion(content=content, usage=call_usage, model=model, provider=self.name)
            try:
                parsed = schema.model_validate_json(content or "")
            except ValidationError as exc:
                last_error = str(exc)
                if attempt == 0:
                    kwargs["messages"] = kwargs["messages"] + [
                        {"role": "assistant", "content": content or ""},
                        {
                            "role": "user",
                            "content": "Your last reply did not match the required JSON schema. "
                            f"Fix these errors and answer again with valid JSON only:\n{last_error}",
                        },
                    ]
                    continue
                raise StructuredOutputError(
                    f"{schema.__name__} output invalid after one retry: {last_error}"
                ) from None
            return Completion(
                content=content, parsed=parsed, usage=call_usage, model=model, provider=self.name
            )
        raise StructuredOutputError(f"{schema.__name__} output invalid: {last_error}")  # unreachable
