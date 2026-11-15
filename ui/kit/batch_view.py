"""Batch page builders: result rows, the table frame, folder helpers, metric figures, savings lines.

Everything except `table_config` is pure. Strings that are not in `copy.TEXT` live here as constants,
worded per design 5.4 and 6. Builders return plain text; the page escapes it (`fmt.md`) before showing.
"""

import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Sequence

import pandas as pd
import streamlit as st

from config.settings import REPO_ROOT
from core.metrics import RunMetrics, SavingsEstimate, compute_metrics
from models import InvoiceResult, Status
from ui.kit import case, copy, fmt, theme
from ui.kit.runtime import Runtime
from ui.kit.uploads import candidate_folders

DEFAULT_FOLDER = "data/invoices"

LABEL_FOLDER = "Folder"
LABEL_UPLOAD = "Upload files instead"
HELP_UPLOAD = "PDF with a text layer, TXT, JSON, CSV or XML. The files are saved and become the folder to run."
LABEL_INSPECT = "Open a case"
BUTTON_STOP = "Stop after this file"
NOTE_PROCESSED_ORDER = "Processed in filename order."
SECTION_METRICS = "What this run measured"
SECTION_METRICS_HISTORY = "Measured since the last reset"
SECTION_SAVINGS = "Estimated annual savings"
NO_PROVIDER_TITLE = "Ways to get a model:"

_FORMAT_ORDER = ("txt", "pdf", "json", "csv", "xml")
_MODEL_COST_UNPRICED = "Not priced yet"


# --------------------------------------------------------------------- folders


def _display(path: Path) -> str:
    try:
        return path.resolve().relative_to(REPO_ROOT.resolve()).as_posix()
    except ValueError:
        return str(path)


def folder_options(rt: Runtime) -> list[str]:
    """Known folders: `data/invoices` first, then other data folders and uploaded batches (repo-relative when inside the repo)."""
    found = [_display(p) for p in candidate_folders(rt.data_dir, rt.upload_dir)]
    return [DEFAULT_FOLDER] + [f for f in found if f != DEFAULT_FOLDER]


def folder_path(rt: Runtime, option: str) -> Path:
    """A typed or chosen folder as a path: relative paths are relative to the project folder."""
    path = Path(option.strip())
    return path if path.is_absolute() else REPO_ROOT / path


def display_path(path: Path) -> str:
    return _display(path)


def count_caption(files: Sequence[Path]) -> str:
    """`20 files: 7 TXT, 3 PDF, 6 JSON. Processed in filename order.`"""
    n = len(files)
    by_format: dict[str, int] = {}
    for f in files:
        suffix = Path(f).suffix.lower().lstrip(".") or "no extension"
        by_format[suffix] = by_format.get(suffix, 0) + 1
    ordered = [k for k in _FORMAT_ORDER if k in by_format] + sorted(k for k in by_format if k not in _FORMAT_ORDER)
    parts = ", ".join(f"{by_format[k]} {k.upper()}" for k in ordered)
    return f"{n} {'file' if n == 1 else 'files'}: {parts}. {NOTE_PROCESSED_ORDER}"


# --------------------------------------------------------------------- progress


def lower_first(text: str) -> str:
    """`Checking inventory…` becomes `checking inventory…`; an acronym (`VP is deciding…`) is kept."""
    if len(text) > 1 and text[1].isupper():
        return text
    return text[:1].lower() + text[1:]


def progress_text(done: int, total: int, file: str | None, step: str | None) -> str:
    """`Processing 7 of 20: invoice_1007.csv, checking inventory…` (design 5.4)."""
    index = min(done + 1, total) if total else done + 1
    text = f"Processing {index} of {total}" if total else f"Processing {index}"
    if file:
        text += f": {file}"
        if step:
            text += f", {lower_first(step)}"
    return text


def stopped_text(done: int, total: int) -> str:
    return f"Stopped after {done} of {total} files."


def outcome_sentence(counts: Sequence[tuple[Status, int]]) -> str:
    n = sum(c for _, c in counts)
    return f"{n} {'file' if n == 1 else 'files'}: {copy.counts_sentence(counts)}"


