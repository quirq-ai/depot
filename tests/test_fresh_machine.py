"""V0-DEP-01 done-when: on a fresh machine qq installs and runs the version the repo pins.

A fresh machine here is an empty QQ_HOME and HOME. The bootstrap (bin/qq) builds the
launcher from this checkout, the launcher reads the consumer repo's pin, installs that
version from a depot mirror and runs it. The mirror is a local git repo holding this
depot's source relabelled with the pinned version, so the test needs no release.
"""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

DEPOT = Path(__file__).resolve().parents[1]
PINNED = "0.0.9"

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture(scope="module")
def mirror(tmp_path_factory):
    """A depot mirror with tag v0.0.9 whose qq reports 0.0.9."""
    repo = tmp_path_factory.mktemp("depot-mirror")
    for name in ("pyproject.toml", "README.md", "src"):
        src = DEPOT / name
        (shutil.copytree if src.is_dir() else shutil.copy2)(src, repo / name)
    shutil.rmtree(repo / "src" / "qqdepot.egg-info", ignore_errors=True)
    init = repo / "src" / "qqdepot" / "__init__.py"
    init.write_text(init.read_text().replace('__version__ = "', f'__version__ = "{PINNED}"  # was "'))
    git(repo, "init", "-q", "-b", "main")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "mirror")
    git(repo, "tag", f"v{PINNED}")
    return repo


def fresh_machine(tmp_path: Path) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith("QQ_") and k != "VIRTUAL_ENV"}
    env["HOME"] = str(tmp_path / "home")
    env.pop("XDG_CACHE_HOME", None)
    return env


def consumer(tmp_path: Path, qq_table: str) -> Path:
    repo = tmp_path / "consumer"
    (repo / "infra").mkdir(parents=True)
    (repo / "infra" / "repo.toml").write_text(
        f'schema = "quirq-repo/1"\n{qq_table}\n[[targets]]\nname = "t"\nkind = "k"\n')
    (repo / "sub").mkdir()
    return repo


def qq(cwd: Path, env: dict, *args) -> subprocess.CompletedProcess:
    return subprocess.run([str(DEPOT / "bin" / "qq"), *args], cwd=cwd, env=env,
                          capture_output=True, text=True, timeout=600)


def test_installs_and_runs_pinned_version(tmp_path, mirror):
    env = fresh_machine(tmp_path)
    env["QQ_DEPOT_URL"] = mirror.as_uri()
    repo = consumer(tmp_path, f'[qq]\nversion = "{PINNED}"\n')

    first = qq(repo / "sub", env, "--version")
    assert first.returncode == 0, first.stderr
    assert first.stdout.strip() == f"qq {PINNED}"
    assert f"installing qq {PINNED}" in first.stderr
    home = tmp_path / "home" / ".cache" / "qq"
    assert (home / "versions" / PINNED / ".qq-installed").is_file()

    again = qq(repo, env, "--version")
    assert again.stdout.strip() == f"qq {PINNED}"
    assert "installing" not in again.stderr  # reused, not reinstalled


def test_pin_by_commit(tmp_path, mirror):
    env = fresh_machine(tmp_path)
    sha = git(mirror, "rev-parse", "HEAD")
    repo = consumer(tmp_path, f'[qq]\nversion = "{PINNED}"\nsource = "{mirror.as_uri()}"\ndigest = "git:{sha}"\n')
    run = qq(repo, env, "--version")
    assert run.returncode != 0 and "does not trust" in run.stderr   # nothing installed from it
    assert not (tmp_path / "home" / ".cache" / "qq" / "versions").exists()
    env["QQ_TRUSTED_SOURCES"] = mirror.as_uri()
    run = qq(repo, env, "--version")
    assert run.returncode == 0, run.stderr
    assert run.stdout.strip() == f"qq {PINNED}"


def test_outside_a_repo_runs_the_launcher(tmp_path):
    env = fresh_machine(tmp_path)
    run = qq(tmp_path, env, "--version")
    assert run.returncode == 0, run.stderr
    assert run.stdout.strip().startswith("qq ")
    assert "installing" not in run.stderr


def test_concurrent_first_runs(tmp_path, mirror):
    """Several sessions running qq for the first time at once each get the pinned version."""
    env = fresh_machine(tmp_path)
    env["QQ_DEPOT_URL"] = mirror.as_uri()
    repo = consumer(tmp_path, f'[qq]\nversion = "{PINNED}"\n')
    runs = [subprocess.Popen([str(DEPOT / "bin" / "qq"), "--version"], cwd=repo, env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(3)]
    results = [(r.wait(timeout=600), *r.communicate()) for r in runs]
    for code, out, err in results:
        assert code == 0, err
        assert out.strip() == f"qq {PINNED}"
    assert sum("setting up the launcher" in err for _, _, err in results) == 1
    assert sum("installing qq" in err for _, _, err in results) == 1
