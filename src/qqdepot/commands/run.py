"""qq run and a repo's own commands: run a command in the repo's qq environment.

    qq run "COMMAND LINE"          run it once, with /bin/sh
    qq run PROGRAM [ARG ...]       run PROGRAM once, directly, with no shell in between
    qq create NAME COMMAND...      make NAME a qq command of this repo (infra/commands/NAME.sh)
    qq NAME [ARG ...]              run it, like any other qq command

Every command runs from the repo root. The bin directory of each toolchain `qq sync` linked under
<repo>/.qq/toolchains comes first on PATH, so the command runs the versions the repo pins. qq then
becomes the command (exec), so its exit code and signals are the command's own. When qq itself
fails (bad usage, no repo, a broken command file) it exits 125; a command that cannot start gives
127 (not found) or 126 (not runnable), as a shell does.

A repo's command is code in the repo, like any script there: qq runs one only when you name it.
"""
from __future__ import annotations

import argparse
import os
import re
import shlex
import stat
import sys
import tempfile
from pathlib import Path

from qqsync import pins
from qqsync.errors import ManifestError
from qqsync.manifest import load

from qqdepot import store
from qqdepot.commands.build import TOOLCHAINS, repo_root
from qqdepot.pin import MANIFEST

COMMANDS = Path("infra/commands")
# A name is a file name and a word on qq's command line, never a path or shell syntax.
NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
SHELL = "/bin/sh"
QQ_FAILED = 125   # qq's own failure, as env and timeout use it; 126 and 127 as a shell does
# A mention of the script's arguments: "$@", $*, $#, $1-$9, or ${@}, ${*}, ${1}... (not ${#name}).
ARGUMENT = re.compile(r"\$(?:[@*#1-9]|\{[@*1-9])")
BUILT_FOR = "linux-x86_64"   # the only platform toolchains are published for so far
HEADER = ("#!/bin/sh\n"
          "# Created with `qq create {name}`; run it with `qq {name} [ARG ...]`.\n"
          "# It runs from the repo root with the repo's pinned toolchains first on PATH;\n"
          "# arguments given after the name reach it as \"$@\".\n")


class RunError(Exception):
    pass


def check_name(name: str) -> str:
    if not NAME.fullmatch(name):
        raise RunError(f"{name!r} is not a command name: use lowercase letters, digits, '-' and '_'"
                       " (at most 64, starting with a letter or digit)")
    return name


def _lstat(path: Path) -> os.stat_result | None:
    """`path`'s own status, or None when it does not exist. Any other failure (no permission) is
    an error: Path.exists() would report it as missing, and qq would run something else."""
    try:
        return path.lstat()
    except FileNotFoundError:
        return None
    except OSError as e:
        raise RunError(f"cannot read {path}: {e.strerror}") from None


def _commands_dir(root: Path, make: bool = False) -> Path | None:
    """<root>/infra/commands as a real directory in the repo, or None when it does not exist."""
    path = root
    for part in COMMANDS.parts:
        path = path / part
        st = _lstat(path)
        if st is None:
            if not make:
                return None
            path.mkdir()
        elif stat.S_ISLNK(st.st_mode):
            raise RunError(f"{path} is a symlink; a repo's commands must live in the repo itself")
        elif not stat.S_ISDIR(st.st_mode):
            raise RunError(f"{path} is not a directory")
    return path


def saved_path(root: Path, name: str) -> Path | None:
    """The repo command NAME's script, or None when there is none."""
    directory = _commands_dir(root)
    if directory is None:
        return None
    path = directory / f"{check_name(name)}.sh"
    st = _lstat(path)
    if st is None:
        return None
    if stat.S_ISLNK(st.st_mode):
        raise RunError(f"{path} is a symlink; a repo's command must be a file in the repo")
    if not stat.S_ISREG(st.st_mode):
        raise RunError(f"{path} is not a file")
    return path


def saved_names(root: Path) -> list[str]:
    directory = _commands_dir(root)
    if directory is None:
        return []
    try:
        entries = list(os.scandir(directory))
    except OSError as e:
        raise RunError(f"cannot read {directory}: {e.strerror}") from None
    return sorted(e.name[:-3] for e in entries
                  if e.name.endswith(".sh") and NAME.fullmatch(e.name[:-3]) and e.is_file(follow_symlinks=False))


