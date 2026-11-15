import json
import shutil
from decimal import Decimal as D

import pytest

from config.settings import REPO_ROOT
from llm.fake import ScriptedFake
from llm.fake_golden import golden_script
from main import main
from models import PaymentWrite
from service import Service

SAMPLES = REPO_ROOT / "data" / "invoices"
INV = str(SAMPLES / "invoice_1001.txt")


@pytest.fixture
def run(settings, capsys):
    """run(*argv, **service_kwargs) -> (exit_code, stdout, stderr); each call gets a fresh Service on the tmp db."""

    def _run(*argv, **kwargs):
        def factory(args):
            return Service(settings, provider=ScriptedFake(golden_script()), env={},
                           sleep=lambda s: None, **kwargs)

        code = main(list(argv), service_factory=factory)
        out, err = capsys.readouterr()
        return code, out, err

    return _run


def queue_two(run, tmp_path):
    d = tmp_path / "pending"
    d.mkdir()
    for n in ("invoice_1012.pdf", "invoice_1014.xml"):
        shutil.copy(SAMPLES / n, d / n)
    code, _, _ = run("--dir", str(d), "--json")
    assert code == 0


def spend(settings, usd):
    with Service(settings, provider=ScriptedFake(golden_script()), env={}) as s:
        iid = s.repo.create_invoice(trace_id="t-s", file_path="p", file_name="s.json", file_hash="hh")
        s.repo.record_payment(PaymentWrite(
            idempotency_key="k-s", invoice_id=iid, trace_id="t-s", vendor="v", amount=usd, currency="USD",
            amount_usd=usd, attempts=1, month="2026-01", stock_deltas={}, bank_response={}))


def test_single_invoice_json_is_one_object(run):
    code, out, _ = run(f"--invoice_path={INV}", "--json")
    assert code == 0
    data = json.loads(out)  # whole stdout is exactly one JSON value
    assert data["status"] == "PAID" and data["file"] == "invoice_1001.txt"
    assert isinstance(data["amount"], str)  # Decimals are strings
    assert out.strip().startswith("{") and out.strip().endswith("}")


def test_hyphenated_flag_alias(run):
    code, out, _ = run("--invoice-path", INV, "--json")
    assert code == 0 and json.loads(out)["status"] == "PAID"


def test_rich_single_invoice(run):
    code, out, _ = run("--invoice_path", INV)
    assert code == 0
    assert "PAID" in out and "Provider" in out and "fake" in out
    assert "Payment receipt" in out and "Decision" in out and "Budget" in out
    assert "--reset" in out


def test_rich_banner_without_provider_name(run):
    _, out, _ = run("--invoice_path", INV)
    assert "As of" in out and "In stock" in out


def test_batch_json(run, tmp_path):
    d = tmp_path / "in"
    d.mkdir()
    for n in ("invoice_1001.txt", "invoice_1004.json", "invoice_1012.pdf"):
        shutil.copy(SAMPLES / n, d / n)
    code, out, _ = run("--dir", str(d), "--json")
    data = json.loads(out)
    assert code == 0 and len(data["results"]) == 3
    assert data["distinct_documents"] == 3 and "budget" in data
    assert data["counts"].get("PENDING_HUMAN") == 1


def test_batch_rich_summary(run, tmp_path):
    d = tmp_path / "in"
    d.mkdir()
    for n in ("invoice_1001.txt", "invoice_1004.json", "invoice_1012.pdf"):
        shutil.copy(SAMPLES / n, d / n)
    code, out, _ = run("--dir", str(d))
    assert code == 0
    assert "Batch summary" in out and "Automation" in out and "decided without a person" in out
    assert "invoice_1004.json" in out


def test_queue_json_and_rich(run, tmp_path):
    code, out, _ = run("--queue", "--json")
    assert code == 0 and json.loads(out) == {"queue": []}
    queue_two(run, tmp_path)
    code, out, _ = run("--queue", "--json")
    refs = [q["invoice_ref"] for q in json.loads(out)["queue"]]
    assert refs == ["INV-1012", "INV-1014"] or sorted(refs) == ["INV-1012", "INV-1014"]
    code, out, _ = run("--queue")
    assert "INV-1012" in out and "INV-1014" in out and "Recommendation" in out


def test_preview_json_and_rich(run, tmp_path):
    queue_two(run, tmp_path)
    code, out, _ = run("--resolve", "INV-1014", "--fx-rate", "1.1", "--json")
    p = json.loads(out)
    assert code == 0 and p["needs_fx_rate"] is True and p["fx_rate"] == "1.1"
    code, out, _ = run("--resolve", "INV-1012")
    assert code == 0 and "Preview: INV-1012" in out and "--approve" in out


