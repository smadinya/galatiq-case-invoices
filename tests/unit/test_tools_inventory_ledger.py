from decimal import Decimal

from models import InvoiceUpdate
from tools.inventory import get_stock_level
from tools.ledger import get_budget_status


def test_stock_found_matches_loosely(make_deps):
    out = get_stock_level(make_deps(), "widget a")
    assert out == {"item": "widget a", "found": True, "stock": 90, "unit_price": "250.00"}


def test_stock_unknown_with_hint(make_deps):
    out = get_stock_level(make_deps(), "WidgetC")
    assert out["found"] is False and "stock" not in out
    assert out["did_you_mean"] in ("WidgetA", "WidgetB", None)


def test_stock_typo_hint(make_deps):
    assert get_stock_level(make_deps(), "GadgetXX")["did_you_mean"] == "GadgetX"


def test_budget_status(make_deps, repo, make_state, settings):
    deps = make_deps()
    out = get_budget_status(deps)
    limit = settings.monthly_budget_usd
    assert out == {"month": "2026-01", "limit": f"{limit:.2f}", "spent": "0.00", "remaining": f"{limit:.2f}"}
    state = make_state()
    with repo.transaction():
        repo._conn.execute(
            "INSERT INTO budget_ledger (month, amount_usd, invoice_id, ts) VALUES (?,?,?,?)",
            ("2026-01", "1234.50", state.invoice_id, "2026-01-05T00:00:00"),
        )
    out = get_budget_status(deps)
    assert out["spent"] == "1234.50"
    assert Decimal(out["remaining"]) == limit - Decimal("1234.50")
