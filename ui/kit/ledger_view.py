"""Ledger page logic: the ordered payment events, chart frames and specs, the payments table and CSV.

Pure functions over repository records, so they can be tested by their data. The x axis of both
charts is the ordered payment events ("Start", each payment by invoice reference, "Now"), not
wall-clock time (design spec section 5.3).
"""

import calendar
import csv
import datetime as dt
import io
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import altair as alt
import pandas as pd

from models import BudgetLedgerEntry, BudgetStatus, InvoiceRecord, PaymentRecord, StockLedgerEntry
from ui.kit import fmt, theme

START_LABEL = "Start"
NOW_LABEL = "Now"

SPENDING_HEIGHT = 220
STOCK_HEIGHT = 110

COPY = {
    "spending": "Spending against the limit",
    "stock": "Stock after each payment",
    "payments": "Payments",
    "movements": "Stock movements ({n})",
    "no_stock": "{item} has had no stock since the start, so any invoice for it is rejected.",
    "other_months": "Spending recorded in other months, not charted here",
    "csv": "Download CSV",
    "limit_label": "Monthly limit {limit}",
}


# ------------------------------------------------------------------ steps


@dataclass(frozen=True)
class LedgerStep:
    seq: int  # 0 for "start", then 1.. in payment order
    label: str  # "Start" or the invoice reference
    invoice_id: int | None
    ref: str | None
    ts: dt.datetime | None
    stock: dict[str, int]  # level of every item after this step
    spent: Decimal  # cumulative USD for the month after this step
    amount_usd: Decimal | None  # this payment's USD (None for the start)
    vendor: str | None = field(default=None, compare=False)


def invoice_ref(rec: InvoiceRecord | None, invoice_id: int | None = None) -> str:
    """`INV-1004 R1`, `INV-1012`, or the file name when there is no number."""
    if rec is None:
        return f"Invoice {invoice_id}" if invoice_id is not None else "-"
    if rec.invoice_number:
        return rec.invoice_number + (f" {rec.revision}" if rec.revision else "")
    return rec.file_name


def ledger_steps(
    stock: Sequence[StockLedgerEntry], budget: Sequence[BudgetLedgerEntry],
    invoices: Sequence[InvoiceRecord], items: Sequence[str],
) -> list[LedgerStep]:
    """"start" (the seed levels) plus one step per paid invoice, in ledger order.

    `budget` holds the month being shown. A payment that only has stock movements (its
    budget entry belongs to another month) still gets a step, with `amount_usd` None.
    """
    by_id = {r.id: r for r in invoices}
    names = list(items)
    for e in stock:
        if e.item not in names:
            names.append(e.item)
    level = {n: 0 for n in names}
    first_ts: dt.datetime | None = None
    for e in sorted(stock, key=lambda e: e.id):
        if e.invoice_id is None:
            level[e.item] += e.delta
            first_ts = first_ts or e.ts
    steps = [LedgerStep(0, START_LABEL, None, None, first_ts, dict(level), Decimal("0"), None)]

    # Payment order is ledger-id order (ids only grow; timestamps can tie or be skewed). A payment writes its
    # stock and budget rows in one transaction, so the first stock row's id places it; budget id is the fallback.
    order: dict[int, tuple[int, dt.datetime]] = {}
    amounts: dict[int, Decimal] = {}
    for e in stock:
        if e.invoice_id is not None:
            key = (e.id, e.ts)
            order[e.invoice_id] = min(order.get(e.invoice_id, key), key)
    for b in budget:
        if b.invoice_id is None:
            continue
        amounts[b.invoice_id] = amounts.get(b.invoice_id, Decimal("0")) + b.amount_usd
        if b.invoice_id not in order:
            order[b.invoice_id] = (b.id, b.ts)
        elif order[b.invoice_id][1] > b.ts:
            order[b.invoice_id] = (order[b.invoice_id][0], b.ts)
    deltas: dict[int, dict[str, int]] = {}
    for e in stock:
        if e.invoice_id is not None:
            d = deltas.setdefault(e.invoice_id, {})
            d[e.item] = d.get(e.item, 0) + e.delta

    spent = Decimal("0")
    for seq, (inv_id, (_, ts)) in enumerate(sorted(order.items(), key=lambda kv: kv[1]), start=1):
        for item, delta in deltas.get(inv_id, {}).items():
            level[item] += delta
        amount = amounts.get(inv_id)
        spent += amount or Decimal("0")
        rec = by_id.get(inv_id)
        ref = invoice_ref(rec, inv_id)
        steps.append(LedgerStep(seq, ref, inv_id, ref, ts, dict(level), spent, amount, rec.vendor_name if rec else None))
    return steps


# ------------------------------------------------------------------ frames


