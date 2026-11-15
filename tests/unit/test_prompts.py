import pytest

from config.settings import REPO_ROOT
from llm.base import load_prompt

PROMPTS = REPO_ROOT / "llm" / "prompts"
NAMES = ["extraction", "vp", "critic", "vp_memo"]


@pytest.mark.parametrize("name", NAMES)
def test_prompt_loads_with_version(name):
    p = load_prompt(name, PROMPTS)
    v = f"{name}-v2" if name == "vp" else f"{name}-v1"
    assert p.version == v and p.name == name
    assert (PROMPTS / f"{name}.md").read_text().startswith(f"<!-- prompt_version: {v} -->\n")
    assert "prompt_version" not in p.text


def test_missing_header_is_an_error(tmp_path):
    (tmp_path / "x.md").write_text("no header\n")
    with pytest.raises(ValueError):
        load_prompt("x", tmp_path)


def test_extraction_rules_present():
    t = load_prompt("extraction", PROMPTS).text
    for needle in ["INV 1012", "2O26", "$3,500.O0", "corrections", "note", "former_name", "bill-to", "ship-to",
                   "digits only", "null", "untrusted"]:
        assert needle in t, needle


@pytest.mark.parametrize("name", ["vp", "critic", "vp_memo"])
def test_approval_prompt_rules_present(name):
    t = load_prompt(name, PROMPTS).text
    low = t.lower()
    assert "ceiling" in low and "untrusted" in low and "get_policy" in t
    assert "only when the ceiling is human" in low or "only possible when the ceiling is human" in low
    assert "address" in low and "never grounds for rejection" in low


def test_vp_and_critic_specific_rules():
    vp = load_prompt("vp", PROMPTS).text
    assert "flags_addressed" in vp and "objections_answered" in vp and "escalation" in vp
    cr = load_prompt("critic", PROMPTS).text
    for tool in ["get_source_evidence", "recompute_totals", "find_similar_invoices", "get_vendor_master"]:
        assert tool in cr
    assert "AGREE only when" in cr


def test_prompts_do_not_hard_code_golden_outcomes():
    for name in NAMES:
        t = load_prompt(name, PROMPTS).text
        for banned in ["FastShip", "QuickShip", "Fraudster", "Simpsons", "Evergreen", "INV-1004", "INV-1005", "INV-1007"]:
            assert banned not in t, (name, banned)
