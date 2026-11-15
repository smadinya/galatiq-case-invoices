"""Batch and metrics (design 5.4): run a folder, see every file's outcome, see what the run measured."""

import streamlit as st

from core.metrics import latest_per_file, savings_estimate
from llm.select import NoProviderAvailable
from models import InvoiceResult, Status
from ui.kit import batch_view as bv
from ui.kit import components, copy, fmt, live, theme
from ui.kit.data import file_sha256
from ui.kit.jobs import Busy, JobSnapshot
from ui.kit.runtime import get_runtime
from ui.kit.sidebar import session_config
from ui.kit.uploads import sample_files, store_batch_upload

theme.style()
st.title(copy.t("title.batch"))

rt = get_runtime()
cfg = session_config(rt)
st.session_state.setdefault("batch.folder", bv.DEFAULT_FOLDER)


# ------------------------------------------------------------------ callbacks


def _on_upload() -> None:
    uploaded = st.session_state.get("batch.upload") or []
    st.session_state.pop("batch.upload_error", None)
    if not uploaded:
        return
    try:
        folder = store_batch_upload(rt.upload_dir, [(f.name, f.getvalue()) for f in uploaded])
    except ValueError as exc:
        st.session_state["batch.upload_error"] = str(exc)
        return
    st.session_state["batch.folder"] = bv.display_path(folder)


def _pick_case() -> None:
    st.session_state["batch.case"] = st.session_state.pop("batch.inspect", None)


def _close_case() -> None:
    st.session_state["batch.case"] = None


# ------------------------------------------------------------------- sections


def _case_dialog(result: InvoiceResult) -> None:
    @st.dialog(bv.case_title(result), width="large", on_dismiss=_close_case)
    def body() -> None:
        with rt.reader(cfg) as reader:
            record = reader.invoice_by_trace(result.trace_id)
            detail = reader.case_detail(record) if record is not None else None
        components.render_case(result, detail, key="batch.case")

    body()


def _results_table(results: list[InvoiceResult], *, interactive: bool) -> None:
    rows = bv.result_rows(results)
    frame = bv.table_frame(rows)
    config = bv.table_config(rows, theme.theme_type())
    if not interactive:
        st.dataframe(frame, hide_index=True, column_config=config, height=bv.table_height(len(rows)))
        return
    event = st.dataframe(
        frame, hide_index=True, column_config=config, height=bv.table_height(len(rows)),
        on_select="rerun", selection_mode="single-row", key="batch.table",
    )
    chosen = tuple(event.selection.rows)
    if chosen != st.session_state.get("batch.sel_seen", ()):
        st.session_state["batch.sel_seen"] = chosen
        if chosen and chosen[0] < len(rows):
            st.session_state["batch.case"] = rows[chosen[0]].trace_id
    files = {r.trace_id: r.file for r in rows}
    st.selectbox(
        bv.LABEL_INSPECT, list(files), index=None, key="batch.inspect", format_func=files.get,
        placeholder="Choose a file", on_change=_pick_case,
    )
    open_trace = st.session_state.get("batch.case")
    shown = next((r for r in results if r.trace_id == open_trace), None) if open_trace else None
    if shown is not None:
        _case_dialog(shown)


def _error_callout(results: list[InvoiceResult]) -> None:
    errors = sum(1 for r in results if r.status == Status.ERROR)
    if errors:
        with st.container(border=True):
            components.status_badge(Status.ERROR)
            st.markdown(fmt.md(bv.error_callout(errors)))


def _outcome(results: list[InvoiceResult], *, link: bool) -> None:
    counts = bv.status_counts(results)
    st.header(fmt.md(bv.outcome_sentence(counts)), anchor=False)
    components.segmented_bar(counts)
    waiting = dict(counts).get(Status.PENDING_HUMAN, 0)
    if link and waiting:
        st.page_link(components.QUEUE_PAGE, label=bv.waiting_link_label(waiting), icon=":material/pending_actions:")


def _measures(results: list[InvoiceResult], *, history: bool) -> None:
    if not results:
        return
    measured = bv.measure(results)
    shown = measured.shown
    st.subheader(bv.SECTION_METRICS_HISTORY if history else bv.SECTION_METRICS, anchor=False)
    figures = bv.metric_figures(shown, replay=measured.replay)
    for col, fig in zip(st.columns(len(figures)), figures):
        with col:
            st.metric(fig.label, fig.value, help=fmt.md(fig.help), border=False)
    st.caption(fmt.md(bv.metrics_caption(measured)))

    estimate = savings_estimate(shown, rt.settings)
    if estimate is None:
        return
    lines = bv.savings_lines(estimate, shown)
    st.subheader(bv.SECTION_SAVINGS, anchor=False)
    st.markdown(fmt.md(lines.formula))
    st.markdown(fmt.md(lines.substituted))
    st.markdown(f"**{fmt.md(lines.result)}**")
    st.dataframe(bv.savings_table(estimate, shown), hide_index=True, height=35 * 5)


