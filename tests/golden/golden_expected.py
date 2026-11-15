"""DESIGN §14.2 as data. If the seed or a † decision changes, edit this file and db/seed.py only."""
import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal as D

from models import Ceiling, Status

AS_OF = dt.date(2026, 1, 31)
MONTH = "2026-01"
BUDGET_LIMIT = D("50000")
APPROVE, HUMAN, REJECT = Ceiling.APPROVE, Ceiling.HUMAN, Ceiling.REJECT


@dataclass(frozen=True)
class GoldenRow:
    file: str
    status: Status
    flags: tuple[str, ...]            # multiset of codes; compare with collections.Counter
    ceiling: Ceiling | None
    paid: D | None = None             # Receipt.amount; None when nothing was paid
    path: str = "fast"                # "fast" | "critic" | "memo" | "dup"
    decided_by: str | None = None     # Decision.by
    critic_rounds: int = 0            # InvoiceState.round
    address_check: str | None = None
    duplicate_of: str | None = None
    scripted: bool = False            # † row
    human_reason: str | None = None
    human_recommendation: Ceiling | None = None
    human_payment_amount: D | None = None
    evidence: dict = field(default_factory=dict)   # {flag_code: {key: expected}} for the first flag of that code


GOLDEN_ROWS: tuple[GoldenRow, ...] = (
    GoldenRow("invoice_1001.txt", Status.PAID, (), APPROVE, D("5000.00"), "fast", "vp", 0, "not_on_invoice"),
    GoldenRow("invoice_1002.txt", Status.REJECTED,
              ("insufficient_stock", "over_threshold", "past_due", "terms_mismatch", "value_normalized"),
              REJECT, None, "memo", "rules", 0, "no_record",
              evidence={"insufficient_stock": {"item": "GadgetX", "requested": 20, "available": 13}}),
    GoldenRow("invoice_1003.txt", Status.REJECTED,
              ("zero_stock_item", "invalid_due_date", "unknown_vendor", "over_threshold"),
              REJECT, None, "memo", "rules", 0, "no_record"),
    GoldenRow("invoice_1004.json", Status.PAID, (), APPROVE, D("1890.00"), "fast", "vp", 0, "verified"),
    GoldenRow("invoice_1004_revised.json", Status.PENDING_HUMAN, ("revision_of_paid_invoice",), HUMAN,
              None, "critic", "vp+critic", 1, "verified", scripted=True,
              human_reason="revision_of_paid_invoice", human_recommendation=HUMAN, human_payment_amount=D("4050.00")),
    GoldenRow("invoice_1005.json", Status.REJECTED, ("unknown_vendor", "over_threshold"), APPROVE,
              None, "critic", "vp+critic", 1, "no_record", scripted=True),
    GoldenRow("invoice_1006.csv", Status.PAID, (), APPROVE, D("2750.00"), "fast", "vp", 0, "no_record"),
    GoldenRow("invoice_1007.csv", Status.PAID, ("math_mismatch", "over_threshold"), APPROVE, D("15525.00"),
              "critic", "vp+critic", 1, "no_record", scripted=True,
              evidence={"math_mismatch": {"difference": "-110.00", "stated_total": "15525.00",
                                          "recomputed_total": "15635.00"}}),
    GoldenRow("invoice_1008.txt", Status.REJECTED,
              ("unknown_item", "unknown_item", "unknown_vendor", "near_threshold", "past_due"),
              REJECT, None, "memo", "rules", 0, "no_record"),
    GoldenRow("invoice_1009.json", Status.REJECTED,
              ("non_positive_quantity", "missing_vendor", "invalid_due_date", "math_mismatch"),
              REJECT, None, "memo", "rules", 0, "no_record"),
    GoldenRow("invoice_1010.txt", Status.PAID, ("price_above_catalog",), APPROVE, D("7185.00"),
              "critic", "vp+critic", 2, "no_record", scripted=True,
              evidence={"price_above_catalog": {"lines": [{"index": 3, "item": "WidgetA", "unit_price": "300.00",
                                                           "catalog": "250.00", "pct": "20.0"}]}}),
    GoldenRow("invoice_1011.pdf", Status.PAID, (), APPROVE, D("3000.00"), "fast", "vp", 0, "no_record"),
    GoldenRow("invoice_1011.txt", Status.DUPLICATE, (), None, None, "dup", None, 0, None,
              duplicate_of="invoice_1011.pdf"),
    GoldenRow("invoice_1012.pdf", Status.PENDING_HUMAN, ("vendor_renamed", "near_threshold", "value_normalized"),
              HUMAN, None, "critic", "vp+critic", 1, "no_record", scripted=True,
              human_reason="vendor_renamed", human_recommendation=HUMAN, human_payment_amount=D("9975.00")),
    GoldenRow("invoice_1012.txt", Status.DUPLICATE, (), None, None, "dup", None, 0, None,
              duplicate_of="invoice_1012.pdf"),
    GoldenRow("invoice_1013.json", Status.REJECTED,
              ("insufficient_stock", "over_threshold", "price_below_catalog", "math_mismatch"),  # math_mismatch: Q1
              REJECT, None, "memo", "rules", 0, "verified",
              evidence={"insufficient_stock": {"item": "GadgetX", "requested": 9, "available": 8},
                        "math_mismatch": {"difference": "50.00"}}),
    GoldenRow("invoice_1013.pdf", Status.DUPLICATE, (), None, None, "dup", None, 0, None,
              duplicate_of="invoice_1013.json"),
    GoldenRow("invoice_1014.xml", Status.PENDING_HUMAN, ("foreign_currency",), HUMAN, None,
              "critic", "vp+critic", 1, "no_record", scripted=True,
              human_reason="foreign_currency", human_recommendation=HUMAN, human_payment_amount=D("4125.00")),
    GoldenRow("invoice_1015.csv", Status.PAID, (), APPROVE, D("6500.00"), "fast", "vp", 0, "no_record"),
    GoldenRow("invoice_1016.json", Status.REJECTED, ("unknown_item",), REJECT, None, "memo", "rules", 0, "verified",
              evidence={"unknown_item": {"item": "WidgetC"}}),
)

