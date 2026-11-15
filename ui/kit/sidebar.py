"""The sidebar, "the books" (design 4.3): the state every run depends on, then the actions that change it.

Order: title, model, budget, stock, reset, run settings. A busy indicator (a `run_every` fragment)
appears only while a job runs and reruns the app when the job finishes. The reset dialog is
flag-opened: `cfg.reset_open` is set by the button's callback and the dialog function runs on every
pass while it is set, so clicks inside it are never lost.

Sidebar-only wording lives in `COPY` below; everything shared comes from `ui.kit.copy`.
"""

import calendar
import datetime as dt

import streamlit as st

from models import BudgetStatus
from ui.kit import components, copy, fmt
from ui.kit.runtime import ProviderView, Runtime, UiConfig

COPY = {
    "app": "Acme accounts payable",
    "seed.demo": "Demo data",
    "seed.readme": "The brief's starting data",
    "model": "Model",
    "model.replay": "Replay of recorded Grok calls",
    "model.grok": "Grok, live",
    "stock": "Stock",
    "carry_over": "Stock and spending carry over between runs.",
    "spent": "{spent} of {limit} spent",
    "left": "**{left}** left",
    "over": ":material/warning: Over the limit by {over}",
    "settings": "Run settings",
    "provider": "Model provider",
    "provider.help": "Auto uses Grok when XAI_API_KEY is set, otherwise replay when every file has a recording.",
    "fail_rate": "Bank failure rate",
    "fail_rate.help": "Makes the mock bank fail this share of payment attempts, to show retries and the "
    "'payment failed' review. Leave at 0% for normal runs.",
    "slow": "Slow down replay so steps can be read",
    "slow.help": "Replay answers instantly. This waits briefly after each step on the Process page. "
    "Measured times are not affected.",
    "reset.title": "Reset demo data",
    "reset.body": "This clears every processed invoice, payment, review and paused run, then restores the "
    "starting stock and the {budget} monthly budget. Logs are kept.",
    "reset.starting": "Starting data",
    "reset.demo": "Demo data (recommended)",
    "reset.demo_help": "WidgetA 90, WidgetB 50, GadgetX 13, FakeItem 0. Raised from the brief's numbers so stock "
    "used by earlier invoices doesn't block later ones.",
    "reset.readme": "The brief's starting data",
    "reset.readme_help": "WidgetA 15, WidgetB 10, GadgetX 5, FakeItem 0. Because stock carries over, most "
    "invoices then fail the stock check: 2 paid, 1 for a person, 14 rejected, 3 duplicates.",
    "reset.toast": "Reset to {seed}. History cleared.",
    "busy": "A run is in progress. Settings and reset are available when it finishes.",
    "files_progress": "{done} of {total} files",
}

PAGE_KEY_PREFIXES = ("process.", "queue.", "batch.", "ledger.")
_PROVIDERS = ("auto", "grok", "replay")
_PROVIDER_LABEL = {"auto": "Auto", "grok": "Grok", "replay": "Replay"}


def session_config(rt: Runtime) -> UiConfig:
    """The per-session `UiConfig` from the `cfg.*` keys, seeded from `rt.default_config` on first use."""
    default = rt.default_config
    st.session_state.setdefault("cfg.provider", default.provider_choice or "auto")
    st.session_state.setdefault("cfg.fail_rate", float(default.payment_fail_rate))
    st.session_state.setdefault("cfg.slow_replay", bool(default.slow_replay))
    choice = st.session_state["cfg.provider"]
    return UiConfig(
        as_of=default.as_of,
        provider_choice=choice if choice in ("grok", "replay") else None,
        payment_fail_rate=float(st.session_state["cfg.fail_rate"]),
        slow_replay=bool(st.session_state["cfg.slow_replay"]),
    )


def clear_page_state() -> None:
    for key in [k for k in st.session_state.keys() if isinstance(k, str) and k.startswith(PAGE_KEY_PREFIXES)]:
        del st.session_state[key]


# ----------------------------------------------------------------- sections


