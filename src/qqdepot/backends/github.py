"""GitHub backend: thin wrappers over the `gh` command line.

Every call returns as soon as GitHub has accepted it. Nothing here waits for a build: CI runs
on GitHub, and the verdict reaches the agent through qq's watcher (see watch.py).
"""
from __future__ import annotations

import json
import shutil
import subprocess

from qqdepot.backends import Change, ChangeError

FIELDS = "number,url,state,headRefOid,baseRefName,headRefName"


def _run(cmd: list[str], cwd=None) -> str:
    if cmd[0] == "gh" and shutil.which("gh") is None:
        raise ChangeError("qq needs the GitHub CLI `gh` on PATH (https://cli.github.com), signed in with gh auth login")
    try:
        return subprocess.run(cmd, cwd=cwd, check=True, text=True, capture_output=True).stdout
    except subprocess.CalledProcessError as e:
        detail = "\n".join((e.stderr or e.stdout or "").strip().splitlines()[-5:])
        raise ChangeError(f"{' '.join(cmd[:3])} failed (exit {e.returncode}):\n{detail}") from None


def _json(cmd: list[str], cwd=None):
    out = _run(cmd, cwd)
    try:
        return json.loads(out)
    except json.JSONDecodeError as e:
        raise ChangeError(f"{' '.join(cmd[:3])} printed non-JSON: {e}") from None


def _change(repo: str, pr: dict) -> Change:
    return Change(repo=repo, number=int(pr["number"]), url=pr["url"], state=pr["state"].lower(),
                  head=pr["headRefOid"], base=pr["baseRefName"], branch=pr["headRefName"])


def repo(given: str | None, cwd=None) -> str:
    if given:
        return given
    return _run(["gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"], cwd).strip()


def default_branch(repo: str) -> str:
    return _run(["gh", "repo", "view", repo, "--json", "defaultBranchRef", "--jq", ".defaultBranchRef.name"]).strip()


def current_branch(root) -> str:
    try:
        return _run(["git", "symbolic-ref", "--quiet", "--short", "HEAD"], root).strip()
    except ChangeError:
        raise ChangeError("HEAD is detached; check out a branch first (git switch -c <name>)") from None


def push(root, base: str) -> str:
    """Push the checked-out branch to origin under the same name and return the name."""
    branch = current_branch(root)
    if branch == base:
        raise ChangeError(f"you are on {base}, the branch changes land on; "
                          "commit on a new branch (git switch -c <name>) and run qq upload there")
    _run(["git", "push", "--quiet", "--set-upstream", "origin", f"HEAD:refs/heads/{branch}"], root)
    return branch


def find(repo: str, branch: str) -> Change | None:
    prs = _json(["gh", "pr", "list", "--repo", repo, "--head", branch, "--state", "open",
                 "--json", FIELDS, "--limit", "1"])
    return _change(repo, prs[0]) if prs else None


def create(repo: str, branch: str, base: str, title: str | None, body: str | None, draft: bool) -> Change:
    cmd = ["gh", "pr", "create", "--repo", repo, "--head", branch, "--base", base]
    cmd += ["--title", title, "--body", body or ""] if title else ["--fill"]
    if draft:
        cmd.append("--draft")
    _run(cmd)
    return view(repo, branch)


def view(repo: str, ref: str) -> Change:
    return _change(repo, _json(["gh", "pr", "view", str(ref), "--repo", repo, "--json", FIELDS]))


def runs(repo: str, sha: str) -> list[int]:
    """Workflow run IDs for one commit. GitHub starts them a moment after a push, so this may be
    empty when qq returns; the watcher and qq status read them again."""
    data = _json(["gh", "run", "list", "--repo", repo, "--commit", sha,
                  "--json", "databaseId", "--limit", "100"])
    return sorted(int(r["databaseId"]) for r in data)


def checks(repo: str, sha: str) -> dict[str, str]:
    """Check name -> conclusion, or status while running. A re-run is newer and decides."""
    out = _run(["gh", "api", "--paginate", f"repos/{repo}/commits/{sha}/check-runs?per_page=100&filter=all",
                "--jq", '.check_runs[] | {name, state: (.conclusion // .status), started: (.started_at // "9999")}'])
    seen: dict[str, tuple[str, str]] = {}
    for line in out.splitlines():
        if not line.strip():
            continue
        run = json.loads(line)
        if run["name"] not in seen or run["started"] >= seen[run["name"]][0]:
            seen[run["name"]] = (run["started"], run["state"])
    return {name: state for name, (_, state) in seen.items()}


def enqueue(repo: str, number: int, sha: str, method: str) -> None:
    """Merge once required checks pass (the merge queue when the repo has one). Returns at once.
    --match-head-commit refuses if someone pushed after the commit the gate is judging."""
    _run(["gh", "pr", "merge", str(number), "--repo", repo, "--auto", f"--{method}", "--match-head-commit", sha])


def token() -> str | None:
    try:
        return _run(["gh", "auth", "token"]).strip() or None
    except ChangeError:
        return None
