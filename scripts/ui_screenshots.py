"""Screenshot the real dashboard in light and dark mode at several widths (a dev tool, not run by pytest).

    uv run python scripts/ui_screenshots.py --out DIR --port 8599 [--state fresh|batch|walkthrough]
        [--modes light,dark] [--widths 1440,390] [--shots name=URLPATH ...]

The state is seeded with the replay provider on a temporary database, so nothing touches `acme.db`,
nothing calls a model and the result is deterministic. A shot's URL path may use `{trace_1010}`,
`{trace_1012}` and `{invoice_id_1014}`, which are filled in from the seeded state (`--state batch`
or `walkthrough`), for example `--shots 'process-1012=/?trace={trace_1012}'`.

Needs Node with Playwright (the newest `~/.npm/_npx/*/node_modules` that has it, or PW_NODE_MODULES)
and a Chromium headless shell (the newest in `~/.cache/ms-playwright`, or PW_CHROME).
Exit status is non-zero when any page shows a Streamlit exception.
"""

import argparse
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SHOTS = ("process=/", "queue=/queue", "ledger=/ledger", "batch=/batch")
_PATH_KEYS = ("db_path", "checkpoint_path", "log_dir", "replay_dir", "prompts_dir", "policy_path")


def write_settings(tmp: Path) -> Path:
    """A copy of config/settings.toml whose paths are absolute: state in `tmp`, assets in the repo."""
    targets = {
        "db_path": tmp / "acme.db", "checkpoint_path": tmp / "checkpoints.db", "log_dir": tmp / "logs",
        "replay_dir": REPO_ROOT / "fixtures/replay", "prompts_dir": REPO_ROOT / "llm/prompts",
        "policy_path": REPO_ROOT / "policy.md",
    }
    text = (REPO_ROOT / "config/settings.toml").read_text(encoding="utf-8")
    for key in _PATH_KEYS:
        text, n = re.subn(rf'^{key}\s*=.*$', f'{key} = "{targets[key]}"', text, flags=re.M)
        if n != 1:
            raise SystemExit(f"config/settings.toml: expected one {key} line, found {n}")
    out = tmp / "settings.toml"
    out.write_text(text, encoding="utf-8")
    return out


def seed(settings_path: Path, state: str) -> dict[str, str]:
    """Reset the demo data and, for `batch` and `walkthrough`, run the samples with the recorded model."""
    sys.path.insert(0, str(REPO_ROOT))
    sys.path.insert(0, str(REPO_ROOT / "tests/golden"))
    from config.settings import load_settings
    from service import Service

    ids: dict[str, str] = {}
    settings = load_settings(settings_path, env={})
    with Service(settings, provider_choice="replay", env={}, sleep=lambda s: None) as svc:
        svc.reset("demo")
        if state == "fresh":
            return ids
        batch = svc.run_batch(REPO_ROOT / "data/invoices")
        for result in batch.results:
            if result.file == "invoice_1010.txt":
                ids["trace_1010"] = result.trace_id
            if result.file == "invoice_1012.pdf":
                ids["trace_1012"] = result.trace_id
            if result.file == "invoice_1014.xml":
                ids["invoice_id_1014"] = str(svc.repo.get_invoice_by_trace(result.trace_id).id)
        if state == "walkthrough":
            from golden_expected import POST_BATCH_STEPS

            for ref, action, kwargs, expect in POST_BATCH_STEPS:
                if "raises" in expect:
                    continue
                svc.resolve(ref, action=action, note=kwargs["note"], fx_rate=kwargs.get("fx_rate"))
    return ids


def wait_healthy(port: int, proc: subprocess.Popen, timeout: float = 45.0) -> None:
    url = f"http://127.0.0.1:{port}/_stcore/health"
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            raise SystemExit(f"streamlit exited early with status {proc.returncode}")
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.read().decode().strip() == "ok":
                    return
        except OSError:
            pass
        time.sleep(0.3)
    raise SystemExit(f"streamlit did not become healthy on port {port} within {timeout:.0f} s")


def expand(shots: list[str], ids: dict[str, str]) -> list[str]:
    out = []
    for shot in shots:
        try:
            out.append(shot.format(**ids))
        except KeyError as exc:
            raise SystemExit(f"shot {shot!r} needs {exc}, which exists only with --state batch or walkthrough")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8599)
    parser.add_argument("--state", choices=("fresh", "batch", "walkthrough"), default="batch")
    parser.add_argument("--modes", default="light,dark")
    parser.add_argument("--widths", default="1440,390")
    parser.add_argument("--shots", nargs="*", default=list(DEFAULT_SHOTS), metavar="NAME=URLPATH")
    args = parser.parse_args(argv)

    args.out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="acme-shots-") as tmp_name:
        tmp = Path(tmp_name)
        settings_path = write_settings(tmp)
        ids = seed(settings_path, args.state)
        shots = expand(args.shots, ids)
        for name, value in ids.items():
            print(f"seeded {name}={value}", flush=True)

        env = {**os.environ, "ACME_SETTINGS": str(settings_path), "ACME_UI_PROVIDER": "replay", "XAI_API_KEY": ""}
        server_log = (tmp / "streamlit.log").open("w")
        proc = subprocess.Popen(
            [sys.executable, "-m", "streamlit", "run", "ui/app.py", "--server.headless", "true",
             "--server.port", str(args.port), "--browser.gatherUsageStats", "false",
             "--server.fileWatcherType", "none"],
            cwd=REPO_ROOT, env=env, stdout=server_log, stderr=subprocess.STDOUT,
        )
        try:
            wait_healthy(args.port, proc)
            node = subprocess.run(
                ["node", str(REPO_ROOT / "scripts/ui_screenshots.mjs"), "--base", f"http://127.0.0.1:{args.port}",
                 "--out", str(args.out), "--modes", args.modes, "--widths", args.widths,
                 *[x for shot in shots for x in ("--shot", shot)]],
                cwd=REPO_ROOT, env=os.environ,
            )
            return node.returncode
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
            server_log.close()


if __name__ == "__main__":
    sys.exit(main())
