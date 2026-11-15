"""Record real Grok calls as replay fixtures (fixtures/replay) plus manifest.json and expected_results.json.

Usage: uv run python scripts/record.py [--dir data/invoices] [--out fixtures/replay] [--only FILE] [--as-of 2026-01-31]
Needs XAI_API_KEY, GROK_EXTRACTION_MODEL and GROK_REASONING_MODEL (for example in .env).
"""

import argparse
import datetime as dt
import hashlib
import json
import os
import shutil
import sys
import tempfile
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tests" / "golden"))

from config.settings import load_settings  # noqa: E402
from golden_expected import GOLDEN_ROWS  # noqa: E402
from llm.base import load_prompt  # noqa: E402
from llm.openai_compat import OpenAICompatProvider  # noqa: E402
from llm.recorder import RecordingProvider  # noqa: E402
from models import Status  # noqa: E402
from service import Service  # noqa: E402

NEEDED = ("XAI_API_KEY", "GROK_EXTRACTION_MODEL", "GROK_REASONING_MODEL")
PROMPT_STAGES = ("extraction", "vp", "vp_memo", "critic")
MONTH_FMT = "%Y-%m"


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Record Grok calls as replay fixtures.")
    p.add_argument("--dir", default="data/invoices", help="invoice directory (default data/invoices)")
    p.add_argument("--out", default="fixtures/replay", help="output directory (default fixtures/replay)")
    p.add_argument("--only", default=None, help="record only this file name (merges into existing manifest)")
    p.add_argument("--as-of", default="2026-01-31", help="as-of date (default 2026-01-31)")
    return p.parse_args(argv)


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def summarize(res) -> dict:
    out = {
        "status": res.status.value,
        "flags": sorted(f.code.value for f in res.flags),
        "ceiling": res.ceiling.value if res.ceiling else None,
        "paid": str(res.payment.amount) if res.payment else None,
        "by": res.decision.by if res.decision else None,
        "rounds": len([t for t in res.transcript if t.actor == "critic"]),
    }
    if res.status == Status.PENDING_HUMAN and res.human is not None:
        out["human"] = {
            "reason": res.human.reason.value,
            "recommendation": res.human.recommendation.value if res.human.recommendation else None,
        }
    if res.error:
        out["error"] = res.error
    return out


def golden_diff(row, res) -> list[str]:
    diffs = []
    if res.status != row.status:
        diffs.append(f"status {res.status.value} != {row.status.value}")
    got, want = Counter(f.code.value for f in res.flags), Counter(row.flags)
    if got != want:
        diffs.append(f"flags +{sorted((got - want).elements())} -{sorted((want - got).elements())}")
    if res.ceiling != row.ceiling:
        diffs.append(f"ceiling {res.ceiling} != {row.ceiling}")
    paid = res.payment.amount if res.payment else None
    if paid != row.paid:
        diffs.append(f"paid {paid} != {row.paid}")
    return diffs


def print_reasoning(res) -> None:
    for turn in res.transcript:
        o = turn.output
        if turn.actor == "vp" and turn.kind == "decision":
            print(f"    VP (round {turn.round}): {o.get('decision')}")
            print(f"      reasoning: {o.get('reasoning')}")
            for fr in o.get("flags_addressed") or []:
                print(f"      flag {fr.get('code')}: {fr.get('response')}")
            for oa in o.get("objections_answered") or []:
                print(f"      objection answered: {oa}")
        elif turn.actor == "vp":
            print(f"    VP memo: {o.get('memo')}")
        else:
            print(f"    CRITIC (round {turn.round}): {o.get('verdict')} proposes {o.get('proposed_decision')}")
            for ob in o.get("objections") or []:
                print(f"      objection about {ob.get('about')}: {ob.get('evidence')} -> {ob.get('requested_change')}")
    if res.decision:
        print(f"    final: {res.decision.final.value} by {res.decision.by}: {res.decision.reasoning}")


