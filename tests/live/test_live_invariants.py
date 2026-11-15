"""DESIGN §14.1 live invariants: the real model runs the batch; only invariants are asserted, never exact outputs."""

import pytest

from config.settings import REPO_ROOT
from models import Ceiling, Severity, Verdict
from obs import JsonlLogger
from service import Service

pytestmark = pytest.mark.live
SAMPLES = REPO_ROOT / "data" / "invoices"
AS_OF = __import__("datetime").date(2026, 1, 31)


@pytest.fixture(scope="module")
def live_results(tmp_path_factory, live_settings, live_provider):
    root = tmp_path_factory.mktemp("live")
    settings = live_settings.model_copy(
        update={"db_path": root / "acme.db", "checkpoint_path": root / "checkpoints.db", "log_dir": root / "logs"}
    )
    with Service(settings, provider=live_provider, as_of=AS_OF, log=JsonlLogger(settings.log_dir)) as svc:
        return svc.run_batch(SAMPLES).results


def test_no_errors(live_results):
    assert [(r.file, r.error) for r in live_results if r.error] == []


def test_final_not_above_ceiling(live_results):
    for r in live_results:
        if r.decision is not None and r.ceiling is not None:
            assert r.decision.final <= r.ceiling, r.file


def test_every_escalation_flag_addressed(live_results):
    for r in live_results:
        vp = [t for t in r.transcript if t.actor == "vp" and t.kind == "decision"]
        if not vp:
            continue
        addressed = {fr["code"] for fr in vp[-1].output["flags_addressed"]}
        escalations = {f.code.value for f in r.flags if f.severity == Severity.ESCALATION}
        assert escalations <= addressed, (r.file, escalations - addressed)


def test_large_approvals_have_critic_agree(live_results):
    for r in live_results:
        if r.decision and r.decision.final == Ceiling.APPROVE and r.amount_usd is not None and r.amount_usd > 10000:
            critic = [t for t in r.transcript if t.actor == "critic"]
            assert critic and critic[-1].output["verdict"] == Verdict.AGREE.value, r.file
