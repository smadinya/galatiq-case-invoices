"""Ingestion nodes: load, parse, extract with the LLM, ground-check and normalize one document."""

from pathlib import Path
from typing import Any

import tools.grounding as grounding
import tools.normalize as normalizer
import tools.parsers as parsers
from agents._loop import run_structured
from core.text import quote_untrusted
from deps import Deps
from llm.base import load_prompt
from models import (
    CallContext,
    Ceiling,
    ExtractedInvoice,
    FlagCode,
    InvoiceState,
    Message,
    make_flag,
)

STAGE = "extraction"


def _unreadable(state: InvoiceState, deps: Deps, reason: str) -> dict[str, Any]:
    deps.repo.audit(trace_id=state.trace_id, actor="system", event="parse_failed", payload={"reason": reason})
    return {
        "parse_error": reason,
        "flags": state.flags
        + [make_flag(FlagCode.UNREADABLE_DOCUMENT, f"Document could not be read: {reason}", reason=reason)],
        "ceiling": Ceiling.HUMAN,
    }


def load_document(state: InvoiceState, deps: Deps) -> dict[str, Any]:
    path = Path(state.file_path)
    try:
        fmt = parsers.detect_format(path)
        out: dict[str, Any] = {"format": fmt}
        if fmt in ("txt", "pdf"):
            data = path.read_bytes()
            out["raw_text"] = parsers.decode_text(data) if fmt == "txt" else parsers.extract_pdf_text(data)
        elif not path.is_file():
            raise parsers.ParseError(f"file not found: {path.name}")
        return out
    except OSError as exc:
        return _unreadable(state, deps, f"cannot read file: {exc.__class__.__name__}")
    except parsers.ParseError as exc:
        return _unreadable(state, deps, str(exc))


def parse_structured(state: InvoiceState, deps: Deps) -> dict[str, Any]:
    try:
        data = Path(state.file_path).read_bytes()
        assert state.format is not None, "parse_structured needs state.format"
        extracted = parsers.parse_structured(state.format, data)
        spans = grounding.find_evidence_spans(extracted, parsers.decode_text(data))
    except OSError as exc:
        return _unreadable(state, deps, f"cannot read file: {exc.__class__.__name__}")
    except parsers.ParseError as exc:
        return _unreadable(state, deps, str(exc))
    return {"extracted": extracted, "evidence_spans": spans}


def extract_with_llm(state: InvoiceState, deps: Deps) -> dict[str, Any]:
    assert state.raw_text is not None, "extract_with_llm needs raw_text"
    prompt = load_prompt(STAGE, deps.settings.prompts_dir)
    ctx = CallContext(
        trace_id=state.trace_id,
        file_hash=state.file_hash,
        file_name=state.file_name,
        stage=STAGE,
        prompt_version=prompt.version,
        call_index=state.llm_calls.get(STAGE, 0),
    )
    content = quote_untrusted("Document text", state.raw_text)
    if state.grounding_errors:
        errors = "\n".join(f"- {e}" for e in state.grounding_errors)
        content += (
            "\n\nYour previous extraction did not match the document:\n"
            f"{errors}\n"
            "Extract the invoice again, copying every value verbatim from the document text."
        )
    output, usage = run_structured(
        provider=deps.provider,
        ctx=ctx,
        system=prompt.text,
        messages=[Message(role="user", content=content)],
        schema=ExtractedInvoice,
        log=deps.log,
    )
    attempt = state.extraction_attempts + 1
    deps.repo.audit(
        trace_id=state.trace_id,
        actor="system",
        event="extraction",
        payload={
            "attempt": attempt,
            "prompt_version": prompt.version,
            "corrections": [c.model_dump(mode="json") for c in output.corrections],
        },
    )
    return {
        "extracted": output,
        "corrections": list(output.corrections),
        "extraction_attempts": attempt,
        "llm_calls": {**state.llm_calls, STAGE: state.llm_calls.get(STAGE, 0) + 1},
        "usage": state.usage.plus_call(usage),
    }


def ground_check(state: InvoiceState, deps: Deps) -> dict[str, Any]:
    assert state.extracted is not None and state.raw_text is not None, "ground_check needs extraction and text"
    result = grounding.ground_check(state.extracted, state.raw_text)
    if result.ok:
        return {"grounding_errors": [], "evidence_spans": result.spans}
    if state.extraction_attempts <= deps.settings.extraction_retries:
        deps.repo.audit(
            trace_id=state.trace_id,
            actor="system",
            event="grounding_failed",
            payload={"attempt": state.extraction_attempts, "errors": result.errors},
        )
        deps.log.event(
            "retry",
            trace_id=state.trace_id,
            node="ground_check",
            invoice=state.file_name,
            data={"stage": STAGE, "attempt": state.extraction_attempts, "errors": result.errors},
        )
        return {"grounding_errors": result.errors}
    return {
        "grounding_errors": result.errors,
        **_unreadable(state, deps, "extraction not grounded in the document: " + "; ".join(result.errors)),
    }


def normalize(state: InvoiceState, deps: Deps) -> dict[str, Any]:
    assert state.extracted is not None, "normalize needs an extraction"
    try:
        result = normalizer.normalize(state.extracted)
    except normalizer.NormalizationError as exc:
        return _unreadable(state, deps, str(exc))
    return {"invoice": result.invoice, "normalized_changes": result.changes}
