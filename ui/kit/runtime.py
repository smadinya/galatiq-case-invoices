"""Composition root for the dashboard: settings, service factory, jobs, provider cache. No connections.

`default_runtime()` is a `st.cache_resource` singleton. A `Runtime` holds no connection and no
environment: the environment lives only in the production factory's closure, and a `Service` is
built per job and closed after it. Editing this module while a job runs makes Streamlit build a
second `Runtime` with a separate write lock; that only matters during development.
"""

import datetime as dt
import os
import threading
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Literal, Mapping, Protocol, Sequence

import streamlit as st

from config.settings import REPO_ROOT, Settings, load_settings
from llm.replay import ReplayProvider
from llm.select import NoProviderAvailable
from models import InvoiceResult, ResolutionPreview
from obs import EventLogger, JsonlLogger
from service import Service
from ui.kit import copy
from ui.kit.data import ReadSession
from ui.kit.jobs import Busy, Job, JobContext, JobRunner, TeeLogger

RUNTIME_KEY = "acme.runtime"  # tests put a Runtime here before at.run()


@dataclass(frozen=True)
class UiConfig:
    """Per session (`st.session_state`), passed into every Runtime call."""

    as_of: dt.date
    provider_choice: Literal["grok", "replay"] | None = None  # None = auto
    payment_fail_rate: float = 0.0  # probability 0..1, not money
    slow_replay: bool = True


class ServiceFactory(Protocol):
    def __call__(self, config: UiConfig, *, observer: Callable[[dict[str, Any]], None] | None = None) -> Service: ...


@dataclass(frozen=True)
class ProviderView:
    name: str
    models: dict[str, str]
    recording_date: str | None
    reason: str | None = None  # why the chosen provider could not resolve; never contains a secret


@dataclass(frozen=True)
class Preflight:
    ok: bool
    provider: str
    message: str | None  # never contains secrets


def production_factory(settings: Settings, env: Mapping[str, str]) -> ServiceFactory:
    def factory(config: UiConfig, *, observer: Callable[[dict[str, Any]], None] | None = None) -> Service:
        log: EventLogger = JsonlLogger(settings.log_dir)
        if observer is not None:
            log = TeeLogger(log, observer)
        return Service(
            settings, provider_choice=config.provider_choice, env=env, as_of=config.as_of,
            payment_fail_rate=config.payment_fail_rate, log=log,
        )

    return factory


def _count_files(directory: Path) -> int:
    return sum(1 for p in Path(directory).iterdir() if p.is_file() and not p.name.startswith("."))


