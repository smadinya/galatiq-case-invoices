"""Fixtures for the dashboard tests: a runtime on temp databases with a scripted model, and AppTest on top."""

import pytest

from ui.kit.testing import app_test, golden_snapshot, make_runtime, runtime_from_snapshot


@pytest.fixture
def ui_rt(tmp_path):
    return make_runtime(tmp_path / "rt")


@pytest.fixture(scope="session")
def golden_db(tmp_path_factory):
    """The DESIGN 14.2 batch, run once per session with the scripted model."""
    return golden_snapshot(tmp_path_factory.mktemp("golden-ui"))


@pytest.fixture
def ui_rt_batch(tmp_path, golden_db):
    return runtime_from_snapshot(tmp_path / "rt-batch", golden_db)


@pytest.fixture
def app(ui_rt):
    return app_test(ui_rt)


@pytest.fixture
def app_batch(ui_rt_batch):
    return app_test(ui_rt_batch)
