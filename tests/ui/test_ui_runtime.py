import dataclasses
import datetime as dt
import threading
from decimal import Decimal

import pytest
from streamlit.testing.v1 import AppTest

from config.settings import REPO_ROOT
from llm.fake import ScriptedFake
from llm.fake_golden import golden_script
from models import Status
from service import NotFound
from ui.kit.data import file_sha256
from ui.kit.jobs import SLOW_NODES, Busy, TeeLogger
from ui.kit.runtime import RUNTIME_KEY, UiConfig, default_runtime, production_factory
from ui.kit.testing import build_settings, make_runtime, runtime_from_snapshot
from obs import JsonlLogger

SAMPLES = REPO_ROOT / "data/invoices"
FEB = dt.date(2026, 2, 15)


def _runtime_script():
    import streamlit as st

    from ui.kit.runtime import get_runtime

    st.text(str(get_runtime().settings.db_path))


def test_get_runtime_prefers_session_state(ui_rt):
    at = AppTest.from_function(_runtime_script, default_timeout=30)
    at.session_state[RUNTIME_KEY] = ui_rt
    at.run()
    assert not at.exception and at.text[0].value == str(ui_rt.settings.db_path)


def test_default_runtime_refuses_to_run_under_pytest():
    with pytest.raises(RuntimeError, match="pytest"):
        default_runtime()


def test_production_factory_passes_the_run_settings_and_tees_only_with_an_observer(tmp_path):
    settings = build_settings(tmp_path)
    factory = production_factory(settings, {})
    cfg = UiConfig(FEB, "replay", 0.5)
    with factory(cfg) as svc:
        assert (svc.as_of, svc.provider_choice, svc._payment_fail_rate) == (FEB, "replay", 0.5)
        assert isinstance(svc.log, JsonlLogger)
    seen = []
    with factory(cfg, observer=seen.append) as svc:
        assert isinstance(svc.log, TeeLogger)
        svc.log.event("run_start", trace_id="t")
    assert seen and seen[0]["trace_id"] == "t"


def test_provider_for_a_fake_model(ui_rt):
    view = ui_rt.provider(ui_rt.default_config)
    assert view.name == "fake" and view.models == {} and view.recording_date is None
    assert ui_rt.provider(ui_rt.default_config) is view  # cached


def test_provider_none_when_nothing_is_configured(tmp_path):
    rt = make_runtime(tmp_path, provider=None, env={})
    view = rt.provider(rt.default_config)
    assert view.name == "none" and view.reason is None


def test_provider_replay_reads_the_recording_manifest(tmp_path):
    settings = build_settings(tmp_path, replay_dir=REPO_ROOT / "fixtures/replay")
    rt = make_runtime(tmp_path, provider=None, env={}, settings=settings,
                      default_config=UiConfig(settings.as_of, "replay"))
    view = rt.provider(rt.default_config)
    assert view.name == "replay" and view.models["extraction"] and view.recording_date.startswith("2026-")


def test_provider_grok_needs_no_network_and_never_exposes_the_key(tmp_path):
    settings = build_settings(tmp_path).model_copy(update={"extraction_model": "m-ext", "reasoning_model": "m-rsn"})
    rt = make_runtime(tmp_path, provider=None, env={"XAI_API_KEY": "xai-SECRET-123"}, settings=settings,
                      default_config=UiConfig(settings.as_of, "grok"))
    view = rt.provider(rt.default_config)
    assert (view.name, view.models) == ("grok", {"extraction": "m-ext", "reasoning": "m-rsn"})
    assert "xai-SECRET-123" not in repr(view) and "xai-SECRET-123" not in repr(rt.preflight(rt.default_config, ["a"]))


def test_provider_reason_when_the_chosen_provider_cannot_resolve(tmp_path):
    rt = make_runtime(tmp_path, provider=None, env={}, default_config=None)
    view = rt.provider(UiConfig(rt.settings.as_of, "grok"))
    assert view.name == "none" and "XAI_API_KEY" in view.reason


