"""Review queue: the decisions only a person may make (design 5.2).

Reads happen first (list, record, state, preview), widgets follow, and a submit runs one
resolve job whose outcome is kept in `queue.flash` across the rerun. The live region shows
display-only text; no widget is created inside it.
"""

from decimal import Decimal

import streamlit as st

from models import FlagCode, InvoiceRecord, InvoiceState, QueueItem, ResolutionPreview, Status
from service import NotFound, ResolutionRefused
from ui.kit import components, copy, fmt, live, queue_view as qv, theme
from ui.kit.jobs import Busy
from ui.kit.runtime import get_runtime
from ui.kit.sidebar import session_config

FORM_KEYS = ("queue.action", "queue.note", "queue.fx_rate", "queue.confirm_overspend")
STATE_KEYS = ("queue.refusal", "queue.reveal_confirm", "queue.notice", "queue.gone_rerun")
SHOW_FACTS = {FlagCode.VENDOR_RENAMED, FlagCode.PAYMENT_FAILED}


def _clear(keys) -> None:
    for key in keys:
        st.session_state.pop(key, None)


def _on_select() -> None:
    """The reader picked another item: the result panel and any messages belong to the old one."""
    _clear(("queue.flash",) + STATE_KEYS)


def _on_next(next_id: int) -> None:
    _clear(("queue.flash",) + STATE_KEYS)
    st.session_state["queue.selected"] = next_id


def _amount(item: QueueItem, record: InvoiceRecord | None) -> str:
    if record is not None and record.amount is not None:
        return fmt.money(record.amount, record.currency)
    review = item.review
    if review.payment_amount is not None:
        return fmt.money(review.payment_amount, review.currency)
    return ""


def _caption(item: QueueItem, record: InvoiceRecord | None) -> str:
    parts = [item.vendor or copy.t("case.no_vendor"), _amount(item, record)]
    return fmt.md(", ".join(p for p in parts if p))


def _facts_line(item: QueueItem, record: InvoiceRecord | None) -> str:
    parts = [item.vendor or copy.t("case.no_vendor"), _amount(item, record), f"from {item.file_name}"]
    return fmt.md(", ".join(p for p in parts if p))


def _render_resolving(ref: str):
    def render(snapshot) -> None:
        if snapshot.running:
            st.markdown(f":material/progress_activity: {fmt.md(qv.RESOLVING.format(ref=ref))}")

    return render


# ------------------------------------------------------------------ empty and result panels


def _empty_state(all_items: list[QueueItem], resolved: list[QueueItem], reader) -> None:
    if not all_items:
        st.markdown(fmt.md(copy.t("empty.queue")))
        st.page_link("views/1_Process.py", label=copy.t("link.process"), icon=":material/description:")
        st.page_link("views/4_Batch.py", label=copy.t("link.batch"), icon=":material/stacks:")
        return
    approved, rejected, paid = qv.queue_summary(resolved, reader.payments())
    st.markdown(fmt.md(copy.t("empty.queue_done", approved=approved, rejected=rejected, total=fmt.money(paid))))
    st.page_link("views/3_Ledger.py", label=copy.t("link.ledger"), icon=":material/menu_book:")


def _flash_panel(flash: dict) -> None:
    kind = flash["kind"]
    text = fmt.md(flash["message"])
    if kind == "success":
        st.success(text)
    elif kind == "error":
        components.status_badge(Status.ERROR)
        st.markdown(fmt.md(copy.t("error.system_body")))
        if flash.get("detail"):
            st.code(flash["detail"], language=None)
        st.caption(fmt.md(copy.t("error.system_trace", trace_id=flash["trace_id"])))
    else:
        components.status_badge(Status(flash["status"]))
        st.markdown(text)


# --------------------------------------------------------------------------- detail


def _reason_block(item: QueueItem, evidence: qv.ReasonEvidence) -> None:
    with st.container(width=760):
        st.markdown(fmt.md(evidence.explanation))
    if item.reason in SHOW_FACTS and evidence.facts:
        components.kv_block(list(evidence.facts))


