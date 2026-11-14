"""Tool sets handed to the VP and the Critic."""

from deps import Deps
from models import BoundTool, InvoiceState
from tools.evidence import get_pipeline_trace, get_source_evidence, recompute_totals
from tools.inventory import get_stock_level
from tools.ledger import get_budget_status
from tools.policy import get_policy
from tools.vendors import find_similar_invoices, get_vendor_history, get_vendor_master

SHARED_TOOLS = (get_vendor_history, get_stock_level, get_budget_status, get_policy)
CRITIC_ONLY_TOOLS = (
    get_source_evidence, recompute_totals, find_similar_invoices, get_vendor_master, get_pipeline_trace,
)


def _bind(tools, deps: Deps, state: InvoiceState) -> list[BoundTool]:
    return [t.bind(deps=deps, state=state) for t in tools]


def vp_tools(deps: Deps, state: InvoiceState) -> list[BoundTool]:
    return _bind(SHARED_TOOLS, deps, state)


def critic_tools(deps: Deps, state: InvoiceState) -> list[BoundTool]:
    return _bind(SHARED_TOOLS + CRITIC_ONLY_TOOLS, deps, state)
