"""V0-DEP-04: qq upload, try, land and status over a fake gh and a fake qqgate.

The done-when: an agent opens a change with `qq try`, gets a run ID back while CI is still
running, and later receives the verdict through --notify without any call of its own waiting.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

FAKES = Path(__file__).parent / "fakes"
REPO = "quirq-ai/demo"


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A checkout on branch `feature` whose origin is a local bare repo, and the fakes on PATH."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    work = tmp_path / "work"
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
    for k, v in (("user.name", "t"), ("user.email", "t@example.com")):
        git(work, "config", k, v)
    (work / "README").write_text("demo\n")
    git(work, "add", ".")
    git(work, "commit", "-qm", "base")
    git(work, "remote", "add", "origin", str(origin))
    git(work, "push", "-q", "origin", "main")
    git(work, "switch", "-qc", "feature")
    (work / "change.txt").write_text("one\n")
    git(work, "add", ".")
    git(work, "commit", "-qm", "a change")

    state = tmp_path / "gh.json"
    state.write_text(json.dumps({"repo": REPO, "default_branch": "main", "token": "fake-token",
                                 "prs": {}, "checks": {}, "runs": {}, "merges": [], "calls": []}))
    monkeypatch.setenv("PATH", f"{FAKES}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_GH_STATE", str(state))
    monkeypatch.setenv("FAKE_ORIGIN", str(origin))
    monkeypatch.setenv("QQ_GATE", str(FAKES / "qqgate"))
    monkeypatch.setenv("QQ_GATE_CONFIG", str(tmp_path / "infra-config"))
    monkeypatch.setenv("FAKE_GATE_REPOS", "demo")
    monkeypatch.setenv("FAKE_GATE_REQUIRED", "demo-presubmit")
    monkeypatch.delenv("QQ_NOTIFY", raising=False)
    monkeypatch.chdir(work)

    class World:
        def __init__(self):
            self.work, self.state, self.tmp = work, state, tmp_path

        def read(self):
            return json.loads(state.read_text())

        def update(self, fn):
            s = self.read()
            fn(s)
            tmp = state.with_suffix(".tmp")
            tmp.write_text(json.dumps(s))
            os.replace(tmp, state)

        def head(self):
            return git(work, "rev-parse", "HEAD")

        def set_checks(self, sha, checks):
            self.update(lambda s: s["checks"].__setitem__(sha, checks))

    return World()


def qq(*args, check=True):
    """Run qq as an agent would: a separate process that must return before the verdict exists."""
    p = subprocess.run([sys.executable, "-m", "qqdepot.cli", *args], capture_output=True, text=True,
                       env={**os.environ, "QQ_PINNED": "1"}, timeout=60)
    if check and p.returncode != 0:
        raise AssertionError(f"qq {' '.join(args)} exited {p.returncode}:\n{p.stdout}\n{p.stderr}")
    return p


def wait_for(path: Path, timeout=30) -> dict:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if path.is_file():
            return json.loads(path.read_text())
        time.sleep(0.1)
    raise AssertionError(f"no verdict at {path} after {timeout}s")


def notify_cmd(tmp_path):
    """A notify command standing in for an agent harness: it records what was pushed to it."""
    out = tmp_path / "pushed.json"
    script = tmp_path / "notify.py"
    script.write_text("import json, os, sys\n"
                      "d = json.load(sys.stdin); d['env_verdict'] = os.environ['QQ_VERDICT']\n"
                      f"open({str(out)!r}, 'w').write(json.dumps(d))\n")
    return f"{sys.executable} {script}", out


def test_try_returns_a_run_id_at_once_and_the_verdict_is_pushed_later(world):
    cmd, pushed = notify_cmd(world.tmp)
    sha = world.head()
    world.set_checks(sha, {"demo-presubmit": "in_progress"})
    world.update(lambda s: s["runs"].__setitem__(sha, [4242]))

    start = time.monotonic()
    receipt = json.loads(qq("try", "--json", "--notify", cmd, "--interval", "0.2", "--deadline", "60").stdout)
    assert time.monotonic() - start < 30   # returned while CI is still running
    assert receipt["run_id"] == f"try-quirq-ai-demo-1-{sha[:12]}"
    assert receipt["runs"] == [4242]
    assert receipt["change"]["head"] == sha and receipt["change"]["number"] == 1
    time.sleep(1)
    assert not pushed.exists() and not Path(receipt["verdict_file"]).exists()  # still running

    world.set_checks(sha, {"demo-presubmit": "success"})
    got = wait_for(pushed)
    assert got["result"] == "pass" and got["env_verdict"] == "pass"
    assert got["verdict"]["gated"] and got["verdict"]["green"] == ["demo-presubmit"]
    assert wait_for(Path(receipt["verdict_file"]))["result"] == "pass"
    # The gate read check results with the backend's credential, not one qq stores.
    assert Path(f"{world.state}.gate-token").read_text() == "fake-token"


def test_a_red_check_is_pushed_as_refused(world):
    cmd, pushed = notify_cmd(world.tmp)
    world.set_checks(world.head(), {"demo-presubmit": "failure"})
    qq("try", "--notify", cmd, "--interval", "0.2", "--deadline", "60")
    got = wait_for(pushed)
    assert got["result"] == "refused"
    assert got["verdict"]["failing"] == [{"name": "demo-presubmit", "state": "failure"}]


def test_upload_twice_updates_the_same_change(world):
    first = json.loads(qq("upload", "--json", "--title", "V0-X: demo").stdout)
    (world.work / "change.txt").write_text("two\n")
    git(world.work, "commit", "-qam", "more")
    second = json.loads(qq("upload", "--json").stdout)
    assert second["number"] == first["number"] == 1
    assert second["head"] == world.head() != first["head"]
    creates = [c for c in world.read()["calls"] if c[:2] == ["pr", "create"]]
    assert len(creates) == 1 and creates[0][creates[0].index("--title") + 1] == "V0-X: demo"


def test_a_new_push_supersedes_the_watched_commit(world):
    cmd, pushed = notify_cmd(world.tmp)
    world.set_checks(world.head(), {"demo-presubmit": "in_progress"})
    qq("try", "--notify", cmd, "--interval", "0.2", "--deadline", "60")
    (world.work / "change.txt").write_text("two\n")
    git(world.work, "commit", "-qam", "more")
    git(world.work, "push", "-q", "origin", "feature")
    assert wait_for(pushed)["result"] == "superseded"


def test_land_enqueues_with_the_gate_merge_method_and_pushes_landed(world):
    cmd, pushed = notify_cmd(world.tmp)
    sha = world.head()
    qq("upload")
    world.set_checks(sha, {"demo-presubmit": "success"})
    receipt = json.loads(qq("land", "--json", "--notify", cmd, "--interval", "0.2", "--deadline", "60").stdout)
    assert receipt["run_id"].startswith("land-")
    assert world.read()["merges"] == [["1", "--repo", REPO, "--auto", "--squash", "--match-head-commit", sha]]
    time.sleep(1)
    assert not pushed.exists()   # the gate passed, but land waits for the merge itself
    world.update(lambda s: s["prs"]["1"].update(state="MERGED", headRefOid=sha))
    assert wait_for(pushed)["result"] == "landed"


def test_status_exit_codes(world):
    qq("upload")
    sha = world.head()
    world.set_checks(sha, {"demo-presubmit": "queued"})
    assert qq("status", check=False).returncode == 3
    world.set_checks(sha, {"demo-presubmit": "success"})
    p = qq("status", check=False)
    assert p.returncode == 0 and "pass" in p.stdout and "green    demo-presubmit" in p.stdout
    world.set_checks(sha, {"demo-presubmit": "spoofed"})
    assert qq("status", "1", check=False).returncode == 1


def test_status_of_an_ungated_repo_reports_the_backend_checks(world, monkeypatch):
    monkeypatch.setenv("FAKE_GATE_REPOS", "other")
    qq("upload")
    world.set_checks(world.head(), {"lint": "success", "unit": "skipped"})
    p = qq("status", "--json", check=False)
    data = json.loads(p.stdout)
    assert p.returncode == 0 and data["result"] == "pass" and data["verdict"]["gated"] is False
    world.set_checks(world.head(), {})
    assert qq("status", check=False).returncode == 3   # no checks yet is not a pass


def test_refuses_to_upload_from_the_base_branch_or_a_detached_head(world):
    git(world.work, "switch", "-q", "main")
    p = qq("upload", check=False)
    assert p.returncode == 2 and "you are on main" in p.stderr
    git(world.work, "switch", "-q", "--detach", "HEAD")
    p = qq("try", check=False)
    assert p.returncode == 2 and "detached" in p.stderr


def test_status_without_a_change_says_to_upload(world):
    p = qq("status", check=False)
    assert p.returncode == 2 and "run qq upload first" in p.stderr


def test_missing_gh_is_an_actionable_error(world, monkeypatch):
    monkeypatch.setenv("PATH", str(Path(sys.executable).parent))
    p = qq("status", "1", check=False)
    assert p.returncode == 2 and "needs the GitHub CLI" in p.stderr
