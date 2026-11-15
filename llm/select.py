"""Provider selection: --provider, then Grok (key present), then replay, else a clear message."""

from dataclasses import dataclass
from typing import Mapping, Sequence

from config.settings import Settings
from llm.openai_compat import OpenAICompatProvider
from llm.replay import ReplayProvider
from models import Provider


@dataclass(frozen=True)
class ProviderInfo:
    name: str
    models: dict[str, str]
    recording_date: str | None


class NoProviderAvailable(Exception):
    pass


def _grok(settings: Settings, env: Mapping[str, str]) -> tuple[Provider, ProviderInfo]:
    missing = []
    if not env.get("XAI_API_KEY"):
        missing.append("XAI_API_KEY")
    if not settings.extraction_model:
        missing.append("GROK_EXTRACTION_MODEL")
    if not settings.reasoning_model:
        missing.append("GROK_REASONING_MODEL")
    if missing:
        raise NoProviderAvailable(
            "Grok needs these environment variables (for example in .env): " + ", ".join(missing)
        )
    provider = OpenAICompatProvider(
        api_key=env["XAI_API_KEY"],
        base_url=settings.xai_base_url,
        extraction_model=settings.extraction_model,
        reasoning_model=settings.reasoning_model,
    )
    models = {"extraction": settings.extraction_model, "reasoning": settings.reasoning_model}
    return provider, ProviderInfo("grok", models, None)


def _replay(settings: Settings) -> tuple[Provider, ProviderInfo]:
    provider = ReplayProvider(settings.replay_dir)
    return provider, ProviderInfo("replay", provider.models(), provider.recording_date())


def select_provider(
    choice: str | None,
    *,
    settings: Settings,
    env: Mapping[str, str],
    file_hashes: Sequence[str] = (),
) -> tuple[Provider, ProviderInfo]:
    if choice == "grok" or (choice is None and env.get("XAI_API_KEY")):
        return _grok(settings, env)
    if choice == "replay":
        return _replay(settings)
    if choice == "ollama":
        raise NoProviderAvailable("Ollama is tier 3 and is not available in this build.")
    if choice is not None:
        raise NoProviderAvailable(f"Unknown provider {choice!r}; choose grok or replay.")
    replay = ReplayProvider(settings.replay_dir)
    missing = [h[:16] for h in file_hashes if not replay.has_recording(h)]
    if not missing:
        return _replay(settings)
    raise NoProviderAvailable(
        "No model provider is available. Ways to get one:\n"
        "  1. Put XAI_API_KEY, GROK_EXTRACTION_MODEL and GROK_REASONING_MODEL in .env (Grok).\n"
        "  2. Record once with a key (uv run python scripts/record.py), then run with --provider replay.\n"
        "  3. Ollama (local model) arrives in tier 3.\n"
        f"Files without a recording (file hash prefix): {', '.join(missing)}"
    )
