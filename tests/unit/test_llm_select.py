import json

import pytest

from llm.openai_compat import OpenAICompatProvider
from llm.replay import ReplayProvider
from llm.select import NoProviderAvailable, ProviderInfo, select_provider

KEY = "xai-select-key"


@pytest.fixture
def cfg(settings, tmp_path):
    return settings.model_copy(
        update={"replay_dir": tmp_path / "replay", "extraction_model": "ext", "reasoning_model": "rea"}
    )


def make_recording(cfg, file_hash):
    d = cfg.replay_dir / file_hash[:16]
    d.mkdir(parents=True)
    (d / "extraction-000.json").write_text("{}")


def test_grok_when_key_present(cfg):
    p, info = select_provider(None, settings=cfg, env={"XAI_API_KEY": KEY})
    assert isinstance(p, OpenAICompatProvider)
    assert info == ProviderInfo("grok", {"extraction": "ext", "reasoning": "rea"}, None)
    assert KEY not in repr(p) and KEY not in repr(info)


def test_explicit_grok_needs_key_and_models(cfg):
    with pytest.raises(NoProviderAvailable, match="XAI_API_KEY"):
        select_provider("grok", settings=cfg, env={})
    bare = cfg.model_copy(update={"extraction_model": None, "reasoning_model": None})
    with pytest.raises(NoProviderAvailable) as ei:
        select_provider(None, settings=bare, env={"XAI_API_KEY": KEY})
    assert "GROK_EXTRACTION_MODEL" in str(ei.value) and "GROK_REASONING_MODEL" in str(ei.value)
    assert KEY not in str(ei.value)


def test_explicit_replay_wins_over_key(cfg):
    (cfg.replay_dir).mkdir()
    (cfg.replay_dir / "manifest.json").write_text(json.dumps({"recorded_at": "2026-01-01T00:00:00Z", "models": {"reasoning": "r"}}))
    p, info = select_provider("replay", settings=cfg, env={"XAI_API_KEY": KEY})
    assert isinstance(p, ReplayProvider)
    assert info.name == "replay" and info.recording_date == "2026-01-01T00:00:00Z" and info.models == {"reasoning": "r"}


def test_ollama_is_tier_3(cfg):
    with pytest.raises(NoProviderAvailable, match="Ollama is tier 3"):
        select_provider("ollama", settings=cfg, env={"XAI_API_KEY": KEY})


def test_fake_and_unknown_not_selectable(cfg):
    for choice in ("fake", "bogus"):
        with pytest.raises(NoProviderAvailable):
            select_provider(choice, settings=cfg, env={})


def test_no_key_replay_when_all_recorded(cfg):
    make_recording(cfg, "a" * 64)
    make_recording(cfg, "b" * 64)
    p, info = select_provider(None, settings=cfg, env={}, file_hashes=["a" * 64, "b" * 64])
    assert isinstance(p, ReplayProvider) and info.name == "replay"


def test_no_key_missing_recording_lists_ways_and_files(cfg):
    make_recording(cfg, "a" * 64)
    with pytest.raises(NoProviderAvailable) as ei:
        select_provider(None, settings=cfg, env={}, file_hashes=["a" * 64, "c" * 64])
    msg = str(ei.value)
    assert ".env" in msg and "scripts/record.py" in msg and "--provider replay" in msg and "Ollama" in msg
    assert "c" * 16 in msg and "a" * 16 not in msg
