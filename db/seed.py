"""Starting stock and vendor master. Package A owns this file.

This is the one place the starting stock lives.
"""

import argparse
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from core.text import clean_item_key, vendor_key
from db.repo import Repo
from models import InventoryItem


@dataclass(frozen=True)
class InventorySeed:
    item: str
    stock: int
    unit_price: Decimal
    category: str


@dataclass(frozen=True)
class VendorSeed:
    name: str
    address: str | None
    payment_terms: str | None
    status: str = "approved"


STARTING_STOCK: dict[str, tuple[InventorySeed, ...]] = {
    "demo": (
        InventorySeed("WidgetA", 90, Decimal("250.00"), "widget"),
        InventorySeed("WidgetB", 50, Decimal("500.00"), "widget"),
        InventorySeed("GadgetX", 13, Decimal("750.00"), "gadget"),
        InventorySeed("FakeItem", 0, Decimal("1000.00"), "other"),
    ),
    "readme": (
        InventorySeed("WidgetA", 15, Decimal("250.00"), "widget"),
        InventorySeed("WidgetB", 10, Decimal("500.00"), "widget"),
        InventorySeed("GadgetX", 5, Decimal("750.00"), "gadget"),
        InventorySeed("FakeItem", 0, Decimal("1000.00"), "other"),
    ),
}

VENDOR_MASTER: tuple[VendorSeed, ...] = (
    VendorSeed("Widgets Inc.", "100 Main St, Chicago, IL 60601", None),
    VendorSeed("Gadgets Co.", None, "Net 30"),
    VendorSeed("Precision Parts Ltd.", "742 Evergreen Terrace, Springfield, IL 62704", "Net 30"),
    VendorSeed("Acme Industrial Supplies", None, "Net 15"),
    VendorSeed("MegaWidgets Corp", None, None),
    VendorSeed("Consolidated Materials Group", None, "Net 30"),
    VendorSeed("Summit Manufacturing Co.", None, "Net 30"),
    VendorSeed("FastShip Ltd.", None, "Net 30"),
    VendorSeed("Atlas Industrial Supply", "500 Commerce Blvd, Detroit, MI 48201", "Net 60"),
    VendorSeed("TechParts International", None, "Net 30"),
    VendorSeed("Reliable Components Inc.", None, None),
)


def apply_seed(repo: Repo, seed: str = "demo") -> None:
    if seed not in STARTING_STOCK:
        raise ValueError(f"unknown seed {seed!r}; choose from {sorted(STARTING_STOCK)}")
    with repo.transaction():
        for s in STARTING_STOCK[seed]:
            repo.insert_inventory(
                InventoryItem(
                    item=s.item, item_key=clean_item_key(s.item), stock=s.stock,
                    unit_price=s.unit_price, category=s.category,
                )
            )
        for v in VENDOR_MASTER:
            repo.insert_vendor(
                name=v.name, normalized_name=vendor_key(v.name), status=v.status,
                address=v.address, payment_terms=v.payment_terms,
            )
        repo.set_meta("seed", seed)


def reset_database(db_path: Path, seed: str = "demo") -> Repo:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm", "-journal"):
        Path(str(db_path) + suffix).unlink(missing_ok=True)
    repo = Repo(db_path)
    repo.init_schema()
    apply_seed(repo, seed)
    return repo


def main() -> None:
    from config.settings import load_settings

    parser = argparse.ArgumentParser(description="Reset acme.db and apply a starting seed.")
    parser.add_argument("--seed", choices=sorted(STARTING_STOCK), default="demo")
    parser.add_argument("--db", type=Path, default=None)
    args = parser.parse_args()
    db = args.db or load_settings().db_path
    reset_database(db, args.seed).close()
    print(f"reset {db} with seed {args.seed}")


if __name__ == "__main__":
    main()
