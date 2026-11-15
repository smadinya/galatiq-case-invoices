"""Rendering for the dashboard (design 5.0 and 5.4): badges, flags, the case view, the source panel.

Display-only components create no widgets, so they are safe inside a polling loop and can repeat on a
page. `chat_transcript`, `receipt` and `render_case` may create widgets; every widget key they create
is prefixed with the `key` argument, and they must never run inside a polling loop.

Untrusted text (document text, vendor names, notes, model reasoning, tool results) only ever goes
through `fmt.md` (Markdown surfaces) or `fmt.esc` (HTML). `unsafe_allow_html` is never used.
"""

from decimal import Decimal
from typing import Any, Callable, Sequence

import pandas as pd
import streamlit as st

from models import (
    AgentTurn,
    Ceiling,
    Decision,
    Flag,
    FlagCode,
    HumanReview,
    InvoiceResult,
    Receipt,
    Severity,
    Status,
    Verdict,
)
from ui.kit import case, copy, fmt, theme
from ui.kit.case import CaseDetail, ChatTurn, FieldRow, SourceLine
from ui.kit.copy import t
from ui.kit.theme import CEILING, FILL, ON_FILL, SEVERITY, STATUS, TINT, VERDICT

QUEUE_PAGE = "views/2_Review_queue.py"
PROCESS_PAGE = "views/1_Process.py"
_QUEUE_ICON = ":material/pending_actions:"
_PROCESS_ICON = ":material/description:"


# ---------------------------------------------------------------- badges


def status_badge(status: Status) -> None:
    sem = STATUS[status]
    st.badge(sem.label, icon=sem.icon, color=sem.token)


def ceiling_badge(ceiling: Ceiling) -> None:
    sem = CEILING[ceiling]
    st.badge(t("label.ceiling_badge", label=sem.label), icon=sem.icon, color=sem.token, help=t("help.ceiling"))


# ------------------------------------------------------------------ flags


def _money_text(raw: Any, currency: str | None = None) -> str:
    try:
        return fmt.money(Decimal(str(raw)), currency)
    except Exception:
        return str(raw)


def _flag_words(flag: Flag) -> str:
    """Evidence in words where it helps (design 5.0 B); plain text, escape before showing."""
    ev, code = flag.evidence, flag.code
    parts: list[str] = []
    if code == FlagCode.UNKNOWN_ITEM and ev.get("did_you_mean"):
        parts.append(f"Did you mean {ev['did_you_mean']}?")
    if code == FlagCode.INSUFFICIENT_STOCK and ev.get("item") is not None:
        parts.append(f"{ev['item']}: needs {ev.get('requested')}, {ev.get('available')} in stock.")
    if code == FlagCode.ZERO_STOCK_ITEM and ev.get("item") is not None:
        parts.append(f"{ev['item']}: no stock.")
    if code in (FlagCode.PRICE_ABOVE_CATALOG, FlagCode.PRICE_BELOW_CATALOG):
        for line in ev.get("lines") or []:
            try:
                parts.append(
                    f"Line {int(line['index']) + 1}, {line['item']}: {_money_text(line['unit_price'])} "
                    f"against {_money_text(line['catalog'])} catalog."
                )
            except (KeyError, TypeError, ValueError):
                continue
    return " ".join(parts)


def flag_list(flags: Sequence[Flag]) -> None:
    """Flags grouped by severity, worst first, each group under a "{label} ({n})" badge (design 5.0 B)."""
    if not flags:
        st.markdown(fmt.md(t("case.no_flags")))
        return
    for severity in (Severity.ERROR, Severity.HUMAN, Severity.ESCALATION, Severity.INFO):
        group = [f for f in flags if f.severity == severity]
        if not group:
            continue
        sem = SEVERITY[severity]
        with st.container():
            st.badge(f"{sem.label} ({len(group)})", icon=sem.icon, color=sem.token, help=severity.value)
            for flag in group:
                cp = copy.FLAG_COPY[flag.code]
                st.markdown(f"**{fmt.md(cp.label)}.** {fmt.md(cp.sentence)}")
                detail = " ".join(x for x in (flag.message, _flag_words(flag)) if x)
                if detail:
                    st.caption(fmt.md(detail))


# ------------------------------------------------------------- html blocks


