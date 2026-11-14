"""Shared fixtures. Package A owns this file; later packages add fixtures in their own test files."""

import datetime as dt
import hashlib
import random
import socket
from decimal import Decimal
from pathlib import Path

import pytest

from config.settings import REPO_ROOT, load_settings
from core.text import clean_item_key, vendor_key
from core.values import parse_terms_days, to_cents
from db.seed import reset_database
from deps import Deps
from models import Invoice, InvoiceState, LineItem
from obs import MemoryLogger

SAMPLES = REPO_ROOT / "data" / "invoices"


def _is_live(request: pytest.FixtureRequest) -> bool:
    return request.node.get_closest_marker("live") is not None


@pytest.fixture(autouse=True)
def _no_network(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    if _is_live(request):
        return

    def _blocked(*args, **kwargs):
        raise RuntimeError("network access is blocked in tests (mark the test `live` to allow it)")

    monkeypatch.setattr(socket.socket, "connect", _blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)


@pytest.fixture(autouse=True)
def _scrub_env(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    if _is_live(request):
        return
    for name in ("XAI_API_KEY", "GROK_EXTRACTION_MODEL", "GROK_REASONING_MODEL"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def settings(tmp_path: Path):
    base = load_settings(env={})
    return base.model_copy(
        update={
            "db_path": tmp_path / "acme.db",
            "checkpoint_path": tmp_path / "checkpoints.db",
            "log_dir": tmp_path / "logs",
        }
    )


@pytest.fixture
def repo(settings):
    r = reset_database(settings.db_path, "demo")
    yield r
    r.close()


@pytest.fixture
def as_of() -> dt.date:
    return dt.date(2026, 1, 31)


@pytest.fixture
def memlog() -> MemoryLogger:
    return MemoryLogger()


@pytest.fixture
def sleeps() -> list[float]:
    return []


class NoProvider:
    """Local stand-in so package A does not depend on the LLM layer."""

    name = "none"

    def complete(self, **kwargs):
        raise AssertionError("unexpected LLM call")


@pytest.fixture
def make_deps(repo, settings, as_of, memlog, sleeps):
    def _make(provider=None, **overrides) -> Deps:
        kwargs = dict(
            repo=repo,
            provider=provider if provider is not None else NoProvider(),
            settings=settings,
            as_of=as_of,
            log=memlog,
            sleep=sleeps.append,
            rng=random.Random(0),
        )
        kwargs.update(overrides)
        return Deps(**kwargs)

    return _make


def _dec(value) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def build_invoice(
    lines=(("WidgetA", 1, "250.00"),), *, number="INV-9001", revision=None, vendor="Widgets Inc.",
    former_name=None, address=None, currency="USD", invoice_date=dt.date(2026, 1, 20),
    due_date=dt.date(2026, 2, 19), terms="Net 30", subtotal=None, tax_rate=None, tax_amount=None,
    shipping=None, total=None, notes=None, is_revision=False,
) -> Invoice:
    items = [
        LineItem(
            item=name, item_key=clean_item_key(name), quantity=int(qty), unit_price=Decimal(str(price)),
        )
        for name, qty, price in lines
    ]
    lines_sum = sum((to_cents(li.quantity * li.unit_price) for li in items), Decimal("0"))
    rate, tax_amt, ship = _dec(tax_rate), _dec(tax_amount), _dec(shipping)
    if total is None:
        tax = tax_amt if tax_amt is not None else (to_cents(lines_sum * rate) if rate is not None else Decimal("0"))
        total_dec = lines_sum + tax + (ship or Decimal("0"))
    else:
        total_dec = Decimal(str(total))
    return Invoice(
        invoice_number=number, revision=revision, is_revision=is_revision or revision is not None,
        vendor_name=vendor, vendor_former_name=former_name,
        vendor_key=vendor_key(vendor) if vendor else None, vendor_address=address,
        invoice_date=invoice_date, due_date=due_date, due_date_raw=due_date.isoformat() if due_date else None,
        currency=currency, line_items=items, subtotal=_dec(subtotal), tax_rate=rate, tax_amount=tax_amt,
        shipping=ship, total=total_dec, payment_terms=terms, terms_days=parse_terms_days(terms), notes=notes,
    )


@pytest.fixture
def make_invoice():
    return build_invoice


@pytest.fixture
def make_state(repo):
    counter = {"n": 0}

    def _make(invoice=None, *, file_name="invoice_9001.json", **fields) -> InvoiceState:
        counter["n"] += 1
        n = counter["n"]
        trace_id = f"inv-{Path(file_name).stem}-{n:08x}"
        file_hash = hashlib.sha256(f"{file_name}:{n}".encode()).hexdigest()
        file_path = f"data/invoices/{file_name}"
        invoice_id = repo.create_invoice(
            trace_id=trace_id, file_path=file_path, file_name=file_name, file_hash=file_hash
        )
        values = dict(
            trace_id=trace_id, invoice_id=invoice_id, file_path=file_path, file_name=file_name,
            file_hash=file_hash, format=Path(file_name).suffix.lstrip(".") or None, invoice=invoice,
        )
        values.update(fields)
        return InvoiceState(**values)

    return _make
