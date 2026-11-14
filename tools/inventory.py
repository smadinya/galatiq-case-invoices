"""Inventory lookup tool."""

from core.text import clean_item_key, did_you_mean
from core.values import money_str
from deps import Deps
from tools.registry import tool


@tool()
def get_stock_level(deps: Deps, item: str) -> dict:
    """Look up the current stock and catalog unit price of an inventory item.

    Matching ignores case, whitespace and punctuation only.
    """
    found = deps.repo.get_item_by_key(clean_item_key(item))
    if found is not None:
        return {"item": item, "found": True, "stock": found.stock, "unit_price": money_str(found.unit_price)}
    candidates = {clean_item_key(i.item): i.item for i in deps.repo.list_inventory()}
    return {"item": item, "found": False, "did_you_mean": did_you_mean(clean_item_key(item), candidates)}
