"""Risk scoring. Tier 1 has no risk score: this node is a pass-through."""

from typing import Any

from deps import Deps
from models import InvoiceState


def score_risk(state: InvoiceState, deps: Deps) -> dict[str, Any]:
    """Tier 3 adds an additive risk score; tier 1 changes nothing."""
    return {}
