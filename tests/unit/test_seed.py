from decimal import Decimal

from core.text import vendor_key
from db.repo import Repo
from db.seed import VENDOR_MASTER, apply_seed, reset_database


def test_demo_seed(tmp_path):
    repo = reset_database(tmp_path / "a.db", "demo")
    assert repo.stock_levels() == {"WidgetA": 90, "WidgetB": 50, "GadgetX": 13, "FakeItem": 0}
    prices = {i.item: i.unit_price for i in repo.list_inventory()}
    assert prices == {
        "WidgetA": Decimal("250.00"), "WidgetB": Decimal("500.00"),
        "GadgetX": Decimal("750.00"), "FakeItem": Decimal("1000.00"),
    }
    assert repo.seed_name() == "demo"
    assert repo.get_item_by_key("widgeta").stock == 90
    ledger = repo.stock_ledger()
    assert len(ledger) == 4 and all(e.reason == "seed" for e in ledger)
    repo.close()


def test_readme_seed(tmp_path):
    repo = reset_database(tmp_path / "a.db", "readme")
    assert repo.stock_levels() == {"WidgetA": 15, "WidgetB": 10, "GadgetX": 5, "FakeItem": 0}
    assert {i.item: i.unit_price for i in repo.list_inventory()}["GadgetX"] == Decimal("750.00")
    assert repo.seed_name() == "readme"
    repo.close()


def test_vendor_master(repo):
    vendors = repo.list_vendors()
    assert len(vendors) == 11 and all(v.status == "approved" for v in vendors)
    assert sum(1 for v in vendors if v.address) == 3
    assert len({v.normalized_name for v in vendors}) == 11
    assert all(v.normalized_name == vendor_key(v.name) for v in vendors)
    assert repo.find_vendor_by_key("fastship").name == "FastShip Ltd."
    assert len(VENDOR_MASTER) == 11


def test_reset_database_wipes_previous_data(tmp_path):
    path = tmp_path / "a.db"
    repo = reset_database(path, "demo")
    repo.create_invoice(trace_id="t", file_path="p", file_name="f", file_hash="h")
    repo.audit(trace_id="t", actor="system", event="x", payload={})
    with repo.transaction():
        repo._conn.execute("UPDATE inventory SET stock=1 WHERE item='WidgetA'")
    repo.close()
    repo = reset_database(path, "readme")
    assert repo.list_invoices() == [] and repo.audit_for_trace("t") == []
    assert repo.stock_levels()["WidgetA"] == 15
    assert repo.seed_name() == "readme"
    assert len(repo.list_vendors()) == 11
    repo.close()


def test_apply_seed_rejects_unknown(repo):
    import pytest

    fresh = Repo(":memory:")
    fresh.init_schema()
    with pytest.raises(ValueError):
        apply_seed(fresh, "nope")
    assert fresh.seed_name() is None