def _recommendation(item: QueueItem, record: InvoiceRecord | None) -> None:
    review = item.review
    st.subheader(qv.VP_HEADING, anchor=False)
    st.markdown(fmt.md(copy.recommendation_text(review.recommendation)))
    if review.recommendation_reasoning:
        with st.container(width=760):
            st.caption(qv.REASONING_CAPTION)
            st.markdown(fmt.md(review.recommendation_reasoning))
    flags = record.result.flags if record is not None and record.result is not None else []
    labels = [
        copy.flag_label(f) for f in flags if f.code != item.reason
    ]
    labels = list(dict.fromkeys(labels))
    if labels:
        st.markdown(fmt.md(qv.OTHER_FLAGS.format(labels=", ".join(labels))))


def _refusal_text(kind: str) -> str | None:
    refusal = st.session_state.get("queue.refusal")
    return refusal["text"] if refusal and refusal["kind"] == kind else None


def _detail(rt, cfg, reader, item: QueueItem) -> None:
    record = reader.invoice(item.invoice_id)
    state = reader.invoice_state(item.trace_id)
    vendor = reader.vendor(record.vendor_id) if record is not None and record.vendor_id else None
    aliases = reader.vendor_aliases(vendor.id) if vendor is not None else []
    budget = reader.budget_status()
    evidence = qv.reason_evidence(
        item, record, state, vendor, reader.payments() if item.reason == FlagCode.PAYMENT_FAILED else [],
        budget=budget, aliases=aliases,
    )

    # Form state is read from session_state first so the preview can depend on the rate.
    fx_text = st.session_state.get("queue.fx_rate", "")
    preview: ResolutionPreview | None = None
    busy = gone = False
    try:
        preview = rt.preview(cfg, item.trace_id, fmt.parse_fx_rate(fx_text))
    except Busy:
        busy = True
    except NotFound:
        gone = True
    if gone:
        if st.session_state.get("queue.gone_rerun") != item.trace_id:
            st.session_state["queue.gone_rerun"] = item.trace_id
            st.session_state["queue.notice"] = qv.GONE.format(ref=item.invoice_ref)
            st.rerun()
        st.warning(fmt.md(qv.GONE.format(ref=item.invoice_ref)))
        return

    st.header(fmt.md(f"{item.invoice_ref}: {evidence.title.lower()}"), anchor=False)
    with st.container(horizontal=True, vertical_alignment="center"):
        components.status_badge(Status.PENDING_HUMAN)
        st.caption(f"waiting since {fmt.when(item.created_at)}")
    st.markdown(_facts_line(item, record))
    _reason_block(item, evidence)
    _recommendation(item, record)
    st.page_link(
        components.PROCESS_PAGE, label=copy.t("link.open_case"), icon=":material/description:",
        query_params={"trace": item.trace_id},
    )

    can_approve = preview.can_approve if preview is not None else True
    action: str = "approve" if can_approve else "reject"

    if preview is not None and can_approve:
        st.subheader(qv.PREVIEW_HEADING, anchor=False)
        if preview.needs_fx_rate:
            st.text_input(
                qv.RATE_LABEL.format(currency=preview.currency), key="queue.fx_rate", placeholder=qv.RATE_PLACEHOLDER,
                help=qv.RATE_HELP.format(currency=preview.currency), disabled=busy,
            )
            if fx_text.strip() and fmt.parse_fx_rate(fx_text) is None:
                st.caption(qv.RATE_INVALID)
            msg = _refusal_text("fx")
            if msg and fmt.parse_fx_rate(fx_text) is None:
                st.warning(fmt.md(msg))
        rows = qv.preview_rows(preview, item, state)
        if rows:
            components.kv_block(rows)
        if preview.approve_outcome == "reject_stock":
            st.error(fmt.md(qv.stock_error(preview)))
    elif busy:
        st.info(qv.BUSY)

    st.subheader(qv.DECISION_HEADING, anchor=False)
    if preview is not None and not can_approve:
        st.markdown(fmt.md(qv.READ_ONLY))
    else:
        st.session_state.setdefault("queue.action", "approve")
        picked = st.segmented_control(
            "Action", ["approve", "reject"], key="queue.action", label_visibility="collapsed",
            format_func=lambda v: {"approve": "Approve", "reject": "Reject"}[v], disabled=busy,
        )
        action = picked or "approve"

    note = st.session_state.get("queue.note", "")
    confirm = bool(st.session_state.get("queue.confirm_overspend", False))
    force_confirm = bool(st.session_state.get("queue.reveal_confirm"))
    fs = qv.form_state(
        preview, action=action, note=note, fx_text=fx_text, confirm=confirm, force_confirm=force_confirm,  # type: ignore[arg-type]
    )

    if action == "approve" and fs.needs_confirm and preview is not None:
        msg = _refusal_text("confirm")
        if msg and not confirm:
            st.warning(fmt.md(msg))
        label = qv.overspend_label(preview) if preview.needs_overspend_confirmation else "Approve the overspend."
        st.checkbox(fmt.md(label), key="queue.confirm_overspend", disabled=busy)

    st.text_area(
        qv.NOTE_LABEL, key="queue.note", placeholder=qv.note_placeholder(item, state), disabled=busy,
    )
    msg = _refusal_text("note")
    if msg and not note.strip():
        st.warning(fmt.md(msg))

    clicked = st.button(
        qv.submit_label(preview, action), key="queue.submit", type="primary",  # type: ignore[arg-type]
        disabled=busy or not fs.can_submit,
    )
    if not busy and not fs.can_submit and preview is not None:
        caption = qv.missing_caption(fs.problems, action)  # type: ignore[arg-type]
        if caption:
            st.caption(caption)
    for kind in ("approve", "error"):
        msg = _refusal_text(kind)
        if msg:
            st.warning(fmt.md(msg)) if kind == "approve" else st.error(fmt.md(msg))

    if clicked and fs.can_submit and preview is not None:
        _submit(rt, cfg, item, action, note, fs, confirm, preview)


