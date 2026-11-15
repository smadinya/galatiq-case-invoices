"""SQLite storage for the Acme pipeline. Package A owns this file.

Money is stored as TEXT and read back as Decimal; sums are computed in Python.
"Live" invoice means status not in {PROCESSING, ERROR, DUPLICATE, SUPERSEDED}.
"""

import datetime as dt
import json
import re
import sqlite3
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Iterator, Literal

from core.text import vendor_key
from models import (
    Actor, AliasKind, AuditEntry, BudgetStatus, InventoryItem, InvoiceRecord, InvoiceResult, InvoiceUpdate,
    PaymentRecord, PaymentWrite, QueueItem, QueueStatus, QueueWrite, Receipt, StockLedgerEntry, VendorAlias,
    VendorRecord, HumanReview, BudgetLedgerEntry,
)
from obs import utc_now

SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"

_NOT_LIVE = ("PROCESSING", "ERROR", "DUPLICATE", "SUPERSEDED")
_LIVE_SQL = "status NOT IN ('PROCESSING','ERROR','DUPLICATE','SUPERSEDED')"

# InvoiceUpdate field -> invoices column
_UPDATE_COLUMNS = {
    "fingerprint": "fingerprint", "vendor_id": "vendor_id", "vendor_key": "vendor_key",
    "vendor_name": "vendor_name", "invoice_number": "invoice_number", "revision": "revision",
    "status": "status", "ceiling": "ceiling", "amount": "amount", "currency": "currency",
    "amount_usd": "amount_usd", "invoice_date": "invoice_date", "result": "result_json",
}

_REF = re.compile(r"^(?:INV(?:OICE)?)?[\s\-#:.]*(\d+)(?:[\s\-]*R(\d+))?$", re.IGNORECASE)


def _dec(value: str | None) -> Decimal | None:
    return Decimal(value) if value is not None else None


def _dts(value: str | None) -> dt.datetime | None:
    return dt.datetime.fromisoformat(value) if value is not None else None


