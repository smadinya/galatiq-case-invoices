import streamlit as st

from ui.kit import components, copy, fmt, ledger_view as lv, theme
from ui.kit.runtime import get_runtime
from ui.kit.sidebar import session_config

theme.style()
st.title(copy.TEXT["title.ledger"])

rt = get_runtime()
cfg = session_config(rt)
mode = theme.theme_type()

with rt.reader(cfg) as reader:
    inventory = reader.inventory()
    start = reader.starting_stock()
    stock_entries = reader.stock_ledger()
    budget_entries = reader.budget_ledger()
    all_budget = reader._read(lambda r: r.budget_ledger(), [])  # other months; no public read for them yet
    budget = reader.budget_status()
    payments = reader.payments()
    invoices = reader.invoices()

items = [i.item for i in inventory]
paid = [p for p in payments if p.status == "SUCCESS"]
other_months = lv.other_month_totals([e for e in all_budget if e.month != budget.month])

st.markdown(fmt.md(lv.summary_sentence(budget, payments, approved_by_person=budget.spent > budget.limit)))

nothing_paid = not payments and budget.spent <= 0 and not any(e.invoice_id is not None for e in stock_entries)
if nothing_paid:
    # Nothing paid yet: the books at starting levels, then where to go next.
    if inventory:
        components.meter_list([(i.item, i.stock, start.get(i.item, i.stock)) for i in inventory])
    st.markdown(fmt.md(copy.TEXT["empty.ledger_next"]))
    st.page_link("views/4_Batch.py", label=copy.TEXT["title.batch"], icon=":material/stacks:")
else:
    steps = lv.ledger_steps(stock_entries, budget_entries, invoices, items)
    rows = lv.payment_rows(payments, invoices, [s.invoice_id for s in steps if s.invoice_id is not None])
    sframe = lv.stock_frame(steps)
    bframe = lv.budget_frame(steps, budget.limit)

    st.subheader(lv.COPY["spending"])
    st.altair_chart(
        lv.spending_chart(bframe, budget.limit, budget.spent, mode=mode), theme=None, width="stretch",
        alt=lv.spending_alt(budget, len(paid)),
    )

    st.subheader(lv.COPY["stock"])
    charted = [i for i in items if start.get(i, 0) > 0]
    cols = st.columns(3)
    for n, item in enumerate(charted):
        with cols[n % 3]:
            first = start[item]
            current = steps[-1].stock.get(item, first)
            st.altair_chart(
                lv.stock_chart(item, sframe, first, mode=mode), theme=None, width="stretch",
                alt=lv.stock_alt(item, first, current),
            )
    for item in items:
        if start.get(item, 0) <= 0:
            st.caption(fmt.md(lv.COPY["no_stock"].format(item=item)))

    if rows:
        head, action = st.columns([4, 1], vertical_alignment="bottom")
        head.subheader(lv.COPY["payments"])
        action.download_button(
            lv.COPY["csv"], lv.payments_csv(rows), file_name="payments.csv", mime="text/csv", key="ledger.csv",
            type="secondary",
        )
        table = lv.payments_table(rows)
        st.dataframe(
            table, hide_index=True, width="stretch", key="ledger.payments",
            alt="Payments, oldest first: when, invoice, vendor, amount, currency, FX rate, USD, attempts, status.",
            column_order=["Paid", "Invoice", "Vendor", "Amount", "Currency", "FX rate", "USD", "Attempts", "Status", "Receipt"],
            column_config={
                "Amount": st.column_config.NumberColumn("Amount", format="accounting"),
                "USD": st.column_config.NumberColumn("USD", format="dollar"),
                "Attempts": st.column_config.NumberColumn("Attempts", format="%d"),
                "FX rate": st.column_config.TextColumn("FX rate", alignment="right"),
                "Status": st.column_config.TextColumn("Status"),
            },
        )
        failed = [r for r in rows if r.error]
        for r in failed:
            st.caption(fmt.md(f"{r.ref}: {r.error}"))

    with st.expander(lv.COPY["movements"].format(n=len(stock_entries))):
        st.dataframe(
            lv.movements_table(stock_entries, invoices), hide_index=True, width="stretch", key="ledger.movements",
            alt="Every stock movement, oldest first, including the starting levels.",
        )

if other_months:
    st.subheader(lv.COPY["other_months"])
    for name, amount, n in other_months:
        st.markdown(fmt.md(f"{name}: {fmt.money(amount)} in {n} {'entry' if n == 1 else 'entries'}"))
