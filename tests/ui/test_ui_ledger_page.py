"""The Ledger page on fresh and batch runtimes."""

from ui.kit.testing import open_page


def test_fresh_empty_state(app):
    open_page(app, "ledger")
    assert not app.exception
    text = " ".join(m.value for m in app.markdown)
    assert "January 2026: nothing paid yet. $50,000.00 of the monthly limit is available." in text.replace("\\", "")
    assert "Run the batch or process an invoice to see payments here." in text.replace("\\", "")
    assert not app.get("dataframe") and not app.get("vega_lite_chart")
    assert "page: \"batch\"" in str([e.proto for e in app.get("page_link")])


def test_batch_state(app_batch):
    open_page(app_batch, "ledger")
    assert not app_batch.exception
    text = " ".join(m.value for m in app_batch.markdown).replace("\\", "")
    assert "$19,140.00 paid of the $50,000.00 monthly limit, $30,860.00 left. 5 payments, none failed." in text
    headers = [h.value for h in app_batch.subheader]
    assert headers[:3] == ["Spending against the limit", "Stock after each payment", "Payments"]
    assert len(app_batch.get("vega_lite_chart")) == 4  # spending + WidgetA, WidgetB, GadgetX
    assert any("FakeItem has had no stock since the start" in c.value for c in app_batch.caption)
    tables = app_batch.get("dataframe")
    assert len(tables) == 2  # payments, movements
    assert len(tables[0].value) == 5
    assert [b.key for b in app_batch.get("download_button")] == ["ledger.csv"]