def test_provider_is_none_while_busy_and_not_cached(tmp_path):
    rt = make_runtime(tmp_path, inline=False)
    gate = threading.Event()
    job = rt.jobs.submit("batch", "x", lambda ctx: gate.wait(10))
    try:
        assert rt.provider(UiConfig(rt.settings.as_of, "replay")) is None
    finally:
        gate.set()
        job.wait(10)
    assert rt.provider(UiConfig(rt.settings.as_of, "replay")) is not None


def test_preflight_rules(tmp_path):
    cfg_hash = file_sha256(SAMPLES / "invoice_1001.txt")
    fake = make_runtime(tmp_path / "a")
    assert fake.preflight(fake.default_config, []).ok
    assert fake.preflight(fake.default_config, [cfg_hash]).ok and fake.preflight(fake.default_config, [cfg_hash]).provider == "fake"

    none = make_runtime(tmp_path / "b", provider=None, env={})
    assert none.preflight(none.default_config, []).ok
    blocked = none.preflight(none.default_config, [cfg_hash])
    assert not blocked.ok and blocked.provider == "none" and "XAI_API_KEY" in blocked.message

    settings = build_settings(tmp_path / "c", replay_dir=REPO_ROOT / "fixtures/replay")
    replay = make_runtime(tmp_path / "c", provider=None, env={}, settings=settings,
                          default_config=UiConfig(settings.as_of, "replay"))
    assert replay.preflight(replay.default_config, [cfg_hash]).ok
    missing = replay.preflight(replay.default_config, [cfg_hash, "f" * 64])
    assert not missing.ok and missing.provider == "replay" and "1 of 2" in missing.message


def test_preview_raises_busy_while_a_job_holds_the_lock(ui_rt_batch):
    rt = make_runtime(ui_rt_batch.settings.db_path.parent, inline=False)
    gate = threading.Event()
    job = rt.jobs.submit("batch", "x", lambda ctx: gate.wait(10))
    try:
        with pytest.raises(Busy) as busy:
            rt.preview(rt.default_config, "INV-1012", None)
        assert busy.value.job is job
    finally:
        gate.set()
        job.wait(10)
    preview = rt.preview(rt.default_config, "INV-1012", None)
    assert preview.payment_amount == Decimal("9975.00")
    with pytest.raises(NotFound):
        rt.preview(rt.default_config, "INV-9999", None)


def test_start_invoice_pays_a_clean_invoice(ui_rt):
    snap = ui_rt.start_invoice(ui_rt.default_config, SAMPLES / "invoice_1001.txt").snapshot()
    assert snap.state == "done" and snap.value.status == Status.PAID
    assert snap.results[0].file == "invoice_1001.txt" and snap.live["invoice_1001.txt"].trace_id == snap.value.trace_id
    assert any(e.kind == "node_start" for e in snap.events)  # the tee logger feeds the job


def test_start_batch_runs_the_folder_with_progress(ui_rt):
    job = ui_rt.start_batch(ui_rt.default_config, SAMPLES)
    snap = job.snapshot()
    assert snap.total_files == 20 and len(snap.results) == 20
    assert snap.value.counts == {"PAID": 5, "REJECTED": 9, "PENDING_HUMAN": 3, "DUPLICATE": 3}


def test_start_resolve_pays_a_queued_invoice(ui_rt_batch):
    with ui_rt_batch.reader(ui_rt_batch.default_config) as r:
        item = next(q for q in r.queue() if q.invoice_ref == "INV-1012")
    snap = ui_rt_batch.start_resolve(
        ui_rt_batch.default_config, item.trace_id, action="approve", note="confirmed",
        fx_rate=None, confirm_overspend=False).snapshot()
    assert snap.state == "done" and snap.value.status == Status.PAID and snap.value.amount == Decimal("9975.00")


