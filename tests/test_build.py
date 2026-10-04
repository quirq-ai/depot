"""V0-DEP-03: qq build and qq test run the same adapter actions as CI.

CI runs a repo's goal through recipes (`qqrecipes execute`). These tests run `qq test` and
that CI command on one commit and check both leave the same JUnit results.
"""
import re
import subprocess
import textwrap
from pathlib import Path

import pytest
from qqrecipes import cli as recipes
from qqrecipes import loader

from qqdepot import cli
from qqdepot.commands import build


@pytest.fixture(autouse=True)
def parity_adapters(monkeypatch):
    monkeypatch.setattr(loader, "PACKAGE", "parity_adapters")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "a.txt").write_text("good\n")
    (tmp_path / "data" / "b.txt").write_text("good\n")
    (tmp_path / "infra").mkdir()
    (tmp_path / "infra" / "repo.toml").write_text(textwrap.dedent("""\
        schema = "quirq-repo/1"
        [[targets]]
        name = "files"
        kind = "check-files"
        srcs = ["data/a.txt", "data/b.txt"]
        """))
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                    "commit", "-q", "-m", "one commit"], check=True)
    return tmp_path


def junit(out: Path) -> dict[str, str]:
    """Every JUnit file under `out`, with timings removed: they differ run to run."""
    return {str(p.relative_to(out)): re.sub(r' time="[^"]*"', "", p.read_text())
            for p in sorted(out.rglob("*.xml"))}


def test_local_and_ci_junit_match(repo, monkeypatch, tmp_path):
    (repo / "sub").mkdir()
    monkeypatch.chdir(repo / "sub")
    assert cli.main(["test"]) == 0
    local = junit(repo / ".qq" / "out")

    ci_out = tmp_path / "ci-out"
    assert recipes.main(["execute", "test", "--repo", str(repo), "--out", str(ci_out)]) == 0
    assert local == junit(ci_out)
    assert set(local) == {"junit/check.xml", "junit/files.build.copy.xml"}
    assert local["junit/check.xml"].count("<testcase") == 2


def test_failures_match_too(repo, monkeypatch, tmp_path):
    (repo / "data" / "b.txt").write_text("bad\n")
    monkeypatch.chdir(repo)
    assert cli.main(["test"]) == 1
    ci_out = tmp_path / "ci-out"
    assert recipes.main(["execute", "test", "--repo", str(repo), "--out", str(ci_out)]) == 1
    local = junit(repo / ".qq" / "out")
    assert local == junit(ci_out)
    assert 'failures="1"' in local["junit/check.xml"]


def test_build_only_builds(repo, monkeypatch):
    monkeypatch.chdir(repo)
    (repo / "data" / "b.txt").write_text("bad\n")  # the test would fail; a build must not run it
    assert cli.main(["build", "files"]) == 0
    assert (repo / "build-out" / "all").read_text() == "good\nbad\n"
    assert not (repo / ".qq" / "out" / "junit" / "check.xml").exists()


def test_outside_a_repo(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert cli.main(["test"]) == 2
    assert "inside a repo" in capsys.readouterr().err


def test_synced_toolchains_are_passed(repo):
    (repo / ".qq" / "toolchains" / "sh").mkdir(parents=True)
    args = cli.build_parser().parse_args(["test", "files", "--toolchain", "x=/opt/x", "--keep-going"])
    argv = build.recipes_argv("test", args, repo)
    assert argv == ["execute", "test", "--repo", str(repo), "--target=files",
                    f"--toolchain=sh={repo / '.qq' / 'toolchains' / 'sh'}", "--toolchain=x=/opt/x",
                    "--keep-going"]