def kv_block(rows: Sequence[tuple[str, str]]) -> None:
    """A label/value grid (`<dl>`), escaped; stacks label over value on narrow screens."""
    items = "".join(f"<dt>{fmt.esc(k)}</dt><dd>{fmt.esc(v)}</dd>" for k, v in rows)
    st.html(f'<dl class="k-kv">{items}</dl>')


def meter_list(rows: Sequence[tuple[str, int, int]]) -> None:
    """(item, current, starting). An item that started at zero reads "none in stock" and has no bar."""
    items = []
    for item, current, starting in rows:
        name = f"<span>{fmt.esc(item)}</span>"
        if starting <= 0:
            items.append(f'<li>{name}<span class="k-none">none in stock</span></li>')
            continue
        width = max(0, min(100, current * 100 // starting))
        items.append(
            f'<li>{name}<span class="k-meter"><span style="width:{width}%"></span></span>'
            f"<span>{current:,} of {starting:,}</span></li>"
        )
    st.html(f'<ul class="k-meters">{"".join(items)}</ul>')


_BAR_STYLE = """<style>
.k-bar { display: flex; gap: 2px; height: 28px; margin: 0.5rem 0 0.25rem; }
.k-bar > div { min-width: 0; overflow: hidden; border-radius: 4px; padding-left: 0.5rem; line-height: 28px; font-weight: 600; white-space: nowrap; }
.k-legend.k-legend { list-style: none; display: flex; flex-wrap: wrap; gap: 0.25rem 1.25rem; margin: 0 0 0.5rem; padding: 0; }
.k-legend li { display: flex; align-items: center; gap: 0.4rem; }
.k-legend i { display: inline-block; width: 0.75rem; height: 0.75rem; border-radius: 2px; }
</style>"""


def segmented_bar(counts: Sequence[tuple[Status, int]]) -> None:
    """One whole split into parts (design 5.4): counts printed in the blocks, a legend, an aria-label."""
    by_status = {s: n for s, n in counts}
    shown = [(s, by_status[s]) for s in copy.OUTCOME_ORDER if by_status.get(s, 0) > 0]
    if not shown:
        return
    mode = theme.theme_type()
    blocks = "".join(
        f'<div style="flex:{n} 1 0;background:{FILL[mode][STATUS[s].token]};color:{ON_FILL[mode][STATUS[s].token]}">{n}</div>'
        for s, n in shown
    )
    legend = "".join(
        f'<li><i style="background:{FILL[mode][STATUS[s].token]}"></i>{fmt.esc(STATUS[s].label)}</li>' for s, _ in shown
    )
    label = fmt.esc(copy.counts_sentence(shown))
    st.html(f'{_BAR_STYLE}<div class="k-bar" role="img" aria-label="{label}">{blocks}</div><ul class="k-legend">{legend}</ul>')


def _source_style(mode: str) -> str:
    rules = "".join(
        f".k-src .k-sev-{sem.token} {{ border-left-color: {FILL[mode][sem.token]}; background: {TINT[mode][sem.token]}; }}\n"
        for sem in SEVERITY.values()
    )
    return f"<style>\n.k-src {{ --k-rule: {theme.RULE[mode]}; }}\n{rules}</style>"


def source_panel(lines: Sequence[SourceLine], *, caption: str | None = None) -> None:
    """The source text in a monospace panel with line numbers; flagged lines get a severity rule and tint.

    One `st.html` block. All text is escaped, and long lines scroll sideways so printed alignment is kept.
    """
    if caption:
        st.caption(fmt.md(caption))
    mode = theme.theme_type()
    items = []
    for line in lines:
        css = f' class="k-sev-{SEVERITY[line.severity].token}"' if line.severity else ""
        items.append(f'<li{css}><span class="k-n">{line.number}</span><span>{fmt.esc(line.text)}</span></li>')
    st.html(f'{_source_style(mode)}<ol class="k-src" aria-label="{fmt.esc(t("source.aria"))}">{"".join(items)}</ol>')


# --------------------------------------------------------------- the chat


def _lower(label: str) -> str:
    return label[:1].lower() + label[1:]


def _quote(text: str) -> str:
    return "> " + fmt.md(text).replace("\n", "\n> ")


def _tool_table(turn: ChatTurn) -> pd.DataFrame:
    return pd.DataFrame(
        {
            t("label.tool"): [copy.TOOL_NAMES.get(v.name, v.name) for v in turn.tools],
            t("label.asked_about"): [v.arguments for v in turn.tools],
            t("label.answer"): [v.answer for v in turn.tools],
        }
    )


def _render_agent_turn(turn: ChatTurn, source: AgentTurn, key: str) -> None:
    name = "VP" if turn.actor == "vp" else "Critic"
    with st.chat_message(name):
        with st.container(horizontal=True, vertical_alignment="center"):
            head, sep, tail = turn.title.partition(",")
            st.markdown(f"**{fmt.md(head)}**{fmt.md(sep + tail)}")
            if turn.actor == "vp" and turn.proposes is not None:
                sem = CEILING[turn.proposes]
                st.badge(t("label.proposes", ceiling=_lower(sem.label)), icon=sem.icon, color=sem.token)
            elif turn.actor == "critic" and turn.verdict in ("AGREE", "OBJECT"):
                sem = VERDICT[Verdict(turn.verdict)]
                st.badge(sem.label, icon=sem.icon, color=sem.token)
                if turn.proposes is not None:
                    st.markdown(fmt.md(t("label.critic_proposes", ceiling=_lower(copy.ceiling_label(turn.proposes)))))
        if turn.body:
            st.markdown(fmt.md(turn.body))
        if turn.actor == "vp" and turn.points:
            st.markdown(f"**{fmt.md(t('label.flags_answered'))}**")
            for label, response in turn.points:
                st.markdown(f"**{fmt.md(label)}:** {fmt.md(response)}")
        if turn.actor == "vp" and turn.answered:
            st.markdown(f"**{fmt.md(t('label.objections_answered'))}**")
            for objection, answer in turn.answered:
                st.markdown(_quote(objection))
                st.markdown(fmt.md(answer))
        if turn.actor == "critic":
            for about, text in turn.points:
                st.markdown(f"**{fmt.md(about)}.** {fmt.md(text)}")
        if turn.tools:
            with st.expander(copy.tools_label(source.tool_calls)):
                st.dataframe(_tool_table(turn), hide_index=True, width="stretch")
                if st.toggle(t("label.tools_raw"), key=key):
                    st.json([c.model_dump(mode="json") for c in source.tool_calls])


def _final_line(decision: Decision) -> None:
    sem = CEILING[decision.final]
    with st.container(horizontal=True, vertical_alignment="center"):
        st.markdown(f"**{fmt.md(t('label.final_decision'))}**")
        st.badge(sem.label, icon=sem.icon, color=sem.token)
        st.caption(fmt.md(case.final_caption(decision)))


def _gavel(text: str) -> None:
    st.markdown(f":material/gavel: {fmt.md(text)}")


def chat_transcript(
    transcript: Sequence[AgentTurn],
    decision: Decision | None = None,
    human: HumanReview | None = None,
    *,
    key: str,
    notices: Sequence[str] = (),
) -> None:
    """The VP and Critic exchange as chat messages (monogram avatars, no emoji), then policy notices and
    the final decision line when `decision` is given. Widget keys are prefixed with `key`."""
    turns = case.chat_turns(transcript, decision, human)
    with st.container(width=760):
        for i, turn in enumerate(turns):
            if turn.actor in ("vp", "critic"):
                _render_agent_turn(turn, transcript[i], f"{key}.raw.{i}")
            elif turn.actor == "person":
                label = t("label.person_approved" if turn.verdict == "approve" else "label.person_rejected")
                st.markdown(f"**{fmt.md(label)}.** {fmt.md(turn.body)}")
        for notice in notices:
            _gavel(notice)
        if decision is not None:
            _final_line(decision)


# ---------------------------------------------------------------- receipt


def _attempts_text(n: int) -> str:
    return t("pay.attempts_one") if n <= 1 else t("pay.attempts_retried", n=n, failed=n - 1)


def _receipt_rows(payment: Receipt) -> list[tuple[str, str]]:
    rows = [
        (t("pay.paid_to"), payment.vendor),
        (t("pay.amount"), fmt.money(payment.amount, payment.currency)),
    ]
    if payment.fx_rate is not None and payment.currency != "USD":
        rows.append((t("pay.rate"), t("pay.rate_value", rate=copy.rate_text(payment.fx_rate), currency=payment.currency)))
        rows.append((t("pay.in_dollars"), fmt.money(payment.amount_usd)))
    rows += [
        (t("pay.attempts"), _attempts_text(payment.attempts)),
        (t("pay.paid_at"), fmt.when(payment.paid_at)),
        (t("pay.receipt"), payment.idempotency_key),
    ]
    return rows


def receipt(payment: Receipt, *, key: str, extra_rows: Sequence[tuple[str, str]] = ()) -> None:
    """Who was paid, how much, in dollars, attempts and the receipt key; the bank's reply in an expander."""
    kv_block([*_receipt_rows(payment), *extra_rows])
    with st.expander(t("label.bank_response")):
        st.json(payment.bank_response)


# --------------------------------------------------------------- the case


def _ref(result: InvoiceResult) -> str:
    if result.invoice_number:
        return result.invoice_number + (f" {result.revision}" if result.revision else "")
    return result.file


def _facts_html(result: InvoiceResult) -> str:
    inv = result.invoice
    vendor = result.vendor or t("case.no_vendor")
    if inv is not None and inv.vendor_former_name and result.vendor:
        vendor += f" (formerly {inv.vendor_former_name})"
    facts = [fmt.esc(_ref(result)), fmt.esc(vendor)]
    if result.amount is not None:
        amount = fmt.money(result.amount, result.currency)
        if result.currency not in (None, "USD") and result.amount_usd is not None:
            amount += f" = {fmt.money(result.amount_usd)}"
            if result.payment is not None and result.payment.fx_rate is not None:
                amount += f" at {copy.rate_text(result.payment.fx_rate)}"
        facts.append(f'<span style="white-space:nowrap">{fmt.esc(amount)}</span>')
    if inv is not None:
        due = fmt.day(inv.due_date) if inv.due_date else inv.due_date_raw
        if due:
            facts.append(f"due {fmt.esc(due)}")
    return '<p style="margin:0">' + ", ".join(facts) + "</p>"


def _page_link(page: str, label: str, icon: str, params: dict[str, str] | None) -> None:
    if params:
        st.page_link(page, label=label, icon=icon, query_params=params)
    else:
        st.page_link(page, label=label, icon=icon)


def _outcome(result: InvoiceResult, state_invoice_id: int | None, detail: CaseDetail | None, links: bool) -> None:
    st.header(fmt.md(copy.headline(result)), anchor=False)
    with st.container(horizontal=True, vertical_alignment="center"):
        status_badge(result.status)
        if result.ceiling is not None and result.status not in (Status.DUPLICATE, Status.ERROR):
            ceiling_badge(result.ceiling)
    st.html(_facts_html(result))
    if links and result.status == Status.PENDING_HUMAN:
        _page_link(QUEUE_PAGE, t("link.decide"), _QUEUE_ICON, {"item": str(state_invoice_id)} if state_invoice_id else None)
    if result.short_circuited:
        processed = detail.processed_at if detail else None
        st.info(t("case.stored", date=fmt.day(processed.date())) if processed else t("case.stored_undated"))


def _check_column(rows: Sequence[FieldRow], mode: str, width: Any = "medium") -> tuple[list[list[str]], dict[str, Any]]:
    cells = [[copy.flag_label(f) for f in r.flags] for r in rows]
    colours = {copy.flag_label(f): TINT[mode][SEVERITY[f.severity].token] for r in rows for f in r.flags}
    config: dict[str, Any] = {}
    if colours:
        options = sorted(colours)
        config["Check"] = st.column_config.MultiselectColumn(
            "Check", options=options, color=[colours[o] for o in options], width=width
        )
    return cells, config


def _table(columns: dict[str, list[Any]], rows: Sequence[FieldRow], key: str, alt: str, *, right: Sequence[str] = (), narrow: dict[str, int] | None = None) -> None:
    mode = theme.theme_type()
    cells, config = _check_column(rows, mode, (narrow or {}).get("Check", "medium"))
    frame = pd.DataFrame(columns)
    if config:
        frame["Check"] = pd.Series(cells, index=frame.index, dtype=object)
    for name in right:
        config[name] = st.column_config.TextColumn(name, alignment="right")
    for name, px in (narrow or {}).items():
        if name == "Check":
            continue
        config[name] = st.column_config.TextColumn(name, width=px, alignment="right" if name in right else "left")
    st.dataframe(frame, hide_index=True, column_config=config, key=key, alt=alt, width="stretch")


def _display(value: str | None) -> str:
    return value if value else "-"


def _lines_table(result: InvoiceResult, detail: CaseDetail | None, rows: Sequence[FieldRow], key: str) -> None:
    state = detail.state if detail else None
    invoice = (state.invoice if state else None) or result.invoice
    if invoice is None or not invoice.line_items:
        st.markdown(fmt.md(t("case.no_lines")))
        return
    checks = {c.index: c for c in (state.totals.lines if state and state.totals else [])}
    cur = invoice.currency
    line_rows = [[r for r in rows if r.group == f"line {i + 1}"] for i in range(len(invoice.line_items))]
    # one display row per line: the Check cell is the union of its field flags
    merged = [
        FieldRow(f"line_items[{i}]", f"line {i + 1}", "", None, None, False, (), tuple({id(f): f for r in g for f in r.flags}.values()), None)
        for i, g in enumerate(line_rows)
    ]
    columns = _line_columns(invoice, checks)
    right = tuple(c for c in ("Qty", "Unit price", "Amount", "Recomputed") if c in columns)
    _table(columns, merged, key, "Line items", right=right, narrow={"#": 28, "Item": 112, "Qty": 36, "Unit price": 72, "Amount": 72, "Check": 150})


def _line_columns(invoice: Any, checks: dict[int, Any]) -> dict[str, list[Any]]:
    """Columns of the line-items table. The table sits in a half-width column, so Recomputed is
    shown only where it differs from the stated amount, and a line note is folded into the Item cell."""
    items = invoice.line_items
    cur = invoice.currency
    columns: dict[str, list[Any]] = {
        "#": [str(i + 1) for i in range(len(items))],
        "Item": [f"{ln.item} ({ln.note})" if ln.note else ln.item for ln in items],
        "Qty": [str(ln.quantity) for ln in items],
        "Unit price": [fmt.money(ln.unit_price, cur) for ln in items],
        "Amount": [_display(fmt.money(ln.amount, cur) if ln.amount is not None else None) for ln in items],
    }
    if any(i in checks and checks[i].recomputed_amount != ln.amount for i, ln in enumerate(items)):
        columns["Recomputed"] = [
            fmt.money(checks[i].recomputed_amount, cur) if i in checks else "-" for i in range(len(items))
        ]
    return columns


def _totals_table(result: InvoiceResult, detail: CaseDetail | None, rows: Sequence[FieldRow], key: str) -> None:
    state = detail.state if detail else None
    cur = result.currency
    by_path = {r.path: r for r in rows}
    entries: list[tuple[str, str, FieldRow | None]] = []

    def stated(path: str, label: str) -> None:
        row = by_path.get(path)
        if row is not None:
            entries.append((label, _display(row.value), row))

    stated("subtotal", "Subtotal")
    totals = state.totals if state else None
    if totals is not None:
        entries.append(("Lines sum", fmt.money(totals.lines_sum, cur), None))
    stated("tax_amount", "Tax")
    stated("shipping", "Shipping")
    stated("total", "Total stated")
    if totals is not None:
        entries.append(("Total recomputed", fmt.money(totals.total_recomputed, cur), None))
        entries.append(("Difference", fmt.money(totals.difference, cur), by_path.get("total") if not totals.total_ok else None))
    if not entries:
        return
    shim = [
        FieldRow("", "totals", label, None, value, False, (), row.flags if row else (), None) for label, value, row in entries
    ]
    _table(
        {"Item": [e[0] for e in entries], "Amount": [e[1] for e in entries]}, shim, key, "Invoice totals", right=("Amount",)
    )


def _read_as(state: Any) -> str:
    if state is None:
        return ""
    pairs = {c.field: f"{c.raw} → {c.normalized}" for c in state.normalized_changes}
    for c in state.corrections:
        pairs.setdefault(c.field, f"{c.raw} → {c.interpreted}")
    return "; ".join(pairs.values())


def _source_caption(detail: CaseDetail | None) -> str:
    kind = detail.source_kind if detail else None
    if kind is None and detail and detail.state and detail.state.format:
        kind = {"txt": "text", "pdf": "pdf"}.get(detail.state.format, "file")
    return t({"text": "source.text", "pdf": "source.pdf", "file": "source.file"}.get(kind or "text", "source.text"))


def _document(result: InvoiceResult, detail: CaseDetail | None, key: str) -> None:
    st.subheader(t("section.document"), anchor=False)
    state = detail.state if detail else None
    rows = case.field_rows(result, state)
    if any(f.code == FlagCode.UNREADABLE_DOCUMENT for f in result.flags):
        error = (state.parse_error if state else None) or "; ".join(state.grounding_errors if state else []) or next(
            f.message for f in result.flags if f.code == FlagCode.UNREADABLE_DOCUMENT
        )
        st.markdown(fmt.md(t("case.unreadable_panel", error=error)))
    left, right = st.columns(2, gap="large")
    with left:
        if not any(f.code == FlagCode.UNREADABLE_DOCUMENT for f in result.flags):
            st.markdown(f"**{fmt.md(t('section.extracted'))}**")
            head = [r for r in rows if r.group == "header"]
            _table(
                {"Field": [r.label for r in head], "Value": [_display(r.value) for r in head]},
                head, f"{key}.fields", "Extracted invoice fields",
            )
            _lines_table(result, detail, rows, f"{key}.lines")
            _totals_table(result, detail, rows, f"{key}.totals")
            read_as = _read_as(state)
            if read_as:
                st.caption(f"{fmt.md(t('section.read_as'))}: {fmt.md(read_as)}")
    with right:
        st.markdown(f"**{fmt.md(t('section.source'))}**")
        text = (detail.source if detail else None) or (state.raw_text if state else None)
        if text:
            source_panel(case.source_lines(text, rows), caption=_source_caption(detail))
        else:
            st.caption(fmt.md(t("case.no_source")))
        path = detail.original_path if detail else None
        if path is not None:
            try:
                data = path.read_bytes()
            except OSError:
                data = None
            if data is not None:
                st.download_button(
                    t("button.download_original"), data, file_name=path.name, key=f"{key}.download", on_click="ignore"
                )


def _approval(result: InvoiceResult, key: str) -> None:
    st.subheader(t("section.approval"), anchor=False)
    if not result.transcript and result.decision is None:
        st.caption(fmt.md(t("approval.none_duplicate" if result.status == Status.DUPLICATE else "approval.none_unreadable")))
        return
    for sentence in (case.ceiling_sentence(result), case.path_sentence(result)):
        if sentence:
            st.markdown(fmt.md(sentence))
    chat_transcript(
        result.transcript, result.decision, result.human, key=f"{key}.chat", notices=case.policy_notices(result)
    )


def _stock_used(result: InvoiceResult, state: Any) -> str:
    inv = result.invoice
    if inv is None:
        return ""
    names: dict[str, str] = {}
    qty: dict[str, int] = {}
    for line in inv.line_items:
        names.setdefault(line.item_key, line.item)
        qty[line.item_key] = qty.get(line.item_key, 0) + line.quantity
    if state is not None and state.dedupe is not None and state.dedupe.added_quantities:
        qty = dict(state.dedupe.added_quantities)
    return ", ".join(f"{names.get(k, k)} −{q}" for k, q in qty.items() if q > 0)


def _first_sentence(text: str) -> str:
    text = " ".join(text.split())
    head, sep, _ = text.partition(". ")
    return head + sep.strip() if sep else text


def _payment_section(
    result: InvoiceResult, detail: CaseDetail | None, key: str, links: bool,
    payment_rows: Sequence[tuple[str, str]] | None,
) -> None:
    state = detail.state if detail else None
    status = result.status
    st.subheader(t("section.payment" if status == Status.PAID else "section.next_step"), anchor=False)
    if status == Status.PAID and result.payment is not None:
        if payment_rows is None:
            stock = _stock_used(result, state)
            payment_rows = [(t("pay.stock_used"), stock)] if stock else []
        receipt(result.payment, key=f"{key}.receipt", extra_rows=payment_rows)
        return
    cur = result.currency
    if status == Status.PENDING_HUMAN:
        reason = case.reason_text(result, state)
        st.markdown(fmt.md(t("next.pending", reason=reason)))
    elif status == Status.REJECTED:
        d, h = result.decision, result.human
        memo = (h.resolution.note if h and h.resolution and h.outcome != "rejected_stock" else None) or (
            h.outcome_note if h and h.outcome == "rejected_stock" else None
        ) or (d.reasoning if d else "")
        st.markdown(fmt.md(t("next.rejected", why=_first_sentence(memo))))
    elif status == Status.DUPLICATE:
        original = result.duplicate_of or ""
        dedupe = state.dedupe if state else None
        if dedupe is not None and dedupe.original_status is not None:
            if dedupe.original_status == Status.PAID:
                phrase = f"paid {fmt.money(dedupe.paid_to_date, cur)}"
            else:
                phrase = STATUS[dedupe.original_status].label.lower()
            st.markdown(fmt.md(t("next.duplicate", file=original, original=phrase)))
        else:
            st.markdown(fmt.md(t("next.duplicate_plain", file=original)))
        if links and dedupe is not None and dedupe.original_trace_id:
            _page_link(PROCESS_PAGE, t("link.open_file", file=original), _PROCESS_ICON, {"trace": dedupe.original_trace_id})
    elif status == Status.CREDIT_DUE:
        credit = copy.credit_amount(result)
        dedupe = state.dedupe if state else None
        if dedupe is not None and dedupe.paid_to_date and credit is not None:
            st.markdown(
                fmt.md(
                    t("next.credit", less=fmt.money(credit, cur), paid=fmt.money(dedupe.paid_to_date, cur),
                      credit=fmt.money(credit, cur))
                )
            )
        else:
            st.markdown(fmt.md(t("next.credit_plain", credit=fmt.money(credit, cur))))
    elif status == Status.SUPERSEDED:
        st.markdown(fmt.md(t("next.superseded")))


def _run_details(result: InvoiceResult, detail: CaseDetail | None, key: str) -> None:
    state = detail.state if detail else None
    u = result.usage
    replay = result.provider == "replay"
    rows = [
        (t("run.trace"), result.trace_id),
        (t("run.file"), result.file),
    ]
    if state is not None and state.format:
        rows.append((t("run.format"), state.format.upper()))
    rows += [
        (t("run.provider"), result.provider),
        (t("run.calls"), str(u.llm_calls)),
        (t("run.tokens_in"), fmt.tokens(u.tokens_in)),
        (t("run.tokens_out"), fmt.tokens(u.tokens_out)),
        (t("run.model_time_replay" if replay else "run.model_time"), fmt.seconds(u.llm_latency_ms)),
        (t("run.log_label"), t("run.log", trace_id=result.trace_id)),
    ]
    with st.expander(t("label.run_details")):
        kv_block(rows)
        if u.node_ms:
            st.dataframe(
                pd.DataFrame(
                    {
                        t("run.steps"): [copy.NODE_LABEL.get(n, n) for n in u.node_ms],
                        "ms": [str(ms) for ms in u.node_ms.values()],
                    }
                ),
                hide_index=True, key=f"{key}.steps", width="stretch",
                column_config={"ms": st.column_config.TextColumn("ms", alignment="right")},
            )
        path = state.file_path if state is not None else f"data/invoices/{result.file}"
        st.markdown(f"**{fmt.md(t('run.cli'))}**")
        st.code(f"uv run python main.py --invoice_path={path}", language="bash")


def _system_error(result: InvoiceResult) -> None:
    with st.container(border=True):
        sem = STATUS[Status.ERROR]
        st.badge(t("error.system_title"), icon=sem.icon, color=sem.token)
        st.markdown(fmt.md(t("error.system_body")))
        if result.error:
            st.code(result.error, language=None)
        st.caption(fmt.md(t("error.system_trace", trace_id=result.trace_id)))


def render_case(
    result: InvoiceResult, detail: CaseDetail | None, *, key: str, links: bool = True,
    payment_rows: Sequence[tuple[str, str]] | None = None, after_error: Callable[[], None] | None = None,
) -> None:
    """One invoice as a case file (design 5.0): outcome, checks, document, approval, payment, run details.

    An ERROR result swaps checks through payment for the system-error container; `after_error`, when
    given, runs directly under that container (the Process page puts "Process again" there). `links=False` omits the page links (for tests without navigation).
    `payment_rows`, when given, replaces the default extra rows under a PAID receipt; pages pass ledger
    facts the result doesn't carry ("Stock used" by inventory name, "January spending is now ...").
    May contain widgets keyed under `key`; never call it inside a polling loop.
    """
    theme.style()
    state = detail.state if detail else None
    _outcome(result, state.invoice_id if state else None, detail, links)
    if result.status == Status.ERROR:
        _system_error(result)
        if after_error is not None:
            after_error()
        _run_details(result, detail, key)
        return
    st.subheader(t("section.checks"), anchor=False)
    flag_list(case.sorted_flags(result.flags))
    if result.address_check:
        st.caption(fmt.md(t(f"address.{result.address_check}")))
    _document(result, detail, key)
    _approval(result, key)
    _payment_section(result, detail, key, links, payment_rows)
    _run_details(result, detail, key)
