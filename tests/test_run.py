"""qq run: a command, or a saved one, runs from the repo root with the repo's pinned toolchains."""
import hashlib
import io
import os
import stat
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
    archive = tmp_path / "hello.tar.gz"
    archive.write_bytes(buf.getvalue())
    return archive.as_uri(), "sha256:" + hashlib.sha256(buf.getvalue()).hexdigest()


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
    with (root / "infra" / "repo.toml").open("a") as f:
        f.write(f'[toolchains.hello]\nsource = "{source}"\ndigest = "{digest}"\n')


def test_command_line_runs_from_repo_root_with_exit_code(repo, capfd):
    assert cli.main(["run", "pwd; exit 3"]) == 3
    assert capfd.readouterr().out.strip() == str(repo)


def test_several_words_run_without_a_shell(repo, capfd):
    assert cli.main(["run", "printf", "%s|", "a b", "$HOME", ";", "x"]) == 0
    assert capfd.readouterr().out == "a b|$HOME|;|x|"


def test_double_dash_and_options_after_the_command(repo, capfd):
    assert cli.main(["run", "--", "printf", "%s", "--list"]) == 0
    assert capfd.readouterr().out == "--list"


def test_missing_program_is_127(repo, capfd):
    assert cli.main(["run", "no-such-program-here", "x"]) == 127
    assert "command not found" in capfd.readouterr().err


def test_signal_exit_code(repo):
    assert cli.main(["run", "kill -TERM $$"]) == 128 + 15


def test_outside_a_repo(tmp_path, monkeypatch, capfd):
    monkeypatch.chdir(tmp_path)
    assert cli.main(["run", "true"]) == 2
    assert "inside a repo" in capfd.readouterr().err


def test_nothing_to_run(repo, capfd):
    assert cli.main(["run"]) == 2
    assert "nothing to run" in capfd.readouterr().err


def test_synced_toolchain_comes_first_on_path(repo, tmp_path, monkeypatch, capfd):
    pin_hello(repo, tmp_path)
    shadow = tmp_path / "shadow"
    shadow.mkdir()
    (shadow / "hello").write_text("#!/bin/sh\necho 'from PATH'\n")
    (shadow / "hello").chmod(0o755)
    monkeypatch.setenv("PATH", f"{shadow}:/usr/bin:/bin")

    assert cli.main(["run", "hello"]) == 0
    out, err = capfd.readouterr()
    assert out.strip() == "from PATH"
    assert "not synced here: hello" in err

    assert cli.main(["sync"]) == 0
    capfd.readouterr()
    assert cli.main(["run", "hello"]) == 0
    out, err = capfd.readouterr()
    assert out.strip() == "pinned hello"
    assert "not synced" not in err


def test_committed_toolchain_dir_never_lands_on_path(repo, tmp_path, monkeypatch, capfd):
    pin_hello(repo, tmp_path)
    fake = repo / ".qq" / "toolchains" / "hello" / "bin"
    fake.mkdir(parents=True)
    (fake / "hello").write_text("#!/bin/sh\necho 'from the repo'\n")
    (fake / "hello").chmod(0o755)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    assert cli.main(["run", "hello"]) == 127
    assert "not synced here: hello" in capfd.readouterr().err


def test_save_list_show_and_run(repo, capfd):
    assert cli.main(["run", "--save", "greet", 'echo "hi $1 from $(basename "$PWD")"']) == 0
    script = repo / "infra" / "commands" / "greet.sh"
    assert script.read_text().startswith("#!/bin/sh\n")
    assert script.stat().st_mode & stat.S_IXUSR
    capfd.readouterr()

    assert cli.main(["run", "greet", "you"]) == 0
    out, err = capfd.readouterr()
    assert out.strip() == "hi you from repo"
    assert "running saved command greet (infra/commands/greet.sh)" in err

    assert cli.main(["run", "--list"]) == 0
    assert capfd.readouterr().out == 'greet\techo "hi $1 from $(basename "$PWD")"\n'
    assert cli.main(["run", "--show", "greet"]) == 0
    assert capfd.readouterr().out == script.read_text()


def test_save_several_words_keeps_each_word(repo, capfd):
    assert cli.main(["run", "--save", "p", "printf", "%s|", "a b", "$HOME"]) == 0
    capfd.readouterr()
    assert cli.main(["run", "p"]) == 0
    assert capfd.readouterr().out == "a b|$HOME|"


def test_saved_command_arguments_are_not_shell_code(repo, tmp_path, capfd):
    assert cli.main(["run", "--save", "echoes", 'printf "%s\\n" "$@"']) == 0
    marker = tmp_path / "pwned"
    assert cli.main(["run", "echoes", f"; touch {marker}", f"$(touch {marker})"]) == 0
    assert not marker.exists()
    assert capfd.readouterr().out.splitlines()[-2:] == [f"; touch {marker}", f"$(touch {marker})"]


def test_save_refuses_overwrite_without_force(repo, capfd):
    assert cli.main(["run", "--save", "t", "echo one"]) == 0
    assert cli.main(["run", "--save", "t", "echo two"]) == 2
    assert "--force" in capfd.readouterr().err
    assert cli.main(["run", "--save", "t", "--force", "echo two"]) == 0
    capfd.readouterr()
    assert cli.main(["run", "t"]) == 0
    assert capfd.readouterr().out.strip() == "two"


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


def test_an_unsaved_word_runs_as_a_command(repo, capfd):
    assert cli.main(["run", "true"]) == 0
    assert cli.main(["run", "false"]) == 1
