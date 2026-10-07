"""qq run, qq create and qq NAME: commands run from the repo root with the repo's pinned toolchains."""
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
    assert cli.main(["run", "true"]) == 125
    assert "inside a repo" in capfd.readouterr().err


def test_nothing_to_run(repo, capfd):
    assert cli.main(["run"]) == 125
    assert "nothing to run" in capfd.readouterr().err


def test_no_abbreviated_options(repo, capfd):
    r = qq("run", "--sav", "x", "true")
    assert r.returncode == 125
    assert "unrecognized arguments" in r.stderr


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


def test_a_command_exiting_2_is_not_a_qq_error(repo):
    assert qq("run", "exit 2").returncode == 2


@pytest.mark.parametrize("argv", [["-x"], ["--"], [""], ["  "], ["--save", "x", "true"]])
def test_qq_run_usage_errors_exit_125(repo, argv):
    r = qq("run", *argv)
    assert r.returncode == 125, r.stderr


def test_quoted_command_line_with_more_words_hints(repo):
    r = qq("run", "ls -la", "foo")
    assert r.returncode == 127
    assert "one quoted argument" in r.stderr and "qq run 'ls -la foo'" in r.stderr


def test_dash_command_line_is_not_a_shell_option(repo):
    r = qq("run", "--", "-x")
    assert r.returncode == 127 and "-x: not found" in r.stderr


def test_not_synced_note_names_the_one_built_platform(repo, tmp_path, monkeypatch, capfd):
    pin_hello(repo, tmp_path)
    from qqdepot.commands import run
    for platform, noted in (("linux-x86_64", False), ("linux-arm64", True), ("macos-arm64", True)):
        monkeypatch.setattr(run.pins, "current_platform", lambda: platform)
        run.environment(repo)
        assert ("published for linux-x86_64 only" in capfd.readouterr().err) == noted


def test_broken_pin_is_125_for_qq_run_and_repo_commands(repo):
    assert cli.main(["create", "space", "true"]) == 0
    (repo / "infra" / "repo.toml").write_text('schema = "quirq-repo/1"\n[qq]\nversion = "not a version"\n')
    env = {**os.environ}
    env.pop("QQ_PINNED", None)
    for argv, code in ((["run", "true"], 125), (["space"], 125), (["sync"], 2)):
        r = subprocess.run([sys.executable, "-m", "qqdepot.cli", *argv], capture_output=True, text=True, env=env)
        assert r.returncode == code, (argv, r.stderr)


# qq create NAME and qq NAME: a repo's own commands, run like qq's.

def test_create_then_run_like_any_qq_command(repo, capfd):
    assert cli.main(["create", "greet", 'echo "hi $1 from $(basename "$PWD")"']) == 0
    assert "created qq greet (infra/commands/greet.sh)" in capfd.readouterr().out
    script = repo / "infra" / "commands" / "greet.sh"
    assert script.read_text().startswith("#!/bin/sh\n# Created with `qq create greet`")
    assert script.stat().st_mode & stat.S_IXUSR
    r = qq("greet", "you")
    assert (r.returncode, r.stdout.strip(), r.stderr) == (0, "hi you from repo", "")


def test_qq_name_becomes_the_command(repo):
    assert cli.main(["create", "die", "kill -TERM $$"]) == 0
    assert qq("die").returncode == -15
    assert cli.main(["create", "two", "exit 2"]) == 0
    assert qq("two").returncode == 2


def test_create_from_several_words_passes_arguments_on(repo):
    assert cli.main(["create", "p", "printf", "%s|", "a b", "$HOME"]) == 0
    assert (repo / "infra" / "commands" / "p.sh").read_text().endswith("printf '%s|' 'a b' '$HOME' \"$@\"\n")
    r = qq("p", "c d", "$(id)")
    assert (r.returncode, r.stdout) == (0, "a b|$HOME|c d|$(id)|")


def test_arguments_are_not_shell_code(repo, tmp_path):
    assert cli.main(["create", "echoes", 'printf "%s\\n" "$@"']) == 0
    marker = tmp_path / "pwned"
    r = qq("echoes", f"; touch {marker}", f"$(touch {marker})")
    assert r.returncode == 0 and not marker.exists()
    assert r.stdout.splitlines() == [f"; touch {marker}", f"$(touch {marker})"]


def test_arguments_are_never_dropped(repo):
    assert cli.main(["create", "fixed", "echo fixed"]) == 0
    assert qq("fixed").stdout == "fixed\n"
    r = qq("fixed", "--fast")
    assert r.returncode == 125 and r.stdout == ""
    assert "never mentions its arguments" in r.stderr and "--fast" in r.stderr
    assert cli.main(["create", "takes", 'echo "got ${1}"']) == 0
    assert qq("takes", "--fast").stdout == "got --fast\n"
    assert cli.main(["create", "f", "echo a b | awk '{print $1}'"]) == 0
    assert qq("f", "--fast").returncode == 125


@pytest.mark.parametrize("script, mentions", [
    ('echo "$@"', True), ("echo $1", True), ("echo ${1:-x}", True), ("echo $*", True),
    ("[ $# -gt 0 ]", True), ("echo ${@}", True),
    ("awk '{print $1}'", False), ("awk '{\n print $1\n}' f", False), ("n=${#v}", False),
    ("cmd # was $1", False), ("echo \\$1", False), ("# $1\ncmd", False),
    ("cmd # x\fy $1", False), ("cmd # x\u2028 $1", False), ("echo a#$1", True), ('echo "#$1"', True),
])
def test_mentions_arguments(script, mentions):
    from qqdepot.commands import run
    assert run.mentions_arguments(script) is mentions