def stock_frame(steps: Sequence[LedgerStep]) -> pd.DataFrame:
    """Long format: one row per step and item. `pos` is the x position; a final `Now` row repeats the last level."""
    rows: list[dict[str, Any]] = []
    shown = [*steps, _now(steps)] if steps else []
    for pos, s in enumerate(shown):
        for item, level in s.stock.items():
            rows.append({"pos": pos, "label": s.label, "item": item, "stock": level})
    return pd.DataFrame(rows, columns=["pos", "label", "item", "stock"])


def budget_frame(steps: Sequence[LedgerStep], limit: Decimal) -> pd.DataFrame:
    """One row per step plus a closing `Now` row. Floats here are Altair axis positions only."""
    rows: list[dict[str, Any]] = []
    shown = [*steps, _now(steps)] if steps else []
    for pos, s in enumerate(shown):
        paid = s.seq > 0 and s.label != NOW_LABEL
        rows.append({
            "pos": pos, "label": s.label, "spent": float(s.spent), "limit": float(limit),
            "payment": paid,
            "invoice": s.ref if paid else "",
            "vendor": (s.vendor or "") if paid else "",
            "paid_amount": fmt.money(s.amount_usd) if paid and s.amount_usd is not None else "",
            "spent_text": fmt.money(s.spent),
            "paid_at": fmt.when(s.ts) if paid else "",
        })
    return pd.DataFrame(rows)


def _now(steps: Sequence[LedgerStep]) -> LedgerStep:
    last = steps[-1]
    return LedgerStep(last.seq + 1, NOW_LABEL, None, None, None, dict(last.stock), last.spent, None)


# ------------------------------------------------------------------ payments table


@dataclass(frozen=True)
class PaymentRow:
    ref: str
    vendor: str
    file: str
    amount: str
    currency: str
    fx_rate: str | None
    amount_usd: str
    status: str
    attempts: int
    when: str
    error: str | None
    receipt: str = ""
    amount_value: Decimal | None = field(default=None, compare=False)
    usd_value: Decimal | None = field(default=None, compare=False)


def _attempts_word(n: int) -> str:
    return f"{n} attempt" if n == 1 else f"{n} attempts"


def payment_rows(
    payments: Sequence[PaymentRecord], invoices: Sequence[InvoiceRecord], order: Sequence[int] = (),
) -> list[PaymentRow]:
    """Oldest first, so the table reads like a ledger.

    `order` lists invoice ids in ledger order (the chart steps); timestamps can tie or be skewed when
    invoices run side by side, so the ledger order wins and the timestamp breaks the remaining ties.
    """
    rank = {inv_id: n for n, inv_id in enumerate(order)}
    by_id = {r.id: r for r in invoices}
    ordered = sorted(payments, key=lambda p: (rank.get(p.invoice_id, len(rank)), p.created_at or dt.datetime.min.replace(tzinfo=dt.timezone.utc), p.idempotency_key))
    rows = []
    for p in ordered:
        rec = by_id.get(p.invoice_id)
        failed = p.status == "FAILED"
        usd = p.amount_usd if p.amount_usd is not None else (p.amount if p.currency == "USD" else None)
        rows.append(PaymentRow(
            ref=invoice_ref(rec, p.invoice_id), vendor=(rec.vendor_name if rec and rec.vendor_name else "-"),
            file=rec.file_name if rec else "-", amount=fmt.money(p.amount, p.currency).replace("$", "").strip()
            if p.currency == "USD" else f"{p.amount:,.2f}",
            currency=p.currency, fx_rate=(f"{p.fx_rate.normalize():f}" if p.fx_rate is not None else None)
            if p.currency != "USD" else None,
            amount_usd=fmt.money(usd) if usd is not None and not failed else "-",
            status=f"Failed after {p.attempts} attempts" if failed else "Paid",
            attempts=p.attempts, when=fmt.when(p.updated_at or p.created_at), error=p.error if failed else None,
            receipt=p.idempotency_key, amount_value=p.amount, usd_value=None if failed else usd,
        ))
    return rows


def payments_table(rows: Sequence[PaymentRow]) -> pd.DataFrame:
    """The frame behind the `st.dataframe`, numeric where the column format needs a number."""
    return pd.DataFrame({
        "Paid": [r.when for r in rows],
        "Invoice": [r.ref for r in rows],
        "Vendor": [r.vendor for r in rows],
        "Amount": [float(r.amount_value) if r.amount_value is not None else None for r in rows],
        "Currency": [r.currency for r in rows],
        "FX rate": [r.fx_rate or "" for r in rows],
        "USD": [float(r.usd_value) if r.usd_value is not None else None for r in rows],
        "Attempts": [r.attempts for r in rows],
        "Status": [r.status for r in rows],
        "Error": [r.error or "" for r in rows],
        "Receipt": [r.receipt for r in rows],
    })


