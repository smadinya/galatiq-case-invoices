"""Service layer: run lifecycle, crash containment, queue, resolve, reset and banner."""

import datetime as dt
import hashlib
import random
import re
import sqlite3
import time
import traceback
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Literal, Mapping

from langgraph.types import Command

import agents.human as human_agent
from config.settings import Settings
from db.repo import Repo
from db.seed import apply_seed, reset_database
from deps import Deps
from graph import build_graph, make_checkpointer
from llm.base import NullProvider
from llm.select import NoProviderAvailable, ProviderInfo, select_provider
from models import (
    Banner,
    BatchResult,
    BudgetStatus,
    HumanResolution,
    InvoiceResult,
    InvoiceState,
    InvoiceUpdate,
    Provider,
    QueueItem,
    ResolutionPreview,
    Status,
)
from obs import EventLogger, JsonlLogger, utc_now


class ServiceError(Exception):
    pass


class ResolutionRefused(ServiceError):
    """Nothing resumed; the queue item stays PENDING."""


class NotFound(ResolutionRefused):
    pass


class AmbiguousReference(ResolutionRefused):
    def __init__(self, message: str, candidates: list[str]) -> None:
        super().__init__(message)
        self.candidates = candidates


class NoteRequired(ResolutionRefused):
    pass


class FxRateRequired(ResolutionRefused):
    pass


class ApproveNotAllowed(ResolutionRefused):
    pass


class OverspendConfirmationRequired(ResolutionRefused):
    def __init__(self, message: str, overspend_usd: Decimal, budget: BudgetStatus) -> None:
        super().__init__(message)
        self.overspend_usd = overspend_usd
        self.budget = budget


def new_trace_id(path: Path) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", Path(path).stem.lower()).strip("-") or "invoice"
    return f"inv-{slug}-{uuid.uuid4().hex[:8]}"


def build_result(state: InvoiceState, provider_name: str) -> InvoiceResult:
    inv = state.invoice
    return InvoiceResult(
        trace_id=state.trace_id,
        file=state.file_name,
        invoice_number=inv.invoice_number if inv else None,
        revision=inv.revision if inv else None,
        vendor=(inv.vendor_name if inv else None),
        status=state.status,
        ceiling=state.ceiling,
        flags=list(state.flags),
        address_check=state.address_check,
        amount=inv.total if inv else None,
        currency=inv.currency if inv else None,
        amount_usd=state.amount_usd,
        decision=state.decision,
        transcript=list(state.transcript),
        human=state.human,
        payment=state.payment,
        duplicate_of=state.duplicate_of,
        invoice=inv,
        provider=provider_name,
        usage=state.usage,
    )


class _LazyProvider:
    """Resolves the real provider on the first LLM call, so no-LLM paths need no key or recordings."""

    def __init__(self, service: "Service") -> None:
        self._service = service

    @property
    def name(self) -> str:
        return self._service._resolved_name()

    def complete(self, **kwargs: Any):
        return self._service._resolve_provider().complete(**kwargs)