def status_counts(results: Sequence[InvoiceResult]) -> list[tuple[Status, int]]:
    tally: dict[Status, int] = {}
    for r in results:
        tally[r.status] = tally.get(r.status, 0) + 1
    return [(s, tally[s]) for s in copy.OUTCOME_ORDER if s in tally]


def history_line(n: int) -> str:
    return f"Since the last reset: {n} {'invoice' if n == 1 else 'invoices'} processed."


def error_callout(n: int) -> str:
    noun = "file hit" if n == 1 else "files hit"
    return (
        f"{n} {noun} a system error. Nothing was paid for {'it' if n == 1 else 'them'} and "
        f"{'it is' if n == 1 else 'they are'} not in the review queue. Open a row for the trace ID, "
        f"fix the cause, then process the file again."
    )


def waiting_link_label(n: int) -> str:
    return copy.t("link.review_waiting_one") if n == 1 else copy.t("link.review_waiting", n=n)


_WAY = re.compile(r"^\s*\d+\.\s*(.+)$")


def ways_to_get_a_model(message: str) -> list[str]:
    """The numbered ways in `NoProviderAvailable`'s own message, without their numbers."""
    return [m.group(1).strip() for line in message.splitlines() if (m := _WAY.match(line))]


# ------------------------------------------------------------------------ rows


@dataclass(frozen=True)
class ResultRow:
    file: str
    ref: str  # invoice number and revision, or "-"
    vendor: str
    status: str  # UI label
    ceiling: str
    flags: str  # short labels, comma-joined, most severe first
    paid: str  # money paid, or ""
    reason: str  # the "Why" column
    seconds: str
    llm_calls: int
    tokens: int
    error: str | None
    trace_id: str
    reused: bool
    amount: str = "-"
    status_token: str = "gray"
    flag_labels: tuple[str, ...] = ()
    flag_tokens: tuple[str, ...] = ()


def invoice_ref(result: InvoiceResult) -> str:
    if result.invoice_number:
        return result.invoice_number + (f" {result.revision}" if result.revision else "")
    return "-"


def case_title(result: InvoiceResult) -> str:
    """The dialog title: `INV-1004 R1 from Widgets Inc.`; the file name stands in for a missing number."""
    ref = invoice_ref(result)
    return f"{result.file if ref == '-' else ref} from {result.vendor or copy.t('case.no_vendor')}"


def result_rows(results: Sequence[InvoiceResult]) -> list[ResultRow]:
    rows = []
    for r in results:
        sem = theme.STATUS[r.status]
        ordered = case.sorted_flags(r.flags)
        labels: dict[str, str] = {}
        for f in ordered:
            labels.setdefault(copy.flag_label(f), theme.SEVERITY[f.severity].token)
        paid = fmt.money(r.payment.amount, r.payment.currency) if r.payment is not None and r.status == Status.PAID else ""
        rows.append(ResultRow(
            file=r.file,
            ref=invoice_ref(r),
            vendor=r.vendor or copy.t("case.no_vendor"),
            status=sem.label,
            ceiling=copy.ceiling_label(r.ceiling) if r.ceiling else "",
            flags=", ".join(labels),
            paid=paid,
            reason=copy.why(r),
            seconds=fmt.seconds(sum(r.usage.node_ms.values())) if not r.short_circuited and r.usage.node_ms else "-",
            llm_calls=r.usage.llm_calls,
            tokens=r.usage.tokens_in + r.usage.tokens_out,
            error=r.error if r.status == Status.ERROR else None,
            trace_id=r.trace_id,
            reused=r.short_circuited,
            amount=fmt.money(r.amount, r.currency) if r.amount is not None else "-",
            status_token=sem.token,
            flag_labels=tuple(labels),
            flag_tokens=tuple(labels.values()),
        ))
    return rows


_COLUMNS = {
    "file": "File", "ref": "Invoice", "vendor": "Vendor", "amount": "Amount", "status": "Status",
    "reason": "Why", "flags": "Flags", "calls": "Model calls",
}