def main(argv=None) -> int:
    args = parse_args(argv)
    missing = [n for n in NEEDED if not os.environ.get(n)]
    if missing:
        print("Missing required environment variables: " + ", ".join(missing), file=sys.stderr)
        return 2
    directory = Path(args.dir)
    if not directory.is_absolute():
        directory = REPO_ROOT / directory
    out_dir = Path(args.out)
    if not out_dir.is_absolute():
        out_dir = REPO_ROOT / out_dir
    as_of = dt.date.fromisoformat(args.as_of)
    files = sorted(p for p in directory.iterdir() if p.is_file())
    if args.only:
        files = [p for p in files if p.name == args.only]
        if not files:
            print(f"No such file in {directory}: {args.only}", file=sys.stderr)
            return 2

    env = dict(os.environ)
    settings = load_settings(env=env)
    hashes = {p.name: file_sha256(p) for p in files}

    if args.only:
        shutil.rmtree(out_dir / hashes[args.only][:16], ignore_errors=True)
    else:
        shutil.rmtree(out_dir, ignore_errors=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    inner = OpenAICompatProvider(
        api_key=env["XAI_API_KEY"],
        base_url=settings.xai_base_url,
        extraction_model=env["GROK_EXTRACTION_MODEL"],
        reasoning_model=env["GROK_REASONING_MODEL"],
    )
    provider = RecordingProvider(inner, out_dir)

    results = []
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        run_settings = settings.model_copy(
            update={
                "db_path": tmp_path / "acme.db",
                "checkpoint_path": tmp_path / "checkpoints.db",
                "log_dir": tmp_path / "logs",
            }
        )
        with Service(run_settings, provider=provider, as_of=as_of) as svc:
            if args.only:
                results.append(svc.run_invoice(files[0]))
            else:
                results = list(svc.run_batch(directory).results)
            stock = svc.repo.stock_levels()
            spent = svc.repo.budget_spent(as_of.strftime(MONTH_FMT))

    rows = {r.file: summarize(r) for r in results}
    by_file = {r.file: r for r in results}

    manifest_path = out_dir / "manifest.json"
    expected_path = out_dir / "expected_results.json"
    manifest = {"files": {}}
    expected = {"files": {}}
    if args.only:
        for path, target in ((manifest_path, manifest), (expected_path, expected)):
            if path.is_file():
                target.update(json.loads(path.read_text(encoding="utf-8")))
    manifest.update(
        {
            "recorded_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "models": {"extraction": env["GROK_EXTRACTION_MODEL"], "reasoning": env["GROK_REASONING_MODEL"]},
            "prompt_versions": {
                s: load_prompt(s, settings.prompts_dir).version for s in PROMPT_STAGES
            },
        }
    )
    manifest.setdefault("files", {}).update(hashes)
    expected.setdefault("files", {}).update(rows)
    if not args.only:
        expected["stock"] = stock
        expected["spent"] = str(spent)
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    expected_path.write_text(json.dumps(expected, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    golden = {r.file: r for r in GOLDEN_ROWS}
    print(f"{'file':28} {'status':14} {'paid':>10} {'by':10} {'rnd':>3}  flags")
    for name, r in rows.items():
        print(
            f"{name:28} {r['status']:14} {r['paid'] or '-':>10} {r['by'] or '-':10} {r['rounds']:>3}  "
            + ",".join(r["flags"])
        )
    print()

    matching, differing, errors = 0, [], []
    for name, res in by_file.items():
        row = golden.get(name)
        if res.status == Status.ERROR:
            errors.append(name)
            print(f"ERROR {name}: {res.error}")
            continue
        diffs = golden_diff(row, res) if row else ["not in GOLDEN_ROWS"]
        if diffs:
            differing.append(name)
        else:
            matching += 1
        if (row is not None and row.scripted) or diffs:
            tag = "DIFFERS" if diffs else "scripted row"
            print(f"--- {name} ({tag}) " + ("; ".join(diffs) if diffs else ""))
            print_reasoning(res)
    print()
    print(
        f"Summary: {matching} rows match golden, {len(differing)} differ"
        + (f" ({', '.join(differing)})" if differing else "")
        + (f", {len(errors)} ERROR ({', '.join(errors)})" if errors else "")
        + f". Final ledger {stock}, spent {spent}."
    )
    return 1 if errors else 0


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv(REPO_ROOT / ".env", override=False)
    sys.exit(main())
