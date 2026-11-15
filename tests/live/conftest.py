"""Live-test fixtures. The key is read with dotenv_values (never load_dotenv), so os.environ stays clean."""

import os

import pytest
from dotenv import dotenv_values

from config.settings import REPO_ROOT, load_settings

_NEEDED = ("XAI_API_KEY", "GROK_EXTRACTION_MODEL", "GROK_REASONING_MODEL")


@pytest.fixture(scope="session")
def live_env() -> dict[str, str]:
    env = {k: v for k, v in dotenv_values(REPO_ROOT / ".env").items() if v}
    for name in _NEEDED:  # a real environment variable also counts
        if os.environ.get(name):
            env.setdefault(name, os.environ[name])
    missing = [n for n in _NEEDED if not env.get(n)]
    if missing:
        pytest.skip("live tests need " + ", ".join(missing))
    return env


@pytest.fixture(scope="session")
def live_settings(live_env):
    return load_settings(env=live_env)


@pytest.fixture(scope="session")
def live_provider(live_env, live_settings):
    from llm.select import select_provider

    provider, _ = select_provider("grok", settings=live_settings, env=live_env)
    return provider