def table_frame(rows: Sequence[ResultRow]) -> pd.DataFrame:
    """The results table as the page shows it: pills are one-item and many-item lists."""
    return pd.DataFrame({
        _COLUMNS["file"]: [r.file for r in rows],
        _COLUMNS["ref"]: [r.ref for r in rows],
        _COLUMNS["vendor"]: [r.vendor for r in rows],
        _COLUMNS["amount"]: [r.amount for r in rows],
        _COLUMNS["status"]: [[r.status] for r in rows],
        _COLUMNS["reason"]: [r.reason for r in rows],
        _COLUMNS["flags"]: [list(r.flag_labels) for r in rows],
        _COLUMNS["calls"]: [r.llm_calls for r in rows],
    })


def table_config(rows: Sequence[ResultRow], mode: theme.Mode) -> dict[str, Any]:
    """`column_config` for `table_frame`: status pills tinted by status, flag pills tinted by severity."""
    status_options = [theme.STATUS[s].label for s in theme.STATUS]
    status_colors = [theme.TINT[mode][theme.STATUS[s].token] for s in theme.STATUS]
    flag_tokens: dict[str, str] = {}
    for r in rows:
        for label, token in zip(r.flag_labels, r.flag_tokens):
            flag_tokens.setdefault(label, token)
    flag_options = list(flag_tokens)
    flag_colors = [theme.TINT[mode][flag_tokens[o]] for o in flag_options]
    cfg = st.column_config
    return {
        _COLUMNS["file"]: cfg.TextColumn(_COLUMNS["file"], pinned=True, width=176),
        _COLUMNS["ref"]: cfg.TextColumn(_COLUMNS["ref"], width=104),
        _COLUMNS["vendor"]: cfg.TextColumn(_COLUMNS["vendor"], width=176),
        _COLUMNS["amount"]: cfg.TextColumn(_COLUMNS["amount"], alignment="right", width=112),
        _COLUMNS["status"]: cfg.MultiselectColumn(_COLUMNS["status"], options=status_options, color=status_colors, width=132),
        _COLUMNS["reason"]: cfg.TextColumn(_COLUMNS["reason"], width=280),
        _COLUMNS["flags"]: cfg.MultiselectColumn(
            _COLUMNS["flags"], options=flag_options or None, color=flag_colors or None, width=260
        ),
        _COLUMNS["calls"]: cfg.NumberColumn(_COLUMNS["calls"], format="%d", width="small"),
    }


def table_height(n: int) -> int:
    return min(38 + 35 * max(n, 1), 38 + 35 * 14)


# --------------------------------------------------------------------- metrics


@dataclass(frozen=True)
class Figure:
    label: str
    value: str
    help: str


def replay_run(results: Sequence[InvoiceResult]) -> bool:
    ran = [r for r in results if r.status != Status.ERROR]
    return bool(ran) and all(r.provider == "replay" for r in ran)


def metric_figures(m: RunMetrics, *, replay: bool) -> list[Figure]:
    """The four measured figures (design 5.4) with their help texts. `m` is the run, or the original run of a re-run."""
    if m.automation_rate is None:
        decided = "-"
    else:
        decided = fmt.pct(m.decided_without_person, m.distinct)
    if replay:
        time_help = (
            "Model replies came from recordings. "
            f"Live Grok took {fmt.seconds(_ms(m.avg_llm_latency_s))} per invoice when recorded."
        )
    else:
        time_help = "Measured wall time of the pipeline."
    calls = "-" if m.llm_calls_per_doc is None else str(m.llm_calls_per_doc.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))
    return [
        Figure(
            "Decided without a person", decided,
            f"{m.decided_without_person} of {m.distinct} distinct documents were paid or rejected without a person. "
            "Duplicates are excluded.",
        ),
        Figure("Time per invoice", fmt.seconds(_ms(m.avg_processing_s)), time_help),
        Figure(
            "Model calls per invoice", calls,
            "Extraction, VP and Critic calls. Duplicates count their extraction call.",
        ),
        Figure(
            "Tokens per invoice", fmt.tokens_compact(m.tokens_per_doc),
            "Input plus output tokens. Model cost per invoice arrives in tier 3, priced from these.",
        ),
    ]


