"""Process: pick or upload one invoice, watch it run, then read the case (design 5.0, 5.1)."""

import calendar
from pathlib import Path

import pandas as pd
import streamlit as st

from models import InvoiceRecord, InvoiceResult, Status
from ui.kit import case, components, copy, fmt, live, process_view as pv, theme, uploads
from ui.kit.jobs import Busy, Job
from ui.kit.runtime import Runtime, UiConfig, get_runtime
from ui.kit.sidebar import session_config

SS = st.session_state
t = copy.t


def _paced(rt: Runtime, cfg: UiConfig) -> bool:
    view = rt.provider(cfg)
    return cfg.slow_replay and view is not None and view.name == "replay"


def _start(rt: Runtime, cfg: UiConfig, path: Path) -> None:
    """Start the run, forget the previous result, and rerun so the inputs show as disabled."""
    try:
        job = rt.start_invoice(cfg, path)
    except Busy:
        st.warning(pv.TEXT["block.busy"])
        return
    SS["process.job_id"] = job.id
    for key in ("process.trace", "process.stored"):
        SS.pop(key, None)
    st.query_params.pop("trace", None)
    st.rerun()


def _active_job(rt: Runtime) -> Job | None:
    """This session's job, else a running invoice job (the user came back or reloaded)."""
    job = rt.jobs.get(SS["process.job_id"]) if SS.get("process.job_id") else None
    if job is None:
        current = rt.jobs.current()
        if current is not None and current.kind == "invoice":
            SS["process.job_id"] = current.id
            return current
    return job if job is not None and job.kind == "invoice" else None


def _render_run(snap, file: str, paced: bool) -> None:
    label = live.status_label(snap, file)
    if snap.running:
        state = "running"
        if paced:
            label += f" ({t('info.replay_slowed')})"
    else:
        result = snap.value if isinstance(snap.value, InvoiceResult) else None
        state = "error" if snap.error is not None or result is None or result.status == Status.ERROR else "complete"
    with st.status(fmt.md(label), state=state, expanded=snap.running):
        live.render_timeline(live.timeline(snap.events, file, finished=not snap.running))


def _show_failure(error: BaseException, file: str) -> None:
    if pv.is_no_model(error):
        ways = "\n".join(f"- {fmt.md(w)}" for w in pv.no_model_ways(str(error)))
        st.warning(f"**{fmt.md(t('error.no_model_title'))}**\n\n{ways}")
    else:
        st.error(fmt.md(pv.TEXT["failed"].format(file=file, error=error)))


def _follow(rt: Runtime, cfg: UiConfig, job: Job, target: pv.RunTarget | None) -> bool:
    """Show the run. True when the page should stop here (running, or a failure to read)."""
    file = pv.job_file(job)
    paced = _paced(rt, cfg)
    snap = live.follow(job, lambda s: _render_run(s, file, paced))
    if snap.error is not None:
        if target is not None and target.file_name != file:
            return False  # the user moved on to another file
        _show_failure(snap.error, file)
        return True
    if isinstance(snap.value, InvoiceResult):
        SS["process.trace"] = snap.value.trace_id
        SS["process.job_done"] = job.id
        if snap.value.short_circuited:
            SS["process.stored"] = snap.value.trace_id
        st.rerun()
    return True


def _source_preview(target: pv.RunTarget) -> None:
    text = pv.preview_text(target.path)
    if target.path.suffix.lower() == ".pdf":
        st.caption(fmt.md(pv.TEXT["preview.pdf"]))
    elif text is None:
        st.caption(fmt.md(pv.TEXT["preview.unreadable"]))
    else:
        kind = "source.text" if target.path.suffix.lower() == ".txt" else "source.file"
        components.source_panel(case.source_lines(text, []), caption=t(kind))


def _empty_state(rt: Runtime) -> None:
    st.markdown(fmt.md(t("empty.process")))
    names = [p.name for p in uploads.sample_files(rt.samples_dir)]
    rows = [(n, copy.SAMPLE_GUIDE[n]) for n in names if n in copy.SAMPLE_GUIDE]
    if rows:
        st.dataframe(
            pd.DataFrame(rows, columns=["File", "What's in it"]), hide_index=True, width="stretch",
            height="content", key="process.guide",
            column_config={
                "File": st.column_config.TextColumn(width="medium"),
                "What's in it": st.column_config.TextColumn(width="large"),
            },
        )


