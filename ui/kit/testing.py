"""Helpers for tests anywhere: build a runtime on temp databases with a fake model, drive the app.

No pytest import. Nothing here runs at import time.
"""

import random
import shutil
from pathlib import Path
from typing import Any, Callable, Literal, Mapping

from streamlit.testing.v1 import AppTest

from config.settings import REPO_ROOT, Settings, load_settings
from llm.fake import ScriptedFake
from llm.fake_golden import golden_script
from models import Provider
from obs import EventLogger, JsonlLogger
from service import Service
from ui.kit.jobs import JobRunner, TeeLogger
from ui.kit.runtime import RUNTIME_KEY, Runtime, ServiceFactory, UiConfig

PAGE_FILES = {
    "process": "views/1_Process.py",
    "queue": "views/2_Review_queue.py",
    "ledger": "views/3_Ledger.py",
    "batch": "views/4_Batch.py",
}

_WIDGET_LISTS = (
    "button", "radio", "segmented_control", "selectbox", "text_input", "text_area", "checkbox",
    "toggle", "date_input", "slider", "pills", "file_uploader", "number_input", "multiselect",
)


def build_settings(root: Path, *, replay_dir: Path | None = None) -> Settings:
    """Settings with every database, checkpoint and log path under `root`; hermetic replay dir."""
    base = load_settings(env={})
    return base.model_copy(update={
        "db_path": root / "acme.db", "checkpoint_path": root / "checkpoints.db", "log_dir": root / "logs",
        "replay_dir": replay_dir if replay_dir is not None else root / "replay",
    })


def fake_factory(
    settings: Settings, *, provider: Provider | Literal["golden"] | None = "golden",
    env: Mapping[str, str] | None = None, payment_seed: int = 0,
) -> ServiceFactory:
    """A factory like the production one, with a scripted model, a no-op sleep and a seeded bank."""

    def factory(config: UiConfig, *, observer: Callable[[dict[str, Any]], None] | None = None) -> Service:
        log: EventLogger = JsonlLogger(settings.log_dir)
        if observer is not None:
            log = TeeLogger(log, observer)
        chosen = ScriptedFake(golden_script()) if provider == "golden" else provider
        return Service(
            settings, provider=chosen, provider_choice=config.provider_choice,  # type: ignore[arg-type]
            env=env or {}, as_of=config.as_of, payment_fail_rate=config.payment_fail_rate,
            sleep=lambda s: None, rng=random.Random(payment_seed), log=log,
        )

    return factory


def make_runtime(
    root: Path, *, provider: Provider | Literal["golden"] | None = "golden", env: Mapping[str, str] | None = None,
    inline: bool = True, settings: Settings | None = None, default_config: UiConfig | None = None,
    sleep: Callable[[float], None] | None = None,
) -> Runtime:
    root = Path(root)
    settings = settings or build_settings(root)
    rt = Runtime(
        settings=settings, factory=fake_factory(settings, provider=provider, env=env),
        default_config=default_config or UiConfig(settings.as_of), jobs=JobRunner(inline=inline),
        samples_dir=REPO_ROOT / "data/invoices", data_dir=REPO_ROOT / "data", upload_dir=root / "uploads",
        sleep=sleep or (lambda s: None),
    )
    rt.ensure_initialized()
    return rt


def golden_snapshot(cache_root: Path) -> Path:
    """Run the DESIGN 14.2 batch once (fake model) into `cache_root` and return it."""
    rt = make_runtime(cache_root)
    job = rt.start_batch(rt.default_config, rt.samples_dir)
    snap = job.snapshot()
    if snap.error is not None:
        raise snap.error
    return Path(cache_root)


def runtime_from_snapshot(root: Path, snapshot: Path, **kw: Any) -> Runtime:
    """Copy `acme.db` and `checkpoints.db` from `snapshot` into `root`, then build a runtime on them."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    for name in ("acme.db", "checkpoints.db"):
        shutil.copyfile(Path(snapshot) / name, root / name)
    return make_runtime(root, **kw)


def app_test(rt: Runtime, *, timeout: float = 30) -> AppTest:
    at = AppTest.from_file(str(REPO_ROOT / "ui/app.py"), default_timeout=timeout)
    at.session_state[RUNTIME_KEY] = rt
    return at


def open_page(at: AppTest, page: Literal["process", "queue", "ledger", "batch"], **query: str) -> AppTest:
    """Run the app, switch to `page` (with query parameters) and run again. Returns `at`."""
    if not getattr(at, "_acme_started", False):
        at.run()
        at._acme_started = True  # type: ignore[attr-defined]
    at.query_params.clear()
    at.query_params.update(query)
    at.switch_page(PAGE_FILES[page])
    return at.run()


def widget(at: AppTest, key: str) -> Any:
    """The widget with this key, whatever its type. KeyError when absent."""
    for name in _WIDGET_LISTS:
        for w in getattr(at, name, ()):
            if getattr(w, "key", None) == key:
                return w
    raise KeyError(key)


def nav_titles(at: AppTest) -> list[str]:
    """The titles `st.navigation` registered on the last run, in order (for example "Review queue (3)")."""
    return [info["page_name"] for info in at._registered_pages.values()]


def page_text(at: AppTest) -> str:
    """`str(proto)` of every element under main and sidebar (what the browser would be sent)."""
    parts = []
    for root in (at.main, at.sidebar):
        for node in root:
            proto = getattr(node, "proto", None)
            if proto is not None:
                parts.append(str(proto))
    return "\n".join(parts)


__all__ = [
    "PAGE_FILES", "app_test", "build_settings", "fake_factory", "golden_snapshot", "make_runtime", "open_page",
    "nav_titles", "page_text", "runtime_from_snapshot", "widget",
]
