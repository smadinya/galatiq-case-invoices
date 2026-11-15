import sys
from decimal import Decimal as D

import pytest

from config.settings import REPO_ROOT, load_settings
from llm.fake import ScriptedFake
from llm.fake_golden import golden_script
from obs import JsonlLogger
from service import Service

sys.path.insert(0, str(REPO_ROOT / "tests" / "golden"))
from golden_expected import AS_OF  # noqa: E402

SAMPLES = REPO_ROOT / "data" / "invoices"


def _service(root, provider):
    settings = load_settings(env={}).model_copy(
        update={"db_path": root / "acme.db", "checkpoint_path": root / "cp.db", "log_dir": root / "logs"}
    )
    return Service(settings, provider=provider, as_of=AS_OF, sleep=lambda s: None, log=JsonlLogger(settings.log_dir))


@pytest.fixture
def batch(tmp_path):
    svc = _service(tmp_path, ScriptedFake(golden_script()))
    svc.run_batch(SAMPLES)
    yield svc
    svc.close()


def test_inv_1012_stock_needed_is_invoice_quantities(batch):
    p = batch.preview_resolution("INV-1012")
    assert p.stock_needed == {"WidgetA": 12, "WidgetB": 7, "GadgetX": 4}
    levels = batch.repo.stock_levels()
    assert p.stock_levels == {k: levels[k] for k in p.stock_needed}
    assert p.approve_outcome == "pay" and p.stock_shortfalls == []


def test_inv_1004_r1_only_added_quantities(batch):
    p = batch.preview_resolution("INV-1004 R1")
    assert p.stock_needed == {"GadgetX": 5}
    assert p.stock_levels == {"GadgetX": batch.repo.stock_levels()["GadgetX"]}
    assert p.approve_outcome == "pay" and p.stock_shortfalls == []


def test_inv_1014_quantities(batch):
    p = batch.preview_resolution("INV-1014")
    assert p.stock_needed["WidgetB"] == 6 and p.stock_needed == {"WidgetA": 4, "WidgetB": 6}
    levels = batch.repo.stock_levels()
    assert p.stock_levels == {k: levels[k] for k in p.stock_needed}
    assert p.approve_outcome == "blocked" and p.stock_shortfalls == []
    assert batch.preview_resolution("INV-1014", fx_rate=D("1.08")).approve_outcome == "pay"


def test_unreadable_document_has_empty_stock_fields(tmp_path):
    f = tmp_path / "invoice_9002.pdf"
    f.write_bytes(b"not a pdf")
    svc = _service(tmp_path / "w", ScriptedFake({}))
    try:
        svc.run_invoice(f)
        p = svc.preview_resolution("invoice_9002.pdf")
        assert p.stock_needed == {} and p.stock_levels == {}
        assert p.approve_outcome == "blocked" and p.stock_shortfalls == [] and not p.can_approve
    finally:
        svc.close()