def _sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class Service:
    def __init__(
        self,
        settings: Settings,
        *,
        provider: Provider | None = None,
        provider_choice: str | None = None,
        env: Mapping[str, str] | None = None,
        as_of: dt.date | None = None,
        payment_fail_rate: float = 0.0,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
        log: EventLogger | None = None,
        node_hook: Callable[[str, InvoiceState], None] | None = None,
        clock: Callable[[], dt.datetime] = utc_now,
    ) -> None:
        self.settings = settings
        self.as_of = as_of or settings.as_of
        self.provider_choice = provider_choice
        self.env: Mapping[str, str] = {} if env is None else env
        self.clock = clock
        self.log: EventLogger = log if log is not None else JsonlLogger(settings.log_dir)
        self._explicit = provider
        self._provider: Provider | None = provider
        self._info: ProviderInfo | None = None
        self._hashes: tuple[str, ...] = ()
        self._payment_fail_rate = payment_fail_rate
        self._sleep = sleep
        self._rng = rng or random.Random()
        self._node_hook = node_hook
        self._open()

    # ------------------------------------------------------------ lifecycle

    def _open(self) -> None:
        s = self.settings
        s.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.repo = Repo(s.db_path, clock=self.clock)
        self.repo.init_schema()
        if self.repo.seed_name() is None:
            apply_seed(self.repo, s.default_seed)
        self._wire()

    def _wire(self) -> None:
        self.saver, self._ckpt_conn = make_checkpointer(self.settings.checkpoint_path)
        self.deps = Deps(
            repo=self.repo, provider=_LazyProvider(self) if self._explicit is None else self._explicit,
            settings=self.settings, as_of=self.as_of, log=self.log,
            payment_fail_rate=self._payment_fail_rate, sleep=self._sleep, rng=self._rng,
            clock=self.clock, node_hook=self._node_hook,
        )
        self.graph = build_graph(self.deps, self.saver)

    def close(self) -> None:
        for closer in (self._ckpt_conn.close, self.repo.close):
            try:
                closer()
            except sqlite3.Error:
                pass

    def __enter__(self) -> "Service":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------- provider

    def _resolve_provider(self) -> Provider:
        if self._provider is None:
            self._provider, self._info = select_provider(
                self.provider_choice, settings=self.settings, env=self.env, file_hashes=self._hashes
            )
        return self._provider

    def _resolved_name(self) -> str:
        return self._provider.name if self._provider is not None else NullProvider.name

    def _provider_info(self) -> ProviderInfo:
        if self._explicit is not None:
            return ProviderInfo(self._explicit.name, {}, None)
        if self._info is not None:
            return self._info
        try:
            info = select_provider(
                self.provider_choice, settings=self.settings, env=self.env, file_hashes=()
            )[1]
        except NoProviderAvailable:
            return ProviderInfo(NullProvider.name, {}, None)
        if info.name == "replay" and self.provider_choice is None and info.recording_date is None:
            # Auto-selected replay with nothing recorded: no file could actually run, so say so.
            return ProviderInfo(NullProvider.name, {}, None)
        return info

    # ----------------------------------------------------------------- runs

    def _config(self, trace_id: str) -> dict[str, Any]:
        return {"configurable": {"thread_id": trace_id}}

    def _stream(
        self, graph_input: Any, trace_id: str, file_name: str,
        on_update: Callable[[str, str, dict], None] | None,
    ) -> None:
        for chunk in self.graph.stream(graph_input, self._config(trace_id), stream_mode="updates"):
            for node, update in chunk.items():
                if node == "__interrupt__":
                    self.log.event("interrupt", trace_id=trace_id, invoice=file_name)
                elif on_update is not None:
                    on_update(file_name, node, update or {})

    def _state(self, trace_id: str) -> InvoiceState:
        return InvoiceState.model_validate(self.graph.get_state(self._config(trace_id)).values)

    def _finish(self, invoice_id: int, trace_id: str) -> InvoiceResult:
        state = self._state(trace_id)
        result = build_result(state, self._resolved_name())
        fields: dict[str, Any] = {"status": result.status}
        if result.ceiling is not None:
            fields["ceiling"] = result.ceiling
        if result.amount_usd is not None:
            fields["amount_usd"] = result.amount_usd
        if state.vendor is not None and state.vendor.vendor_id is not None:
            fields["vendor_id"] = state.vendor.vendor_id
        self.repo.update_invoice(invoice_id, InvoiceUpdate(result=result, **fields))
        return result

    def _error_result(
        self, exc: Exception, *, invoice_id: int, trace_id: str, file_name: str
    ) -> InvoiceResult:
        message = f"{type(exc).__name__}: {exc}"
        self.repo.mark_error(invoice_id, message)
        self.repo.discard_queue_item(invoice_id)
        self.log.event(
            "error", trace_id=trace_id, invoice=file_name,
            data={"error": message, "traceback": traceback.format_exc()},
        )
        return InvoiceResult(
            trace_id=trace_id, file=file_name, status=Status.ERROR, error=message,
            provider=self._resolved_name(),
        )

    def run_invoice(
        self, path: Path, *, on_update: Callable[[str, str, dict], None] | None = None
    ) -> InvoiceResult:
        path = Path(path)
        file_hash = _sha256(path)
        existing = self.repo.find_by_file_hash(file_hash)
        if existing is not None and existing.result is not None:
            self.log.event(
                "short_circuit", trace_id=existing.trace_id, invoice=path.name,
                data={"file_hash": file_hash},
            )
            return existing.result.model_copy(update={"short_circuited": True})
        if not self._hashes:
            self._hashes = (file_hash,)
        trace_id = new_trace_id(path)
        invoice_id = self.repo.create_invoice(
            trace_id=trace_id, file_path=str(path), file_name=path.name, file_hash=file_hash
        )
        self.log.event("run_start", trace_id=trace_id, invoice=path.name)
        try:
            self._stream(
                {"trace_id": trace_id, "invoice_id": invoice_id, "file_path": str(path),
                 "file_name": path.name, "file_hash": file_hash},
                trace_id, path.name, on_update,
            )
            result = self._finish(invoice_id, trace_id)
        except NoProviderAvailable as exc:
            self._error_result(exc, invoice_id=invoice_id, trace_id=trace_id, file_name=path.name)
            raise
        except Exception as exc:
            result = self._error_result(exc, invoice_id=invoice_id, trace_id=trace_id, file_name=path.name)
        self.log.event("run_end", trace_id=trace_id, invoice=path.name, data={"status": result.status.value})
        return result

    def run_batch(
        self,
        directory: Path,
        *,
        on_update: Callable[[str, str, dict], None] | None = None,
        on_result: Callable[[InvoiceResult], None] | None = None,
    ) -> BatchResult:
        directory = Path(directory)
        files = sorted(p for p in directory.iterdir() if p.is_file() and not p.name.startswith("."))
        batch_id = f"batch-{uuid.uuid4().hex[:8]}"
        hashes = {p: _sha256(p) for p in files}
        todo = []
        for p in files:
            rec = self.repo.find_by_file_hash(hashes[p])
            if rec is None or rec.result is None:
                todo.append(hashes[p])
        self._hashes = tuple(todo)
        self.log.event("run_start", trace_id=batch_id, data={"files": len(files), "directory": str(directory)})
        results: list[InvoiceResult] = []
        try:
            for p in files:
                result = self.run_invoice(p, on_update=on_update)
                results.append(result)
                if on_result is not None:
                    on_result(result)
        finally:
            self._hashes = ()
        counts: dict[str, int] = {}
        for r in results:
            counts[r.status.value] = counts.get(r.status.value, 0) + 1
        distinct = [r for r in results if r.status != Status.DUPLICATE]
        decided = sum(1 for r in distinct if r.status in (Status.PAID, Status.REJECTED, Status.CREDIT_DUE))
        self.log.event("run_end", trace_id=batch_id, data={"counts": counts})
        return BatchResult(
            results=results, counts=counts, distinct_documents=len(distinct),
            decided_without_person=decided,
            budget=self.repo.budget_status(self.deps.budget_month, self.settings.monthly_budget_usd),
            stock=self.repo.stock_levels(),
        )

    # ---------------------------------------------------------------- queue

    def list_queue(self) -> list[QueueItem]:
        return self.repo.list_queue()

    def _find(self, ref: str) -> QueueItem:
        items = self.repo.find_pending(ref)
        if not items:
            raise NotFound(f"No pending review matches {ref!r}.")
        if len(items) > 1:
            names = [i.invoice_ref for i in items]
            raise AmbiguousReference(f"{ref!r} matches several pending reviews: {', '.join(names)}", names)
        return items[0]

    def _pending_state(self, item: QueueItem) -> InvoiceState:
        snap = self.graph.get_state(self._config(item.trace_id))
        if "await_human" not in tuple(snap.next):
            raise NotFound(f"{item.invoice_ref} is queued but has no paused run to resume.")
        return InvoiceState.model_validate(snap.values)

    def preview_resolution(self, ref: str, *, fx_rate: Decimal | None = None) -> ResolutionPreview:
        item = self._find(ref)
        return human_agent.assess_resolution(self._pending_state(item), self.deps, fx_rate)

    def resolve(
        self, ref: str, *, action: Literal["approve", "reject"], note: str,
        fx_rate: Decimal | None = None, confirm_overspend: bool = False,
    ) -> InvoiceResult:
        item = self._find(ref)
        if not note or not note.strip():
            raise NoteRequired("A note is required to resolve a review.")
        state = self._pending_state(item)
        if action == "approve":
            preview = human_agent.assess_resolution(state, self.deps, fx_rate)
            if not preview.can_approve:
                raise ApproveNotAllowed(f"{preview.invoice_ref} cannot be approved; it can only be rejected.")
            if preview.needs_fx_rate and fx_rate is None:
                raise FxRateRequired(f"An exchange rate to USD is required for {preview.currency}.")
            if preview.needs_overspend_confirmation and not confirm_overspend:
                raise OverspendConfirmationRequired(
                    f"Approval overspends the monthly budget by {preview.overspend_usd} USD; confirm to proceed.",
                    preview.overspend_usd, preview.budget,
                )
        resolution = HumanResolution(
            action=action, note=note, fx_rate=fx_rate, confirm_overspend=confirm_overspend,
            resolved_at=self.clock(),
        )
        self.log.event("resume", trace_id=item.trace_id, invoice=item.file_name, data={"action": action})
        try:
            self._stream(Command(resume=resolution.model_dump(mode="json")), item.trace_id, item.file_name, None)
            return self._finish(item.invoice_id, item.trace_id)
        except Exception as exc:
            return self._error_result(
                exc, invoice_id=item.invoice_id, trace_id=item.trace_id, file_name=item.file_name
            )

    # ---------------------------------------------------------------- admin

    def reset(self, seed: Literal["demo", "readme"] = "demo") -> None:
        self.close()
        for suffix in ("", "-wal", "-shm", "-journal"):
            Path(str(self.settings.checkpoint_path) + suffix).unlink(missing_ok=True)
        self.repo = reset_database(self.settings.db_path, seed)
        self._wire()

    def banner(self) -> Banner:
        info = self._provider_info()
        return Banner(
            provider=info.name, models=dict(info.models), recording_date=info.recording_date,
            seed=self.repo.seed_name(), as_of=self.as_of, stock=self.repo.stock_levels(),
            budget=self.repo.budget_status(self.deps.budget_month, self.settings.monthly_budget_usd),
        )
