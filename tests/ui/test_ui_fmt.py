import datetime as dt
import re
from decimal import Decimal as D

import pytest

from ui.kit import fmt


def test_money():
    assert fmt.money(D("9975")) == "$9,975.00"
    assert fmt.money(D("4125"), "EUR") == "€4,125.00"
    assert fmt.money(D("12"), "GBP") == "£12.00"
    assert fmt.money(D("-250"), "USD") == "-$250.00"
    assert fmt.money(D("1234"), "CHF") == "1,234.00 CHF"
    assert fmt.money(D("-1234.5"), "CHF") == "-1,234.50 CHF"
    assert fmt.money(None) == "-"
    assert fmt.money(D("2.345")) == "$2.35"
    assert fmt.money(D("5"), None) == "$5.00"


def test_md_escapes_everything_literal():
    out = fmt.md("![x](http://e/a.png) $5")
    assert out == r"\!\[x\]\(http\://e/a\.png\) \$5"
    stripped = re.sub(r"\\.", "", out)
    assert not re.search(r"[\\`*_{}\[\]()#+\-.!|<>~$&:]", stripped)
    assert "[" not in re.sub(r"\\.", "", fmt.md(":red[x] <b>"))


def test_esc():
    out = fmt.esc("<img src=x onerror=1> $5")
    assert "<" not in out and "$" not in out
    assert "&lt;img" in out and "&#36;5" in out


def test_pct():
    assert fmt.pct(14, 17) == "82%"
    assert fmt.pct(1, 8) == "13%"  # half up
    assert fmt.pct(1, 0) == "-"
    assert fmt.pct1(14, 17) == "82.4%"
    assert fmt.pct1(1, 0) == "-"


def test_seconds_tokens():
    assert fmt.seconds(1234) == "1.2 s"
    assert fmt.seconds(60) == "0.06 s"
    assert fmt.seconds(28000) == "28 s"
    assert fmt.seconds(None) == "-"
    assert fmt.tokens(3020) == "3,020"
    assert fmt.tokens(None) == "-"
    assert fmt.tokens_compact(9120) == "9.1K"
    assert fmt.tokens_compact(950) == "950"
    assert fmt.tokens_compact(1_250_000) == "1.3M"
    assert fmt.tokens_compact(None) == "-"


def test_dates():
    assert fmt.day(dt.date(2026, 2, 25)) == "25 Feb 2026"
    assert fmt.day(None) == "-"
    assert fmt.when(dt.datetime(2026, 11, 15, 0, 39)) == "15 Nov 2026, 00:39"
    aware = dt.datetime(2026, 11, 15, 0, 39, tzinfo=dt.timezone.utc)
    local = aware.astimezone()
    assert fmt.when(aware) == f"{fmt.day(local.date())}, {local:%H:%M}"
    assert fmt.when(None) == "-"


@pytest.mark.parametrize("text", ["0", "-1", "abc", "nan", "inf", "", " ", "1e5", "1,08"])
def test_fx_rate_rejects(text):
    assert fmt.parse_fx_rate(text) is None


def test_fx_rate_accepts():
    assert fmt.parse_fx_rate("1.08") == D("1.08")
    assert fmt.parse_fx_rate(" 1.0800 ") == D("1.0800")