_CSV_COLUMNS = ("Paid", "Invoice", "Vendor", "File", "Amount", "Currency", "FX rate", "USD", "Attempts", "Status", "Error", "Receipt")


def _plain(value: str) -> str:
    """Spreadsheets run text that starts with = + - @ as a formula; the CSV is for people who open it there."""
    return "'" + value if value[:1] in ("=", "+", "-", "@") else value


def payments_csv(rows: Sequence[PaymentRow]) -> str:
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(_CSV_COLUMNS)
    for r in rows:
        w.writerow([
            r.when, _plain(r.ref), _plain(r.vendor), _plain(r.file),
            f"{r.amount_value:.2f}" if r.amount_value is not None else "", r.currency, r.fx_rate or "",
            f"{r.usd_value:.2f}" if r.usd_value is not None else "", r.attempts, r.status, _plain(r.error or ""), r.receipt,
        ])
    return out.getvalue()


def movements_table(stock: Sequence[StockLedgerEntry], invoices: Sequence[InvoiceRecord]) -> pd.DataFrame:
    by_id = {r.id: r for r in invoices}
    ordered = sorted(stock, key=lambda e: e.id)
    return pd.DataFrame({
        "Time": [fmt.when(e.ts) for e in ordered],
        "Item": [e.item for e in ordered],
        "Change": [e.delta for e in ordered],
        "Invoice": [invoice_ref(by_id.get(e.invoice_id), e.invoice_id) if e.invoice_id is not None else "" for e in ordered],
        "Reason": [e.reason for e in ordered],
    })


# ------------------------------------------------------------------ copy


def month_name(month: str) -> str:
    """`2026-01` -> `January 2026`."""
    year, mon = month.split("-")
    return f"{calendar.month_name[int(mon)]} {year}"


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def summary_sentence(budget: BudgetStatus, payments: Sequence[PaymentRecord], *, approved_by_person: bool = False) -> str:
    """The sentence under the title. Plain text; the caller escapes it for Markdown."""
    month = month_name(budget.month)
    paid = [p for p in payments if p.status == "SUCCESS"]
    failed = [p for p in payments if p.status == "FAILED"]
    if not paid and not failed and budget.spent <= 0:
        return f"{month}: nothing paid yet. {fmt.money(budget.limit)} of the monthly limit is available."
    over = budget.spent - budget.limit
    if over > 0:
        head = (f"{month}: {fmt.money(budget.spent)} paid against the {fmt.money(budget.limit)} monthly limit, "
                f"over the limit by {fmt.money(over)}" + (", approved by a person." if approved_by_person else "."))
    else:
        head = (f"{month}: {fmt.money(budget.spent)} paid of the {fmt.money(budget.limit)} monthly limit, "
                f"{fmt.money(budget.remaining)} left.")
    tail = f"{_count(len(paid), 'payment')}, " + ("none failed." if not failed else f"{len(failed)} failed.")
    return f"{head} {tail}"


def spending_alt(budget: BudgetStatus, n_payments: int) -> str:
    return (f"Spending rose from $0 to {_chart_money(budget.spent)} over {_count(n_payments, 'payment')}; "
            f"the monthly limit is {_chart_money(budget.limit)}.")


def _chart_money(value: Decimal) -> str:
    """`$19,140` when whole, `$19,140.50` otherwise."""
    return f"${value:,.0f}" if value == value.to_integral_value() else f"${value:,.2f}"


# ------------------------------------------------------------------ charts


def _base_config(chart: alt.Chart | alt.LayerChart, mode: str) -> alt.Chart | alt.LayerChart:
    ink, rule = theme.INK[mode], theme.RULE[mode]  # type: ignore[index]
    return (
        chart.configure(background="transparent")
        .configure_view(stroke=None)
        .configure_axis(
            domain=False, ticks=False, gridColor=rule, gridWidth=1, labelColor=ink, titleColor=ink,
            labelFont="IBM Plex Sans, sans-serif", labelFontSize=12, labelPadding=6,
        )
        .configure_title(color=ink, font="IBM Plex Sans, sans-serif", anchor="start")
    )


def _label_expr(frame: pd.DataFrame) -> str:
    mapping = {str(int(p)): label for p, label in zip(frame["pos"], frame["label"])}
    return f"{json.dumps(mapping)}[datum.label]"


