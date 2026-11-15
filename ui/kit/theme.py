"""Semantic palette (design spec 3.4): one meaning per hue, shared by status, severity and ceiling.

Colour appears only where it means something. Every coloured element also carries an icon
and a text label. Native components (`st.badge`, callouts) read the tokens from
`ui/.streamlit/config.toml`; the fills and tints below are for the places that take hex
values instead (`MultiselectColumn(color=...)`, the HTML bar and the source panel).
"""

from dataclasses import dataclass
from typing import Literal

import streamlit as st

from models import Ceiling, Severity, Status, Verdict

Mode = Literal["light", "dark"]
Token = Literal["green", "red", "yellow", "blue", "violet", "gray"]


@dataclass(frozen=True)
class Semantic:
    token: Token
    label: str
    icon: str  # Material Symbol shortcode for st.badge / st.markdown


STATUS: dict[Status, Semantic] = {
    Status.PAID: Semantic("green", "Paid", ":material/check_circle:"),
    Status.CREDIT_DUE: Semantic("green", "Credit due", ":material/undo:"),
    Status.REJECTED: Semantic("red", "Rejected", ":material/block:"),
    Status.PENDING_HUMAN: Semantic("yellow", "Needs a person", ":material/person:"),
    Status.ERROR: Semantic("violet", "System error", ":material/bug_report:"),
    Status.DUPLICATE: Semantic("gray", "Duplicate", ":material/content_copy:"),
    Status.SUPERSEDED: Semantic("gray", "Replaced by revision", ":material/history:"),
    Status.PROCESSING: Semantic("gray", "Processing", ":material/progress_activity:"),
}

SEVERITY: dict[Severity, Semantic] = {
    Severity.ERROR: Semantic("red", "Blocks payment", ":material/block:"),
    Severity.HUMAN: Semantic("yellow", "Needs a person", ":material/person:"),
    Severity.ESCALATION: Semantic("blue", "VP must answer", ":material/edit_note:"),
    Severity.INFO: Semantic("gray", "For information", ":material/info:"),
}

CEILING: dict[Ceiling, Semantic] = {
    Ceiling.APPROVE: Semantic("green", "Approve", ":material/check_circle:"),
    Ceiling.HUMAN: Semantic("yellow", "Person decides", ":material/person:"),
    Ceiling.REJECT: Semantic("red", "Reject", ":material/block:"),
}

# An objection is something the VP must answer in writing (the escalation meaning), so it is blue.
VERDICT: dict[Verdict, Semantic] = {
    Verdict.AGREE: Semantic("green", "Agrees", ":material/check:"),
    Verdict.OBJECT: Semantic("blue", "Objects", ":material/edit_note:"),
}

FILL: dict[Mode, dict[Token, str]] = {
    "light": {
        "green": "#0CA30C", "red": "#9C3D0C", "yellow": "#FAB219",
        "blue": "#2A78D6", "violet": "#7B2D8E", "gray": "#898781",
    },
    "dark": {
        "green": "#4CC46A", "red": "#C0501F", "yellow": "#FAB219",
        "blue": "#3987E5", "violet": "#B05CC0", "gray": "#898781",
    },
}

TINT: dict[Mode, dict[Token, str]] = {
    "light": {
        "green": "#D5ECD4", "red": "#E9DED4", "yellow": "#F7EED6",
        "blue": "#D9E6F1", "violet": "#E5DCE7", "gray": "#E7E8E5",
    },
    "dark": {
        "green": "#213F2A", "red": "#3B261A", "yellow": "#473B18",
        "blue": "#1D3245", "violet": "#37293D", "gray": "#2F322F",
    },
}

_INK, _WHITE = "#1D2733", "#FFFFFF"
# Label colour on a fill: ink on green, amber and grey; white on rust and plum;
# blue takes white in light mode and ink in dark mode.
ON_FILL: dict[Mode, dict[Token, str]] = {
    "light": {"green": _INK, "red": _WHITE, "yellow": _INK, "blue": _WHITE, "violet": _WHITE, "gray": _INK},
    "dark": {"green": _INK, "red": _WHITE, "yellow": _INK, "blue": _INK, "violet": _WHITE, "gray": _INK},
}

INK: dict[Mode, str] = {"light": "#1D2733", "dark": "#E4EAE3"}
PAPER: dict[Mode, str] = {"light": "#F6F8F5", "dark": "#151A18"}
RULE: dict[Mode, str] = {"light": "#CBD4C9", "dark": "#33403A"}


def theme_type() -> Mode:
    """The viewer's theme from `st.context.theme.type`; `light` when unknown or outside a script run.

    Upstream documents that this can be stale on the very first render or right after a
    theme switch. One stale render is accepted.
    """
    try:
        value = st.context.theme.type
    except Exception:
        return "light"
    return "dark" if value == "dark" else "light"


_STYLE = """
<style>
.k-kv { display: grid; grid-template-columns: max-content 1fr; gap: 0.35rem 1.25rem; margin: 0.25rem 0 0.75rem; }
.k-kv dt { font-weight: 600; margin: 0; }
.k-kv dd { margin: 0; overflow-wrap: anywhere; }
.k-meters { list-style: none; margin: 0.25rem 0 0.5rem; padding: 0; display: grid; gap: 0.4rem; }
.k-meters li { display: grid; grid-template-columns: minmax(4.5rem, max-content) 1fr max-content; gap: 0.6rem; align-items: center; }
.k-meter { display: block; height: 0.5rem; border-radius: 2px; background: color-mix(in srgb, currentColor 14%, transparent); }
.k-meter > span { display: block; height: 100%; border-radius: 2px; background: currentColor; }
.k-meters .k-none { grid-column: 2 / 4; opacity: 0.75; }
.k-src { margin: 0; padding: 0; list-style: none; overflow-x: auto; border: 1px solid var(--k-rule, #CBD4C9);
  border-radius: 0.25rem; font-family: "IBM Plex Mono", monospace, monospace; font-size: 0.8125rem; line-height: 1.5; max-height: 32rem; }
.k-src li { display: flex; white-space: pre; min-width: max-content; border-left: 3px solid transparent; }
.k-src .k-n { flex: none; width: 3.25rem; padding-right: 0.75rem; text-align: right; opacity: 0.55; user-select: none; }
@media (max-width: 640px) {
  .k-kv { grid-template-columns: 1fr; gap: 0; }
  .k-kv dd { margin-bottom: 0.5rem; }
}
</style>
"""


def style() -> None:
    """Inject the shared stylesheet (meter list, key/value grid, source panel base).

    Call once at the top of every page. No selector targets Streamlit's generated classes.
    """
    st.html(_STYLE)
