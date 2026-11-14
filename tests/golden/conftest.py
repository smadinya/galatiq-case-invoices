"""Golden-test fixtures. `make_service` builds a Service on fixed tmp paths; calling it twice reuses the same
database and checkpoints, which simulates separate CLI processes."""

from pathlib import Path

import pytest

from config.settings import load_settings
from golden_expected import AS_OF
from llm.fake import ScriptedFake
from llm.fake_golden import golden_script
from obs import JsonlLogger
from service import Service


def build_settings(root: Path):
    base = load_settings(env={})
    return base.model_copy(
        update={"db_path": root / "acme.db", "checkpoint_path": root / "checkpoints.db", "log_dir": root / "logs"}
    )


def build_factory(root: Path, created: list):
    settings = build_settings(root)

    def make_service(**kw) -> Service:
        kw.setdefault("provider", ScriptedFake(golden_script()))
        kw.setdefault("as_of", AS_OF)
        kw.setdefault("sleep", lambda s: None)
        kw.setdefault("log", JsonlLogger(settings.log_dir))
        svc = Service(settings, **kw)
        created.append(svc)
        return svc

    make_service.settings = settings
    return make_service


@pytest.fixture
def make_service(tmp_path):
    created: list = []
    factory = build_factory(tmp_path, created)
    yield factory
    for s in created:
        try:
            s.close()
        except Exception:
            pass
