"""Canned model outputs for the deterministic golden run (DESIGN §14.2).

Extractions copy the source verbatim (OCR artefacts included); code-side normalize
records the changes and validation raises value_normalized. VP/Critic steps script
the † judgement calls, the fast-path approvals and the rejection memos."""

from llm.fake import Script, critic, memo, tool_call, vp
from models import Ceiling, ExtractedInvoice, FlagCode, Verdict

A, H, R = Ceiling.APPROVE, Ceiling.HUMAN, Ceiling.REJECT


def _x(**fields) -> ExtractedInvoice:
    return ExtractedInvoice.model_validate(fields)


_LINES_1011 = [
    {"item": "WidgetA", "quantity": "6", "unit_price": "$250.00", "amount": "$1,500.00"},
    {"item": "WidgetB", "quantity": "3", "unit_price": "$500.00", "amount": "$1,500.00"},
]
_1012 = dict(
    invoice_number="INV 1012",
    vendor={"name": "QuickShip Distributers", "former_name": "FastShip Ltd."},
    date="26-Jan-2O26",
    due_date="25-Feb-2026",
    line_items=[
        {"item": "Widget A", "quantity": "12", "unit_price": "$250", "amount": "$3,000.00"},
        {"item": "WidgetB", "quantity": "7", "unit_price": "$500", "amount": "$3,500.O0"},
        {"item": "Gadget X", "quantity": "4", "unit_price": "$750", "amount": "$3,000.00"},
    ],
    subtotal="$9,500.00", tax_rate="5%", tax_amount="$475.00", total="$9,975.00",
    payment_terms="Net 30",
    notes="Ref PO-20260115. Deliver to warehouse dock B. Contact Jim at ext 4421 with questions.",
    corrections=[
        {"field": "invoice_number", "raw": "INV 1012", "interpreted": "INV-1012"},
        {"field": "date", "raw": "26-Jan-2O26", "interpreted": "2026-01-26"},
        {"field": "line_items[1].amount", "raw": "$3,500.O0", "interpreted": "$3,500.00"},
    ],
)

GOLDEN_EXTRACTIONS: dict[str, ExtractedInvoice] = {
    "invoice_1001.txt": _x(
        invoice_number="INV-1001", vendor={"name": "Widgets Inc."}, date="2026-01-15", due_date="2026-02-01",
        line_items=[{"item": "WidgetA", "quantity": "10", "unit_price": "$250.00"},
                    {"item": "WidgetB", "quantity": "5", "unit_price": "$500.00"}],
        subtotal="$5,000.00", tax_rate="0%", tax_amount="$0.00", total="$5,000.00", payment_terms="Net 15"),
    "invoice_1002.txt": _x(
        invoice_number="1002", vendor={"name": "Gadgets Co."}, date="Jan 30 2026", due_date="2026-01-30",
        line_items=[{"item": "GadgetX", "quantity": "20", "unit_price": "$750"}],
        total="$15,000.00", payment_terms="Net 30",
        corrections=[{"field": "invoice_number", "raw": "1002", "interpreted": "INV-1002"}]),
    "invoice_1003.txt": _x(
        invoice_number="INV-1003", vendor={"name": "Fraudster LLC"}, date="2026-01-20", due_date="yesterday",
        line_items=[{"item": "FakeItem", "quantity": "100", "unit_price": "$1,000.00"}],
        total="$100,000.00", payment_terms="Immediate",
        notes="URGENT - Pay immediately to avoid penalties!!! Wire transfer preferred."),
    "invoice_1008.txt": _x(
        invoice_number="INV-1008", vendor={"name": "NoProd Industries"}, date="2026-01-10", due_date="2026-01-20",
        line_items=[{"item": "SuperGizmo", "quantity": "12", "unit_price": "$400.00"},
                    {"item": "MegaSprocket", "quantity": "6", "unit_price": "$850.00"}],
        total="$9,900.00"),
    "invoice_1010.txt": _x(
        invoice_number="INV-1010", vendor={"name": "Consolidated Materials Group"},
        date="January 27, 2026", due_date="February 26, 2026",
        line_items=[
            {"item": "WidgetA", "quantity": "8", "unit_price": "$250.00", "amount": "$2,000.00"},
            {"item": "WidgetB", "quantity": "4", "unit_price": "$500.00", "amount": "$2,000.00"},
            {"item": "GadgetX", "quantity": "2", "unit_price": "$750.00", "amount": "$1,500.00"},
            {"item": "WidgetA", "quantity": "4", "unit_price": "$300.00", "amount": "$1,200.00", "note": "rush order"},
        ],
        subtotal="$6,700.00", tax_amount="$335.00", shipping="$150.00", total="$7,185.00", payment_terms="Net 30"),
    "invoice_1011.pdf": _x(
        invoice_number="INV-1011", vendor={"name": "Summit Manufacturing Co."}, date="2026-01-20",
        due_date="2026-02-20", line_items=_LINES_1011, total="$3,000.00"),
    "invoice_1011.txt": _x(
        invoice_number="INV-1011", vendor={"name": "Summit Manufacturing Co."}, date="2026-01-20",
        due_date="2026-02-20", line_items=_LINES_1011, subtotal="$3,000.00", tax_rate="0%",
        tax_amount="$0.00", total="$3,000.00", payment_terms="Net 30"),
    "invoice_1012.pdf": _x(**_1012),
    "invoice_1012.txt": _x(**_1012),
    "invoice_1013.pdf": _x(
        invoice_number="INV-1013", vendor={"name": "Atlas Industrial Supply"}, date="2026-01-24",
        due_date="2026-03-24",
        line_items=[
            {"item": "WidgetA", "quantity": "15", "unit_price": "$250.00", "amount": "$3,750.00"},
            {"item": "WidgetB", "quantity": "10", "unit_price": "$500.00", "amount": "$5,000.00"},
            {"item": "GadgetX", "quantity": "5", "unit_price": "$750.00", "amount": "$3,750.00"},
            {"item": "WidgetA", "quantity": "5", "unit_price": "$240.00", "amount": "$1,200.00", "note": "Volume discount"},
            {"item": "WidgetB", "quantity": "8", "unit_price": "$480.00", "amount": "$3,840.00", "note": "Volume discount"},
            {"item": "GadgetX", "quantity": "3", "unit_price": "$750.00", "amount": "$2,250.00", "note": "Expedited"},
            {"item": "WidgetA", "quantity": "2", "unit_price": "$250.00", "amount": "$500.00", "note": "Replacement"},
            {"item": "GadgetX", "quantity": "1", "unit_price": "$750.00", "amount": "$750.00", "note": "Sample"},
        ],
        subtotal="$21,040.00", tax_rate="7%", tax_amount="$1,472.80", total="$22,562.80", payment_terms="Net 60"),
}

