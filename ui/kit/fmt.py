"""Display formatting for the dashboard (design spec 3.5). Pure functions, no Streamlit.

Money is always `Decimal` and is never passed through `float()`. Untrusted text
(document text, vendor names, notes, model output) must go through `md` for
Markdown surfaces (`st.markdown`, captions, labels) or `esc` for `st.html`.
"""

import datetime as dt
import html
import re
from decimal import ROUND_HALF_UP, Decimal

_CENT = Decimal("0.01")
_SYMBOLS = {"USD": "$", "EUR": "€", "GBP": "£"}
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

# Every ASCII character Markdown (CommonMark + GFM + Streamlit's directives) can act on.
# ':' stops :red[...] directives and autolinks, '$' stops LaTeX, '&' stops entities.
_MD_SPECIALS = re.compile(r"([\\`*_{}\[\]()#+\-.!|<>~$&:])")


def money(value: Decimal | None, currency: str | None = "USD") -> str:
    """`$9,975.00`, `€4,125.00`, `£12.00`, `-$250.00`, `1,234.00 CHF`; `-` for None."""
    if value is None:
        return "-"
    amount = Decimal(value).quantize(_CENT, rounding=ROUND_HALF_UP)
    sign = "-" if amount < 0 else ""
    text = f"{abs(amount):,.2f}"
    code = (currency or "USD").upper()
    symbol = _SYMBOLS.get(code)
    return f"{sign}{symbol}{text}" if symbol else f"{sign}{text} {code}"


def md(text: str) -> str:
    """Escape `text` so `st.markdown`, `st.caption` and widget labels render it literally."""
    return _MD_SPECIALS.sub(r"\\\1", str(text))


def esc(text: str) -> str:
    """Escape `text` for `st.html` (`\\$` would show literally there, so `$` becomes an entity)."""
    return html.escape(str(text)).replace("$", "&#36;")


def _ratio(n: int, d: int, places: int) -> str:
    if d == 0:
        return "-"
    quantum = Decimal(1).scaleb(-places)
    return f"{(Decimal(n) * 100 / Decimal(d)).quantize(quantum, rounding=ROUND_HALF_UP)}%"


def pct(n: int, d: int) -> str:
    """`82%` (rounded half up); `-` when the denominator is zero."""
    return _ratio(n, d, 0)


def pct1(n: int, d: int) -> str:
    """`82.4%`; `-` when the denominator is zero."""
    return _ratio(n, d, 1)


def seconds(ms: int | float | None) -> str:
    """`1.2 s`; two decimals under 0.1 s (`0.06 s`), none from 10 s up (`28 s`); `-` for None."""
    if ms is None:
        return "-"
    s = Decimal(str(ms)) / 1000
    if s >= 10:
        return f"{s.quantize(Decimal(1), rounding=ROUND_HALF_UP)} s"
    if s < Decimal("0.1"):
        return f"{s.quantize(_CENT, rounding=ROUND_HALF_UP)} s"
    return f"{s.quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)} s"


def tokens(n: int | float | None) -> str:
    """`3,020`; `-` for None."""
    if n is None:
        return "-"
    return f"{int(Decimal(str(n)).quantize(Decimal(1), rounding=ROUND_HALF_UP)):,}"


def tokens_compact(n: int | float | None) -> str:
    """`9.1K`, `1.2M`; plain digits under 1,000; `-` for None."""
    if n is None:
        return "-"
    value = Decimal(str(n))
    if abs(value) < 1000:
        return tokens(value)
    for divisor, suffix in ((Decimal(1_000_000), "M"), (Decimal(1000), "K")):
        if abs(value) >= divisor:
            return f"{(value / divisor).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)}{suffix}"
    return tokens(value)  # pragma: no cover


def day(d: dt.date | None) -> str:
    """`25 Feb 2026` (fixed English month names, independent of the machine's locale)."""
    if d is None:
        return "-"
    return f"{d.day} {_MONTHS[d.month - 1]} {d.year}"


def when(ts: dt.datetime | None) -> str:
    """`15 Nov 2026, 00:39`.

    The repository stores timezone-aware UTC. A timezone-aware value is shown in the
    machine's local time (the operator's clock); a naive value is shown as it is.
    """
    if ts is None:
        return "-"
    if ts.tzinfo is not None:
        ts = ts.astimezone()
    return f"{day(ts.date())}, {ts:%H:%M}"


_DECIMAL_TEXT = re.compile(r"^(?:\d+(?:\.\d*)?|\.\d+)$")


def parse_fx_rate(text: str) -> Decimal | None:
    """A positive plain decimal such as `1.08`; None for anything else (`0`, `-1`, `abc`, `nan`, `inf`, empty)."""
    cleaned = (text or "").strip()
    if not _DECIMAL_TEXT.match(cleaned):
        return None
    value = Decimal(cleaned)
    return value if value > 0 else None
