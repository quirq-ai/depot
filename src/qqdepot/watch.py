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
ERROR_BUDGET = 600           # seconds of backend errors in a row before the watcher reports one
# How long a required check may stay unreported while nothing runs (no run started, or every run
# finished) before the change is refused: a check that never reported verified nothing (the
# gate's rule). The grace covers runs that start late (workflow_run, other CI apps).
DEFAULT_GRACE = 300
NOTIFY_ENV = "QQ_NOTIFY"


def run_id(kind: str, change: Change) -> str:
    return f"{kind}-{change.repo.replace('/', '-')}-{change.number}-{change.head[:12]}"


def verdict_path(rid: str) -> Path:
    return qq_home() / "verdicts" / f"{rid}.json"


def spawn(kind: str, backend: str, change: Change, notify: str | None,
          interval: float = DEFAULT_INTERVAL, deadline: float = DEFAULT_DEADLINE,
          method: str | None = None, enqueued: bool = False, grace: float = DEFAULT_GRACE) -> dict:
    """Start the watcher detached from this process and return what the agent needs at once.
    `method` is how a land merges once the gate passes; `enqueued` says it already is queued."""
    rid = run_id(kind, change)
    out = verdict_path(rid)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.unlink(missing_ok=True)   # a new try of the same commit gets a new verdict
    log = out.with_suffix(".log")
    cmd = [sys.executable, "-m", "qqdepot.watch", "--kind", kind, "--backend", backend,
           "--repo", change.repo, "--number", str(change.number), "--sha", change.head,
           "--interval", str(interval), "--deadline", str(deadline), "--grace", str(grace)]
    if notify:
        cmd += ["--notify", notify]
    if method:
        cmd += ["--method", method]
    if enqueued:
        cmd.append("--enqueued")
    # The watcher runs from $QQ_HOME, not the verdicts directory it writes, which may be removed.
    with log.open("a") as fh:   # append: an earlier watcher of this commit may still be writing
        p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=fh, stderr=subprocess.STDOUT,
                             cwd=qq_home(), start_new_session=True, close_fds=True)
    return {"run_id": rid, "verdict_file": str(out), "log": str(log), "watcher_pid": p.pid}


def deliver(rid: str, payload: dict, notify: str | None) -> None:
    """Write the verdict file, then run the notify command. A verdict file that cannot be written
    (its directory removed mid-watch, a full disk) still reaches the notify command.
    The payload carries check names a change's author chose: notify commands read it as data."""
    out = verdict_path(rid)
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_name(f".{out.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(payload, indent=2) + "\n")
        os.replace(tmp, out)
        print(f"qq: {rid}: {payload['result']}; verdict in {out}", flush=True)
    except OSError as e:
        print(f"qq: {rid}: {payload['result']}; cannot write {out}: {e}", flush=True)
    if not notify:
        return
    try:
        p = subprocess.run(shlex.split(notify), input=json.dumps(payload), text=True, capture_output=True,
                           timeout=120, env={**os.environ, "QQ_VERDICT": payload["result"], "QQ_RUN_ID": rid})
        if p.returncode:
            print(f"qq: notify command exited {p.returncode}: {p.stderr.strip()[-500:]}", flush=True)
    except (OSError, ValueError, subprocess.TimeoutExpired) as e:
        print(f"qq: notify command failed: {e}", flush=True)


class Watch:
    """One change followed until it has a final result."""

    def __init__(self, args, backend):
        self.args, self.backend = args, backend
        self.enqueued = args.enqueued
        self.unqueued = None  # since when a queued land has been neither queued nor merged
        self.idle = None      # since when checks are missing and nothing runs that could report them
        self.started = time.monotonic()

    def look(self, change: Change) -> tuple[str | None, dict | None]:
        """(final result or None to keep watching, gate verdict if one was read)."""
        a, be = self.args, self.backend
        if change.head != a.sha:
            return "superseded", None     # someone pushed again; that commit needs its own try
        if change.state == "merged":
            return "landed", None
        if change.state == "closed":
            return "closed", None
        if a.kind == "land" and self.enqueued:
            # Merged is checked first; the queue can drop a change whose merge result failed.
            # A while out of the queue, so a merge that has not shown up as merged yet is no drop.
            if be.queued(change.repo, change.number):
                self.unqueued = None
                return None, None
            self.unqueued = self.unqueued or time.monotonic()
            if time.monotonic() - self.unqueued >= a.grace / 5:
                return "dequeued", gate.verdict(change.repo, a.sha, be)
            return None, None
        v = gate.verdict(change.repo, a.sha, be)
        if v["result"] == "refused":
            return "refused", v
        if v["result"] == "pass":
            if a.kind == "try":
                return "pass", v
            be.enqueue(change.repo, change.number, a.sha, a.method)   # land only after the gate's pass
            self.enqueued = True
            return None, v
        if self._idle(v, be.runs(change.repo, a.sha)):
            self.idle = self.idle or time.monotonic()
            if time.monotonic() - self.idle >= a.grace:
                return "refused", {**v, "reason": "required checks never reported, and nothing is running"}
        else:
            self.idle = None
        return None, v

    @staticmethod
    def _idle(v: dict, runs: dict) -> bool:
        """Nothing that could still report: no check running and no run unfinished."""
        states = [f["state"] for f in v.get("failing", [])] + list(v.get("checks", {}).values())
        if any(s in gate.RUNNING for s in states):
            return False
        return not any(s in gate.RUNNING for s in runs.values())


def watch(args: argparse.Namespace) -> int:
    backend = backends.load(args.backend)
    w = Watch(args, backend)
    first_error, last, change = None, None, None
    while True:
        try:
            change = backend.view(args.repo, args.number)
            result, last = w.look(change)
            first_error = None
        except ChangeError as e:   # may be passing (network, rate limit, token): try again
            print(f"qq: {e}", flush=True)
            first_error = first_error or time.monotonic()
            result = "error" if time.monotonic() - first_error > ERROR_BUDGET else None
            if result:
                last = {"error": str(e)}
        except Exception as e:     # a bug: report it now rather than die without a verdict
            result, last = "error", {"error": f"internal error: {type(e).__name__}: {e}"}
        if result is None and time.monotonic() - w.started > args.deadline:
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
    ap.add_argument("--method", help="land: merge method once the gate passes")
    ap.add_argument("--enqueued", action="store_true", help="land: already queued")
    ap.add_argument("--interval", type=float, default=DEFAULT_INTERVAL)
    ap.add_argument("--deadline", type=float, default=DEFAULT_DEADLINE)
    ap.add_argument("--grace", type=float, default=DEFAULT_GRACE)
    return watch(ap.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