def test_create_refuses_overwrite_without_force(repo, capfd):
    assert cli.main(["create", "t", "echo one"]) == 0
    assert cli.main(["create", "t", "echo two"]) == 125
    assert "--force" in capfd.readouterr().err
    assert cli.main(["create", "--force", "t", "echo two"]) == 0
    assert qq("t").stdout.strip() == "two"


@pytest.mark.parametrize("name", ["sync", "fetch", "build", "test", "land", "status", "run", "create"])
def test_qq_commands_cannot_be_taken(repo, capfd, name):
    assert cli.main(["create", name, "echo hijacked"]) == 125
    assert "already a qq command" in capfd.readouterr().err
    assert not (repo / "infra" / "commands" / f"{name}.sh").exists()


def test_a_committed_script_never_shadows_a_qq_command(repo):
    (repo / "infra" / "commands").mkdir()
    (repo / "infra" / "commands" / "status.sh").write_text("echo hijacked\n")
    r = qq("status", "--help")
    assert "hijacked" not in r.stdout and "usage: qq status" in r.stdout


@pytest.mark.parametrize("name", ["../x", "a/b", "A", "-x", "x;y", "$(id)", "", "a" * 65, "x.sh"])
def test_bad_names_are_refused(repo, capfd, name):
    assert cli.main(["create", "--", name, "true"] if name.startswith("-") else ["create", name, "true"]) in (2, 125)
    assert not (repo / "infra" / "commands").exists() or not any((repo / "infra" / "commands").iterdir())


def test_non_utf8_create_is_refused(repo, capfd):
    bad = b"echo \xff".decode("utf-8", "surrogateescape")
    assert cli.main(["create", "bad", bad]) == 125
    assert "not valid UTF-8" in capfd.readouterr().err


def test_create_needs_a_command(repo, capfd):
    assert cli.main(["create", "empty"]) == 125
    assert "nothing to create" in capfd.readouterr().err


def test_help_lists_the_repo_commands_visibly(repo):
    assert cli.main(["create", "space", "./scripts/space --all"]) == 0
    (repo / "infra" / "commands" / "e.sh").write_bytes(b"echo \x1b]0;t\x07 \\x1b\n")
    out = qq("--help").stdout
    assert "this repo's commands (qq create NAME COMMAND adds one):" in out
    assert "  space  ./scripts/space --all \"$@\"" not in out   # one argument: kept as typed
    assert "  space  ./scripts/space --all" in out
    assert "  e      echo \\x1b]0;t\\x07 \\\\x1b" in out


def test_unknown_name_is_still_an_error(repo):
    r = qq("nope")
    assert r.returncode == 2 and "invalid choice: 'nope'" in r.stderr


def test_outside_a_repo_a_name_is_not_looked_up(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    r = qq("space")
    assert r.returncode == 2 and "invalid choice" in r.stderr


def test_symlinked_commands_are_refused(repo, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "x.sh").write_text("echo outside\n")
    os.symlink(outside, repo / "infra" / "commands")
    r = qq("x")
    assert r.returncode == 125 and "is a symlink" in r.stderr
    (repo / "infra" / "commands").unlink()
    (repo / "infra" / "commands").mkdir()
    os.symlink(outside / "x.sh", repo / "infra" / "commands" / "x.sh")
    r = qq("x")
    assert r.returncode == 125 and "is a symlink" in r.stderr
    assert "outside" not in qq("--help").stdout
    assert qq("run", "true").returncode == 0   # qq run never looks at infra/commands


def test_unreadable_commands_dir_is_an_error(repo):
    (repo / "infra" / "commands").mkdir()
    (repo / "infra" / "commands" / "x.sh").write_text("echo saved\n")
    (repo / "infra" / "commands").chmod(0)
    try:
        try:
            os.lstat(repo / "infra" / "commands" / "x.sh")
            pytest.skip("this user reads any directory (root)")
        except PermissionError:
            pass
        r = qq("x")
        assert r.returncode == 125 and "cannot read" in r.stderr
    finally:
        (repo / "infra" / "commands").chmod(0o755)


@pytest.mark.parametrize("name", ["snyc", "sycn", "synk", "sync2", "fech", "biuld", "lands", "ru", "statu"])
def test_one_typo_from_a_qq_command_is_refused(repo, capfd, name):
    assert cli.main(["create", name, "echo hijacked"]) == 125
    assert "one typo away from qq" in capfd.readouterr().err


def test_a_committed_typo_name_never_runs(repo):
    (repo / "infra" / "commands").mkdir()
    (repo / "infra" / "commands" / "snyc.sh").write_text("echo hijacked\n")
    r = qq("snyc")
    assert r.returncode == 125 and "hijacked" not in r.stdout and "one typo away from qq sync" in r.stderr
    assert "snyc" not in qq("--help").stdout


@pytest.mark.parametrize("name", ["space", "deploy-preview", "lint", "check", "fmt"])
def test_ordinary_names_are_fine(repo, name):
    assert cli.main(["create", name, "true"]) == 0


def test_only_the_exact_file_name_runs(repo, monkeypatch):
    """On a case-insensitive file system, qq space must not run Space.sh."""
    from qqdepot.commands import run
    (repo / "infra" / "commands").mkdir()
    (repo / "infra" / "commands" / "Space.sh").write_text("echo wrong case\n")
    real_lstat = Path.lstat
    monkeypatch.setattr(Path, "lstat", lambda self: real_lstat(self.with_name("Space.sh"))
                        if self.name == "space.sh" else real_lstat(self))
    assert run.saved_path(repo, "space") is None
