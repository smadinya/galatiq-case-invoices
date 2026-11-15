import sqlite3
import threading

import pytest
from streamlit.testing.v1 import AppTest

from ui.kit.runtime import RUNTIME_KEY
from ui.kit.testing import (
    app_test, make_runtime, nav_titles, open_page, page_text, runtime_from_snapshot, widget,
)

PAGES = {"process": "Process an invoice", "queue": "Review queue", "ledger": "Ledger", "batch": "Batch"}


def sidebar_text(at):
    return "\n".join(
        [c.value for c in at.sidebar.markdown] + [c.value for c in at.sidebar.caption]
        + [w.value for w in at.sidebar.warning])


@pytest.mark.parametrize("page", list(PAGES))
def test_every_page_stub_renders(app, page):
    open_page(app, page)
    assert not app.exception, [e.value for e in app.exception]
    assert [t.value for t in app.title] == [PAGES[page]]


def test_the_navigation_lists_four_pages_with_the_queue_count(app, app_batch):
    app.run()
    assert nav_titles(app) == ["Process", "Review queue", "Ledger", "Batch"]
    app_batch.run()
    assert nav_titles(app_batch) == ["Process", "Review queue (3)", "Ledger", "Batch"]


def test_the_sidebar_shows_the_books(app_batch):
    app_batch.run()
    assert not app_batch.exception
    text = sidebar_text(app_batch)
    assert "Acme accounts payable" in text
    assert "As of 31 Jan 2026. Demo data." in text
    assert "fake" in text
    assert "January budget" in text
    assert "\\$19,140\\.00 of \\$50,000\\.00 spent" in text and "\\$30,860\\.00** left" in text
    assert app_batch.sidebar.progress[0].value == 38  # 19,140 of 50,000
    html = page_text(app_batch)
    assert "k-meters" in html
    assert "56 of 90" in html and "32 of 50" in html and "11 of 13" in html and "none in stock" in html
    assert "Stock and spending carry over between runs." in text
    assert [b.label for b in app_batch.sidebar.button] == ["Reset demo data…"]


def test_a_fresh_runtime_shows_the_full_budget(app):
    app.run()
    text = sidebar_text(app)
    assert "\\$0\\.00 of \\$50,000\\.00 spent" in text and "\\$50,000\\.00** left" in text
    assert "90 of 90" in page_text(app)


def test_over_the_limit_fills_the_bar_and_says_by_how_much(ui_rt):
    conn = sqlite3.connect(ui_rt.settings.db_path)
    conn.execute("INSERT INTO budget_ledger(month, amount_usd, invoice_id, ts) VALUES('2026-01','52500.50',NULL,'2026-01-30T00:00:00+00:00')")
    conn.commit()
    conn.close()
    at = app_test(ui_rt).run()
    assert at.sidebar.progress[0].value == 100
    text = sidebar_text(at)
    assert "Over the limit by \\$2,500\\.50" in text and "left" not in text


def test_run_settings_widgets_and_defaults(app):
    app.run()
    assert widget(app, "cfg.provider").value == "auto"
    assert widget(app, "cfg.fail_rate").value == 0.0
    assert widget(app, "cfg.slow_replay").value is True
    assert [e.label for e in app.sidebar.expander] == ["Run settings"]
    assert "cfg.as_of" not in app.session_state  # the as-of date is not a setting


def test_no_model_shows_the_hint_and_the_reason_under_the_provider(tmp_path):
    rt = make_runtime(tmp_path, provider=None, env={})
    at = app_test(rt).run()
    assert not at.exception
    assert "No model available. Add XAI_API_KEY and the two model names to .env" in sidebar_text(at)
    assert len(at.sidebar.warning) == 1
    widget(at, "cfg.provider").set_value("grok").run()
    assert len(at.sidebar.warning) == 2
    assert "XAI_API_KEY" in at.sidebar.warning[1].value.replace("\\", "")


def _job_app():
    import streamlit as st

    from config.settings import REPO_ROOT
    from ui.kit.runtime import get_runtime
    from ui.kit.sidebar import render_sidebar

    rt = get_runtime()
    cfg = render_sidebar(rt)
    if st.button("go", key="go"):
        rt.start_invoice(cfg, REPO_ROOT / "data/invoices/invoice_1001.txt")