class Repo:
    def __init__(self, path: str | Path, *, clock: Callable[[], dt.datetime] = utc_now) -> None:
        self.path = str(path)
        self._clock = clock
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        if self.path != ":memory:":
            self._conn.execute("PRAGMA journal_mode = WAL")
        self._depth = 0

    def close(self) -> None:
        self._conn.close()

    def _now(self) -> str:
        return self._clock().isoformat(timespec="microseconds")

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """BEGIN IMMEDIATE / COMMIT / ROLLBACK; nested calls join the outer one."""
        if self._depth > 0:
            self._depth += 1
            try:
                yield
            finally:
                self._depth -= 1
            return
        self._conn.execute("BEGIN IMMEDIATE")
        self._depth = 1
        try:
            yield
        except BaseException:
            self._depth = 0
            self._conn.execute("ROLLBACK")
            raise
        else:
            self._depth = 0
            self._conn.execute("COMMIT")

    # ------------------------------------------------------------ schema / meta

    def init_schema(self) -> None:
        self._conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))

    def get_meta(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO meta(key, value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def seed_name(self) -> str | None:
        return self.get_meta("seed")

    # --------------------------------------------------------------- inventory

    @staticmethod
    def _item(row: sqlite3.Row) -> InventoryItem:
        return InventoryItem(
            item=row["item"], item_key=row["item_key"], stock=row["stock"],
            unit_price=Decimal(row["unit_price"]), category=row["category"],
        )

    def insert_inventory(self, item: InventoryItem) -> None:
        with self.transaction():
            self._conn.execute(
                "INSERT INTO inventory(item, item_key, stock, unit_price, category) VALUES(?,?,?,?,?)",
                (item.item, item.item_key, item.stock, str(item.unit_price), item.category),
            )
            self._conn.execute(
                "INSERT INTO stock_ledger(item, delta, invoice_id, reason, ts) VALUES(?,?,NULL,'seed',?)",
                (item.item, item.stock, self._now()),
            )

    def list_inventory(self) -> list[InventoryItem]:
        rows = self._conn.execute("SELECT * FROM inventory ORDER BY rowid").fetchall()
        return [self._item(r) for r in rows]

    def get_item_by_key(self, item_key: str) -> InventoryItem | None:
        row = self._conn.execute("SELECT * FROM inventory WHERE item_key=?", (item_key,)).fetchone()
        return self._item(row) if row else None

    def stock_levels(self) -> dict[str, int]:
        rows = self._conn.execute("SELECT item, stock FROM inventory ORDER BY rowid").fetchall()
        return {r["item"]: r["stock"] for r in rows}

    def stock_ledger(self, invoice_id: int | None = None) -> list[StockLedgerEntry]:
        if invoice_id is None:
            rows = self._conn.execute("SELECT * FROM stock_ledger ORDER BY id").fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM stock_ledger WHERE invoice_id=? ORDER BY id", (invoice_id,)
            ).fetchall()
        return [
            StockLedgerEntry(
                id=r["id"], item=r["item"], delta=r["delta"], invoice_id=r["invoice_id"],
                reason=r["reason"], ts=_dts(r["ts"]),
            )
            for r in rows
        ]

    # ----------------------------------------------------------------- vendors

    @staticmethod
    def _vendor(row: sqlite3.Row) -> VendorRecord:
        return VendorRecord(
            id=row["id"], name=row["name"], normalized_name=row["normalized_name"], status=row["status"],
            address=row["address"], payment_terms=row["payment_terms"],
        )

    @staticmethod
    def _alias(row: sqlite3.Row) -> VendorAlias:
        return VendorAlias(
            vendor_id=row["vendor_id"], alias=row["alias"], normalized_alias=row["normalized_alias"],
            kind=row["kind"],
        )

    def insert_vendor(
        self, *, name: str, normalized_name: str, status: str, address: str | None, payment_terms: str | None
    ) -> int:
        cur = self._conn.execute(
            "INSERT INTO vendors(name, normalized_name, status, address, payment_terms) VALUES(?,?,?,?,?)",
            (name, normalized_name, status, address, payment_terms),
        )
        return int(cur.lastrowid)

    def list_vendors(self) -> list[VendorRecord]:
        return [self._vendor(r) for r in self._conn.execute("SELECT * FROM vendors ORDER BY id").fetchall()]

    def get_vendor(self, vendor_id: int) -> VendorRecord | None:
        row = self._conn.execute("SELECT * FROM vendors WHERE id=?", (vendor_id,)).fetchone()
        return self._vendor(row) if row else None

    def find_vendor_by_key(self, key: str) -> VendorRecord | None:
        row = self._conn.execute("SELECT * FROM vendors WHERE normalized_name=?", (key,)).fetchone()
        return self._vendor(row) if row else None

    def find_vendor_by_alias(self, key: str) -> tuple[VendorRecord, VendorAlias] | None:
        row = self._conn.execute("SELECT * FROM vendor_aliases WHERE normalized_alias=?", (key,)).fetchone()
        if not row:
            return None
        vendor = self.get_vendor(row["vendor_id"])
        return (vendor, self._alias(row)) if vendor else None

    def add_vendor_alias(self, vendor_id: int, alias: str, kind: AliasKind) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO vendor_aliases(vendor_id, alias, normalized_alias, kind, created_at) "
            "VALUES(?,?,?,?,?)",
            (vendor_id, alias, vendor_key(alias), kind, self._now()),
        )

    def list_aliases(self, vendor_id: int) -> list[VendorAlias]:
        rows = self._conn.execute(
            "SELECT * FROM vendor_aliases WHERE vendor_id=? ORDER BY id", (vendor_id,)
        ).fetchall()
        return [self._alias(r) for r in rows]

    # ---------------------------------------------------------------- invoices

    @staticmethod
    def _invoice(row: sqlite3.Row) -> InvoiceRecord:
        return InvoiceRecord(
            id=row["id"], trace_id=row["trace_id"], file_path=row["file_path"], file_name=row["file_name"],
            file_hash=row["file_hash"], fingerprint=row["fingerprint"], vendor_id=row["vendor_id"],
            vendor_key=row["vendor_key"], vendor_name=row["vendor_name"],
            invoice_number=row["invoice_number"], revision=row["revision"], status=row["status"],
            ceiling=row["ceiling"], amount=_dec(row["amount"]), currency=row["currency"],
            amount_usd=_dec(row["amount_usd"]),
            invoice_date=dt.date.fromisoformat(row["invoice_date"]) if row["invoice_date"] else None,
            error=row["error"],
            result=InvoiceResult.model_validate_json(row["result_json"]) if row["result_json"] else None,
            created_at=_dts(row["created_at"]), updated_at=_dts(row["updated_at"]),
        )

    def _invoices(self, sql: str, params: tuple = ()) -> list[InvoiceRecord]:
        return [self._invoice(r) for r in self._conn.execute(sql, params).fetchall()]

    def create_invoice(self, *, trace_id: str, file_path: str, file_name: str, file_hash: str) -> int:
        now = self._now()
        cur = self._conn.execute(
            "INSERT INTO invoices(trace_id, file_path, file_name, file_hash, status, created_at, updated_at) "
            "VALUES(?,?,?,?,'PROCESSING',?,?)",
            (trace_id, file_path, file_name, file_hash, now, now),
        )
        return int(cur.lastrowid)

    def update_invoice(self, invoice_id: int, update: InvoiceUpdate) -> None:
        data = update.model_dump(mode="json", exclude_unset=True)
        sets: list[str] = []
        params: list[Any] = []
        for field_name, value in data.items():
            sets.append(f"{_UPDATE_COLUMNS[field_name]}=?")
            if field_name == "result" and value is not None:
                value = json.dumps(value)
            params.append(value)
        sets.append("updated_at=?")
        params.append(self._now())
        params.append(invoice_id)
        self._conn.execute(f"UPDATE invoices SET {', '.join(sets)} WHERE id=?", params)

    def get_invoice(self, invoice_id: int) -> InvoiceRecord | None:
        rows = self._invoices("SELECT * FROM invoices WHERE id=?", (invoice_id,))
        return rows[0] if rows else None

    def get_invoice_by_trace(self, trace_id: str) -> InvoiceRecord | None:
        rows = self._invoices("SELECT * FROM invoices WHERE trace_id=?", (trace_id,))
        return rows[0] if rows else None

    def find_by_file_hash(self, file_hash: str) -> InvoiceRecord | None:
        rows = self._invoices(
            "SELECT * FROM invoices WHERE file_hash=? AND status != 'ERROR' ORDER BY id DESC LIMIT 1",
            (file_hash,),
        )
        return rows[0] if rows else None

    def find_by_fingerprint(self, fingerprint: str, *, exclude_id: int) -> InvoiceRecord | None:
        rows = self._invoices(
            f"SELECT * FROM invoices WHERE fingerprint=? AND id != ? AND {_LIVE_SQL} ORDER BY id LIMIT 1",
            (fingerprint, exclude_id),
        )
        return rows[0] if rows else None

    def find_by_vendor_number(self, vendor_key: str, invoice_number: str, *, exclude_id: int) -> list[InvoiceRecord]:
        return self._invoices(
            f"SELECT * FROM invoices WHERE vendor_key=? AND invoice_number=? AND id != ? AND {_LIVE_SQL} "
            "ORDER BY id",
            (vendor_key, invoice_number, exclude_id),
        )

    def vendor_invoices(
        self, *, vendor_id: int | None, vendor_key: str | None, exclude_trace: str | None = None
    ) -> list[InvoiceRecord]:
        conds: list[str] = []
        params: list[Any] = []
        if vendor_id is not None:
            conds.append("vendor_id=?")
            params.append(vendor_id)
        if vendor_key is not None:
            conds.append("vendor_key=?")
            params.append(vendor_key)
        if not conds:
            return []
        sql = f"SELECT * FROM invoices WHERE ({' OR '.join(conds)}) AND status != 'ERROR'"
        if exclude_trace is not None:
            sql += " AND trace_id != ?"
            params.append(exclude_trace)
        return self._invoices(sql + " ORDER BY id", tuple(params))

    def list_invoices(self) -> list[InvoiceRecord]:
        return self._invoices("SELECT * FROM invoices ORDER BY id")

    def mark_error(self, invoice_id: int, message: str) -> None:
        self._conn.execute(
            "UPDATE invoices SET status='ERROR', error=?, updated_at=? WHERE id=?",
            (message, self._now(), invoice_id),
        )

    def supersede_invoice(self, invoice_id: int) -> None:
        with self.transaction():
            self._conn.execute(
                "UPDATE invoices SET status='SUPERSEDED', updated_at=? WHERE id=?", (self._now(), invoice_id)
            )
            self._conn.execute(
                "UPDATE human_queue SET status='SUPERSEDED' WHERE invoice_id=? AND status='PENDING'",
                (invoice_id,),
            )

    # ---------------------------------------------------------------- payments

    @staticmethod
    def _payment(row: sqlite3.Row) -> PaymentRecord:
        return PaymentRecord(
            idempotency_key=row["idempotency_key"], invoice_id=row["invoice_id"], amount=Decimal(row["amount"]),
            currency=row["currency"], fx_rate=_dec(row["fx_rate"]), amount_usd=_dec(row["amount_usd"]),
            status=row["status"], attempts=row["attempts"],
            receipt=Receipt.model_validate_json(row["receipt_json"]) if row["receipt_json"] else None,
            error=row["error"], created_at=_dts(row["created_at"]), updated_at=_dts(row["updated_at"]),
        )

    def list_payments(self) -> list[PaymentRecord]:
        rows = self._conn.execute("SELECT * FROM payments ORDER BY id").fetchall()
        return [self._payment(r) for r in rows]

    def get_payment(self, idempotency_key: str) -> PaymentRecord | None:
        row = self._conn.execute("SELECT * FROM payments WHERE idempotency_key=?", (idempotency_key,)).fetchone()
        return self._payment(row) if row else None

    def paid_to_date(self, vendor_key: str, invoice_number: str) -> Decimal:
        rows = self._conn.execute(
            "SELECT p.amount FROM payments p JOIN invoices i ON i.id = p.invoice_id "
            "WHERE p.status='SUCCESS' AND i.vendor_key=? AND i.invoice_number=?",
            (vendor_key, invoice_number),
        ).fetchall()
        return sum((Decimal(r["amount"]) for r in rows), Decimal("0"))

    def record_payment(self, p: PaymentWrite) -> Receipt:
        """One transaction: payment row, stock, stock ledger, budget ledger, invoice status."""
        with self.transaction():
            existing = self.get_payment(p.idempotency_key)
            if existing is not None and existing.status == "SUCCESS" and existing.receipt is not None:
                return existing.receipt
            now = self._clock()
            receipt = Receipt(
                idempotency_key=p.idempotency_key, vendor=p.vendor, amount=p.amount, currency=p.currency,
                fx_rate=p.fx_rate, amount_usd=p.amount_usd, attempts=p.attempts, paid_at=now,
                bank_response=p.bank_response,
            )
            self._conn.execute(
                "INSERT INTO payments(idempotency_key, invoice_id, amount, currency, fx_rate, amount_usd, status, "
                "attempts, error, receipt_json, created_at, updated_at) VALUES(?,?,?,?,?,?,'SUCCESS',?,NULL,?,?,?) "
                "ON CONFLICT(idempotency_key) DO UPDATE SET invoice_id=excluded.invoice_id, "
                "amount=excluded.amount, currency=excluded.currency, fx_rate=excluded.fx_rate, "
                "amount_usd=excluded.amount_usd, status='SUCCESS', attempts=excluded.attempts, error=NULL, "
                "receipt_json=excluded.receipt_json, updated_at=excluded.updated_at",
                (
                    p.idempotency_key, p.invoice_id, str(p.amount), p.currency,
                    str(p.fx_rate) if p.fx_rate is not None else None, str(p.amount_usd), p.attempts,
                    receipt.model_dump_json(), now.isoformat(timespec="microseconds"), now.isoformat(timespec="microseconds"),
                ),
            )
            for item, delta in p.stock_deltas.items():
                if delta == 0:
                    continue
                cur = self._conn.execute("UPDATE inventory SET stock = stock + ? WHERE item=?", (delta, item))
                if cur.rowcount == 0:
                    raise ValueError(f"unknown inventory item: {item}")
                self._conn.execute(
                    "INSERT INTO stock_ledger(item, delta, invoice_id, reason, ts) VALUES(?,?,?,'payment',?)",
                    (item, delta, p.invoice_id, now.isoformat(timespec="microseconds")),
                )
            self._conn.execute(
                "INSERT INTO budget_ledger(month, amount_usd, invoice_id, ts) VALUES(?,?,?,?)",
                (p.month, str(p.amount_usd), p.invoice_id, now.isoformat(timespec="microseconds")),
            )
            self._conn.execute(
                "UPDATE invoices SET status=?, updated_at=? WHERE id=?",
                (p.final_status, now.isoformat(timespec="microseconds"), p.invoice_id),
            )
            return receipt

    def record_payment_failure(
        self, *, idempotency_key: str, invoice_id: int, amount: Decimal, currency: str, attempts: int, error: str
    ) -> None:
        now = self._now()
        self._conn.execute(
            "INSERT INTO payments(idempotency_key, invoice_id, amount, currency, status, attempts, error, "
            "created_at, updated_at) VALUES(?,?,?,?,'FAILED',?,?,?,?) "
            "ON CONFLICT(idempotency_key) DO UPDATE SET invoice_id=excluded.invoice_id, amount=excluded.amount, "
            "currency=excluded.currency, status='FAILED', attempts=excluded.attempts, error=excluded.error, "
            "updated_at=excluded.updated_at WHERE payments.status != 'SUCCESS'",
            (idempotency_key, invoice_id, str(amount), currency, attempts, error, now, now),
        )

    # ------------------------------------------------------------------ budget

    def budget_entries(self, month: str) -> list[tuple[int | None, Decimal]]:
        rows = self._conn.execute(
            "SELECT invoice_id, amount_usd FROM budget_ledger WHERE month=? ORDER BY id", (month,)
        ).fetchall()
        return [(r["invoice_id"], Decimal(r["amount_usd"])) for r in rows]

    def budget_ledger(self, month: str | None = None) -> list[BudgetLedgerEntry]:
        if month is None:
            rows = self._conn.execute("SELECT * FROM budget_ledger ORDER BY id").fetchall()
        else:
            rows = self._conn.execute("SELECT * FROM budget_ledger WHERE month=? ORDER BY id", (month,)).fetchall()
        return [
            BudgetLedgerEntry(
                id=r["id"], month=r["month"], amount_usd=Decimal(r["amount_usd"]), invoice_id=r["invoice_id"],
                ts=_dts(r["ts"]),
            )
            for r in rows
        ]

    def budget_spent(self, month: str) -> Decimal:
        return sum((amt for _, amt in self.budget_entries(month)), Decimal("0"))

    def budget_status(self, month: str, limit: Decimal) -> BudgetStatus:
        spent = self.budget_spent(month)
        return BudgetStatus(month=month, limit=limit, spent=spent, remaining=limit - spent)

    # ------------------------------------------------------------- human queue

    _QUEUE_SELECT = (
        "SELECT q.*, i.invoice_number, i.revision, i.file_name, i.vendor_name FROM human_queue q "
        "JOIN invoices i ON i.id = q.invoice_id"
    )

    @staticmethod
    def _queue_item(row: sqlite3.Row) -> QueueItem:
        number, revision = row["invoice_number"], row["revision"]
        if number:
            ref = f"{number} {revision}" if revision else number
        else:
            ref = row["file_name"]
        return QueueItem(
            invoice_id=row["invoice_id"], trace_id=row["trace_id"], invoice_ref=ref, invoice_number=number,
            revision=revision, file_name=row["file_name"], vendor=row["vendor_name"], reason=row["reason"],
            review=HumanReview.model_validate_json(row["recommendation_json"]), status=row["status"],
            resolution=row["resolution"], note=row["note"], created_at=_dts(row["created_at"]),
            resolved_at=_dts(row["resolved_at"]),
        )

    def upsert_queue_item(self, item: QueueWrite) -> None:
        """PENDING; idempotent. Reopens a RESOLVED row (an approved invoice whose payment then
        failed goes back to the queue). Never reopens a SUPERSEDED row."""
        with self.transaction():
            row = self._conn.execute(
                "SELECT status FROM human_queue WHERE invoice_id=?", (item.invoice_id,)
            ).fetchone()
            review_json = item.review.model_dump_json()
            if row is None:
                self._conn.execute(
                    "INSERT INTO human_queue(invoice_id, trace_id, reason, recommendation_json, status, created_at) "
                    "VALUES(?,?,?,?,'PENDING',?)",
                    (item.invoice_id, item.trace_id, item.reason.value, review_json, self._now()),
                )
            elif row["status"] == "PENDING":
                self._conn.execute(
                    "UPDATE human_queue SET trace_id=?, reason=?, recommendation_json=? WHERE invoice_id=?",
                    (item.trace_id, item.reason.value, review_json, item.invoice_id),
                )
            elif row["status"] == "RESOLVED":
                self._conn.execute(
                    "UPDATE human_queue SET trace_id=?, reason=?, recommendation_json=?, status='PENDING', "
                    "resolution=NULL, note=NULL, resolved_at=NULL WHERE invoice_id=?",
                    (item.trace_id, item.reason.value, review_json, item.invoice_id),
                )

    def list_queue(self, status: QueueStatus | None = "PENDING") -> list[QueueItem]:
        if status is None:
            rows = self._conn.execute(f"{self._QUEUE_SELECT} ORDER BY q.invoice_id").fetchall()
        else:
            rows = self._conn.execute(
                f"{self._QUEUE_SELECT} WHERE q.status=? ORDER BY q.invoice_id", (status,)
            ).fetchall()
        return [self._queue_item(r) for r in rows]

    def get_queue_item(self, invoice_id: int) -> QueueItem | None:
        row = self._conn.execute(f"{self._QUEUE_SELECT} WHERE q.invoice_id=?", (invoice_id,)).fetchone()
        return self._queue_item(row) if row else None

    def find_pending(self, ref: str) -> list[QueueItem]:
        """Match PENDING items by trace_id, file name, or invoice number (with optional revision)."""
        ref = ref.strip()
        pending = self.list_queue("PENDING")
        low = ref.lower()
        direct = [
            it for it in pending
            if it.trace_id.lower() == low or it.file_name.lower() == low or Path(it.file_name).stem.lower() == low
        ]
        if direct:
            return direct
        m = _REF.match(ref)
        if not m:
            return []
        number = f"INV-{m.group(1)}"
        revision = f"R{m.group(2)}" if m.group(2) else None
        return [
            it for it in pending
            if it.invoice_number == number and (revision is None or it.revision == revision)
        ]

    def resolve_queue_item(
        self, invoice_id: int, *, resolution: Literal["approve", "reject"], note: str
    ) -> None:
        with self.transaction():
            row = self._conn.execute(
                "SELECT status FROM human_queue WHERE invoice_id=?", (invoice_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"no queue item for invoice {invoice_id}")
            if row["status"] != "PENDING":
                return
            self._conn.execute(
                "UPDATE human_queue SET status='RESOLVED', resolution=?, note=?, resolved_at=? WHERE invoice_id=?",
                (resolution, note, self._now(), invoice_id),
            )

    def discard_queue_item(self, invoice_id: int) -> None:
        self._conn.execute("DELETE FROM human_queue WHERE invoice_id=?", (invoice_id,))

    # ------------------------------------------------------------------- audit

    def audit(self, *, trace_id: str, actor: Actor, event: str, payload: dict[str, Any]) -> None:
        self._conn.execute(
            "INSERT INTO audit_log(trace_id, ts, actor, event, payload_json) VALUES(?,?,?,?,?)",
            (trace_id, self._now(), actor, event, json.dumps(payload, default=str)),
        )

    def audit_for_trace(self, trace_id: str) -> list[AuditEntry]:
        rows = self._conn.execute("SELECT * FROM audit_log WHERE trace_id=? ORDER BY id", (trace_id,)).fetchall()
        return [
            AuditEntry(
                id=r["id"], trace_id=r["trace_id"], ts=_dts(r["ts"]), actor=r["actor"], event=r["event"],
                payload=json.loads(r["payload_json"]),
            )
            for r in rows
        ]
