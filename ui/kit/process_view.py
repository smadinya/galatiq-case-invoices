"""Pure helpers for the Process page: what a run targets, why it can't start, what the ledger adds.

Nothing here renders. `sample_target` and `upload_target` look the file up in the books (through a
`ReadSession`), so a repeat of the same bytes is known to be stored before anything runs.
"""

import re
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Sequence

from llm.select import NoProviderAvailable
from models import BudgetLedgerEntry, InvoiceRecord, InvoiceResult, Status, StockLedgerEntry
from ui.kit import copy, fmt
from ui.kit.data import file_sha256
from ui.kit.jobs import Job
from ui.kit.runtime import Preflight, Runtime, UiConfig
from ui.kit.uploads import store_upload

# Page-specific wording (design 5.1, 6.6, 6.7). Everything shared lives in ui.kit.copy.
TEXT = {
    "source": "Source",
    "source.sample": "Sample",
    "source.upload": "Upload",
    "sample": "Sample invoice",
    "sample.placeholder": "Choose a sample invoice",
    "block.nothing": "Choose a sample or upload a file to enable Process.",
    "block.busy": "A run is in progress. Process is available when it finishes.",
    "preview.pdf": "The text layer of this PDF is shown once the invoice has been processed.",
    "preview.unreadable": "This file can't be shown as text.",
    "missing": "That invoice is no longer in the books, for example after a reset. Pick a sample or upload "
    "a file to process it again.",
    "failed": "Processing {file} stopped before it finished: {error}",
    "no_model_ways": "Ways to get a model:",
}

_WAY = re.compile(r"^\s*\d+\.\s+(.*\S)\s*$")
_CAP = 200_000  # characters of source shown before processing


@dataclass(frozen=True)
class RunTarget:
    path: Path
    file_name: str
    file_hash: str
    stored: InvoiceResult | None  # the stored result of these exact bytes, when there is one


def _target(rt: Runtime, cfg: UiConfig, path: Path, file_name: str) -> RunTarget:
    digest = file_sha256(path)
    with rt.reader(cfg) as reader:
        stored = reader.stored_result(digest)
    return RunTarget(path, file_name, digest, stored)


def sample_target(rt: Runtime, cfg: UiConfig, file_name: str) -> RunTarget:
    """A file from the samples folder. Samples run in place and are never copied."""
    return _target(rt, cfg, Path(rt.samples_dir) / file_name, file_name)


def upload_target(rt: Runtime, cfg: UiConfig, name: str, data: bytes) -> RunTarget:
    """Store the bytes content-addressed (`ValueError` for an empty file or a bad suffix), then look them up."""
    path = store_upload(rt.upload_dir, name, data)
    return _target(rt, cfg, path, path.name)


def run_blocker(target: RunTarget | None, preflight: Preflight | None, busy: Job | None) -> str | None:
    """The one reason Process is disabled, or None. A stored result needs no model, so it is never blocked
    by the provider (only by a run in progress)."""
    if busy is not None:
        return TEXT["block.busy"]
    if target is None:
        return TEXT["block.nothing"]
    if target.stored is not None:
        return None
    if preflight is not None and not preflight.ok:
        return preflight.message or copy.t("error.no_model_sidebar")
    return None


def no_model_ways(message: str) -> list[str]:
    """The numbered ways in a `NoProviderAvailable` message, without their numbers. The whole message
    when it lists none."""
    ways = [m.group(1) for line in message.splitlines() if (m := _WAY.match(line))]
    return ways or [message.strip()]


def record_result(record: InvoiceRecord) -> InvoiceResult | None:
    """The result to show for a stored row. An ERROR row keeps only its message (no result JSON), so the
    result is rebuilt from it; a row still processing has nothing to show yet."""
    if record.result is not None:
        return record.result
    if record.status == Status.ERROR:
        return InvoiceResult(
            trace_id=record.trace_id, file=record.file_name, status=Status.ERROR,
            error=record.error or "The run stopped with an error.", provider="",
        )
    return None


def is_no_model(error: BaseException | None) -> bool:
    return isinstance(error, NoProviderAvailable)


def job_file(job: Job) -> str:
    """The file a single-invoice job is processing (its label is "Processing <file>")."""
    return job.label.removeprefix("Processing ")


def preview_text(path: Path) -> str | None:
    """The file as text for the pre-run source panel; None for a PDF (its text layer needs the pipeline)."""
    path = Path(path)
    if path.suffix.lower() == ".pdf":
        return None
    try:
        return path.read_bytes().decode("utf-8-sig", errors="replace")[:_CAP]
    except OSError:
        return None


def payment_rows(
    record: InvoiceRecord, stock: Sequence[StockLedgerEntry], budget: Sequence[BudgetLedgerEntry], month_name: str
) -> list[tuple[str, str]]:
    """Ledger facts for a PAID receipt: "Stock used" by inventory name and the month's spending after it.

    Stock comes from this invoice's negative stock-ledger entries; spending is the running total of the
    month's budget ledger up to and including this invoice's entry. Empty unless the invoice is PAID.
    """
    if record.status != Status.PAID:
        return []
    rows: list[tuple[str, str]] = []
    used: dict[str, int] = {}
    for entry in stock:
        if entry.invoice_id == record.id and entry.delta < 0:
            used[entry.item] = used.get(entry.item, 0) - entry.delta
    if used:
        rows.append((copy.t("pay.stock_used"), ", ".join(f"{item} −{qty}" for item, qty in used.items())))
    running = Decimal("0")
    for entry in sorted(budget, key=lambda e: e.id):
        running += entry.amount_usd
        if entry.invoice_id == record.id:
            rows.append((copy.t("pay.spending_after", month=month_name), fmt.money(running)))
            break
    return rows