def test_changing_the_failure_rate_reaches_the_next_jobs_service(ui_rt):
    seen = []
    inner = ui_rt.factory

    def spy(cfg, **kw):
        svc = inner(cfg, **kw)
        seen.append((cfg, svc))
        return svc

    ui_rt.factory = spy
    at = AppTest.from_function(_job_app, default_timeout=30)
    at.session_state[RUNTIME_KEY] = ui_rt
    at.run()
    widget(at, "cfg.fail_rate").set_value(0.5)
    widget(at, "cfg.slow_replay").set_value(False)
    at.run()
    seen.clear()
    widget(at, "go").click().run()
    assert not at.exception
    cfg, svc = seen[-1]
    assert cfg.payment_fail_rate == 0.5 and cfg.slow_replay is False and cfg.provider_choice is None
    assert svc._payment_fail_rate == 0.5
    assert svc.as_of == ui_rt.default_config.as_of


def _open_reset(at, seed=None):
    at.session_state["cfg.reset_open"] = True
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    if seed:
        widget_radio = next(r for r in at.radio if r.key == "cfg.reset_seed")
        widget_radio.set_value(seed)
        at.run()
    return at


def test_the_flag_opened_reset_dialog_reseeds_after_a_run_and_clears_page_state(app_batch, ui_rt_batch):
    at = app_batch
    at.run()
    at.session_state["batch.job_id"] = "abc"
    at.session_state["queue.selected"] = 3
    at.session_state["process.trace"] = "inv-x"
    at.session_state["unrelated"] = "kept"
    _open_reset(at)
    labels = [b.label for b in at.button]
    assert "Reset and clear history" in labels and "Cancel" in labels
    widget(at, "cfg.reset_confirm").click().run()
    assert not at.exception, [e.value for e in at.exception]
    with ui_rt_batch.reader(ui_rt_batch.default_config) as r:
        assert r.invoices() == [] and r.seed_name() == "demo" and r.queue(None) == []
    for key in ("batch.job_id", "queue.selected", "process.trace"):
        assert key not in at.session_state
    assert at.session_state["unrelated"] == "kept"
    assert at.session_state["cfg.reset_open"] is False
    assert [t.value for t in at.toast] == ["Reset to demo data. History cleared."]
    assert nav_titles(at)[1] == "Review queue"
    assert "90 of 90" in page_text(at)


def test_reset_to_the_briefs_data_and_cancel(app_batch, ui_rt_batch):
    at = app_batch.run()
    _open_reset(at)
    widget(at, "cfg.reset_cancel").click().run()
    assert at.session_state["cfg.reset_open"] is False
    with ui_rt_batch.reader(ui_rt_batch.default_config) as r:
        assert len(r.invoices()) == 20  # cancel changes nothing
    _open_reset(at, seed="readme")
    widget(at, "cfg.reset_confirm").click().run()
    with ui_rt_batch.reader(ui_rt_batch.default_config) as r:
        assert r.seed_name() == "readme" and r.starting_stock()["WidgetA"] == 15
    assert "15 of 15" in page_text(at)
    assert [t.value for t in at.toast] == ["Reset to the brief's starting data. History cleared."]


def test_the_busy_indicator_shows_while_a_threaded_job_is_blocked(tmp_path, golden_db):
    rt = runtime_from_snapshot(tmp_path / "rt", golden_db, inline=False)
    at = app_test(rt).run()
    assert "A run is in progress" not in sidebar_text(at)
    gate = threading.Event()
    job = rt.jobs.submit("batch", "Running invoices", lambda ctx: gate.wait(20), total_files=20)
    try:
        at.run()
        assert not at.exception
        assert "Running invoices" in sidebar_text(at)
        assert "0 of 20 files" in page_text(at)
        assert widget(at, "cfg.reset").disabled
        assert widget(at, "cfg.provider").disabled and widget(at, "cfg.fail_rate").disabled
        assert widget(at, "cfg.slow_replay").disabled
    finally:
        gate.set()
        job.wait(20)
    at.run()
    assert "Running invoices" not in sidebar_text(at)
    assert not widget(at, "cfg.reset").disabled


def test_the_shell_uses_no_emoji(app_batch):
    app_batch.run()
    text = page_text(app_batch)
    assert not any(ord(ch) > 0x1F000 for ch in text)