def _recorded(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return fmt.day(dt.datetime.fromisoformat(value.replace("Z", "+00:00")).date())
    except ValueError:
        return value


def _model(view: ProviderView | None) -> None:
    st.markdown(f"**{COPY['model']}**")
    if view is None:
        return
    if view.name == "none":
        st.warning(copy.t("error.no_model_sidebar"), icon=":material/warning:")
        return
    models = ", ".join(
        f"{k.capitalize() if i == 0 else k} {fmt.md(v)}" for i, (k, v) in enumerate(view.models.items())
    )
    if view.name == "replay":
        st.markdown(COPY["model.replay"])
        recorded = _recorded(view.recording_date)
        text = " ".join(p for p in (f"Recorded {fmt.md(recorded)}." if recorded else "", f"{models}." if models else "") if p)
    elif view.name == "grok":
        st.markdown(COPY["model.grok"])
        text = f"{models}." if models else ""
    else:
        st.markdown(fmt.md(view.name))
        text = f"{models}." if models else ""
    if text:
        st.caption(text)


def _budget(budget: BudgetStatus) -> None:
    month = calendar.month_name[int(budget.month.split("-")[1])]
    st.markdown(f"**{month} budget**")
    over = budget.spent - budget.limit
    ratio = 1.0 if budget.limit <= 0 or over >= 0 else float(budget.spent / budget.limit)
    st.progress(min(1.0, max(0.0, ratio)))
    st.markdown(COPY["spent"].format(spent=fmt.md(fmt.money(budget.spent)), limit=fmt.md(fmt.money(budget.limit))))
    if over > 0:
        st.markdown(COPY["over"].format(over=fmt.md(fmt.money(over))))
    else:
        st.markdown(COPY["left"].format(left=fmt.md(fmt.money(budget.remaining))))


def _stock(rt: Runtime, cfg: UiConfig) -> None:
    with rt.reader(cfg) as reader:
        items = reader.inventory()
        start = reader.starting_stock()
    st.markdown(f"**{COPY['stock']}**")
    if items:
        components.meter_list([(i.item, i.stock, start.get(i.item, i.stock)) for i in items])


# ------------------------------------------------------------ busy indicator


@st.fragment(run_every=1.0)
def _busy(rt: Runtime) -> None:
    job = rt.jobs.current()
    if job is None:
        st.rerun()  # the job finished: refresh every page and the books
        return
    snap = job.snapshot()
    st.caption(fmt.md(snap.label))
    if snap.total_files:
        done = len(snap.results)
        st.progress(min(1.0, done / snap.total_files), text=COPY["files_progress"].format(done=done, total=snap.total_files))


# -------------------------------------------------------------- reset dialog


def _open_reset() -> None:
    st.session_state["cfg.reset_open"] = True


def _close_reset() -> None:
    st.session_state["cfg.reset_open"] = False


@st.dialog(COPY["reset.title"], on_dismiss=_close_reset)
def _reset_dialog(rt: Runtime, cfg: UiConfig) -> None:
    st.markdown(COPY["reset.body"].format(budget=fmt.md(fmt.money(rt.settings.monthly_budget_usd))))
    st.session_state.setdefault("cfg.reset_seed", "demo")
    st.radio(
        COPY["reset.starting"], ["demo", "readme"], key="cfg.reset_seed",
        format_func=lambda s: COPY[f"reset.{s}"],
        captions=[COPY["reset.demo_help"], COPY["reset.readme_help"]],
    )
    cancel_col, confirm_col = st.columns(2)
    if cancel_col.button(copy.TEXT["button.reset_cancel"], key="cfg.reset_cancel", width="stretch"):
        _close_reset()
        st.rerun()
    if confirm_col.button(copy.TEXT["button.reset_confirm"], key="cfg.reset_confirm", type="primary", width="stretch"):
        from ui.kit.jobs import Busy

        seed = st.session_state["cfg.reset_seed"]
        try:
            job = rt.start_reset(cfg, seed)
        except Busy:
            st.warning(COPY["busy"])
            return
        job.wait()
        error = job.snapshot().error
        if error is not None:
            st.error(f"{type(error).__name__}: {fmt.md(str(error))}")
            return
        _close_reset()
        clear_page_state()
        st.toast(COPY["reset.toast"].format(seed="demo data" if seed == "demo" else "the brief's starting data"))
        st.rerun()


# ----------------------------------------------------------------- the whole


def render_sidebar(rt: Runtime) -> UiConfig:
    """Draw the sidebar and return the session's `UiConfig`."""
    cfg = session_config(rt)
    busy = rt.jobs.current() is not None
    view = rt.provider(cfg)
    with rt.reader(cfg) as reader:
        budget = reader.budget_status()
        seed = reader.seed_name()
    with st.sidebar:
        st.markdown(f"#### {COPY['app']}")
        seed_text = COPY.get(f"seed.{seed}") if seed else None
        st.caption(f"As of {fmt.day(cfg.as_of)}." + (f" {seed_text}." if seed_text else ""))
        if busy:
            _busy(rt)
        _model(view)
        _budget(budget)
        _stock(rt, cfg)
        st.caption(COPY["carry_over"])
        st.button(
            copy.TEXT["button.reset_open"], key="cfg.reset", icon=":material/restart_alt:", width="stretch",
            on_click=_open_reset, disabled=busy,
        )
        with st.expander(COPY["settings"]):
            st.segmented_control(
                COPY["provider"], _PROVIDERS, key="cfg.provider", required=True, disabled=busy,
                format_func=_PROVIDER_LABEL.get, help=COPY["provider.help"],
            )
            if view is not None and view.name == "none" and view.reason and cfg.provider_choice is not None:
                st.warning(fmt.md(view.reason), icon=":material/warning:")
            st.slider(
                COPY["fail_rate"], 0.0, 1.0, step=0.1, format="percent", key="cfg.fail_rate", disabled=busy,
                help=COPY["fail_rate.help"],
            )
            st.toggle(COPY["slow"], key="cfg.slow_replay", disabled=busy, help=COPY["slow.help"])
    if st.session_state.get("cfg.reset_open"):
        _reset_dialog(rt, cfg)
    return cfg
