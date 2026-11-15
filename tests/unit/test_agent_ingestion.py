import json
from pathlib import Path

import pytest

import tools.parsers as parsers
from agents import ingestion
from config.settings import REPO_ROOT
from llm.fake import ScriptedFake
from llm.fake_golden import GOLDEN_EXTRACTIONS
from models import Ceiling, ExtractedInvoice, FlagCode
from tools.grounding import ground_check as core_ground_check

INVOICES = REPO_ROOT / "data" / "invoices"


def real_text(name: str) -> str:
    data = (INVOICES / name).read_bytes()
    return parsers.extract_pdf_text(data) if name.endswith(".pdf") else parsers.decode_text(data)


@pytest.mark.parametrize("name", sorted(GOLDEN_EXTRACTIONS))
def test_golden_extractions_ground_against_real_files(name):
    result = core_ground_check(GOLDEN_EXTRACTIONS[name], real_text(name))
    assert result.ok, result.errors


def unreadable(out):
    assert out["ceiling"] == Ceiling.HUMAN
    assert [f.code for f in out["flags"]] == [FlagCode.UNREADABLE_DOCUMENT]
    assert out["parse_error"]


def test_load_txt_and_pdf_set_raw_text(make_state, make_deps):
    deps = make_deps()
    for name, fmt in (("invoice_1001.txt", "txt"), ("invoice_1011.pdf", "pdf")):
        st = make_state(file_name=name, file_path=str(INVOICES / name), format=None)
        out = ingestion.load_document(st, deps)
        assert out["format"] == fmt
        assert out["raw_text"] and "Widget" in out["raw_text"]


def test_load_structured_has_no_raw_text(make_state, make_deps):
    st = make_state(file_name="invoice_1004.json", file_path=str(INVOICES / "invoice_1004.json"), format=None)
    out = ingestion.load_document(st, make_deps())
    assert out == {"format": "json"}


def test_parse_structured_sets_extracted_and_spans(make_state, make_deps):
    st = make_state(file_name="invoice_1004.json", file_path=str(INVOICES / "invoice_1004.json"), format="json")
    out = ingestion.parse_structured(st, make_deps())
    assert out["extracted"].invoice_number
    assert isinstance(out["evidence_spans"], dict)
    assert "raw_text" not in out


def test_corrupt_json_is_unreadable(make_state, make_deps, tmp_path, repo):
    p = tmp_path / "bad.json"
    p.write_text("{not json")
    st = make_state(file_name="bad.json", file_path=str(p), format="json")
    out = ingestion.parse_structured(st, make_deps())
    unreadable(out)
    assert [a.event for a in repo.audit_for_trace(st.trace_id)] == ["parse_failed"]


def test_garbage_pdf_is_unreadable(make_state, make_deps, tmp_path):
    p = tmp_path / "bad.pdf"
    p.write_bytes(b"this is not a pdf")
    st = make_state(file_name="bad.pdf", file_path=str(p))
    unreadable(ingestion.load_document(st, make_deps()))


def test_unsupported_and_missing_files_are_unreadable(make_state, make_deps, tmp_path):
    st = make_state(file_name="x.docx", file_path=str(tmp_path / "x.docx"), format=None)
    unreadable(ingestion.load_document(st, make_deps()))
    st = make_state(file_name="gone.txt", file_path=str(tmp_path / "gone.txt"))
    unreadable(ingestion.load_document(st, make_deps()))
    st = make_state(file_name="gone.json", file_path=str(tmp_path / "gone.json"), format="json")
    unreadable(ingestion.parse_structured(st, make_deps()))


def run_extraction(make_state, make_deps, steps, name="invoice_1001.txt"):
    fake = ScriptedFake({(name, "extraction"): steps})
    deps = make_deps(fake)
    st = make_state(file_name=name, file_path=str(INVOICES / name), raw_text=real_text(name))
    return st, deps, fake


def step(st, deps):
    out = ingestion.extract_with_llm(st, deps)
    st = st.model_copy(update=out)
    out2 = ingestion.ground_check(st, deps)
    return st.model_copy(update=out2), out, out2


BAD = ExtractedInvoice.model_validate({"invoice_number": "9999", "total": "$123,456.78"})


def test_extraction_retry_then_success(make_state, make_deps, memlog, repo):
    good = GOLDEN_EXTRACTIONS["invoice_1001.txt"]
    st, deps, fake = run_extraction(make_state, make_deps, [BAD, good])
    st, out1, g1 = step(st, deps)
    assert g1["grounding_errors"] and "flags" not in g1
    assert st.extraction_attempts == 1
    assert len(memlog.of("retry")) == 1
    st, out2, g2 = step(st, deps)
    assert st.extraction_attempts == 2
    assert g2["grounding_errors"] == [] and "flags" not in g2
    assert st.llm_calls == {"extraction": 2}
    assert st.usage.llm_calls == 2
    assert [c.ctx.call_index for c in fake.calls] == [0, 1]
    second_msg = fake.calls[1].messages[0].content
    assert "did not match" in second_msg and "verbatim" in second_msg
    assert st.grounding_errors == []
    events = [a.event for a in repo.audit_for_trace(st.trace_id)]
    assert events == ["extraction", "grounding_failed", "extraction"]


def test_two_bad_extractions_are_unreadable(make_state, make_deps):
    st, deps, _ = run_extraction(make_state, make_deps, [BAD, BAD])
    st, _, g1 = step(st, deps)
    assert "ceiling" not in g1
    st, _, g2 = step(st, deps)
    unreadable(g2)
    assert g2["grounding_errors"]
    assert "not grounded" in g2["parse_error"]


def test_extract_uses_untrusted_quote(make_state, make_deps):
    st, deps, fake = run_extraction(make_state, make_deps, [GOLDEN_EXTRACTIONS["invoice_1001.txt"]])
    ingestion.extract_with_llm(st, deps)
    content = fake.calls[0].messages[0].content
    assert content.startswith("Document text [untrusted document text")
    assert fake.calls[0].ctx.prompt_version == "extraction-v1"


def test_normalize_ok_and_failure(make_state, make_deps):
    st = make_state(extracted=GOLDEN_EXTRACTIONS["invoice_1001.txt"])
    out = ingestion.normalize(st, make_deps())
    assert out["invoice"].invoice_number and "normalized_changes" in out
    bad = ExtractedInvoice.model_validate({"invoice_number": "X"})
    out = ingestion.normalize(make_state(extracted=bad), make_deps())
    unreadable(out)


def test_all_goldens_normalize(make_state, make_deps):
    for name, ex in GOLDEN_EXTRACTIONS.items():
        out = ingestion.normalize(make_state(extracted=ex), make_deps())
        assert "invoice" in out, name


def test_state_json_roundtrip_of_raw_text_only_for_text(make_state, make_deps):
    st = make_state(file_name="invoice_1006.csv", file_path=str(INVOICES / "invoice_1006.csv"))
    out = ingestion.load_document(st, make_deps())
    assert "raw_text" not in out
    json.dumps({k: str(v) for k, v in out.items()})