class Runtime:
    def __init__(
        self, *, settings: Settings, factory: ServiceFactory, default_config: UiConfig, jobs: JobRunner,
        samples_dir: Path, data_dir: Path, upload_dir: Path,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.settings = settings
        self.factory = factory
        self.default_config = default_config
        self.jobs = jobs
        self.samples_dir = Path(samples_dir)
        self.data_dir = Path(data_dir)
        self.upload_dir = Path(upload_dir)
        self._sleep = sleep  # used only to pace a replay run; tests inject a recorder
        self._providers: dict[str | None, ProviderView] = {}
        self._providers_lock = threading.Lock()

    # ------------------------------------------------------------------ reads

    def ensure_initialized(self) -> None:
        """Under the write lock: build one Service so `acme.db` exists and is seeded."""
        with self.jobs.lock:
            self.factory(self.default_config).close()

    def reader(self, config: UiConfig) -> ReadSession:
        return ReadSession(self.settings, budget_month=config.as_of.strftime("%Y-%m"))

    def provider(self, config: UiConfig) -> ProviderView | None:
        """The provider for `config.provider_choice`, cached. None when busy and not cached yet."""
        key = config.provider_choice
        with self._providers_lock:
            cached = self._providers.get(key)
        if cached is not None:
            return cached
        if not self.jobs.lock.acquire(blocking=False):
            return None
        try:
            with self.factory(config) as svc:
                banner = svc.banner()
                reason = None
                if banner.provider == "none" and key is not None:
                    try:
                        svc._resolve_provider()  # reports why the chosen provider is unavailable
                    except NoProviderAvailable as exc:
                        reason = str(exc)
                view = ProviderView(banner.provider, dict(banner.models), banner.recording_date, reason)
        finally:
            self.jobs.lock.release()
        with self._providers_lock:
            self._providers[key] = view
        return view

    def preflight(self, config: UiConfig, file_hashes: Sequence[str]) -> Preflight:
        view = self.provider(config)
        name = view.name if view is not None else "unknown"
        if not file_hashes:
            return Preflight(True, name, None)
        if name == "none":
            return Preflight(False, name, (view.reason if view and view.reason else copy.t("error.no_model_sidebar")))
        if name == "replay":
            replay = ReplayProvider(self.settings.replay_dir)
            missing = sum(1 for h in file_hashes if not replay.has_recording(h))
            if missing:
                return Preflight(
                    False, name,
                    f"Replay has no recording for {missing} of {len(file_hashes)} files. "
                    "Choose Grok, or record them first (uv run python scripts/record.py).",
                )
        return Preflight(True, name, None)

    def preview(self, config: UiConfig, ref: str, fx_rate: Decimal | None) -> ResolutionPreview:
        if not self.jobs.lock.acquire(blocking=False):
            raise Busy(self.jobs.current())
        try:
            with self.factory(config) as svc:
                return svc.preview_resolution(ref, fx_rate=fx_rate)
        finally:
            self.jobs.lock.release()

    # ------------------------------------------------------------------ writes

    def start_invoice(self, config: UiConfig, path: Path) -> Job:
        path = Path(path)
        view = self.provider(config)
        pace = config.slow_replay and view is not None and view.name == "replay"

        def work(ctx: JobContext) -> InvoiceResult:
            if pace:
                ctx.set_pace(self._sleep)
            with self.factory(config, observer=ctx.observe) as svc:
                result = svc.run_invoice(path, on_update=ctx.on_update)
                ctx.on_result(result)
                return result

        return self.jobs.submit("invoice", f"Processing {path.name}", work)

    def start_batch(self, config: UiConfig, directory: Path) -> Job:
        directory = Path(directory)

        def work(ctx: JobContext) -> Any:
            with self.factory(config, observer=ctx.observe) as svc:
                return svc.run_batch(directory, on_update=ctx.on_update, on_result=ctx.on_result)

        return self.jobs.submit("batch", f"Running {directory.name}", work, total_files=_count_files(directory))

    def start_resolve(
        self, config: UiConfig, ref: str, *, action: Literal["approve", "reject"], note: str,
        fx_rate: Decimal | None, confirm_overspend: bool,
    ) -> Job:
        def work(ctx: JobContext) -> InvoiceResult:
            with self.factory(config, observer=ctx.observe) as svc:
                result = svc.resolve(
                    ref, action=action, note=note, fx_rate=fx_rate, confirm_overspend=confirm_overspend
                )
                ctx.on_result(result)
                return result

        return self.jobs.submit("resolve", f"Resolving {ref}", work)

    def start_reset(self, config: UiConfig, seed: Literal["demo", "readme"]) -> Job:
        def work(ctx: JobContext) -> None:
            with self.factory(config) as svc:
                svc.reset(seed)
            self.jobs.clear_history()  # the running reset job itself is kept

        return self.jobs.submit("reset", "Resetting data", work)


@st.cache_resource(show_spinner=False)
def default_runtime() -> Runtime:
    if "PYTEST_CURRENT_TEST" in os.environ:
        raise RuntimeError("default_runtime() must not run under pytest; inject a Runtime through RUNTIME_KEY")
    from dotenv import load_dotenv

    load_dotenv(REPO_ROOT / ".env", override=False)
    override = os.environ.get("ACME_SETTINGS")
    settings = load_settings(Path(override) if override else None)
    choice = os.environ.get("ACME_UI_PROVIDER")
    default_config = UiConfig(settings.as_of, choice if choice in ("grok", "replay") else None)  # type: ignore[arg-type]
    rt = Runtime(
        settings=settings, factory=production_factory(settings, os.environ), default_config=default_config,
        jobs=JobRunner(), samples_dir=REPO_ROOT / "data/invoices", data_dir=REPO_ROOT / "data",
        upload_dir=REPO_ROOT / "data/uploads",
    )
    rt.ensure_initialized()
    return rt


def get_runtime() -> Runtime:
    injected = st.session_state.get(RUNTIME_KEY)
    return injected if injected is not None else default_runtime()
