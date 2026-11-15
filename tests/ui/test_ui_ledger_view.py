"""Ledger logic: steps against DESIGN 14.2, frames, chart specs, payments rows, summary and CSV."""

import sys
from decimal import Decimal as D
from pathlib import Path

import pytest

from ui.kit import ledger_view as lv
from ui.kit.runtime import UiConfig

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "golden"))
from golden_expected import GOLDEN_LEDGER, POST_BATCH_STEPS  # noqa: E402

ITEMS = ("WidgetA", "WidgetB", "GadgetX", "FakeItem")


def read(rt, cfg=None):
    cfg = cfg or rt.default_config
    with rt.reader(cfg) as r:
        inv = r.inventory()
        return {
            "steps": lv.ledger_steps(r.stock_ledger(), r.budget_ledger(), r.invoices(), [i.item for i in inv]),
            "budget": r.budget_status(), "payments": r.payments(), "invoices": r.invoices(), "stock": r.stock_ledger(),
        }


def as_tuple(step):
    return (*(step.stock[i] for i in ITEMS), step.spent)


def test_golden_steps_match_design(ui_rt_batch):
    data = read(ui_rt_batch)
    steps = data["steps"]
    assert [as_tuple(s) for s in steps] == [(*row[1:5], row[5]) for row in GOLDEN_LEDGER]
    assert [s.label for s in steps][0] == "Start"
    assert [s.label for s in steps][1:] == ["INV-1001", "INV-1004", "INV-1006", "INV-1011", "INV-1015"]
    assert [s.amount_usd for s in steps][1:] == [D("5000"), D("1890"), D("2750"), D("3000"), D("6500")]


def test_queue_steps_extend_ledger_and_fx_row(ui_rt_batch):
    rt = ui_rt_batch
    for ref, action, kwargs, expect in POST_BATCH_STEPS:
        if "raises" in expect:
            continue
        job = rt.start_resolve(
            rt.default_config, ref, action=action, note=kwargs["note"], fx_rate=kwargs.get("fx_rate"),
            confirm_overspend=False,
        )
        assert job.snapshot().error is None
    data = read(rt)
    assert [as_tuple(s) for s in data["steps"][-3:]] == [
        (44, 25, 7, 0, D("29115")), (44, 25, 2, 0, D("33165")), (40, 19, 2, 0, D("37620")),
    ]
    assert len(data["steps"]) == 9
    rows = lv.payment_rows(data["payments"], data["invoices"], [s.invoice_id for s in data["steps"][1:]])
    eur = [r for r in rows if r.currency == "EUR"]
    assert len(eur) == 1
    assert (eur[0].amount, eur[0].fx_rate, eur[0].amount_usd, eur[0].ref) == ("4,125.00", "1.08", "$4,455.00", "INV-1014")
    assert [r.fx_rate for r in rows if r.currency == "USD"] == [None] * 7


def test_readme_reset_start_row(ui_rt):
    assert ui_rt.start_reset(ui_rt.default_config, "readme").snapshot().error is None
    steps = read(ui_rt)["steps"]
    assert len(steps) == 1
    assert as_tuple(steps[0]) == (15, 10, 5, 0, D("0"))


def test_failed_payment_row(ui_rt):
    cfg = UiConfig(ui_rt.default_config.as_of, payment_fail_rate=1.0)
    ui_rt.start_invoice(cfg, ui_rt.samples_dir / "invoice_1001.txt")
    data = read(ui_rt, cfg)
    rows = lv.payment_rows(data["payments"], data["invoices"], [s.invoice_id for s in data["steps"][1:]])
    assert len(rows) == 1
    assert rows[0].attempts == 4 and rows[0].error
    assert rows[0].status == "Failed after 4 attempts"
    assert rows[0].amount_usd == "-"
    assert len(data["steps"]) == 1  # a failed payment moves neither stock nor budget
    text = lv.summary_sentence(data["budget"], data["payments"])
    assert text.endswith("0 payments, 1 failed.")


