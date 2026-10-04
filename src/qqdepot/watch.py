"""Acknowledge, then push: the verdict reaches the agent after qq has returned.

`qq try` and `qq land` start this watcher in its own session and exit at once with a run ID.
The watcher follows the change until the gate decides, then delivers the verdict:

  - always to $QQ_HOME/verdicts/<run ID>.json (written once, atomically), and
  - to the --notify command, if any, with the verdict JSON on stdin and QQ_VERDICT set to the
    result. An agent harness plugs its own channel in here (a message into the agent's session),
    so the agent never holds a tool call open while CI runs.

TODO(expert): the watcher polls the backend. Replace that with the backend's events (GitHub
check_suite webhooks, or a check the gate posts) so nothing polls.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from qqdepot import backends, gate
from qqdepot.backends import Change, ChangeError
from qqdepot.pin import qq_home

DEFAULT_INTERVAL = 30        # seconds between looks at the change
DEFAULT_DEADLINE = 3 * 3600  # give up and say so after this long
MAX_ERRORS = 5               # consecutive backend errors before the watcher reports one
NOTIFY_ENV = "QQ_NOTIFY"


def run_id(kind: str, change: Change) -> str:
    return f"{kind}-{change.repo.replace('/', '-')}-{change.number}-{change.head[:12]}"


def verdict_path(rid: str) -> Path:
    return qq_home() / "verdicts" / f"{rid}.json"


def spawn(kind: str, backend: str, change: Change, notify: str | None,
          interval: float = DEFAULT_INTERVAL, deadline: float = DEFAULT_DEADLINE) -> dict:
    """Start the watcher detached from this process and return what the agent needs at once."""
    rid = run_id(kind, change)
    out = verdict_path(rid)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.unlink(missing_ok=True)   # a new try of the same commit gets a new verdict
    log = out.with_suffix(".log")
    cmd = [sys.executable, "-m", "qqdepot.watch", "--kind", kind, "--backend", backend,
           "--repo", change.repo, "--number", str(change.number), "--sha", change.head,
           "--interval", str(interval), "--deadline", str(deadline)]
    if notify:
        cmd += ["--notify", notify]
    with log.open("w") as fh:
        p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=fh, stderr=subprocess.STDOUT,
                             cwd=out.parent, start_new_session=True, close_fds=True)
    return {"run_id": rid, "verdict_file": str(out), "log": str(log), "watcher_pid": p.pid}


def deliver(rid: str, payload: dict, notify: str | None) -> None:
    out = verdict_path(rid)
    tmp = out.with_name(f".{out.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n")
    os.replace(tmp, out)
    print(f"qq: {rid}: {payload['result']}; verdict in {out}", flush=True)
    if not notify:
        return
    try:
        p = subprocess.run(shlex.split(notify), input=json.dumps(payload), text=True, capture_output=True,
                           timeout=120, env={**os.environ, "QQ_VERDICT": payload["result"], "QQ_RUN_ID": rid})
        if p.returncode:
            print(f"qq: notify command exited {p.returncode}: {p.stderr.strip()[-500:]}", flush=True)
    except (OSError, ValueError, subprocess.TimeoutExpired) as e:
        print(f"qq: notify command failed: {e}", flush=True)


def decide(kind: str, change: Change, sha: str, backend) -> tuple[str | None, dict | None]:
    """(final result or None to keep watching, gate verdict if one was read)."""
    if change.head != sha:
        return "superseded", None     # someone pushed again; that commit needs its own try
    if change.state == "merged":
        return ("landed" if kind == "land" else "pass"), None
    if change.state == "closed":
        return "closed", None
    v = gate.verdict(change.repo, sha, backend)
    if v["result"] == "refused":
        return "refused", v
    if v["result"] == "pass" and kind == "try":
        return "pass", v
    return None, v                    # land waits for the merge itself, after the gate passes


def watch(args: argparse.Namespace) -> int:
    backend = backends.load(args.backend)
    start, errors, last = time.monotonic(), 0, None
    change = None
    while True:
        try:
            change = backend.view(args.repo, args.number)
            result, last = decide(args.kind, change, args.sha, backend)
            errors = 0
        except ChangeError as e:   # may be passing (network, rate limit): try again
            errors += 1
            print(f"qq: {e}", flush=True)
            result = "error" if errors >= MAX_ERRORS else None
            if result:
                last = {"error": str(e)}
        except Exception as e:     # a bug: report it now rather than die without a verdict
            result, last = "error", {"error": f"internal error: {type(e).__name__}: {e}"}
        if result is None and time.monotonic() - start > args.deadline:
            result = "timed-out"
        if result is not None:
            rid = f"{args.kind}-{args.repo.replace('/', '-')}-{args.number}-{args.sha[:12]}"
            deliver(rid, {
                "run_id": rid, "kind": args.kind, "result": result, "repo": args.repo,
                "number": args.number, "sha": args.sha, "url": change.url if change else None,
                "verdict": last, "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }, args.notify)
            return 0
        time.sleep(args.interval)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m qqdepot.watch", description=__doc__.splitlines()[0])
    ap.add_argument("--kind", choices=("try", "land"), required=True)
    ap.add_argument("--backend", required=True)
    ap.add_argument("--repo", required=True)
    ap.add_argument("--number", type=int, required=True)
    ap.add_argument("--sha", required=True)
    ap.add_argument("--notify")
    ap.add_argument("--interval", type=float, default=DEFAULT_INTERVAL)
    ap.add_argument("--deadline", type=float, default=DEFAULT_DEADLINE)
    return watch(ap.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
