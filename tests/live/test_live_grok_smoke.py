"""Smoke tests against the real Grok API. Run: uv run pytest -m live tests/live/test_live_grok_smoke.py"""

import pytest

from agents._loop import run_structured, run_tool_loop
from config.settings import REPO_ROOT
from llm.base import load_prompt
from models import BoundTool, CallContext, ExtractedInvoice, Message, ToolSpec, VPOutput
from obs import MemoryLogger

pytestmark = pytest.mark.live
PROMPTS = REPO_ROOT / "llm" / "prompts"


def _ctx(stage, version):
    return CallContext(
        trace_id="live-smoke", file_hash="0" * 64, file_name="invoice_1001.txt", stage=stage,
        prompt_version=version, call_index=0,
    )


def test_extraction_of_invoice_1001(live_provider):
    prompt = load_prompt("extraction", PROMPTS)
    text = (REPO_ROOT / "data" / "invoices" / "invoice_1001.txt").read_text(encoding="utf-8")
    out, usage = run_structured(
        provider=live_provider, ctx=_ctx("extraction", prompt.version), system=prompt.text,
        messages=[Message(role="user", content="Invoice text:\n\n" + text)],
        schema=ExtractedInvoice, log=MemoryLogger(),
    )
    assert isinstance(out, ExtractedInvoice)
    assert out.invoice_number and "1001" in out.invoice_number
    assert out.vendor.name and "Widgets" in out.vendor.name
    assert len(out.line_items) >= 2
    assert usage.tokens_in > 0


def test_tools_and_response_format_together(live_provider):
    """Settles plan risk R2: tools plus response_format in one xAI request, through run_tool_loop."""
    stock_spec = ToolSpec(
        name="get_stock_level",
        description="Current stock level of one item.",
        parameters={
            "type": "object",
            "properties": {"item": {"type": "string", "description": "item name"}},
            "required": ["item"],
            "additionalProperties": False,
        },
    )
    seen: list[str] = []

    def get_stock_level(item: str) -> dict:
        seen.append(item)
        return {"item": item, "stock": 90}

    prompt = load_prompt("vp", PROMPTS)
    result = run_tool_loop(
        provider=live_provider, ctx=_ctx("vp", prompt.version), system=prompt.text,
        messages=[Message(role="user", content=(
            "Ceiling: APPROVE. Address check: no_record. No flags.\n"
            "Invoice: 10 x WidgetA at $250.00, total $2,500.00, vendor Widgets Inc.\n"
            "First call get_stock_level for WidgetA to confirm we have 10 in stock, then give your decision."))],
        tools=[BoundTool(stock_spec, get_stock_level)], schema=VPOutput, max_tool_calls=6, log=MemoryLogger(),
    )
    assert isinstance(result.output, VPOutput)
    assert seen, "the model never called the tool"
    assert result.tool_traces and result.calls >= 2