def _ms(seconds: Decimal | None) -> Decimal | None:
    return None if seconds is None else seconds * 1000


def metrics_caption(m: "RunMeasured") -> str:
    """The denominators in words (design 5.4)."""
    run, original = m.run, m.original
    parts = [
        f"{run.decided_without_person} of {run.distinct} distinct documents decided without a person; duplicates excluded."
    ]
    basis = original if original is not None else run
    if original is not None:
        parts.append(
            f"Every file already had a stored result, so time, model calls and tokens are from the original run "
            f"({basis.processed} files)."
        )
    else:
        parts.append(f"Time, model calls and tokens are means over the {run.processed} files that ran.")
        if run.reused:
            parts.append(f"{run.reused} stored results were reused and are not counted in them.")
    if run.errors:
        parts.append(f"{run.errors} system {'error is' if run.errors == 1 else 'errors are'} left out of the means.")
    if m.replay:
        parts.append(
            "Model replies came from recordings. "
            f"Live Grok took {fmt.seconds(_ms(basis.avg_llm_latency_s))} per invoice when recorded."
        )
    return " ".join(parts)


@dataclass(frozen=True)
class RunMeasured:
    run: RunMetrics
    original: RunMetrics | None  # set when nothing ran and the figures come from the stored results' own usage
    replay: bool

    @property
    def shown(self) -> RunMetrics:
        return self.original if self.original is not None else self.run


# --------------------------------------------------------------------- savings


@dataclass(frozen=True)
class SavingsLines:
    formula: str
    substituted: str
    result: str


def _dollars(value: Decimal) -> str:
    return f"${value.quantize(Decimal(1), rounding=ROUND_HALF_UP):,}"


def savings_lines(est: SavingsEstimate, m: RunMetrics) -> SavingsLines:
    rate = fmt.pct1(m.decided_without_person, m.distinct)
    model_cost = (
        "model cost (arrives in tier 3)" if est.llm_cost_per_invoice_usd is None
        else fmt.money(est.llm_cost_per_invoice_usd)
    )
    substituted = f"{est.volume:,} × {rate} × {fmt.money(est.baseline_cost_usd)} − {est.volume:,} × {model_cost}"
    if est.net_usd is None:
        result = f"= {_dollars(est.gross_usd)} a year, before model costs. Estimate."
    else:
        result = f"= {_dollars(est.net_usd)} a year. Estimate."
    return SavingsLines(est.formula, substituted, result)


def savings_table(est: SavingsEstimate, m: RunMetrics) -> pd.DataFrame:
    """Input, Value, Source. Source starts with `Assumption`, `Measured` or `Tier 3`."""
    sources = dict(est.assumptions)
    rows = [
        ("Annual invoice volume", f"{est.volume:,}", sources["Annual invoice volume"]),
        ("Baseline cost per invoice", fmt.money(est.baseline_cost_usd), sources["Baseline cost per invoice"]),
        (
            "Automation rate", fmt.pct1(m.decided_without_person, m.distinct),
            f"Measured: {m.decided_without_person} of {m.distinct} distinct documents",
        ),
    ]
    if est.llm_cost_per_invoice_usd is None:
        calls = "-" if m.llm_calls_per_doc is None else str(m.llm_calls_per_doc.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))
        rows.append((
            "Model cost per invoice", _MODEL_COST_UNPRICED,
            f"Tier 3. Measured usage: {fmt.tokens_compact(m.tokens_per_doc)} tokens, {calls} model calls per invoice",
        ))
    else:
        rows.append(("Model cost per invoice", fmt.money(est.llm_cost_per_invoice_usd), "Measured: priced from this run's tokens"))
    return pd.DataFrame(rows, columns=["Input", "Value", "Source"])


def measure(results: Sequence[InvoiceResult]) -> RunMeasured:
    """Metrics for a set of results. When every file was a stored result, the figures come from their original usage."""
    run = compute_metrics(results)
    original = None
    if run.processed == 0 and run.reused > 0:
        original = compute_metrics([r.model_copy(update={"short_circuited": False}) for r in results])
    return RunMeasured(run, original, replay_run(results))
