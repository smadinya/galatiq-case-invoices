"""Budget tool."""

from core.values import money_str
from deps import Deps
from tools.registry import tool


@tool()
def get_budget_status(deps: Deps) -> dict:
    """Return this month's budget limit, amount spent and amount remaining in USD."""
    status = deps.repo.budget_status(deps.budget_month, deps.settings.monthly_budget_usd)
    return {
        "month": status.month,
        "limit": money_str(status.limit),
        "spent": money_str(status.spent),
        "remaining": money_str(status.remaining),
    }
