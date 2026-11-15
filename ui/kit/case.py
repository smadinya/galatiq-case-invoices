"""Pure builders behind the case view (design 5.0): no Streamlit, no I/O.

`field_rows` pairs every extracted field with the flags that point at it and the source
lines that support it; `source_lines` turns that into highlights for the source panel;
`chat_turns` turns the VP and Critic transcript into displayable turns.

Deviations from the plan's section 4.4 (all additive, all defaulted):
- `CaseDetail.processed_at`: when a stored result was first processed, for the "Already processed on
  {date}" line. Pages fill it from `InvoiceRecord`; without it the line has no date.
- `ChatTurn.proposes`, `ChatTurn.answered`, `ChatTurn.round`: the Critic's proposed decision, the VP's
  objections-answered pairs (kept apart from `points`, which holds flags addressed), and the display round.
- `ToolCallView.answer`: the one-line summary of the result (design 6.5).
- `chat_turns` emits no "rules" turn. Policy lines are not chat bubbles (design 5.0 D.4), so they come from
  `policy_notices` instead. The `rules` actor stays in the type for compatibility.
"""

import datetime as dt
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Mapping, Sequence

from core.text import clean_item_key
from models import (
    AgentTurn,
    Ceiling,
    Decision,
    Flag,
    FlagCode,
    HumanReview,
    Invoice,
    InvoiceResult,
    InvoiceState,
    Severity,
    Totals,
)
from ui.kit import copy, fmt

_SEVERITY_ORDER = (Severity.ERROR, Severity.HUMAN, Severity.ESCALATION, Severity.INFO)
_RANK = {s: i for i, s in enumerate(_SEVERITY_ORDER)}


@dataclass(frozen=True)
class CaseDetail:
    """What `InvoiceResult` lacks: the checkpointed state and the original document."""

    state: InvoiceState | None = None
    source: str | None = None  # text shown in the source panel (TXT/PDF text, or the decoded file)
    source_kind: Literal["text", "pdf", "file"] | None = None
    original_path: Path | None = None
    processed_at: dt.datetime | None = None


@dataclass(frozen=True)
class FieldRow:
    path: str  # same keys as InvoiceState.evidence_spans: "vendor.name", "line_items[3].unit_price", "total"
    group: str  # "header" | "line 1" ... | "totals"
    label: str
    raw: str | None  # as extracted, before normalisation
    value: str | None  # as understood, formatted for display
    corrected: bool  # path appears in corrections or normalized_changes
    evidence: tuple[str, ...]  # source lines that support it
    flags: tuple[Flag, ...]
    severity: Severity | None  # worst flag on the field


@dataclass(frozen=True)
class SourceLine:
    number: int  # 1-based
    text: str
    paths: tuple[str, ...]  # fields this line is evidence for
    severity: Severity | None  # worst flag on any of those fields


@dataclass(frozen=True)
class ToolCallView:
    name: str  # raw tool name; display names are in copy.TOOL_NAMES
    arguments: str  # "Asked about": argument values, comma-joined
    result: Mapping[str, Any]
    answer: str = ""  # one-line summary of the result


@dataclass(frozen=True)
class ChatTurn:
    actor: Literal["vp", "critic", "rules", "person"]
    title: str  # "VP, round 1" | "Critic, round 1" | "VP memo" | "Person"
    verdict: str | None  # VP: the proposed ceiling value; Critic: AGREE | OBJECT; person: approve | reject
    body: str  # untrusted model or person text: render escaped
    points: tuple[tuple[str, str], ...]  # VP: flags addressed; Critic: objections (about, evidence + request)
    tools: tuple[ToolCallView, ...]
    proposes: Ceiling | None = None  # Critic's proposed decision
    answered: tuple[tuple[str, str], ...] = ()  # VP: (objection, answer)
    round: int | None = None  # display round, 1-based


# ---------------------------------------------------------------------- flags


def sorted_flags(flags: Sequence[Flag]) -> list[Flag]:
    """error, human, escalation, info; the original order is kept within a severity."""
    return sorted(flags, key=lambda f: _RANK[f.severity])


def _dedupe(paths: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(paths))