def test_resolve_refusals_exit_3(run, settings, tmp_path):
    queue_two(run, tmp_path)
    code, out, err = run("--resolve", "INV-1014", "--approve", "--note", "ok")
    assert code == 3 and "--fx-rate" in err
    code, out, _ = run("--resolve", "INV-1014", "--approve", "--note", "ok", "--json")
    assert code == 3 and json.loads(out)["error"]["type"] == "FxRateRequired"
    spend(settings, D("45000"))
    code, out, err = run("--resolve", "INV-1012", "--approve", "--note", "ok")
    assert code == 3 and "--confirm-overspend" in err and "$50,000.00" in err and "$54,975.00" in err
    code, out, _ = run("--resolve", "INV-1012", "--approve", "--note", "ok", "--json")
    e = json.loads(out)["error"]
    assert code == 3 and e["type"] == "OverspendConfirmationRequired" and D(e["overspend_usd"]) == D("4975.00")
    code, out, _ = run("--resolve", "INV-9999", "--reject", "--note", "x", "--json")
    assert code == 3 and json.loads(out)["error"]["type"] == "NotFound"


def test_resolve_succeeds(run, tmp_path):
    queue_two(run, tmp_path)
    code, out, _ = run("--resolve", "INV-1014", "--approve", "--fx-rate", "1.1", "--note", "rate ok", "--json")
    r = json.loads(out)
    assert code == 0 and r["status"] == "PAID" and r["payment"]["fx_rate"] == "1.1"
    code, out, _ = run("--resolve", "INV-1012", "--reject", "--note", "no")
    assert code == 0 and "REJECTED" in out


def test_resolve_without_note_is_usage_error(run):
    for flag in ("--approve", "--reject"):
        code, _, err = run("--resolve", "X", flag)
        assert code == 2 and "--note" in err
    code, out, _ = run("--resolve", "X", "--approve", "--json")
    assert code == 2 and json.loads(out)["error"]["type"] == "UsageError"


@pytest.mark.parametrize("argv", [
    [], ["--queue", "--dir", "x"], ["--approve"], ["--seed", "demo", "--queue"],
    ["--resolve", "X", "--approve", "--reject", "--note", "n"], ["--queue", "--fx-rate", "abc"],
    ["--queue", "--as-of", "2026-13-01"], ["--queue", "--payment-fail-rate", "2"],
    ["--invoice_path", "/no/such/file.txt"],
])
def test_usage_errors(run, argv):
    code, _, _ = run(*argv)
    assert code == 2


def test_reset_json_and_rich(run, tmp_path):
    code, out, _ = run("--reset", "--json")
    assert code == 0 and json.loads(out) == {"reset": "demo"}
    code, out, _ = run("--reset", "--seed", "readme", "--json")
    assert json.loads(out) == {"reset": "readme"}
    code, out, _ = run("--reset")
    assert code == 0 and "Reset complete" in out


def test_reset_clears_history(run):
    run("--invoice_path", INV, "--json")
    run("--reset", "--json")
    code, out, _ = run("--invoice_path", INV, "--json")
    assert not json.loads(out)["short_circuited"]


def test_error_row_exit_1(settings, capsys, tmp_path):
    def hook(name, state):
        if name == "validate":
            raise RuntimeError("boom")

    def factory(args):
        return Service(settings, provider=ScriptedFake(golden_script()), env={}, sleep=lambda s: None,
                       node_hook=hook)

    assert main([f"--invoice_path={INV}", "--json"], service_factory=factory) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "ERROR"

    d = tmp_path / "in"
    d.mkdir()
    shutil.copy(SAMPLES / "invoice_1001.txt", d / "invoice_1001.txt")
    assert main(["--dir", str(d)], service_factory=factory) == 1
    out = capsys.readouterr().out
    assert "ERROR" in out and "boom" in out


def test_no_provider_exit_2(settings, capsys):
    def factory(args):
        return Service(settings, env={}, sleep=lambda s: None)

    code = main(["--invoice_path", INV], service_factory=factory)
    cap = capsys.readouterr()
    assert code == 2 and "XAI_API_KEY" in cap.err and "--provider replay" in cap.err
    code = main(["--invoice_path", INV, "--json"], service_factory=factory)
    err = json.loads(capsys.readouterr().out)["error"]
    assert code == 2 and err["type"] == "NoProviderAvailable"


def test_banner_without_provider_hints(settings, capsys):
    def factory(args):
        return Service(settings, env={}, sleep=lambda s: None)

    with Service(settings, env={}) as s:
        pass
    assert main(["--reset"], service_factory=factory) == 0
    out = capsys.readouterr().out
    assert "none" in out and "XAI_API_KEY" in out


def test_json_mode_keeps_stdout_clean(settings, capsys):
    def factory(args):
        print("library noise")
        return Service(settings, provider=ScriptedFake(golden_script()), env={}, sleep=lambda s: None)

    assert main([f"--invoice_path={INV}", "--json"], service_factory=factory) == 0
    cap = capsys.readouterr()
    json.loads(cap.out)
    assert "library noise" in cap.err


def test_main_does_not_load_dotenv(monkeypatch, run):
    import main as m

    assert not hasattr(m, "load_dotenv")


