"""Acme invoice processing: command-line interface.

A thin layer over `service.Service`. Rich output for people, `--json` for machines
(exactly one JSON object on stdout, nothing else).
"""

import argparse
import contextlib
import datetime as dt
import json
import os
import shlex
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable

from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from config.settings import REPO_ROOT, load_settings
from llm.select import NoProviderAvailable
from models import (
    Banner,
    BatchResult,
    Ceiling,
    InvoiceResult,
    QueueItem,
    ResolutionPreview,
    Severity,
    Status,
)
from service import (
    AmbiguousReference,
    ApproveNotAllowed,
    FxRateRequired,
    NoteRequired,
    OverspendConfirmationRequired,
    ResolutionRefused,
    Service,
)

EXIT_OK, EXIT_ERROR, EXIT_USAGE, EXIT_REFUSED = 0, 1, 2, 3

STATUS_STYLE = {
    Status.PAID: "bold green",
    Status.REJECTED: "bold red",
    Status.PENDING_HUMAN: "bold magenta",
    Status.DUPLICATE: "yellow",
    Status.CREDIT_DUE: "cyan",
    Status.SUPERSEDED: "dim",
    Status.ERROR: "bold white on red",
    Status.PROCESSING: "dim",
}
STATUS_BORDER = {
    Status.PAID: "green", Status.REJECTED: "red", Status.PENDING_HUMAN: "magenta", Status.DUPLICATE: "yellow",
    Status.CREDIT_DUE: "cyan", Status.ERROR: "red",
}
SEVERITY_STYLE = {
    Severity.ERROR: "red",
    Severity.HUMAN: "magenta",
    Severity.ESCALATION: "yellow",
    Severity.INFO: "dim",
}
CEILING_STYLE = {Ceiling.APPROVE: "green", Ceiling.HUMAN: "magenta", Ceiling.REJECT: "red"}

STAGES = {
    "load_document": "Reading document",
    "parse_structured": "Parsing structured data",
    "extract_with_llm": "Extracting fields (LLM)",
    "ground_check": "Checking extraction against the text",
    "normalize": "Normalising values",
    "dedupe": "Checking for duplicates",
    "validate": "Validating against inventory, vendors, budget",
    "score_risk": "Scoring risk",
    "compute_ceiling": "Computing decision ceiling",
    "vp_memo": "VP: reading the case",
    "vp_decide": "VP: deciding",
    "critic_review": "Critic: reviewing",
    "clamp_decision": "Applying policy clamp",
    "prepare_human": "Preparing human review",
    "await_human": "Waiting for a person",
    "pay": "Paying",
    "record_rejection": "Recording rejection",
    "record_duplicate": "Recording duplicate",
    "finish": "Finishing",
}


# ------------------------------------------------------------------ helpers


def money(value: Decimal | None, currency: str | None = "USD") -> str:
    if value is None:
        return "-"
    text = f"{value:,.2f}"
    if currency in (None, "USD"):
        return f"${text}"
    return f"{text} {currency}"


def _enum(value: Any) -> str:
    return str(getattr(value, "value", value))


def _status_text(status: Status) -> Text:
    return Text(status.value, style=STATUS_STYLE.get(status, ""))


def _ref(result: InvoiceResult) -> str:
    if result.invoice_number:
        return result.invoice_number + (f" {result.revision}" if result.revision else "")
    return result.file