def _line_indices(flag: Flag) -> list[int]:
    out: list[int] = []
    for entry in flag.evidence.get("lines") or []:
        try:
            out.append(int(entry["index"]))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def flag_fields(flag: Flag, invoice: Invoice | None, totals: Totals | None) -> tuple[str, ...]:
    """The field paths a flag points at (the table in design 5.0 C; paths match `evidence_spans` keys)."""
    code = flag.code
    paths: list[str] = []
    if code in (FlagCode.MISSING_INVOICE_NUMBER, FlagCode.REVISION_OF_PAID_INVOICE,
                FlagCode.CONFLICTING_INVOICE_NUMBER):
        paths = ["invoice_number"]
        if code == FlagCode.REVISION_OF_PAID_INVOICE:
            paths.append("revision")
    elif code in (FlagCode.MISSING_VENDOR, FlagCode.UNKNOWN_VENDOR, FlagCode.VENDOR_RENAMED):
        paths = ["vendor.name"] + (["vendor.former_name"] if code == FlagCode.VENDOR_RENAMED else [])
    elif code in (FlagCode.INVALID_DUE_DATE, FlagCode.PAST_DUE):
        paths = ["due_date"]
    elif code == FlagCode.TERMS_MISMATCH:
        paths = ["due_date", "payment_terms"]
    elif code == FlagCode.FOREIGN_CURRENCY:
        paths = ["currency"]
    elif code == FlagCode.NON_POSITIVE_QUANTITY:
        paths = [f"line_items[{i}].quantity" for i in _line_indices(flag)]
    elif code in (FlagCode.PRICE_ABOVE_CATALOG, FlagCode.PRICE_BELOW_CATALOG):
        paths = [f"line_items[{i}].unit_price" for i in _line_indices(flag)]
    elif code in (FlagCode.UNKNOWN_ITEM, FlagCode.ZERO_STOCK_ITEM, FlagCode.INSUFFICIENT_STOCK):
        item = flag.evidence.get("item")
        if invoice is not None and item:
            key = clean_item_key(str(item))
            with_quantity = code != FlagCode.UNKNOWN_ITEM
            for i, line in enumerate(invoice.line_items):
                if line.item_key == key:
                    paths.append(f"line_items[{i}].item")
                    if with_quantity:
                        paths.append(f"line_items[{i}].quantity")
    elif code == FlagCode.MATH_MISMATCH:
        paths = ["total"]
        if totals is not None:
            if totals.subtotal_ok is False:
                paths.append("subtotal")
            if totals.tax_ok is False:
                paths.append("tax_amount")
            paths += [f"line_items[{c.index}].amount" for c in totals.lines if not c.ok]
    elif code in (FlagCode.OVER_THRESHOLD, FlagCode.NEAR_THRESHOLD, FlagCode.OVER_BUDGET):
        paths = ["total"]
    elif code == FlagCode.VALUE_NORMALIZED:
        for change in flag.evidence.get("changes") or []:
            field = change.get("field") if isinstance(change, Mapping) else None
            if field:
                paths.append(str(field))
    # no_line_items, unreadable_document, payment_failed, high_risk_score, prompt_injection: document level
    return _dedupe(paths)


# ---------------------------------------------------------------- field rows


def _raw(extracted: Any, path: str) -> str | None:
    """The extracted (pre-normalisation) value at a path like `line_items[1].amount`; None when absent."""
    if extracted is None:
        return None
    try:
        node: Any = extracted
        for part in path.split("."):
            if "[" in part:
                name, idx = part[:-1].split("[")
                node = getattr(node, name)[int(idx)]
            else:
                node = getattr(node, part)
        return None if node is None else str(node)
    except (AttributeError, IndexError, ValueError, TypeError):
        return None


def _rate(rate: Any) -> str:
    return f"{format((rate * 100).normalize(), 'f')}%"