# Ledger after each payment: (file, WidgetA, WidgetB, GadgetX, FakeItem, budget spent)
GOLDEN_LEDGER = (
    ("start",             90, 50, 13, 0, D("0")),
    ("invoice_1001.txt",  80, 45, 13, 0, D("5000.00")),
    ("invoice_1004.json", 77, 43, 13, 0, D("6890.00")),
    ("invoice_1006.csv",  72, 40, 13, 0, D("9640.00")),
    ("invoice_1007.csv",  52, 25, 10, 0, D("25165.00")),
    ("invoice_1010.txt",  40, 21,  8, 0, D("32350.00")),
    ("invoice_1011.pdf",  34, 18,  8, 0, D("35350.00")),
    ("invoice_1015.csv",  24, 13,  6, 0, D("41850.00")),
)

GOLDEN_TOTALS = {
    "counts": {"PAID": 7, "REJECTED": 7, "PENDING_HUMAN": 3, "DUPLICATE": 3},
    "distinct_documents": 17,
    "decided_without_person": 14,
    "spent": D("41850.00"),
    "queue_refs": ("INV-1004 R1", "INV-1012", "INV-1014"),
}

# Post-batch queue walk-through (coordinator, replaces the old $55,875 narrative). Run in order on the batch DB.
# Each step: (ref, action, kwargs, expect). "raises" steps must leave the queue and ledger unchanged.
POST_BATCH_STEPS = (
    ("INV-1012", "approve", {"note": "Called FastShip on the number on file; rename confirmed"},
     {"raises": "OverspendConfirmationRequired", "overspend_usd": D("1825.00")}),
    ("INV-1012", "approve", {"note": "Called FastShip on the number on file; rename confirmed", "confirm_overspend": True},
     {"status": "PAID", "paid": D("9975.00"), "currency": "USD", "stock": (12, 6, 2, 0), "spent": D("51825.00"),
      "alias": ("QuickShip Distributers", "FastShip Ltd.")}),
    ("INV-1004", "approve", {"note": "PO amendment confirmed"},
     {"status": "REJECTED", "human_outcome": "rejected_stock",
      "shortfalls": [{"item": "GadgetX", "needed": 5, "available": 2}],
      "stock": (12, 6, 2, 0), "spent": D("51825.00")}),
    ("INV-1014", "approve", {"note": "Rate from treasury"}, {"raises": "FxRateRequired"}),
    ("INV-1014", "approve", {"note": "Rate from treasury", "fx_rate": D("1.08")},
     {"raises": "OverspendConfirmationRequired", "overspend_usd": D("6280.00")}),
    ("INV-1014", "approve", {"note": "Rate from treasury", "fx_rate": D("1.08"), "confirm_overspend": True},
     {"status": "PAID", "paid": D("4125.00"), "currency": "EUR", "fx_rate": D("1.08"), "amount_usd": D("4455.00"),
      "stock": (8, 0, 2, 0), "spent": D("56280.00")}),
)
# After the steps: list_queue() is empty.

ALT_ORDER_STEPS = (
    ("INV-1004", "approve", {"note": "PO amendment confirmed"},          # 41,850 + 4,050 = 45,900: no confirmation needed
     {"status": "PAID", "paid": D("4050.00"), "stock": (24, 13, 1, 0), "spent": D("45900.00")}),
    ("INV-1012", "approve", {"note": "Rename confirmed"},
     {"status": "REJECTED", "human_outcome": "rejected_stock",
      "shortfalls": [{"item": "GadgetX", "needed": 4, "available": 1}], "stock": (24, 13, 1, 0), "spent": D("45900.00")}),
)