def _submit(rt, cfg, item: QueueItem, action: str, note: str, fs: qv.FormState, confirm: bool,
            preview: ResolutionPreview) -> None:
    try:
        job = rt.start_resolve(
            cfg, item.trace_id, action=action, note=note,  # type: ignore[arg-type]
            fx_rate=fs.fx_rate if action == "approve" else None,
            confirm_overspend=confirm if action == "approve" else False,
        )
    except Busy:
        st.info(qv.BUSY)
        return
    snapshot = live.follow(job, _render_resolving(item.invoice_ref))
    err = snapshot.error
    if isinstance(err, ResolutionRefused):
        kind = {
            "NoteRequired": "note", "FxRateRequired": "fx", "OverspendConfirmationRequired": "confirm",
            "ApproveNotAllowed": "approve",
        }.get(type(err).__name__, "gone")
        st.session_state["queue.refusal"] = {
            "kind": kind, "text": qv.refusal_message(err, ref=item.invoice_ref, currency=preview.currency),
        }
        if kind == "confirm":
            st.session_state["queue.reveal_confirm"] = True
        if kind == "gone":
            st.session_state["queue.notice"] = st.session_state["queue.refusal"]["text"]
        st.rerun()
    if err is not None:
        st.session_state["queue.refusal"] = {"kind": "error", "text": f"{type(err).__name__}: {err}"}
        st.rerun()
    result = snapshot.value
    with rt.reader(cfg) as reader:
        budget = reader.budget_status()
    kind, message = qv.outcome_message(result, spent=budget.spent, month=budget.month)
    still_waiting = kind == "warning"
    st.session_state["queue.flash"] = {
        "kind": kind, "message": message, "status": result.status.value, "ref": item.invoice_ref,
        "invoice_id": item.invoice_id, "trace_id": result.trace_id, "detail": result.error,
        "still_waiting": still_waiting,
    }
    _clear(STATE_KEYS)
    st.session_state["queue._reset_form"] = True
    st.rerun()


