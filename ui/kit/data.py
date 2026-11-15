"""The read path: the only UI module that touches storage, and it only reads.

A `ReadSession` opens its own SQLite connections lazily, per script run, and closes them in
`close()`. No connection outlives a call, so a reset (which unlinks the database files) can
never leave a stale handle behind. Reads take no lock: SQLite WAL readers never block, and a
read never seeds or creates a missing database.
"""

import hashlib
import sqlite3
from decimal import Decimal
from pathlib import Path
from typing import Callable, Literal, TypeVar

from config.settings import Settings
from db.repo import Repo
from graph import make_checkpointer
from models import (
    AuditEntry,
    BudgetLedgerEntry,
    BudgetStatus,
    InventoryItem,
    InvoiceRecord,
    InvoiceResult,
    InvoiceState,
    PaymentRecord,
    QueueItem,
    QueueStatus,
    StockLedgerEntry,
    VendorAlias,
    VendorRecord,
)
from tools.parsers import decode_text
from ui.kit.case import CaseDetail

T = TypeVar("T")


def file_sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class ReadSession:
    def __init__(self, settings: Settings, *, budget_month: str) -> None:
        self.settings = settings
        self.budget_month = budget_month
        self._repo: Repo | None = None
        self._tried = False

    # ------------------------------------------------------------ lifecycle

    def close(self) -> None:
        if self._repo is not None:
            try:
                self._repo.close()
            except sqlite3.Error:
                pass
            self._repo = None

    def __enter__(self) -> "ReadSession":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _db(self) -> Repo | None:
        """The repo, or None when the database file does not exist yet (nothing is created)."""
        if self._repo is None and not self._tried:
            self._tried = True
            if Path(self.settings.db_path).is_file():
                try:
                    self._repo = Repo(self.settings.db_path)
                except sqlite3.Error:
                    self._repo = None
        return self._repo

    def _read(self, fn: Callable[[Repo], T], empty: T) -> T:
        repo = self._db()
        if repo is None:
            return empty
        try:
            return fn(repo)
        except sqlite3.OperationalError:  # no tables yet, or the file is being replaced
            return empty

    # --------------------------------------------------------------- reads

    def seed_name(self) -> str | None:
        return self._read(lambda r: r.seed_name(), None)

    def invoices(self) -> list[InvoiceRecord]:
        return self._read(lambda r: r.list_invoices(), [])

    def invoice(self, invoice_id: int) -> InvoiceRecord | None:
        return self._read(lambda r: r.get_invoice(invoice_id), None)

    def invoice_by_trace(self, trace_id: str) -> InvoiceRecord | None:
        return self._read(lambda r: r.get_invoice_by_trace(trace_id), None)

    def stored_result(self, file_hash: str) -> InvoiceResult | None:
        record = self._read(lambda r: r.find_by_file_hash(file_hash), None)
        return record.result if record is not None else None

    def queue(self, status: QueueStatus | None = "PENDING") -> list[QueueItem]:
        """`status=None` returns every status (used for "Resolved (n)")."""
        return self._read(lambda r: r.list_queue(status), [])

    def inventory(self) -> list[InventoryItem]:
        return self._read(lambda r: r.list_inventory(), [])

    def stock_ledger(self) -> list[StockLedgerEntry]:
        return self._read(lambda r: r.stock_ledger(), [])

    def starting_stock(self) -> dict[str, int]:
        """Starting stock per item, from the `seed` rows of the stock ledger."""
        start: dict[str, int] = {}
        for entry in self.stock_ledger():
            if entry.reason == "seed":
                start[entry.item] = start.get(entry.item, 0) + entry.delta
        return start

    def budget_ledger(self) -> list[BudgetLedgerEntry]:
        return self._read(lambda r: r.budget_ledger(self.budget_month), [])

    def budget_ledger_all(self) -> list[BudgetLedgerEntry]:
        """Budget entries for every month (the Ledger page lists the other months)."""
        return self._read(lambda r: r.budget_ledger(), [])

    def budget_status(self) -> BudgetStatus:
        empty = BudgetStatus(
            month=self.budget_month, limit=self.settings.monthly_budget_usd, spent=Decimal("0"),
            remaining=self.settings.monthly_budget_usd,
        )
        return self._read(lambda r: r.budget_status(self.budget_month, self.settings.monthly_budget_usd), empty)

    def payments(self) -> list[PaymentRecord]:
        return self._read(lambda r: r.list_payments(), [])

    def audit(self, trace_id: str) -> list[AuditEntry]:
        return self._read(lambda r: r.audit_for_trace(trace_id), [])

    def vendor(self, vendor_id: int) -> VendorRecord | None:
        return self._read(lambda r: r.get_vendor(vendor_id), None)

    def vendor_aliases(self, vendor_id: int) -> list[VendorAlias]:
        return self._read(lambda r: r.list_aliases(vendor_id), [])

    # ----------------------------------------------------------- checkpoints

    def invoice_state(self, trace_id: str) -> InvoiceState | None:
        """The checkpointed `InvoiceState` for a run (what `graph.get_state` holds), or None."""
        path = Path(self.settings.checkpoint_path)
        if not path.is_file():
            return None
        saver, conn = make_checkpointer(path)
        try:
            tup = saver.get_tuple({"configurable": {"thread_id": trace_id}})
        except sqlite3.Error:
            return None
        finally:
            conn.close()
        if tup is None:
            return None
        values = tup.checkpoint.get("channel_values") or {}
        try:
            return InvoiceState.model_validate(values)
        except ValueError:
            return None

    def source_text(self, record: InvoiceRecord, state: InvoiceState | None) -> str | None:
        """`state.raw_text` (TXT and PDF), else the decoded stored file; None when the file is gone."""
        if state is not None and state.raw_text:
            return state.raw_text
        path = Path(record.file_path)
        if path.suffix.lower() == ".pdf" or not path.is_file():
            return None
        try:
            return decode_text(path.read_bytes())
        except OSError:
            return None

    def case_detail(self, record: InvoiceRecord) -> CaseDetail:
        state = self.invoice_state(record.trace_id)
        suffix = Path(record.file_name).suffix.lower()
        kind: Literal["text", "pdf", "file"] = "text" if suffix == ".txt" else "pdf" if suffix == ".pdf" else "file"
        path = Path(record.file_path)
        return CaseDetail(
            state=state,
            source=self.source_text(record, state),
            source_kind=kind,
            original_path=path if path.is_file() else None,
            processed_at=record.updated_at,
        )
