"""qq upload, try, land and status: send a change through the gate without waiting on it.

    qq upload [--base B] [--title T] [--body B] [--draft]   push this branch, open or update its change
    qq try [CHANGE] [--notify CMD]       upload, then return a run ID; the verdict is pushed later
    qq land [CHANGE] [--notify CMD]      land once the gate passes (merge queue); returns at once
    qq status [CHANGE]                   the gate's verdict now: pass, refused or pending

CHANGE is a number, URL or branch; it defaults to the open change for the checked-out branch.
Acknowledge, then push: try and land print a run ID and exit. A watcher follows the change and
delivers the verdict to $QQ_HOME/verdicts/<run ID>.json and to --notify (default $QQ_NOTIFY),
so an agent never holds a tool call open while CI runs. Backend code is in qqdepot/backends.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from qqdepot import backends, gate, watch
from qqdepot.backends import ChangeError

BACKEND_ENV = "QQ_BACKEND"
# Exit codes of qq status. 2 is "could not decide", as for qqgate.
EXIT = {"pass": 0, "landed": 0, "refused": 1, "closed": 1, "pending": 3}


def _print(args, data: dict, lines: list[str]) -> None:
    print(json.dumps(data, indent=2) if args.json else "\n".join(lines))


def _change(be, repo: str, ref: str | None):
    if ref:
        return be.view(repo, ref)
    branch = be.current_branch(Path.cwd())
    change = be.find(repo, branch)
    if change is None:
        raise ChangeError(f"no open change for branch {branch} in {repo}; run qq upload first")
    return change


def _upload(be, args):
    repo = be.repo(args.repo)
    base = args.base or be.default_branch(repo)
    branch = be.push(Path.cwd(), base)
    change = be.find(repo, branch)
    if change is None:
        change = be.create(repo, branch, base, args.title, args.body, args.draft)
    else:
        change = be.view(repo, change.number)   # the push moved its head
    return change


def run_upload(args, be) -> int:
    change = _upload(be, args)
    _print(args, change.to_json(), [f"{change.url} ({change.branch} -> {change.base}) at {change.head[:12]}"])
    return 0


def _runs(be, change) -> list[int]:
    """Best effort: the watcher is already running, so a failure here must not read as one."""
    try:
        return sorted(be.runs(change.repo, change.head))
    except ChangeError:
        return []


def _receipt(args, kind: str, change, be, **land) -> int:
    notify = args.notify if args.notify is not None else os.environ.get(watch.NOTIFY_ENV)
    w = watch.spawn(kind, args.backend, change, notify or None, args.interval, args.deadline,
                    grace=args.grace, **land)
    receipt = {**w, "change": change.to_json(), "runs": _runs(be, change)}
    lines = [f"{kind} {w['run_id']}: {change.url} at {change.head[:12]}",
             f"  the verdict is pushed to {w['verdict_file']}" + (f" and to {notify}" if notify else ""),
             f"  qq status {change.number} --repo {change.repo} shows it now"]
    _print(args, receipt, lines)
    return 0


def run_try(args, be) -> int:
    change = be.view(be.repo(args.repo), args.change) if args.change else _upload(be, args)
    if change.state != "open":
        raise ChangeError(f"{change.url} is {change.state}; nothing to try")
    return _receipt(args, "try", change, be)


def run_land(args, be) -> int:
    change = _change(be, be.repo(args.repo), args.change)
    if change.state != "open":
        raise ChangeError(f"{change.url} is {change.state}; nothing to land")
    method = args.method
    if method is None:
        req = gate.required(change.repo)
        # suraj, 2026-10-04: squash and merge is the default policy; gate.toml decides gated repos.
        method = req["merge_method"] if req else "squash"
    # Nothing is queued before the gate passes: without a merge queue, gh would merge at once.
    v = gate.verdict(change.repo, change.head, be)
    if v["result"] == "refused":
        data = {"result": "refused", "change": change.to_json(), "verdict": v}
        _print(args, data, [f"{change.url} at {change.head[:12]}: refused, so not landing it"]
               + [f"  failing  {f['name']} ({f['state']})" for f in v.get("failing", [])]
               + [f"  missing  {n}" for n in v.get("missing", [])])
        return 1
    if v["result"] == "pass":
        be.enqueue(change.repo, change.number, change.head, method)
    return _receipt(args, "land", change, be, method=method, enqueued=v["result"] == "pass")


def run_status(args, be) -> int:
    change = _change(be, be.repo(args.repo), args.change)
    if change.state != "open":
        result, v = ("landed" if change.state == "merged" else "closed"), None
    else:
        v = gate.verdict(change.repo, change.head, be)
        result = v["result"]
    data = {"result": result, "change": change.to_json(), "verdict": v}
    lines = [f"{change.url} at {change.head[:12]}: {result}" + ("" if not v or v["gated"] else " (ungated)")]
    if v and v["gated"]:
        lines += [f"  green    {n}" for n in v["green"]]
        lines += [f"  failing  {f['name']} ({f['state']})" for f in v["failing"]]
        lines += [f"  missing  {n}" for n in v["missing"]]
    elif v:
        lines += [f"  {s:<9}{n}" for n, s in sorted(v["checks"].items())]
    _print(args, data, lines)
    return EXIT[result]


def _wrap(fn):
    def run(args) -> int:
        try:
            return fn(args, backends.load(args.backend))
        except ChangeError as e:
            print(f"qq {args.command}: {e}", file=sys.stderr)
            return 2
    return run


def register(sub) -> None:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--repo", help="OWNER/NAME (default: the repo of this checkout)")
    common.add_argument("--backend", default=os.environ.get(BACKEND_ENV, "github"),
                        help=f"where changes are reviewed and landed (default ${BACKEND_ENV} or github)")
    common.add_argument("--json", action="store_true", help="print JSON")
    uploading = argparse.ArgumentParser(add_help=False)
    uploading.add_argument("--base", help="the branch to land on (default: the repo's default branch)")
    uploading.add_argument("--title", help="the change's title (default: from the commits)")
    uploading.add_argument("--body", help="the change's description")
    uploading.add_argument("--draft", action="store_true", help="open it as a draft")
    watching = argparse.ArgumentParser(add_help=False)
    watching.add_argument("--notify", help=f"command that gets the verdict JSON on stdin (default ${watch.NOTIFY_ENV})")
    watching.add_argument("--interval", type=float, default=watch.DEFAULT_INTERVAL, help=argparse.SUPPRESS)
    watching.add_argument("--deadline", type=float, default=watch.DEFAULT_DEADLINE, help=argparse.SUPPRESS)
    watching.add_argument("--grace", type=float, default=watch.DEFAULT_GRACE, help=argparse.SUPPRESS)
    changing = argparse.ArgumentParser(add_help=False)
    changing.add_argument("change", nargs="?", help="number, URL or branch (default: this branch's change)")

    p = sub.add_parser("upload", parents=[common, uploading], help="push this branch and open or update its change")
    p.set_defaults(run=_wrap(run_upload))
    p = sub.add_parser("try", parents=[common, uploading, watching, changing],
                       help="upload, return a run ID now, push the gate's verdict later")
    p.set_defaults(run=_wrap(run_try))
    p = sub.add_parser("land", parents=[common, watching, changing], help="land through the gate; returns at once")
    p.add_argument("--method", choices=("merge", "squash", "rebase"),
                   help="merge method (default: gate.toml merge_method for gated repos, else squash)")
    p.set_defaults(run=_wrap(run_land))
    p = sub.add_parser("status", parents=[common, changing],
                       help="the gate's verdict now (exit 0 pass, 1 refused, 3 pending)")
    p.set_defaults(run=_wrap(run_status))