def _finished(results: list[InvoiceResult], *, history: bool = False) -> None:
    _outcome(results, link=True)
    _error_callout(results)
    _results_table(results, interactive=True)
    _measures(results, history=history)


# ------------------------------------------------------------------ live view


def _live(snap: JobSnapshot) -> None:
    results = list(snap.results)
    total = snap.total_files or 0
    done = len(results)
    file = snap.current_file
    if file in {r.file for r in results}:
        file = None
    step = None
    for event in reversed(snap.events):
        if event.kind == "node_start" and event.node and event.file == file:
            step = copy.active_label(event.node)
            break
    st.progress(
        min(1.0, done / total) if total else 0.0, text=fmt.md(bv.progress_text(done, total, file, step)),
    )
    if results:
        _outcome(results, link=False)
        _results_table(results, interactive=False)


# ----------------------------------------------------------------------- page

folder_text = st.session_state["batch.folder"]
folder = bv.folder_path(rt, folder_text)
files = sample_files(folder) if folder.is_dir() else []

job_id = st.session_state.get("batch.job_id")
job = (rt.jobs.get(job_id) if job_id else None) or rt.jobs.latest("batch")
running = job is not None and job.state == "running"

stored_count = 0
new_hashes: list[str] = []
if files:
    with rt.reader(cfg) as reader:
        for path in files:
            digest = file_sha256(path)
            if reader.stored_result(digest) is None:
                new_hashes.append(digest)
            else:
                stored_count += 1
preflight = rt.preflight(cfg, new_hashes) if files else None

blocked = not files or running or (preflight is not None and not preflight.ok)
with st.container(horizontal=True, vertical_alignment="bottom"):
    st.text_input(bv.LABEL_FOLDER, key="batch.folder", disabled=running, width="stretch")
    run_clicked = st.button(copy.t("button.batch"), key="batch.run", type="primary", disabled=blocked)

if not folder.is_dir():
    st.warning(fmt.md(copy.t("warn.folder_missing", path=folder_text.strip() or bv.DEFAULT_FOLDER)))
elif not files:
    st.warning(fmt.md(copy.t("warn.folder_empty", path=folder_text.strip())))
else:
    st.caption(fmt.md(bv.count_caption(files)))
    this_run_shown = job is not None and st.session_state.get("batch.job_folder") == str(folder)
    if stored_count and not running and not this_run_shown:
        st.info(fmt.md(copy.t("info.rerun_batch", n=stored_count)))
    if preflight is not None and not preflight.ok and not running:
        st.warning(fmt.md(preflight.message or copy.t("error.no_model_sidebar")))

with st.expander(bv.LABEL_UPLOAD):
    st.file_uploader(
        bv.LABEL_UPLOAD, accept_multiple_files=True, key="batch.upload", on_change=_on_upload,
        help=bv.HELP_UPLOAD, label_visibility="collapsed", disabled=running,
    )
    if st.session_state.get("batch.upload_error"):
        st.warning(fmt.md(st.session_state["batch.upload_error"]))

if run_clicked and not blocked:
    try:
        job = rt.start_batch(cfg, folder)
    except Busy:
        st.warning("A run is in progress. Run the batch when it finishes.")
    else:
        st.session_state["batch.job_id"] = job.id
        st.session_state["batch.job_folder"] = str(folder)
        for stale in ("batch.case", "batch.sel_seen"):
            st.session_state.pop(stale, None)
        running = job.state == "running"

if job is None:
    with rt.reader(cfg) as reader:
        history = [r.result for r in latest_per_file(reader.invoices()) if r.result is not None]
    if not history:
        st.markdown(fmt.md(copy.t("empty.batch")))
    else:
        st.markdown(fmt.md(bv.history_line(len(history))))
        _finished(history, history=True)
elif running:
    if st.button(bv.BUTTON_STOP, key="batch.stop", disabled=job.stop_requested):
        job.request_stop()
    live.follow(job, _live)
    st.rerun()
else:
    snap = job.snapshot()
    results = list(snap.results)
    failed_file = None
    if snap.state == "failed" and isinstance(snap.error, NoProviderAvailable):
        failed_file = snap.current_file
        with rt.reader(cfg) as reader:
            latest = [
                rec.result for rec in reader.invoices()
                if rec.file_name == failed_file and rec.result is not None and rec.status == Status.ERROR
            ]
        if latest and failed_file not in {r.file for r in results}:
            results.append(latest[-1])
    if snap.state == "stopped":
        st.caption(bv.stopped_text(len(snap.results), snap.total_files or len(snap.results)))
    elif snap.state == "failed":
        if isinstance(snap.error, NoProviderAvailable):
            st.warning(fmt.md(copy.t("warn.stopped", file=failed_file or "the next file")))
            ways = bv.ways_to_get_a_model(str(snap.error))
            if ways:
                st.markdown("\n".join(f"{i}. {fmt.md(w)}" for i, w in enumerate(ways, 1)))
        else:
            st.error(f"{type(snap.error).__name__}: {fmt.md(str(snap.error))}")
    if results:
        _finished(results)