def test_frames_and_chart_specs(ui_rt_batch):
    data = read(ui_rt_batch)
    steps = data["steps"]
    bf = lv.budget_frame(steps, D("50000"))
    assert list(bf["label"]) == ["Start", "INV-1001", "INV-1004", "INV-1006", "INV-1011", "INV-1015", "Now"]
    assert list(bf["spent"])[-1] == 19140.0 and list(bf["spent"])[-2] == 19140.0
    assert list(bf["payment"]) == [False, True, True, True, True, True, False]
    sf = lv.stock_frame(steps)
    wa = sf[sf["item"] == "WidgetA"]
    assert list(wa["stock"]) == [90, 80, 77, 72, 66, 56, 56]
    assert len(sf) == 7 * 4

    for mode, ink in (("light", "#1D2733"), ("dark", "#E4EAE3")):
        spec = lv.spending_chart(bf, D("50000"), D("19140"), mode=mode).to_dict()
        text = str(spec)
        assert ink in text and spec["config"]["background"] == "transparent"
        assert spec["layer"][0]["encoding"]["y"]["scale"]["domain"] == [0, 55000.0]
        assert "step-after" in text and "Monthly limit $50,000" in text and "$19,140" in text
        assert spec["height"] == 220
        s = lv.stock_chart("WidgetA", sf, 90, mode=mode).to_dict()
        assert s["layer"][0]["encoding"]["y"]["scale"]["domain"] == [0, 90]
        assert s["title"]["subtitle"] == "56 left of 90" and s["height"] == 110


def test_over_limit_chart_label_and_domain(ui_rt_batch):
    steps = read(ui_rt_batch)["steps"]
    bf = lv.budget_frame(steps, D("15000"))
    spec = lv.spending_chart(bf, D("15000"), D("19140"), mode="light").to_dict()
    assert "$19,140, over by $4,140" in str(spec)
    assert spec["layer"][0]["encoding"]["y"]["scale"]["domain"][1] == pytest.approx(19140 * 1.05)


def test_summary_sentences(ui_rt, ui_rt_batch):
    d = read(ui_rt_batch)
    assert lv.summary_sentence(d["budget"], d["payments"]) == (
        "January 2026: $19,140.00 paid of the $50,000.00 monthly limit, $30,860.00 left. 5 payments, none failed."
    )
    e = read(ui_rt)
    assert lv.summary_sentence(e["budget"], e["payments"]) == (
        "January 2026: nothing paid yet. $50,000.00 of the monthly limit is available."
    )
    over = d["budget"].model_copy(update={"limit": D("15000"), "remaining": D("-4140")})
    assert lv.summary_sentence(over, d["payments"], approved_by_person=True).startswith(
        "January 2026: $19,140.00 paid against the $15,000.00 monthly limit, over the limit by $4,140.00, approved by a person."
    )


def test_csv_and_movements(ui_rt_batch):
    d = read(ui_rt_batch)
    rows = lv.payment_rows(d["payments"], d["invoices"], [s.invoice_id for s in d["steps"][1:]])
    lines = lv.payments_csv(rows).splitlines()
    assert lines[0].startswith("Paid,Invoice,Vendor,File,Amount,Currency")
    assert len(lines) == 6 and "INV-1001" in lines[1]
    table = lv.payments_table(rows)
    assert list(table["Invoice"]) == ["INV-1001", "INV-1004", "INV-1006", "INV-1011", "INV-1015"]
    mv = lv.movements_table(d["stock"], d["invoices"])
    assert len(mv) == len(d["stock"]) and "seed" in set(mv["Reason"])


def test_csv_neutralises_formulas():
    row = lv.PaymentRow("=A1", "+x", "f", "1.00", "USD", None, "$1.00", "Paid", 1, "-", None, "k", D("1"), D("1"))
    assert "'=A1,'+x" in lv.payments_csv([row])