# ---------------------------------------------------------------- rejection reason

import datetime as _dt  # noqa: E402
import io  # noqa: E402

from rich.console import Console  # noqa: E402

from main import Renderer, rejection_reason  # noqa: E402
from models import (  # noqa: E402
    BatchResult, BudgetStatus, Ceiling, Decision, FlagCode, HumanResolution, HumanReview,
    InvoiceResult, Status, make_flag,
)


def _res(file="invoice_x.json", status=Status.REJECTED, number="INV-1", *, by=None, flags=(), clamped=False,
         human=None, reasoning="because the numbers do not add up at all"):
    decision = Decision(final=Ceiling.REJECT, by=by, reasoning=reasoning, clamped=clamped) if by else None
    return InvoiceResult(trace_id="t", file=file, invoice_number=number, status=status, provider="fake",
                         flags=[make_flag(FlagCode(c), "m") for c in flags], decision=decision, human=human)


def _flag_of(sev):
    from models import FLAG_SEVERITY
    return next(c.value for c in FlagCode if FLAG_SEVERITY[c] == sev)


def test_reason_none_unless_rejected():
    assert rejection_reason(_res(status=Status.PAID, by="vp")) is None
    assert rejection_reason(_res(status=Status.PENDING_HUMAN)) is None


def test_reason_rules_lists_error_flags_in_order():
    from models import FLAG_SEVERITY, Severity
    errs = [c.value for c in FlagCode if FLAG_SEVERITY[c] == Severity.ERROR][:2]
    esc = _flag_of(Severity.ESCALATION)
    r = _res(by="rules", flags=[errs[1], esc, errs[0], errs[1]])
    assert rejection_reason(r) == f"{errs[1]}, {errs[0]}"  # repeated codes are named once


def test_reason_vp_and_critic_name_escalation_flags():
    from models import Severity
    esc = _flag_of(Severity.ESCALATION)
    assert rejection_reason(_res(by="vp", flags=[esc])) == f"VP: {esc}"
    assert rejection_reason(_res(by="vp+critic", flags=[esc])) == f"VP+Critic: {esc}"


def test_reason_vp_without_flags_uses_short_reasoning():
    out = rejection_reason(_res(by="vp", reasoning="x " * 100))
    assert out.startswith("VP: x x") and len(out) <= 45 and out.endswith("…")


def test_reason_clamped_vp():
    from models import Severity
    esc = _flag_of(Severity.ESCALATION)
    assert rejection_reason(_res(by="vp", flags=[esc], clamped=True)) == f"VP: capped to reject ({esc})"


def test_reason_deadlock():
    from models import Severity
    esc = _flag_of(Severity.ESCALATION)
    assert rejection_reason(_res(by="deadlock", flags=[esc])) == f"deadlock: {esc}"


def _human(outcome, note):
    res = HumanResolution(action="reject", note=note or "n", resolved_at=_dt.datetime(2026, 1, 1))
    return HumanReview(reason=FlagCode.MATH_MISMATCH, reasons=[FlagCode.MATH_MISMATCH], outcome=outcome,
                       outcome_note=note, resolution=res)


def test_reason_human_note_and_stock():
    assert rejection_reason(_res(by="human", human=_human("rejected", "never delivered"))) == \
        "person: never delivered"
    assert rejection_reason(_res(by="human", human=_human("rejected_stock", None))) == "person: stock short"


@pytest.mark.parametrize("width", [80, 120])
def test_batch_render_shows_reasons_within_width(width):
    from models import FLAG_SEVERITY, Severity
    errs = [c.value for c in FlagCode if FLAG_SEVERITY[c] == Severity.ERROR][:2]
    esc = _flag_of(Severity.ESCALATION)
    results = [
        _res("invoice_1004_revised.json", number="INV-1004", by="rules", flags=errs),
        _res("invoice_2.json", number="INV-2", by="vp+critic", flags=[esc]),
        _res("invoice_3.json", number="INV-3", by="human", human=_human("rejected", "vendor never delivered")),
        _res("invoice_4.json", status=Status.PENDING_HUMAN, number="INV-4"),
    ]
    b = BatchResult(results=results, counts={"REJECTED": 3, "PENDING_HUMAN": 1}, distinct_documents=4,
                    decided_without_person=3, stock={},
                    budget=BudgetStatus(month="2026-01", limit=D(100), spent=D(0), remaining=D(100)))
    con = Console(width=width, file=io.StringIO(), color_system=None)
    Renderer(con).batch(b)
    out = con.file.getvalue()
    assert max(len(line) for line in out.splitlines()) <= width
    flat = "".join(out.split())
    assert "Reason" in out and "invoice_1004_revised.json" in out and "PENDING_HUMAN" in out
    for needle in (", ".join(errs), f"VP+Critic: {esc}", "person: vendor never delivered"):
        assert "".join(needle.split()) in flat
