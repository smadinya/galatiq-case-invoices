import datetime as dt
from decimal import Decimal
from pathlib import Path

from config.settings import REPO_ROOT, Settings, load_settings


def test_repo_root_is_project_root():
    assert (REPO_ROOT / "pyproject.toml").exists()


def test_defaults_match_design_16():
    s = load_settings(env={})
    assert s.as_of == dt.date(2026, 1, 31)
    assert s.default_seed == "demo"
    assert s.approval_threshold_usd == Decimal("10000")
    assert s.near_threshold_min_usd == Decimal("9000")
    assert s.price_above_catalog_pct == Decimal("10")
    assert s.math_tolerance_usd == Decimal("0.01")
    assert s.terms_tolerance_days == 3
    assert s.monthly_budget_usd == Decimal("50000")
    assert s.max_critic_rounds == 3
    assert s.max_tool_calls_per_turn == 6
    assert s.extraction_retries == 1
    assert s.payment_retries == 3
    assert s.payment_backoff_s == 0.5
    assert s.risk_reject_score == 60
    assert s.annual_invoice_volume == 20000
    assert s.baseline_cost_per_invoice_usd == Decimal("100")
    assert s.xai_base_url == "https://api.x.ai/v1"


def test_money_fields_are_decimal():
    s = load_settings(env={})
    for name in (
        "approval_threshold_usd", "near_threshold_min_usd", "price_above_catalog_pct", "math_tolerance_usd",
        "monthly_budget_usd", "baseline_cost_per_invoice_usd",
    ):
        assert isinstance(getattr(s, name), Decimal), name


def test_model_names_come_only_from_env_argument(monkeypatch):
    monkeypatch.setenv("GROK_EXTRACTION_MODEL", "from-os-environ")
    s = load_settings(env={})
    assert s.extraction_model is None and s.reasoning_model is None
    s = load_settings(env={"GROK_EXTRACTION_MODEL": "ex", "GROK_REASONING_MODEL": "re"})
    assert (s.extraction_model, s.reasoning_model) == ("ex", "re")
    # default env is os.environ
    assert load_settings().extraction_model == "from-os-environ"


def test_api_key_never_in_settings():
    s = load_settings(env={"XAI_API_KEY": "secret"})
    assert "secret" not in s.model_dump_json()
    assert not any("key" in f for f in Settings.model_fields)


def test_relative_paths_resolve_under_repo_root():
    s = load_settings(env={})
    for name in ("db_path", "checkpoint_path", "log_dir", "replay_dir", "prompts_dir", "policy_path"):
        p = getattr(s, name)
        assert isinstance(p, Path) and p.is_absolute() and REPO_ROOT in p.parents, name
    assert s.db_path == REPO_ROOT / "acme.db"
    assert s.policy_path.exists()