def toolchain_bins(root: Path) -> tuple[list[str], list[str]]:
    """(bin directories to put first on PATH, pinned toolchains not synced here at their pin).

    A toolchain counts only when its link points at the store entry for the digest the manifest
    pins now, so a stale sync or a link committed to the repo never lands on PATH.
    """
    try:
        manifest = load(root / MANIFEST)
    except ManifestError as e:
        raise RunError(str(e)) from None
    store_root = store.store_dir().resolve()
    bins, missing = [], []
    for name in manifest.get("toolchains", {}):
        link = root / TOOLCHAINS / name
        try:
            artifact = store.resolve(manifest, "toolchains", name)
            target = link.resolve(strict=True) if link.is_symlink() else None
        except (store.FetchError, OSError, RuntimeError):   # no pin for this platform; a bad link
            target = None
        if target is None or target != store_root / f"{artifact.algo}-{artifact.value}":
            missing.append(name)
        elif (target / "bin").is_dir():
            bins.append(str(target / "bin"))
    return bins, missing


def environment(root: Path) -> dict[str, str]:
    bins, missing = toolchain_bins(root)
    if missing:
        note = "" if pins.current_platform() == BUILT_FOR else f" (toolchains are published for {BUILT_FOR} only so far)"
        print(f"qq: not synced here: {', '.join(missing)}; using PATH for them. Run qq sync{note}",
              file=sys.stderr)
    env = dict(os.environ)
    env["PATH"] = os.pathsep.join([*bins, env.get("PATH") or os.defpath])
    env["PWD"] = str(root)
    return env


def execute(argv: list[str], root: Path, env: dict[str, str]) -> int:
    """Become the command, so its exit code, signals and terminal are its own. Returns only when
    the command cannot start (127 not found, 126 not runnable, as a shell does)."""
    sys.stdout.flush()
    sys.stderr.flush()
    try:
        os.chdir(root)
        os.execvpe(argv[0], argv, env)
    except FileNotFoundError:
        hint = ""
        if any(c.isspace() for c in argv[0]):
            hint = ("; a command line goes in one quoted argument with nothing after it,"
                    f" e.g. qq run {shlex.quote(' '.join(argv))}")
        print(f"qq: {argv[0]}: command not found{hint}", file=sys.stderr)
        return 127
    except OSError as e:
        print(f"qq: {argv[0]}: cannot run it: {e.strerror}", file=sys.stderr)
        return 126


def qq_commands() -> set[str]:
    """The names qq itself answers to (builtins and plugins): a repo's command never takes one."""
    from qqdepot import cli   # cli imports this module
    return set(cli.build_parser().qq_commands)


def save(root: Path, name: str, words: list[str], force: bool) -> Path:
    """Write the repo command NAME (`qq create`)."""
    check_name(name)
    if name in qq_commands():
        raise RunError(f"{name!r} is already a qq command (qq {name}); pick another name")
    if not words:
        raise RunError(f"nothing to create: qq create {name} COMMAND")
    # One argument is a command line, kept as typed. Several are quoted so each stays one word,
    # and the arguments given when it runs are passed on after them.
    line = words[0] if len(words) == 1 else shlex.join(words) + ' "$@"'
    if not line.strip():
        raise RunError(f"nothing to create: qq create {name} COMMAND")
    try:
        line.encode()
    except UnicodeEncodeError:
        raise RunError("the command is not valid UTF-8; a repo's commands are UTF-8 text") from None
    directory = _commands_dir(root, make=True)
    path = directory / f"{name}.sh"
    st = _lstat(path)
    if st is not None and (stat.S_ISLNK(st.st_mode) or not force):
        raise RunError(f"{path.relative_to(root)} already exists; pass --force to replace it")
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=f".{name}.")
    try:
        with os.fdopen(fd, "w") as out:
            out.write(HEADER.format(name=name) + line.rstrip("\n") + "\n")
        os.chmod(tmp, 0o777 & ~_umask() | 0o100)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return path


def _umask() -> int:
    mask = os.umask(0)
    os.umask(mask)
    return mask


def visible(text: str, keep: str = "") -> str:
    """`text` with control characters (terminal escapes, carriage returns) and bytes that are not
    UTF-8 written as \\xNN and a backslash as \\\\, so what is shown is what runs. Characters in
    `keep` stay as they are."""
    return "".join("\\\\" if c == "\\" else c if c.isprintable() or c in keep
                   else f"\\x{ord(c) & 0xff:02x}" if ord(c) < 0x100 or 0xdc80 <= ord(c) <= 0xdcff
                   else f"\\u{ord(c):04x}" for c in text)