def spending_chart(frame: pd.DataFrame, limit: Decimal, spent: Decimal, *, mode: str | None = None) -> alt.LayerChart:
    """Step line with a 10% wash, an ink rule for the limit, points and direct labels."""
    mode = mode or theme.theme_type()
    ink, paper = theme.INK[mode], theme.PAPER[mode]  # type: ignore[index]
    top = float(max(limit * Decimal("1.1"), spent * Decimal("1.05")))
    n = len(frame)
    x = alt.X(
        "pos:O", title=None, sort=None,
        axis=alt.Axis(labelExpr=_label_expr(frame), labelOverlap="greedy", labelAngle=0 if n <= 10 else -40, grid=False),
    )
    y = alt.Y(
        "spent:Q", title=None, scale=alt.Scale(domain=[0, top], nice=False),
        axis=alt.Axis(format="$,.0f", tickCount=5),
    )
    base = alt.Chart(frame)
    area = base.mark_area(interpolate="step-after", color=ink, opacity=0.1).encode(x=x, y=y)
    line = base.mark_line(interpolate="step-after", color=ink, strokeWidth=2).encode(x=x, y=y)
    tip = [
        alt.Tooltip("invoice:N", title="Invoice"), alt.Tooltip("vendor:N", title="Vendor"),
        alt.Tooltip("paid_amount:N", title="Amount paid"), alt.Tooltip("spent_text:N", title="Spent so far"),
        alt.Tooltip("paid_at:N", title="Paid at"),
    ]
    points = (
        base.transform_filter(alt.datum.payment | (alt.datum.pos == 0))
        .mark_point(filled=True, color=ink, size=60, stroke=paper, strokeWidth=2, opacity=1)
        .encode(x=x, y=y, tooltip=tip)
    )
    rule_data = pd.DataFrame({"limit": [float(limit)]})
    rule = alt.Chart(rule_data).mark_rule(color=ink, strokeWidth=1).encode(y="limit:Q")
    limit_text = COPY["limit_label"].format(limit=_chart_money(limit))
    first = frame.iloc[[0]].assign(limit=float(limit), text=limit_text)
    limit_label = (
        alt.Chart(first).mark_text(align="left", baseline="bottom", dy=-5, dx=-4, color=ink, fontSize=12)
        .encode(x=x, y="limit:Q", text="text:N")
    )
    over = spent - limit
    end_text = _chart_money(spent) + (f", over by {_chart_money(over)}" if over > 0 else "")
    last = frame.iloc[[n - 1]].assign(text=end_text)
    end_label = (
        alt.Chart(last).mark_text(align="right", baseline="bottom", dy=-10, color=ink, fontSize=13, fontWeight="bold")
        .encode(x=x, y=y, text="text:N")
    )
    chart = alt.layer(area, line, rule, points, limit_label, end_label).properties(height=SPENDING_HEIGHT, width="container")
    return _base_config(chart, mode)  # type: ignore[return-value]


def stock_chart(item: str, frame: pd.DataFrame, start: int, *, mode: str | None = None) -> alt.LayerChart:
    """One small multiple: a step line from 0 to the starting stock, sharing the spending chart's event order."""
    mode = mode or theme.theme_type()
    ink, paper = theme.INK[mode], theme.PAPER[mode]  # type: ignore[index]
    data = frame[frame["item"] == item]
    current = int(data["stock"].iloc[-1]) if len(data) else start
    ticks = sorted({0, start // 2, start})
    x = alt.X("pos:O", title=None, sort=None, axis=None)
    y = alt.Y(
        "stock:Q", title=None, scale=alt.Scale(domain=[0, start], nice=False),
        axis=alt.Axis(values=ticks, format="d", grid=True),
    )
    base = alt.Chart(data)
    area = base.mark_area(interpolate="step-after", color=ink, opacity=0.1).encode(x=x, y=y)
    line = base.mark_line(interpolate="step-after", color=ink, strokeWidth=2).encode(x=x, y=y)
    points = (
        base.mark_point(filled=True, color=ink, size=40, stroke=paper, strokeWidth=2, opacity=1)
        .transform_filter(alt.datum.label != NOW_LABEL)
        .encode(x=x, y=y, tooltip=[alt.Tooltip("label:N", title="After"), alt.Tooltip("stock:Q", title=item)])
    )
    title = alt.TitleParams(item, subtitle=f"{current:,} left of {start:,}", subtitleColor=ink, fontSize=14, subtitleFontSize=12)
    chart = alt.layer(area, line, points).properties(height=STOCK_HEIGHT, width="container", title=title)
    return _base_config(chart, mode)  # type: ignore[return-value]


def stock_alt(item: str, start: int, current: int) -> str:
    return f"{item} stock fell from {start:,} to {current:,}."


def other_month_totals(entries: Sequence[BudgetLedgerEntry]) -> list[tuple[str, Decimal, int]]:
    """(month name, total USD, entries) for budget entries outside the month being shown."""
    totals: dict[str, tuple[Decimal, int]] = {}
    for e in entries:
        amount, n = totals.get(e.month, (Decimal("0"), 0))
        totals[e.month] = (amount + e.amount_usd, n + 1)
    return [(month_name(m), a, n) for m, (a, n) in sorted(totals.items())]
