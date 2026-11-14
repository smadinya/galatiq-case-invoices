import pytest

from models import BoundTool
from tools.registry import tool
from tools.toolsets import CRITIC_ONLY_TOOLS, SHARED_TOOLS, critic_tools, vp_tools

ALL = SHARED_TOOLS + CRITIC_ONLY_TOOLS
CONTEXT_NAMES = {"deps", "state"}


def test_names_and_descriptions():
    assert [t.name for t in ALL] == [
        "get_vendor_history", "get_stock_level", "get_budget_status", "get_policy",
        "get_source_evidence", "recompute_totals", "find_similar_invoices", "get_vendor_master",
        "get_pipeline_trace",
    ]
    assert all(t.description and "\n" not in t.description for t in ALL)


def test_specs_are_strict_and_hide_context():
    required = {
        "get_vendor_history": ["vendor"], "get_stock_level": ["item"], "get_budget_status": [],
        "get_policy": ["topic"], "get_source_evidence": ["field"], "recompute_totals": [],
        "find_similar_invoices": ["vendor", "amount", "days"], "get_vendor_master": ["vendor"],
        "get_pipeline_trace": [],
    }
    for t in ALL:
        p = t.spec.parameters
        assert p["type"] == "object" and p["additionalProperties"] is False
        assert p["required"] == required[t.name] == list(p["properties"])
        assert not CONTEXT_NAMES & set(p["properties"])
        text = str(p)
        assert "title" not in p and "'default'" not in text and "'title'" not in text


def test_no_arg_tool_schema():
    spec = ALL[2].spec.parameters
    assert spec == {"type": "object", "properties": {}, "required": [], "additionalProperties": False}


def test_decimal_param_schema_is_any_of():
    amount = {t.name: t for t in ALL}["find_similar_invoices"].spec.parameters["properties"]["amount"]
    assert {"type": "number"} in amount["anyOf"]


def test_context_injection_and_validation():
    @tool()
    def echo(deps, state, text: str, n: int) -> dict:
        """Echo things.

        Second paragraph ignored."""
        return {"deps": deps, "state": state, "text": text, "n": n}

    assert echo.description == "Echo things."
    bound = echo.bind(deps="D", state="S", extra="ignored")
    assert isinstance(bound, BoundTool)
    assert bound.fn(text="a", n=2) == {"deps": "D", "state": "S", "text": "a", "n": 2}
    for bad in ({"text": "a"}, {"text": "a", "n": "x"}, {"text": "a", "n": 1, "zzz": 1}):
        with pytest.raises(ValueError):
            bound.fn(**bad)


def test_context_subset_only_passed_when_declared():
    @tool(context=("state",))
    def only_state(state) -> dict:
        return {"state": state}

    assert only_state.bind(deps="D", state="S").fn() == {"state": "S"}


def test_toolsets(make_deps, make_state):
    deps = make_deps()
    state = make_state()
    vp = vp_tools(deps, state)
    critic = critic_tools(deps, state)
    assert [t.spec.name for t in vp] == [t.name for t in SHARED_TOOLS]
    assert len(vp) == 4 and len(critic) == 9
    assert [t.spec.name for t in critic[:4]] == [t.spec.name for t in vp]