def _build_rows(inv: Invoice | None) -> list[tuple[str, str, str, str | None, bool]]:
    """(path, group, label, value, optional) for every field the document can carry."""
    if inv is None:
        return []
    cur = inv.currency
    due = fmt.day(inv.due_date) if inv.due_date else inv.due_date_raw
    tax = fmt.money(inv.tax_amount, cur) if inv.tax_amount is not None else None
    if tax and inv.tax_rate is not None:
        tax += f" ({_rate(inv.tax_rate)})"
    rows: list[tuple[str, str, str, str | None, bool]] = [
        ("invoice_number", "header", "Invoice number", inv.invoice_number, False),
        ("revision", "header", "Revision", inv.revision, True),
        ("vendor.name", "header", "Vendor", inv.vendor_name, False),
        ("vendor.former_name", "header", "Formerly", inv.vendor_former_name, True),
        ("vendor.address", "header", "Vendor address", inv.vendor_address, True),
        ("date", "header", "Invoice date", fmt.day(inv.invoice_date) if inv.invoice_date else None, False),
        ("due_date", "header", "Due date", due, False),
        ("currency", "header", "Currency", inv.currency, False),
        ("payment_terms", "header", "Terms", inv.payment_terms, False),
    ]
    for i, line in enumerate(inv.line_items):
        group = f"line {i + 1}"
        rows += [
            (f"line_items[{i}].item", group, "Item", line.item, False),
            (f"line_items[{i}].quantity", group, "Quantity", str(line.quantity), False),
            (f"line_items[{i}].unit_price", group, "Unit price", fmt.money(line.unit_price, cur), False),
            (f"line_items[{i}].amount", group, "Amount",
             fmt.money(line.amount, cur) if line.amount is not None else None, True),
        ]
    rows += [
        ("subtotal", "totals", "Subtotal", fmt.money(inv.subtotal, cur) if inv.subtotal is not None else None, True),
        ("tax_amount", "totals", "Tax", tax, True),
        ("shipping", "totals", "Shipping", fmt.money(inv.shipping, cur) if inv.shipping is not None else None, True),
        ("total", "totals", "Total", fmt.money(inv.total, cur), False),
    ]
    return rows


def field_rows(result: InvoiceResult, state: InvoiceState | None) -> list[FieldRow]:
    """One row per document field, with flags, evidence and correction marks.

    With `state=None` the rows come from `result.invoice` (no raw text, evidence or corrections).
    """
    invoice = state.invoice if state is not None and state.invoice is not None else result.invoice
    totals = state.totals if state is not None else None
    extracted = state.extracted if state is not None else None
    spans = state.evidence_spans if state is not None else {}
    changed = set()
    if state is not None:
        changed = {c.field for c in state.corrections} | {c.field for c in state.normalized_changes}
    by_path: dict[str, list[Flag]] = {}
    for flag in sorted_flags(result.flags):
        for path in flag_fields(flag, invoice, totals):
            by_path.setdefault(path, []).append(flag)
    rows: list[FieldRow] = []
    for path, group, label, value, optional in _build_rows(invoice):
        raw = _raw(extracted, path)
        flags = tuple(by_path.get(path, ()))
        if optional and value is None and raw is None and not flags:
            continue
        severity = min((f.severity for f in flags), key=lambda s: _RANK[s]) if flags else None
        rows.append(
            FieldRow(
                path=path, group=group, label=label, raw=raw, value=value, corrected=path in changed,
                evidence=tuple(spans.get(path, ())), flags=flags, severity=severity,
            )
        )
    return rows


# --------------------------------------------------------------- source lines


def _norm(line: str) -> str:
    return " ".join(line.split())


def source_lines(text: str, rows: Sequence[FieldRow]) -> list[SourceLine]:
    """Number the source text and mark the lines that support a field.

    A line is matched against each row's evidence by its whitespace-collapsed text. A flagged row
    without recorded evidence falls back to lines containing its raw or displayed value.
    """
    lines = text.splitlines()
    by_line: dict[str, list[FieldRow]] = {}
    for row in rows:
        for ev in row.evidence:
            by_line.setdefault(_norm(ev), []).append(row)
    fallback = [r for r in rows if r.flags and not r.evidence and (r.raw or r.value)]
    out: list[SourceLine] = []
    for number, line in enumerate(lines, start=1):
        key = _norm(line)
        matched = list(by_line.get(key, ())) if key else []
        if key:
            for row in fallback:
                needle = _norm(row.raw or row.value or "")
                if needle and needle in key and row not in matched:
                    matched.append(row)
        severities = [r.severity for r in matched if r.severity is not None]
        out.append(
            SourceLine(
                number=number, text=line, paths=_dedupe([r.path for r in matched]),
                severity=min(severities, key=lambda s: _RANK[s]) if severities else None,
            )
        )
    return out