FAST_PATH = ("invoice_1001.txt", "invoice_1004.json", "invoice_1006.csv", "invoice_1011.pdf", "invoice_1015.csv")
MEMO_ROWS = ("invoice_1002.txt", "invoice_1003.txt", "invoice_1008.txt", "invoice_1007.csv", "invoice_1009.json",
             "invoice_1013.json", "invoice_1016.json")


def golden_script() -> Script:
    s: Script = {(name, "extraction"): [ex] for name, ex in GOLDEN_EXTRACTIONS.items()}
    for name in FAST_PATH:
        s[(name, "vp")] = [vp(A, "No flags; items in stock; within budget; under $9,000. Approve.")]
    for name in MEMO_ROWS:
        s[(name, "vp_memo")] = [memo("Rejected: the hard-rule (error) flags listed on this invoice block payment.")]
    # † 1004 R1: revision of a paid invoice -> recommend a person
    s[("invoice_1004_revised.json", "vp")] = [vp(H, "R1 of paid INV-1004 adds 5 GadgetX per a PO amendment; "
                                                     "paying the $4,050 difference needs a person to confirm the amendment.")]
    s[("invoice_1004_revised.json", "critic")] = [critic(Verdict.AGREE, H)]
    # † 1005: unknown vendor over $10K -> VP rejects (one tool call first)
    s[("invoice_1005.json", "vp")] = [
        tool_call("call_1005_vp_0", "get_vendor_history", vendor="Global Supply Chain Partners"),
        vp(R, "Vendor is not in the vendor master and has no history; over $10K. Reject until onboarded.",
           flags=[(FlagCode.UNKNOWN_VENDOR, "Not onboarded and no history; reject.")]),
    ]
    s[("invoice_1005.json", "critic")] = [critic(Verdict.AGREE, R)]
    # † 1010: rush line +20% -> Critic objects once, VP answers, Critic agrees (exercises round 2)
    rush = (FlagCode.PRICE_ABOVE_CATALOG, "Rush line WidgetA at $300 vs $250 catalog (+20%) is a labelled rush order.")
    s[("invoice_1010.txt", "vp")] = [
        vp(A, "One rush line 20% over catalog, labelled rush order; math and stock check out. Approve.", flags=[rush]),
        vp(A, "Rush premium is $200 on 4 units for an explicitly labelled rush order; everything else at catalog.",
           flags=[rush], answers=[("Justify the rush premium or reject.",
                                   "The line is labelled rush order and the premium is limited to that line ($200).")]),
    ]
    s[("invoice_1010.txt", "critic")] = [
        critic(Verdict.OBJECT, R, objections=[("price_above_catalog",
                                               "WidgetA (rush order) 4 x $300.00 vs catalog $250.00",
                                               "Justify the rush premium or reject.")]),
        critic(Verdict.AGREE, A),
    ]
    # † 1012 and 1014: human-ceiling rows -> recommend HUMAN (DESIGN §18)
    s[("invoice_1012.pdf", "vp")] = [vp(H, "Invoice says the vendor was formerly FastShip Ltd.; the rename must be "
                                           "verified by calling FastShip's contact on file.")]
    s[("invoice_1012.pdf", "critic")] = [critic(Verdict.AGREE, H)]
    s[("invoice_1014.xml", "vp")] = [vp(H, "EUR invoice; a person must set the exchange rate before payment.")]
    s[("invoice_1014.xml", "critic")] = [critic(Verdict.AGREE, H)]
    return s
