"""qq run: a command, or a saved one, runs from the repo root with the repo's pinned toolchains."""
import hashlib
import io
import os
import stat
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from qqdepot import cli


def toolchain(tmp_path: Path, message: str) -> tuple[str, str]:
    """A toolchain archive with bin/hello printing `message`: (file URI, sha256 digest)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        script = f"#!/bin/sh\necho '{message}'\n".encode()
        info = tarfile.TarInfo("bin/hello")
        info.size, info.mode = len(script), 0o755
        tar.addfile(info, io.BytesIO(script))
    archive = tmp_path / f"hello-{hashlib.sha256(message.encode()).hexdigest()[:8]}.tar.gz"
    archive.write_bytes(buf.getvalue())
    return archive.as_uri(), "sha256:" + hashlib.sha256(buf.getvalue()).hexdigest()


def qq(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    """qq in its own process: `qq run` replaces the process with the command."""
    return subprocess.run([sys.executable, "-m", "qqdepot.cli", *args], cwd=cwd, capture_output=True, text=True,
                          env={**os.environ, "QQ_PINNED": "1"})


@pytest.fixture
def repo(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "repo"
    (root / "infra").mkdir(parents=True)
    (root / "infra" / "repo.toml").write_text('schema = "quirq-repo/1"\n[[targets]]\nname = "t"\nkind = "k"\n')
    (root / "sub").mkdir()
    monkeypatch.chdir(root / "sub")
    return root


def pin_hello(root: Path, tmp_path: Path, message: str = "pinned hello") -> None:
    source, digest = toolchain(tmp_path, message)
    manifest = root / "infra" / "repo.toml"
    text = manifest.read_text().split("[toolchains.hello]")[0]
    manifest.write_text(text + f'[toolchains.hello]\nsource = "{source}"\ndigest = "{digest}"\n')


def test_command_line_runs_from_repo_root_with_exit_code(repo):
    r = qq("run", "pwd; echo $PWD; exit 3")
    assert r.returncode == 3
    assert r.stdout.split() == [str(repo), str(repo)]


def test_several_words_run_without_a_shell(repo):
    r = qq("run", "printf", "%s|", "a b", "$HOME", ";", "x")
    assert (r.returncode, r.stdout) == (0, "a b|$HOME|;|x|")


def test_double_dash_and_options_after_the_command(repo):
    r = qq("run", "--", "printf", "%s", "--list")
    assert (r.returncode, r.stdout) == (0, "--list")


def test_missing_program_is_127(repo):
    r = qq("run", "no-such-program-here", "x")
    assert r.returncode == 127
    assert "command not found" in r.stderr


def test_qq_becomes_the_command(repo):
    """Signals reach the command itself: qq is not left as a parent that dies alone."""
    r = qq("run", "kill -TERM $$")
    assert r.returncode == -15
    r = qq("run", "echo $PPID")   # the shell's parent is whoever started qq
    assert (r.returncode, r.stdout.strip()) == (0, str(os.getpid()))


def test_outside_a_repo(tmp_path, monkeypatch, capfd):
    monkeypatch.chdir(tmp_path)
    assert cli.main(["run", "true"]) == 2
    assert "inside a repo" in capfd.readouterr().err


def test_nothing_to_run(repo, capfd):
    assert cli.main(["run"]) == 2
    assert "nothing to run" in capfd.readouterr().err


@pytest.mark.parametrize("argv, message", [
    (["--force", "true"], "--force only goes with --save"),
    (["--list", "x"], "take no command"),
    (["--show", "x", "y"], "take no command"),
])
def test_option_misuse(repo, capfd, argv, message):
    assert cli.main(["run", *argv]) == 2
    assert message in capfd.readouterr().err


def test_no_abbreviated_options(repo, capfd):
    with pytest.raises(SystemExit):
        cli.main(["run", "--sav", "x", "true"])


def test_synced_toolchain_comes_first_on_path(repo, tmp_path, monkeypatch):
    pin_hello(repo, tmp_path)
    shadow = tmp_path / "shadow"
    shadow.mkdir()
    (shadow / "hello").write_text("#!/bin/sh\necho 'from PATH'\n")
    (shadow / "hello").chmod(0o755)
    monkeypatch.setenv("PATH", f"{shadow}:/usr/bin:/bin")

    for argv in (["hello"], ["--", "hello", "x"]):
        r = qq("run", *argv)
        assert (r.returncode, r.stdout.strip()) == (0, "from PATH")
        assert "not synced here: hello" in r.stderr

    assert cli.main(["sync"]) == 0
    for argv in (["hello"], ["--", "hello", "x"]):
        r = qq("run", *argv)
        assert (r.returncode, r.stdout.strip()) == (0, "pinned hello")
        assert "not synced" not in r.stderr

    # A new pin is not run until it is synced: the old toolchain never stands in for it.
    pin_hello(repo, tmp_path, "pinned hello v2")
    r = qq("run", "hello")
    assert (r.returncode, r.stdout.strip()) == (0, "from PATH")
    assert "not synced here: hello" in r.stderr
    assert cli.main(["sync"]) == 0
    assert qq("run", "hello").stdout.strip() == "pinned hello v2"


def test_committed_toolchain_never_lands_on_path(repo, tmp_path, monkeypatch):
    pin_hello(repo, tmp_path)
    fake = repo / ".qq" / "toolchains" / "hello" / "bin"
    fake.mkdir(parents=True)
    (fake / "hello").write_text("#!/bin/sh\necho 'from the repo'\n")
    (fake / "hello").chmod(0o755)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    r = qq("run", "hello")
    assert r.returncode == 127
    assert "not synced here: hello" in r.stderr


def test_toolchain_link_loop_is_not_synced(repo, tmp_path):
    pin_hello(repo, tmp_path)
    (repo / ".qq" / "toolchains").mkdir(parents=True)
    os.symlink("hello", repo / ".qq" / "toolchains" / "hello")
    r = qq("run", "true")
    assert r.returncode == 0
    assert "not synced here: hello" in r.stderr


def test_save_list_show_and_run(repo, capfd):
    assert cli.main(["run", "--save", "greet", 'echo "hi $1 from $(basename "$PWD")"']) == 0
    script = repo / "infra" / "commands" / "greet.sh"
    assert script.read_text().startswith("#!/bin/sh\n")
    assert script.stat().st_mode & stat.S_IXUSR
    capfd.readouterr()

    r = qq("run", "greet", "you")
    assert (r.returncode, r.stdout.strip()) == (0, "hi you from repo")
    assert "running saved command greet (infra/commands/greet.sh)" in r.stderr

    assert cli.main(["run", "--list"]) == 0
    assert capfd.readouterr().out == 'greet\techo "hi $1 from $(basename "$PWD")"\n'
    assert cli.main(["run", "--show", "greet"]) == 0
    assert capfd.readouterr().out == script.read_text()


def test_list_hides_terminal_escapes(repo, capfd):
    (repo / "infra" / "commands").mkdir()
    (repo / "infra" / "commands" / "e.sh").write_text("echo \x1b]0;title\x07 hi\n")
    assert cli.main(["run", "--list"]) == 0
    assert capfd.readouterr().out == "e\techo ?]0;title? hi\n"


def test_double_dash_skips_saved_commands(repo, capfd):
    assert cli.main(["run", "--save", "printf", "echo SAVED"]) == 0
    assert qq("run", "printf", "x").stdout == "SAVED\n"
    r = qq("run", "--", "printf", "x")
    assert (r.stdout, r.stderr) == ("x", "")


def test_save_several_words_keeps_each_word(repo):
    assert cli.main(["run", "--save", "p", "printf", "%s|", "a b", "$HOME"]) == 0
    r = qq("run", "p")
    assert (r.returncode, r.stdout) == (0, "a b|$HOME|")


def test_saved_command_arguments_are_not_shell_code(repo, tmp_path):
    assert cli.main(["run", "--save", "echoes", 'printf "%s\\n" "$@"']) == 0
    marker = tmp_path / "pwned"
    r = qq("run", "echoes", f"; touch {marker}", f"$(touch {marker})")
    assert r.returncode == 0
    assert not marker.exists()
    assert r.stdout.splitlines() == [f"; touch {marker}", f"$(touch {marker})"]


def test_save_refuses_overwrite_without_force(repo, capfd):
    assert cli.main(["run", "--save", "t", "echo one"]) == 0
    assert cli.main(["run", "--save", "t", "echo two"]) == 2
    assert "--force" in capfd.readouterr().err
    assert cli.main(["run", "--save", "t", "--force", "echo two"]) == 0
    assert qq("run", "t").stdout.strip() == "two"


@pytest.mark.parametrize("name", ["../x", "a/b", "A", "-x", "x;y", "$(id)", "", "a" * 65, "x.sh"])
def test_bad_names_are_refused(repo, capfd, name):
    assert cli.main(["run", f"--save={name}", "true"]) == 2
    assert "not a command name" in capfd.readouterr().err
    assert not (repo / "infra" / "commands").exists() or not any((repo / "infra" / "commands").iterdir())


def test_show_unknown(repo, capfd):
    assert cli.main(["run", "--show", "nope"]) == 2
    assert "no saved command 'nope'" in capfd.readouterr().err


def test_symlinked_commands_are_refused(repo, tmp_path, capfd):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "x.sh").write_text("echo outside\n")
    os.symlink(outside, repo / "infra" / "commands")
    assert cli.main(["run", "x"]) == 2
    assert "is a symlink" in capfd.readouterr().err

    (repo / "infra" / "commands").unlink()
    (repo / "infra" / "commands").mkdir()
    os.symlink(outside / "x.sh", repo / "infra" / "commands" / "x.sh")
    assert cli.main(["run", "x"]) == 2
    assert "is a symlink" in capfd.readouterr().err
    assert cli.main(["run", "--list"]) == 0
    assert capfd.readouterr().out == ""


def test_an_unsaved_word_runs_as_a_command(repo):
    assert qq("run", "true").returncode == 0
    assert qq("run", "false").returncode == 1