def _detail(rt: Runtime, cfg: UiConfig, record: InvoiceRecord, job: Job | None) -> None:
    result = pv.record_result(record)
    assert result is not None
    if SS.get("process.stored") == record.trace_id:
        result = result.model_copy(update={"short_circuited": True})
    if job is not None and isinstance(job.snapshot().value, InvoiceResult) and not result.short_circuited:
        snap = job.snapshot()
        if snap.value.trace_id == record.trace_id:
            _render_run(snap, snap.value.file, False)  # how it ran: this session's own run log, collapsed
    with rt.reader(cfg) as reader:
        detail = reader.case_detail(record)
        rows = None
        if record.status == Status.PAID:
            month = calendar.month_name[cfg.as_of.month]
            rows = pv.payment_rows(record, reader.stock_ledger(), reader.budget_ledger(), month) or None
    components.render_case(result, detail, key="process.case", payment_rows=rows)
    if result.status == Status.ERROR:
        again = pv.RunTarget(Path(record.file_path), record.file_name, record.file_hash, None)
        pre = rt.preflight(cfg, [record.file_hash])
        blocker = pv.run_blocker(again, pre, rt.jobs.current())
        if st.button(t("button.process_again"), key="process.again", disabled=blocker is not None):
            _start(rt, cfg, again.path)
        if blocker:
            st.caption(fmt.md(blocker))


def main() -> None:
    theme.style()
    rt = get_runtime()
    cfg = session_config(rt)
    st.title(t("title.process"))

    busy = rt.jobs.current()
    job = _active_job(rt)

    # ---- inputs
    c_source, c_pick, c_run = st.columns([1.2, 3, 1], vertical_alignment="bottom")
    with c_source:
        source = st.segmented_control(
            pv.TEXT["source"], ["sample", "upload"], default="sample", key="process.source",
            format_func=lambda v: pv.TEXT[f"source.{v}"], disabled=busy is not None,
        ) or "sample"
    target: pv.RunTarget | None = None
    with c_pick:
        if source == "sample":
            names = [p.name for p in uploads.sample_files(rt.samples_dir)]
            name = st.selectbox(
                pv.TEXT["sample"], names, index=None, placeholder=pv.TEXT["sample.placeholder"],
                key="process.sample", disabled=busy is not None,
            )
            if name:
                target = pv.sample_target(rt, cfg, name)
        else:
            file = st.file_uploader(
                t("label.uploader"), type=["pdf", "txt", "json", "csv", "xml"], key="process.upload",
                help=t("help.uploader"), disabled=busy is not None,
            )
            if file is not None:
                try:
                    target = pv.upload_target(rt, cfg, file.name, file.getvalue())
                except ValueError as exc:
                    st.error(fmt.md(str(exc)))
    preflight = rt.preflight(cfg, [target.file_hash]) if target is not None and target.stored is None else None
    blocker = pv.run_blocker(target, preflight, busy)
    with c_run:
        clicked = st.button(t("button.process"), key="process.run", type="primary", disabled=blocker is not None)
    note = st.empty()  # the reason Process is disabled; left blank when a case already fills the page
    if target is not None and preflight is not None and preflight.provider == "none":
        st.warning(fmt.md(t("warn.no_model_process")))
    if clicked and target is not None:
        _start(rt, cfg, target.path)

    def say_blocker() -> None:
        if blocker:
            note.caption(fmt.md(blocker))

    # ---- a run: running, just finished, or failed
    if job is not None and (job.snapshot().running or SS.get("process.job_done") != job.id):
        say_blocker()
        if _follow(rt, cfg, job, target):
            return

    # ---- the case, when it is the file in front of the user
    trace = st.query_params.get("trace") or SS.get("process.trace")
    if trace:
        with rt.reader(cfg) as reader:
            record = reader.invoice_by_trace(str(trace))
        if record is None or pv.record_result(record) is None:
            st.info(fmt.md(pv.TEXT["missing"]))
        elif target is None or target.file_hash == record.file_hash:
            if blocker != pv.TEXT["block.nothing"]:
                say_blocker()
            _detail(rt, cfg, record, job)
            return
    say_blocker()
    if target is not None:
        _source_preview(target)
    else:
        _empty_state(rt)


main()