class _UsageError(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # type: ignore[override]
        raise _UsageError(message)


def _fx_rate(text: str) -> Decimal:
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise argparse.ArgumentTypeError(f"invalid exchange rate {text!r}")
    if not value.is_finite() or value <= 0:
        raise argparse.ArgumentTypeError("exchange rate must be a positive number")
    return value


def _date(text: str) -> dt.date:
    try:
        return dt.date.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid date {text!r}; use YYYY-MM-DD")


def build_parser() -> argparse.ArgumentParser:
    p = _Parser(
        prog="main.py",
        description="Acme invoice processing. Process invoices, review the human queue, resolve items.",
        epilog="State carries over between runs; use --reset to start fresh.",
    )
    run = p.add_argument_group("what to do (choose one)")
    run.add_argument("--invoice_path", "--invoice-path", dest="invoice_path", metavar="FILE",
                     help="process one invoice file")
    run.add_argument("--dir", metavar="DIR", help="process every file in a directory (batch summary)")
    run.add_argument("--queue", action="store_true", help="list invoices waiting for a person")
    run.add_argument("--resolve", metavar="REF",
                     help="resolve a queued invoice (INV-1004, INV-1004 R1, file name); alone it previews")
    run.add_argument("--reset", action="store_true", help="restore starting stock and budget, clear history")

    res = p.add_argument_group("resolving")
    res.add_argument("--approve", action="store_true", help="approve the queued invoice")
    res.add_argument("--reject", action="store_true", help="reject the queued invoice")
    res.add_argument("--note", help="why (required with --approve / --reject)")
    res.add_argument("--fx-rate", type=_fx_rate, metavar="RATE", help="rate to USD for a foreign-currency invoice")
    res.add_argument("--confirm-overspend", action="store_true", help="allow approval to exceed the monthly budget")

    p.add_argument("--seed", choices=("demo", "readme"), help="starting data for --reset")
    common = p.add_argument_group("common")
    common.add_argument("--provider", choices=("grok", "replay", "ollama"), help="model provider (default: auto)")
    common.add_argument("--json", action="store_true", help="print one JSON object, no formatting")
    common.add_argument("--as-of", type=_date, metavar="YYYY-MM-DD", help="date used for past-due checks")
    common.add_argument("--payment-fail-rate", type=float, default=0.0, metavar="FLOAT",
                        help="probability the mock bank fails an attempt (testing)")
    return p


def _validate(args: argparse.Namespace) -> None:
    modes = [bool(args.invoice_path), bool(args.dir), args.queue, bool(args.resolve), args.reset]
    if sum(modes) != 1:
        raise _UsageError(
            "choose exactly one of --invoice_path, --dir, --queue, --resolve, --reset"
            if any(modes) else "nothing to do: give --invoice_path, --dir, --queue, --resolve or --reset"
        )
    if args.approve and args.reject:
        raise _UsageError("--approve and --reject are mutually exclusive")
    if (args.approve or args.reject or args.note or args.fx_rate or args.confirm_overspend) and not args.resolve:
        raise _UsageError("--approve, --reject, --note, --fx-rate and --confirm-overspend need --resolve REF")
    if (args.approve or args.reject) and not (args.note and args.note.strip()):
        raise _UsageError("--note is required with --approve / --reject")
    if args.seed and not args.reset:
        raise _UsageError("--seed only applies to --reset")
    if not 0.0 <= args.payment_fail_rate <= 1.0:
        raise _UsageError("--payment-fail-rate must be between 0 and 1")


def default_factory(args: argparse.Namespace) -> Service:
    return Service(
        load_settings(),
        provider_choice=args.provider,
        env=os.environ,
        as_of=args.as_of,
        payment_fail_rate=args.payment_fail_rate,
    )


# --------------------------------------------------------------- rich output


class Renderer:
    def __init__(self, console: Console) -> None:
        self.c = console
        self._current_file: str | None = None

    # banner ---------------------------------------------------------------

    def banner(self, b: Banner) -> None:
        if b.provider == "none":
            provider = Text.assemble(("none", "bold yellow"),
                                     ("  (set XAI_API_KEY in .env, or use --provider replay)", "dim"))
        else:
            provider = Text(b.provider, style="bold cyan")
            if b.models:
                provider.append("  " + ", ".join(f"{k}: {v}" for k, v in b.models.items()), style="dim")
            if b.recording_date:
                provider.append(f"  recorded {b.recording_date}", style="dim")
        info = Table.grid(padding=(0, 2))
        info.add_column(style="bold")
        info.add_column()
        info.add_row("Provider", provider)
        info.add_row("Seed", b.seed or "-")
        info.add_row("As of", b.as_of.isoformat())
        pct = f"{(b.budget.spent / b.budget.limit * 100):.0f}%" if b.budget.limit else "-"
        info.add_row("Budget", Text.assemble(
            f"{money(b.budget.spent)} used of {money(b.budget.limit)} ({pct}), ",
            (f"{money(b.budget.remaining)} remaining", "green" if b.budget.remaining > 0 else "red"),
            f"  [{b.budget.month}]"))
        stock = Table(box=None, padding=(0, 2), show_header=True, header_style="bold dim")
        stock.add_column("Item")
        stock.add_column("In stock", justify="right")
        for item, qty in sorted(b.stock.items()):
            stock.add_row(item, Text(str(qty), style="red" if qty <= 0 else ""))
        self.c.print(Panel(
            Group(info, Text(""), stock, Text(""), Text(b.hint, style="dim italic")),
            title="[bold]Acme invoice processing[/bold]", title_align="left", border_style="blue",
        ))

    # progress -------------------------------------------------------------

    def on_update(self, file_name: str, node: str, update: dict) -> None:
        if file_name != self._current_file:
            self._current_file = file_name
            self.c.print(Text(f"\n{file_name}", style="bold"))
        detail = ""
        if node == "compute_ceiling" and update.get("ceiling") is not None:
            detail = _enum(update["ceiling"])
        elif node == "validate" and "flags" in update:
            detail = f"{len(update['flags'])} flag(s)"
        elif node in ("vp_decide",) and update.get("vp_output") is not None:
            detail = _enum(getattr(update["vp_output"], "decision", ""))
        elif node == "critic_review" and update.get("critic_output") is not None:
            detail = _enum(getattr(update["critic_output"], "verdict", ""))
        elif node == "clamp_decision" and update.get("decision") is not None:
            detail = _enum(update["decision"].final)
        elif node in ("pay", "record_rejection", "record_duplicate", "finish") and update.get("status"):
            detail = _enum(update["status"])
        line = Text.assemble(("  > ", "dim cyan"), STAGES.get(node, node))
        if detail:
            line.append(f"  {detail}", style="dim")
        self.c.print(line)

    # one invoice ----------------------------------------------------------

    def result(self, r: InvoiceResult) -> None:
        head = Table.grid(padding=(0, 2))
        head.add_column(style="bold")
        head.add_column()
        head.add_row("Status", _status_text(r.status))
        head.add_row("Invoice", f"{_ref(r)}  ({r.file})")
        if r.vendor:
            head.add_row("Vendor", r.vendor)
        if r.amount is not None:
            amt = money(r.amount, r.currency)
            if r.currency and r.currency != "USD":
                amt += f"  ({money(r.amount_usd) if r.amount_usd is not None else 'USD amount needs a rate'})"
            head.add_row("Amount", amt)
        if r.ceiling is not None:
            head.add_row("Ceiling", Text(r.ceiling.value, style=CEILING_STYLE[r.ceiling]))
        if r.duplicate_of:
            head.add_row("Duplicate of", r.duplicate_of)
        if r.short_circuited:
            head.add_row("Note", Text("already processed; stored result shown, nothing re-run", style="dim"))
        if r.error:
            head.add_row("Error", Text(r.error, style="red"))
        self.c.print(Panel(head, title=f"[bold]{r.file}[/bold]", title_align="left",
                           border_style=STATUS_BORDER.get(r.status, "white")))
        if r.flags:
            self.flags(r)
        if r.transcript or r.decision:
            self.decision(r)
        if r.payment:
            self.receipt(r)
        if r.status == Status.PENDING_HUMAN and r.human:
            self.pending(r)
        elif r.human and r.human.outcome:
            note = r.human.outcome_note or ""
            self.c.print(Text.assemble(("Human review: ", "bold magenta"), r.human.outcome, f"  {note}"))

    def flags(self, r: InvoiceResult) -> None:
        t = Table(title="Flags", title_justify="left", header_style="bold", expand=False)
        t.add_column("Severity")
        t.add_column("Code")
        t.add_column("Message", overflow="fold", max_width=70)
        order = list(Severity)
        for f in sorted(r.flags, key=lambda f: order.index(f.severity)):
            style = SEVERITY_STYLE[f.severity]
            t.add_row(Text(f.severity.value, style=style), Text(f.code.value, style=style),
                      Text(f.message, style=style))
        self.c.print(t)

    def decision(self, r: InvoiceResult) -> None:
        parts: list[Any] = []
        for turn in r.transcript:
            out = turn.output
            tools = ", ".join(tc.name for tc in turn.tool_calls)
            if turn.actor == "vp":
                title = "VP memo" if turn.kind == "memo" else f"VP decision (round {turn.round})"
                if turn.kind == "memo":
                    line = Text.assemble((title, "bold cyan"), "\n", str(out.get("memo", "")))
                else:
                    dec = str(out.get("decision", ""))
                    line = Text.assemble((title, "bold cyan"), ": ",
                                         (dec, CEILING_STYLE.get(Ceiling(dec), "") if dec in Ceiling._value2member_map_ else ""),
                                         "\n", str(out.get("reasoning", "")))
                    for oa in out.get("objections_answered", []) or []:
                        line.append(f"\n  answered: {oa.get('objection', '')} -> {oa.get('answer', '')}", style="dim")
            else:
                verdict = str(out.get("verdict", ""))
                line = Text.assemble((f"Critic (round {turn.round})", "bold yellow"), ": ",
                                     (verdict, "green" if verdict == "AGREE" else "yellow"))
                if out.get("proposed_decision"):
                    line.append(f"  proposes {out['proposed_decision']}", style="dim")
                for ob in out.get("objections", []) or []:
                    line.append(f"\n  objection: {ob.get('about', '')}: {ob.get('evidence', '')}", style="yellow")
            if tools:
                line.append(f"\n  tools: {tools}", style="dim")
            parts.extend([line, Text("")])
        d = r.decision
        if d:
            final = Text.assemble(("Final: ", "bold"), (d.final.value, CEILING_STYLE[d.final]),
                                  (f"   decided by {d.by}", "dim"))
            if d.clamped:
                final.append("\nClamped by policy: the proposal was overridden by a hard rule.", style="bold yellow")
            if not r.transcript:
                final.append("\n" + d.reasoning)
            parts.append(final)
        elif parts:
            parts.pop()
        self.c.print(Panel(Group(*parts), title="Decision", title_align="left", border_style="cyan"))

    def receipt(self, r: InvoiceResult) -> None:
        p = r.payment
        assert p is not None
        t = Table.grid(padding=(0, 2))
        t.add_column(style="bold")
        t.add_column()
        t.add_row("Paid to", p.vendor)
        t.add_row("Amount", money(p.amount, p.currency))
        t.add_row("Currency", p.currency + (f" (rate {p.fx_rate})" if p.fx_rate else ""))
        t.add_row("USD", money(p.amount_usd))
        t.add_row("Attempts", str(p.attempts))
        t.add_row("Paid at", p.paid_at.strftime("%Y-%m-%d %H:%M:%S"))
        self.c.print(Panel(t, title="Payment receipt", title_align="left", border_style="green"))

    def pending(self, r: InvoiceResult) -> None:
        h = r.human
        assert h is not None
        ref = _ref(r)
        q = shlex.quote(ref)
        body: list[Any] = [Text.assemble(("Reason: ", "bold"), h.reason.value.replace("_", " "))]
        if len(h.reasons) > 1:
            body.append(Text("Also: " + ", ".join(x.value for x in h.reasons if x != h.reason), style="dim"))
        if h.recommendation:
            body.append(Text.assemble(("Recommendation: ", "bold"), h.recommendation.value))
        if h.recommendation_reasoning:
            body.append(Text(h.recommendation_reasoning, style="dim"))
        if h.payment_amount is not None:
            body.append(Text.assemble(("Approval would pay: ", "bold"), money(h.payment_amount, h.currency)))
        extra = " --fx-rate RATE" if h.reason.value == "foreign_currency" else ""
        body += [Text(""), Text("Next steps", style="bold"),
                 Text(f"  python main.py --resolve {q}", style="cyan"),
                 Text(f"  python main.py --resolve {q} --approve{extra} --note \"...\"", style="green"),
                 Text(f"  python main.py --resolve {q} --reject --note \"...\"", style="red")]
        self.c.print(Panel(Group(*body), title="Waiting for a person", title_align="left", border_style="magenta"))

    # batch ----------------------------------------------------------------

    def batch(self, b: BatchResult) -> None:
        t = Table(title="Batch summary", title_justify="left", header_style="bold")
        t.add_column("File", overflow="fold")
        t.add_column("Invoice")
        t.add_column("Status")
        t.add_column("Ceiling")
        t.add_column("Flags", justify="right")
        t.add_column("Paid", justify="right")
        for r in b.results:
            err = r.status == Status.ERROR
            paid = money(r.payment.amount, r.payment.currency) if r.payment else "-"
            row_style = "red" if err else None
            t.add_row(
                r.file, _ref(r) if r.invoice_number else "-", _status_text(r.status),
                r.ceiling.value if r.ceiling else "-", str(len(r.flags)) if r.flags else "-", paid,
                style=row_style,
            )
            if err and r.error:
                t.add_row("", Text(r.error, style="red"), "", "", "", "", style="red")
        self.c.print(t)
        counts = b.counts
        paid_total = sum((r.payment.amount_usd for r in b.results if r.payment and not r.short_circuited), Decimal("0"))
        pct = round(100 * b.decided_without_person / b.distinct_documents) if b.distinct_documents else 0
        tot = Table.grid(padding=(0, 2))
        tot.add_column(style="bold")
        tot.add_column()
        tot.add_row("Paid", f"{counts.get('PAID', 0)}  ({money(paid_total)} USD)")
        tot.add_row("Rejected", str(counts.get("REJECTED", 0)))
        tot.add_row("Needs a person", str(counts.get("PENDING_HUMAN", 0)))
        tot.add_row("Duplicates", str(counts.get("DUPLICATE", 0)))
        if counts.get("ERROR"):
            tot.add_row("Errors", Text(str(counts["ERROR"]), style="bold red"))
        tot.add_row("Automation",
                    f"{b.decided_without_person} of {b.distinct_documents} ({pct}%) decided without a person")
        tot.add_row("Budget", f"{money(b.budget.spent)} spent, {money(b.budget.remaining)} of "
                              f"{money(b.budget.limit)} remaining")
        self.c.print(Panel(tot, title="Totals", title_align="left", border_style="blue"))
        if counts.get("PENDING_HUMAN"):
            self.c.print(Text("Run `python main.py --queue` to see what is waiting for you.", style="magenta"))

    # queue / preview ------------------------------------------------------

    def queue(self, items: list[QueueItem]) -> None:
        if not items:
            self.c.print(Panel("Nothing is waiting for a person.", border_style="green"))
            return
        t = Table(title="Waiting for a person", title_justify="left", header_style="bold")
        for col in ("Ref", "Reason", "Recommendation", "Amount", "File"):
            t.add_column(col, justify="right" if col == "Amount" else "left", overflow="fold")
        for it in items:
            rv = it.review
            t.add_row(Text(it.invoice_ref, style="bold magenta"), rv.reason.value.replace("_", " "),
                      rv.recommendation.value if rv.recommendation else "-",
                      money(rv.payment_amount, rv.currency) if rv.payment_amount is not None else "-",
                      it.file_name)
        self.c.print(t)
        self.c.print(Text('Next: python main.py --resolve REF   (preview), then --approve|--reject --note "..."',
                          style="dim"))

    def preview(self, p: ResolutionPreview) -> None:
        t = Table.grid(padding=(0, 2))
        t.add_column(style="bold")
        t.add_column()
        t.add_row("Reason", p.reason.value.replace("_", " "))
        if not p.can_approve:
            t.add_row("Approve", Text("not possible; this can only be rejected", style="red"))
        else:
            if p.payment_amount is not None:
                amt = money(p.payment_amount, p.currency)
                if p.payment_amount_usd is not None and p.currency != "USD":
                    amt += f"  = {money(p.payment_amount_usd)} at rate {p.fx_rate}"
                t.add_row("Approval pays", amt)
            outcome = {"pay": "pay the invoice", "credit_due": "nothing to pay (credit due)",
                       "reject_stock": "would be rejected: stock shortfall", "blocked": "blocked"}
            t.add_row("Outcome", outcome[p.approve_outcome])
            if p.needs_fx_rate and p.fx_rate is None:
                t.add_row("Needs", Text(f"--fx-rate (rate from {p.currency} to USD)", style="yellow"))
            t.add_row("Budget", f"{money(p.budget.spent)} of {money(p.budget.limit)} used")
            if p.needs_overspend_confirmation:
                t.add_row("Overspend", Text(f"{money(p.overspend_usd)} over budget; needs --confirm-overspend",
                                            style="yellow"))
        for s in p.stock_shortfalls:
            t.add_row("Short on stock", Text(f"{s.item}: needs {s.needed}, has {s.available}", style="red"))
        q = shlex.quote(p.invoice_ref)
        fx = f" --fx-rate {p.fx_rate if p.fx_rate is not None else 'RATE'}" if p.needs_fx_rate else ""
        self.c.print(Panel(t, title=f"Preview: {p.invoice_ref}", title_align="left", border_style="magenta"))
        self.c.print(Text(f"python main.py --resolve {q} --approve{fx} --note \"...\"", style="green"))
        self.c.print(Text(f"python main.py --resolve {q} --reject --note \"...\"", style="red"))


# ------------------------------------------------------------------- errors


def _refusal(exc: ResolutionRefused) -> tuple[str, dict[str, Any]]:
    extra: dict[str, Any] = {}
    msg = str(exc)
    if isinstance(exc, OverspendConfirmationRequired):
        reach = exc.budget.limit + exc.overspend_usd
        msg = (f"Add --confirm-overspend (spending would reach {money(reach)} of {money(exc.budget.limit)})")
        extra["overspend_usd"] = str(exc.overspend_usd)
    elif isinstance(exc, FxRateRequired):
        msg = f"{exc} Add --fx-rate RATE."
    elif isinstance(exc, ApproveNotAllowed):
        msg = f"{exc} Use --reject --note \"...\" instead."
    elif isinstance(exc, NoteRequired):
        msg = f"{exc} Add --note \"...\"."
    elif isinstance(exc, AmbiguousReference):
        extra["candidates"] = exc.candidates
    return msg, extra


# --------------------------------------------------------------------- main


def _dump(model: Any) -> Any:
    return model.model_dump(mode="json")


def main(
    argv: list[str] | None = None,
    *,
    service_factory: Callable[[argparse.Namespace], Service] | None = None,
) -> int:
    parser = build_parser()
    real_out = sys.stdout
    try:
        args = parser.parse_args(argv)
        _validate(args)
    except _UsageError as exc:
        wants_json = "--json" in (argv if argv is not None else sys.argv[1:])
        if wants_json:
            print(json.dumps({"error": {"type": "UsageError", "message": str(exc)}}), file=real_out)
        else:
            print(f"usage error: {exc}\nTry: python main.py --help", file=sys.stderr)
        return EXIT_USAGE
    except SystemExit as exc:  # --help
        return int(exc.code or 0)

    as_json: bool = args.json
    console = Console(file=real_out, highlight=False)
    err_console = Console(file=sys.stderr, highlight=False)
    ui = Renderer(console)

    def emit(obj: Any) -> None:
        print(json.dumps(obj, indent=2), file=real_out)

    def fail(code: int, kind: str, message: str, **extra: Any) -> int:
        if as_json:
            emit({"error": {"type": kind, "message": message, **extra}})
        else:
            err_console.print(Text(message, style="red"))
        return code

    factory = service_factory or default_factory
    # In JSON mode, anything a library prints goes to stderr so stdout is a single object.
    guard = contextlib.redirect_stdout(sys.stderr) if as_json else contextlib.nullcontext()
    try:
        with guard, factory(args) as svc:
            on_update = None if as_json else ui.on_update
            if args.invoice_path:
                path = Path(args.invoice_path)
                if not path.is_file():
                    return fail(EXIT_USAGE, "UsageError", f"No such file: {path}")
                if not as_json:
                    ui.banner(svc.banner())
                result = svc.run_invoice(path, on_update=on_update)
                if as_json:
                    emit(_dump(result))
                else:
                    ui.result(result)
                return EXIT_ERROR if result.status == Status.ERROR else EXIT_OK

            if args.dir:
                directory = Path(args.dir)
                if not directory.is_dir():
                    return fail(EXIT_USAGE, "UsageError", f"No such directory: {directory}")
                if not as_json:
                    ui.banner(svc.banner())
                batch = svc.run_batch(directory, on_update=on_update)
                if as_json:
                    emit(_dump(batch))
                else:
                    console.print()
                    ui.batch(batch)
                return EXIT_ERROR if any(r.status == Status.ERROR for r in batch.results) else EXIT_OK

            if args.queue:
                items = svc.list_queue()
                if as_json:
                    emit({"queue": [_dump(i) for i in items]})
                else:
                    ui.queue(items)
                return EXIT_OK

            if args.resolve:
                if not (args.approve or args.reject):
                    preview = svc.preview_resolution(args.resolve, fx_rate=args.fx_rate)
                    emit(_dump(preview)) if as_json else ui.preview(preview)
                    return EXIT_OK
                result = svc.resolve(
                    args.resolve, action="approve" if args.approve else "reject", note=args.note,
                    fx_rate=args.fx_rate, confirm_overspend=args.confirm_overspend,
                )
                emit(_dump(result)) if as_json else ui.result(result)
                return EXIT_ERROR if result.status == Status.ERROR else EXIT_OK

            if args.reset:
                seed = args.seed or svc.settings.default_seed
                svc.reset(seed)
                if as_json:
                    emit({"reset": seed})
                else:
                    console.print(Text(f"Reset complete: seed '{seed}'.", style="bold green"))
                    ui.banner(svc.banner())
                return EXIT_OK
    except NoProviderAvailable as exc:
        return fail(EXIT_USAGE, "NoProviderAvailable", str(exc))
    except ResolutionRefused as exc:
        msg, extra = _refusal(exc)
        return fail(EXIT_REFUSED, type(exc).__name__, msg, **extra)
    return EXIT_USAGE  # unreachable: _validate guarantees one mode


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv(REPO_ROOT / ".env", override=False)
    sys.exit(main())
