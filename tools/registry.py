"""The @tool decorator: turns a function signature into a strict ToolSpec and a bindable callable."""

import inspect
import json
import typing
from typing import Any, Callable

from pydantic import ConfigDict, ValidationError, create_model

from models import BoundTool, ToolSpec

_DROP_KEYS = ("title", "default")


def _clean(node: Any) -> Any:
    """Strip titles/defaults and make every object strict (all required, no extra properties)."""
    if isinstance(node, dict):
        out = {k: _clean(v) for k, v in node.items() if k not in _DROP_KEYS or k == "properties"}
        if out.get("type") == "object" or "properties" in out:
            props = out.get("properties", {})
            out["properties"] = props
            out["required"] = list(props)
            out["additionalProperties"] = False
        return out
    if isinstance(node, list):
        return [_clean(v) for v in node]
    return node


class ToolFunction:
    def __init__(self, fn: Callable[..., dict], name: str | None, context: tuple[str, ...]) -> None:
        self.fn = fn
        self.name = name or fn.__name__
        self.context = context
        doc = inspect.getdoc(fn) or ""
        self.description = " ".join(doc.split("\n\n")[0].split())
        sig = inspect.signature(fn)
        hints = typing.get_type_hints(fn)
        self._context_params = [p for p in sig.parameters if p in context]
        fields: dict[str, Any] = {}
        for pname, param in sig.parameters.items():
            if pname in context:
                continue
            fields[pname] = (hints.get(pname, str), ...)
        self.model = create_model(
            f"{self.name}_args", __config__=ConfigDict(extra="forbid"), **fields
        )
        schema = _clean(self.model.model_json_schema())
        schema.setdefault("properties", {})
        schema["required"] = list(schema["properties"])
        schema["additionalProperties"] = False
        self.spec = ToolSpec(name=self.name, description=self.description, parameters=schema)
        self.__name__ = fn.__name__
        self.__doc__ = fn.__doc__

    def __call__(self, *args, **kwargs) -> dict:
        return self.fn(*args, **kwargs)

    def bind(self, **context: Any) -> BoundTool:
        ctx = {k: v for k, v in context.items() if k in self._context_params}
        missing = [k for k in self._context_params if k not in ctx]
        if missing:
            raise TypeError(f"tool {self.name} needs context {missing}")

        def run(**arguments: Any) -> dict[str, Any]:
            try:
                parsed = self.model.model_validate(arguments)
            except ValidationError as exc:
                raise ValueError(f"invalid arguments for {self.name}: {exc.errors(include_url=False)}") from exc
            values = {k: getattr(parsed, k) for k in self.model.model_fields}
            return self.fn(**ctx, **values)

        return BoundTool(spec=self.spec, fn=run)


def tool(
    *, name: str | None = None, context: tuple[str, ...] = ("deps", "state")
) -> Callable[[Callable[..., dict]], ToolFunction]:
    def wrap(fn: Callable[..., dict]) -> ToolFunction:
        return ToolFunction(fn, name, context)

    return wrap