# ----------------------------------------------------------------------------- page


theme.style()
st.title(copy.TEXT["title.queue"])

rt = get_runtime()
cfg = session_config(rt)

with rt.reader(cfg) as reader:
    everything = reader.queue(None)
    pending = [i for i in everything if i.status == "PENDING"]
    resolved = [i for i in everything if i.status in ("RESOLVED", "SUPERSEDED")]
    records = {i.invoice_id: reader.invoice(i.invoice_id) for i in pending}

    if st.session_state.pop("queue._reset_form", False):
        _clear(FORM_KEYS + ("queue.refusal", "queue.reveal_confirm"))

    ids = [i.invoice_id for i in pending]
    by_id = {i.invoice_id: i for i in pending}
    # Selection: ?item= wins when it is new; otherwise the session value; otherwise the first item.
    param = st.query_params.get("item")
    if param and param != st.session_state.get("queue._qp"):
        if param.isdigit() and int(param) in by_id:
            st.session_state["queue.selected"] = int(param)
            st.session_state.pop("queue.flash", None)
        st.session_state["queue._qp"] = param
    previous = st.session_state.get("queue.selected")
    if ids and previous not in by_id:
        if (
            previous is not None
            and st.session_state.get("queue.flash", {}).get("invoice_id") != previous
            and "queue.notice" not in st.session_state
        ):
            st.session_state["queue.notice"] = qv.GONE.format(ref=st.session_state.get("queue._last_ref", "That invoice"))
        st.session_state["queue.selected"] = ids[0]
    selected = st.session_state.get("queue.selected")
    if selected in by_id and st.session_state.get("queue._last") != selected:
        _clear(FORM_KEYS + ("queue.refusal", "queue.reveal_confirm", "queue.gone_rerun"))
        st.session_state["queue._last"] = selected
    if selected in by_id:
        st.session_state["queue._last_ref"] = by_id[selected].invoice_ref

    if pending:
        count = len(pending)
        st.markdown(qv.SUBTITLE_ONE if count == 1 else qv.SUBTITLE.format(count=f"{count} invoices"))

    flash = st.session_state.get("queue.flash")
    if selected in by_id and (flash is None or flash.get("still_waiting")):
        st.query_params["item"] = str(selected)
        st.session_state["queue._qp"] = str(selected)
    if not pending and flash is None:
        _empty_state(everything, resolved, reader)
    else:
        left, right = st.columns([1, 2.2], gap="large")
        with left:
            if pending:
                st.radio(
                    "Waiting for a person", ids, key="queue.selected", label_visibility="collapsed",
                    format_func=lambda i: fmt.md(f"{by_id[i].invoice_ref}: {copy.reason_label(by_id[i].reason)}"),
                    captions=[_caption(by_id[i], records[i]) for i in ids], on_change=_on_select,
                )
            with st.expander(f"Resolved ({len(resolved)})"):
                if resolved:
                    for item in sorted(resolved, key=lambda i: i.resolved_at or i.created_at, reverse=True):
                        st.markdown(fmt.md(qv.resolved_line(item)))
                else:
                    st.caption("Nothing resolved yet.")
        with right:
            notice = st.session_state.get("queue.notice")
            if notice:
                st.warning(fmt.md(notice))
            if flash is not None and not flash.get("still_waiting"):
                _flash_panel(flash)
                if pending:
                    nxt = pending[0]
                    st.button(
                        copy.t("button.queue_next", ref=nxt.invoice_ref), key="queue.next", type="primary",
                        on_click=_on_next, args=(nxt.invoice_id,),
                    )
                else:
                    _empty_state(everything, resolved, reader)
            else:
                if flash is not None:
                    st.warning(fmt.md(flash["message"]))
                if selected in by_id:
                    _detail(rt, cfg, reader, by_id[selected])