def test_start_resolve_reports_a_refusal_as_the_job_error(ui_rt_batch):
    snap = ui_rt_batch.start_resolve(
        ui_rt_batch.default_config, "INV-1012", action="approve", note="  ", fx_rate=None,
        confirm_overspend=False).snapshot()
    assert snap.state == "failed" and type(snap.error).__name__ == "NoteRequired"


def test_start_reset_reseeds_and_clears_history(ui_rt_batch):
    rt = ui_rt_batch
    rt.start_invoice(rt.default_config, SAMPLES / "invoice_1001.txt")
    assert rt.jobs.latest() is not None
    job = rt.start_reset(rt.default_config, "readme")
    assert job.snapshot().state == "done"
    with rt.reader(rt.default_config) as r:
        assert r.invoices() == [] and r.seed_name() == "readme"
        assert r.starting_stock()["WidgetA"] == 15
    assert rt.jobs.latest("invoice") is None and rt.jobs.latest("batch") is None


def test_configs_are_isolated_by_their_budget_month(ui_rt_batch):
    rt = ui_rt_batch
    january, february = UiConfig(dt.date(2026, 1, 31)), UiConfig(FEB)
    with rt.reader(january) as j, rt.reader(february) as f:
        assert (j.budget_month, f.budget_month) == ("2026-01", "2026-02")
        assert j.budget_status().spent == Decimal("19140.00") and f.budget_status().spent == 0
    with rt.factory(february) as svc:
        assert svc.as_of == FEB
    with rt.factory(january) as svc:
        assert svc.as_of == dt.date(2026, 1, 31)


# ------------------------------------------------------------------ replay pacing


class ReplayLike(ScriptedFake):
    name = "replay"


def paced_runtime(tmp_path, slept):
    return make_runtime(tmp_path, provider=ReplayLike(golden_script()), sleep=slept.append)


def test_a_replay_run_is_paced_per_the_rules(tmp_path):
    slept: list[float] = []
    rt = paced_runtime(tmp_path, slept)
    snap = rt.start_invoice(rt.default_config, SAMPLES / "invoice_1001.txt").snapshot()
    nodes = [e.node for e in snap.events if e.kind == "update"]
    assert nodes and slept == [0.8 if n in SLOW_NODES else 0.3 for n in nodes]
    assert 0.8 in slept and slept.count(0.8) == 2  # extract_with_llm and vp_decide


def test_pacing_is_off_when_slow_replay_is_off(tmp_path):
    slept: list[float] = []
    rt = paced_runtime(tmp_path, slept)
    cfg = dataclasses.replace(rt.default_config, slow_replay=False)
    assert rt.start_invoice(cfg, SAMPLES / "invoice_1001.txt").snapshot().state == "done"
    assert slept == []


def test_a_fake_provider_is_never_paced(tmp_path):
    slept: list[float] = []
    rt = make_runtime(tmp_path, sleep=slept.append)
    assert rt.start_invoice(rt.default_config, SAMPLES / "invoice_1001.txt").snapshot().state == "done"
    assert slept == []


def test_batch_and_resolve_are_never_paced(tmp_path, golden_db):
    slept: list[float] = []
    rt = runtime_from_snapshot(
        tmp_path / "rt", golden_db, provider=ReplayLike(golden_script()), sleep=slept.append)
    assert rt.start_batch(rt.default_config, SAMPLES).snapshot().state == "done"  # stored results: no model
    assert rt.start_resolve(rt.default_config, "INV-1012", action="reject", note="no",
                            fx_rate=None, confirm_overspend=False).snapshot().state == "done"
    assert slept == []
    fresh = make_runtime(tmp_path / "fresh", provider=ReplayLike(golden_script()), sleep=slept.append)
    assert fresh.start_batch(fresh.default_config, SAMPLES).snapshot().state == "done"  # real model calls
    assert slept == []