def _read(path: Path) -> str:
    return path.read_bytes().decode("utf-8", errors="surrogateescape")   # bytes that are not UTF-8 too


def mentions_arguments(script: str) -> bool:
    """True when the script mentions its arguments outside single quotes, comments and backslash
    escapes. Only a mention: a function's own $1 or a heredoc cannot be told apart by reading."""
    i, in_double, word_start = 0, False, True
    while i < len(script):
        c = script[i]
        if c == "\\":
            i, word_start = i + 2, False
            continue
        if c == "'" and not in_double:
            end = script.find("'", i + 1)
            if end < 0:
                return False
            i, word_start = end + 1, False
            continue
        if c == '"':
            in_double = not in_double
        elif c == "#" and word_start and not in_double:
            end = script.find("\n", i)   # a comment ends at a newline, nothing else
            if end < 0:
                return False
            i, word_start = end + 1, True
            continue
        elif c == "$" and ARGUMENT.match(script, i):
            return True
        word_start = c in " \t\n;|&()"
        i += 1
    return False


def _summary(path: Path) -> str:
    """The script's first command line, shown visibly."""
    for line in _read(path).split("\n"):
        if line.strip() and not line.lstrip().startswith("#"):
            return visible(line.strip())
    return ""


def repo_commands() -> list[tuple[str, str]]:
    """(name, first command line) for each command of the repo here, for qq --help; none outside
    a repo or when infra/commands cannot be read."""
    root = repo_root(Path.cwd())
    try:
        return [] if root is None else [(n, _summary(root / COMMANDS / f"{n}.sh")) for n in saved_names(root)]
    except (RunError, OSError):
        return []


def run_saved(name: str, arguments: list[str]) -> int | None:
    """`qq NAME ARG...` for the repo command NAME, or None when the repo here has none by that name."""
    root = repo_root(Path.cwd())
    if root is None or not NAME.fullmatch(name):
        return None
    try:
        path = saved_path(root, name)
        if path is None:
            return None
        if arguments and not mentions_arguments(_read(path)):
            raise RunError(f"qq {name} never mentions its arguments (\"$@\"), so {shlex.join(arguments)}"
                           f" would be dropped; add \"$@\" where they go in {path.relative_to(root)}")
        return execute([SHELL, str(path), *arguments], root, environment(root))
    except (RunError, OSError) as e:
        print(f"qq: {e}", file=sys.stderr)
        return QQ_FAILED


def run_command(args: argparse.Namespace) -> int:
    root = repo_root(Path.cwd())
    if root is None:
        print(f"qq: no {MANIFEST} here or above {Path.cwd()}; run qq run inside a repo", file=sys.stderr)
        return QQ_FAILED
    words = args.words[1:] if args.words[:1] == ["--"] else args.words
    try:
        if not words or not words[0].strip():
            raise RunError('nothing to run: qq run "COMMAND" (qq create NAME COMMAND keeps one as qq NAME)')
        argv = [SHELL, "-c", "--", words[0]] if len(words) == 1 else words
        return execute(argv, root, environment(root))
    except (RunError, OSError) as e:
        print(f"qq: {e}", file=sys.stderr)
        return QQ_FAILED


class _Parser(argparse.ArgumentParser):
    """Usage errors exit QQ_FAILED, so they never look like the command's own exit code."""

    def error(self, message: str):
        self.print_usage(sys.stderr)
        hint = " (put -- before a program whose name starts with -)" if "unrecognized" in message else ""
        self.exit(QQ_FAILED, f"{self.prog}: error: {message}{hint}\n")


def _arguments(p: argparse.ArgumentParser) -> argparse.ArgumentParser:
    p.add_argument("words", nargs=argparse.REMAINDER, metavar="COMMAND",
                   help="a command line in one argument, or a program and its arguments")
    p.set_defaults(run=run_command)
    return p


def main(argv: list[str]) -> int:
    """`qq run ARGS`, parsed on its own so that qq's usage errors exit QQ_FAILED."""
    parser = _arguments(_Parser(prog="qq run", description=__doc__, allow_abbrev=False,
                                formatter_class=argparse.RawDescriptionHelpFormatter))
    return run_command(parser.parse_args(argv))


def register(sub) -> None:
    _arguments(sub.add_parser("run", help="run a command once, with the repo's pinned toolchains",
                              description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
                              allow_abbrev=False))
