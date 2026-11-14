"""Typed settings loaded from config/settings.toml. Package A owns this file."""

import datetime as dt
import os
import tomllib
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, Mapping

from pydantic import BaseModel

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SETTINGS_PATH = Path(__file__).resolve().parent / "settings.toml"


class Settings(BaseModel):
    as_of: dt.date
    default_seed: Literal["demo", "readme"]
    approval_threshold_usd: Decimal
    near_threshold_min_usd: Decimal
    price_above_catalog_pct: Decimal
    math_tolerance_usd: Decimal
    terms_tolerance_days: int
    monthly_budget_usd: Decimal
    max_critic_rounds: int
    max_tool_calls_per_turn: int
    extraction_retries: int
    payment_retries: int
    payment_backoff_s: float
    risk_reject_score: int
    annual_invoice_volume: int
    baseline_cost_per_invoice_usd: Decimal
    db_path: Path
    checkpoint_path: Path
    log_dir: Path
    replay_dir: Path
    prompts_dir: Path
    policy_path: Path
    xai_base_url: str
    extraction_model: str | None = None  # env GROK_EXTRACTION_MODEL
    reasoning_model: str | None = None  # env GROK_REASONING_MODEL


_PATH_FIELDS = ("db_path", "checkpoint_path", "log_dir", "replay_dir", "prompts_dir", "policy_path")


def load_settings(path: Path | None = None, *, env: Mapping[str, str] | None = None) -> Settings:
    """Load settings. XAI_API_KEY is never read into Settings."""
    env = os.environ if env is None else env
    with open(path or DEFAULT_SETTINGS_PATH, "rb") as fh:
        raw = tomllib.load(fh)
    flat: dict[str, Any] = {}
    for key, value in raw.items():
        if isinstance(value, dict):
            flat.update(value)
        else:
            flat[key] = value
    for name in _PATH_FIELDS:
        p = Path(flat[name])
        flat[name] = p if p.is_absolute() else REPO_ROOT / p
    flat["extraction_model"] = env.get("GROK_EXTRACTION_MODEL") or None
    flat["reasoning_model"] = env.get("GROK_REASONING_MODEL") or None
    return Settings(**flat)