# ----------------------------------------------------------------- transcript


def _tool_views(turn: AgentTurn) -> tuple[ToolCallView, ...]:
    return tuple(
        ToolCallView(
            name=c.name,
            arguments=copy.tool_asked(c.arguments),
            result=c.result,
            answer=copy.tool_summary(c.name, c.arguments, c.result),
        )
        for c in turn.tool_calls
    )


def _flag_code_label(raw: Any) -> str:
    try:
        return copy.FLAG_COPY[FlagCode(raw)].label
    except (ValueError, KeyError):
        return str(raw)


def chat_turns(
    transcript: Sequence[AgentTurn], decision: Decision | None = None, human: HumanReview | None = None
) -> list[ChatTurn]:
    """The VP and Critic exchange, one turn per `AgentTurn`, then the person's resolution if any.

    `decision` is accepted for symmetry with `chat_transcript`; the final line is drawn from it there.
    """
    turns: list[ChatTurn] = []
    for t in transcript:
        out = t.output or {}
        shown_round = t.round + 1
        if t.actor == "vp" and t.kind == "memo":
            turns.append(ChatTurn("vp", copy.t("label.memo"), None, str(out.get("memo", "")), (), _tool_views(t)))
        elif t.actor == "vp":
            points = tuple(
                (_flag_code_label(p.get("code")), str(p.get("response", ""))) for p in out.get("flags_addressed") or []
            )
            answered = tuple(
                (str(a.get("objection", "")), str(a.get("answer", ""))) for a in out.get("objections_answered") or []
            )
            turns.append(
                ChatTurn(
                    "vp", copy.t("label.round", actor="VP", n=shown_round), str(out.get("decision", "")) or None,
                    str(out.get("reasoning", "")), points, _tool_views(t),
                    proposes=_as_ceiling(out.get("decision")), answered=answered, round=shown_round,
                )
            )
        else:
            points = tuple(
                (
                    str(o.get("about", "")),
                    f"{o.get('evidence', '')} " + copy.t("label.requested_change", text=o.get("requested_change", "")),
                )
                for o in out.get("objections") or []
            )
            turns.append(
                ChatTurn(
                    "critic", copy.t("label.round", actor="Critic", n=shown_round), str(out.get("verdict", "")) or None,
                    "", points, _tool_views(t), proposes=_as_ceiling(out.get("proposed_decision")), round=shown_round,
                )
            )
    if human is not None and human.resolution is not None:
        turns.append(
            ChatTurn("person", "Person", human.resolution.action, human.resolution.note, (), ())
        )
    return turns


def _as_ceiling(raw: Any) -> Ceiling | None:
    try:
        return Ceiling(raw) if raw is not None else None
    except ValueError:
        return None


# ------------------------------------------------------- approval sentences


def _critic_rounds(result: InvoiceResult) -> int:
    return sum(1 for t in result.transcript if t.actor == "critic")


def ceiling_sentence(result: InvoiceResult) -> str:
    """Design 5.0 D.1; empty when there is no ceiling (duplicates, errors)."""
    c = result.ceiling
    if c == Ceiling.APPROVE:
        return copy.t("ceiling.APPROVE")
    if c == Ceiling.HUMAN:
        code = result.human.reason if result.human else next(
            (f.code for f in result.flags if f.severity == Severity.HUMAN), None
        )
        return copy.t("ceiling.HUMAN", reason=copy.reason_label(code))
    if c == Ceiling.REJECT:
        labels = ", ".join(copy.labels_for(result.flags, Severity.ERROR, lower=True)) or "see memo"
        return copy.t("ceiling.REJECT", labels=labels)
    return ""


def path_sentence(result: InvoiceResult) -> str:
    """Design 5.0 D.2; empty when there is no decision."""
    d = result.decision
    if d is None:
        return ""
    if d.by == "human":
        return copy.t("path.person")
    if d.by == "rules":
        return copy.t("path.rules")
    if d.by == "deadlock":
        return copy.t("path.deadlock")
    if d.by == "vp":
        return copy.t("path.fast")
    n = _critic_rounds(result)
    return copy.t("path.critic_one") if n == 1 else copy.t("path.critic", n=max(n, 1))


def final_caption(decision: Decision) -> str:
    """Design 5.0 D.5: who decided, in words."""
    return {
        "rules": copy.t("final.rules"),
        "vp": copy.t("final.vp"),
        "vp+critic": copy.t("final.vp_critic"),
        "deadlock": copy.t("final.deadlock"),
        "human": copy.t("final.person"),
    }[decision.by]


def policy_notices(result: InvoiceResult) -> list[str]:
    """Design 5.0 D.4: the policy limits that changed the outcome, as plain lines (icon added by the renderer)."""
    d = result.decision
    if d is None:
        return []
    notices: list[str] = []
    if d.by == "deadlock":
        notices.append(copy.t("notice.deadlock", final=copy.ceiling_label(d.final)))
    if d.clamped:
        ceiling = result.ceiling
        if d.proposed is not None and ceiling is not None and d.proposed > ceiling:
            notices.append(
                copy.t("notice.clamp_ceiling", proposed=copy.ceiling_label(d.proposed), ceiling=copy.ceiling_label(ceiling))
            )
        elif d.proposed == Ceiling.HUMAN and ceiling == Ceiling.APPROVE:
            notices.append(copy.t("notice.human_to_reject"))
        elif any(f.code == FlagCode.OVER_THRESHOLD for f in result.flags):
            notices.append(copy.t("notice.over_threshold"))
    return notices


# ------------------------------------------------------------ review reasons


def _quantities_text(state: InvoiceState | None, invoice: Invoice | None) -> str:
    """"5 GadgetX": the quantities a revision adds, named as the invoice prints them."""
    if state is None or state.dedupe is None or invoice is None:
        return ""
    names = {line.item_key: line.item for line in invoice.line_items}
    return ", ".join(f"{q} {names.get(k, k)}" for k, q in state.dedupe.added_quantities.items())


def reason_text(result: InvoiceResult, state: InvoiceState | None) -> str:
    """Why a person must decide (design 6.3 template, filled from what the result and state hold).

    Falls back to the flag's plain sentence when a template field isn't available (for example
    the monthly figures of `over_budget`, which only the review queue's preview knows).
    """
    code = result.human.reason if result.human else next(
        (f.code for f in result.flags if f.severity == Severity.HUMAN), None
    )
    if code is None or code not in copy.REASON_COPY:
        return ""
    invoice = (state.invoice if state is not None else None) or result.invoice
    fields: dict[str, str] = {
        "currency": result.currency or "USD",
        "vendor": result.vendor or "The vendor",
        "number": result.invoice_number or result.file,
        "former": (invoice.vendor_former_name if invoice else None) or "",
        "error": (state.payment_error if state else None) or "",
        "original_ref": result.invoice_number or result.file,
        "paid_to_date": fmt.money(state.dedupe.paid_to_date, result.currency) if state and state.dedupe else "",
        "added": _quantities_text(state, invoice),
    }
    if code == FlagCode.UNREADABLE_DOCUMENT:
        errors = state.parse_error if state and state.parse_error else None
        if errors is None and state and state.grounding_errors:
            errors = "; ".join(state.grounding_errors)
        flag = next((f for f in result.flags if f.code == code), None)
        fields["error"] = errors or (flag.message if flag else "")
    needed = {
        FlagCode.FOREIGN_CURRENCY: ("currency",),
        FlagCode.REVISION_OF_PAID_INVOICE: ("original_ref", "paid_to_date", "added"),
        FlagCode.CONFLICTING_INVOICE_NUMBER: ("vendor", "number"),
        FlagCode.VENDOR_RENAMED: ("vendor", "former"),
        FlagCode.PAYMENT_FAILED: ("error",),
        FlagCode.UNREADABLE_DOCUMENT: ("error",),
    }.get(code)
    if needed is None or any(not fields[n] for n in needed):
        return copy.FLAG_COPY[code].sentence
    return copy.REASON_COPY[code].why.format(**fields)
