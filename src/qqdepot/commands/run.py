"""qq run: run a command, or a saved one, in the repo's qq environment.

    qq run "COMMAND LINE"          run it with /bin/sh
    qq run PROGRAM [ARG ...]       run PROGRAM directly, with no shell in between
    qq run NAME [ARG ...]          run the saved command infra/commands/NAME.sh with these arguments
    qq run --save NAME COMMAND...  save a command as infra/commands/NAME.sh, to commit with the repo
    qq run --list                  list the saved commands
    qq run --show NAME             print one

Every command runs from the repo root. The bin directory of each toolchain `qq sync` linked under
<repo>/.qq/toolchains comes first on PATH, so the command runs the versions the repo pins. qq exits
with the command's exit code (128 + N when signal N ends it).

A saved command is code in the repo, like any script there: qq runs one only when you name it.
"""
from __future__ import annotations

import argparse
import os
import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

from qqsync.errors import ManifestError
from qqsync.manifest import load

from qqdepot import store
from qqdepot.commands.build import TOOLCHAINS, repo_root
from qqdepot.pin import MANIFEST

COMMANDS = Path("infra/commands")
# A name is a file name and a word on qq's command line, never a path or shell syntax.
NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
SHELL = "/bin/sh"
HEADER = ("#!/bin/sh\n"
          "# Saved with `qq run --save {name}`; run it with `qq run {name} [ARG ...]`.\n"
          "# It runs from the repo root with the repo's pinned toolchains first on PATH;\n"
          "# arguments after the name are \"$@\".\n")


class RunError(Exception):
    pass


def check_name(name: str) -> str:
    if not NAME.fullmatch(name):
        raise RunError(f"{name!r} is not a command name: use lowercase letters, digits, '-' and '_'"
                       " (at most 64, starting with a letter or digit)")
    return name


def _commands_dir(root: Path, make: bool = False) -> Path | None:
    """<root>/infra/commands as a real directory in the repo, or None when it does not exist."""
    path = root
    for part in COMMANDS.parts:
        path = path / part
        if path.is_symlink():
            raise RunError(f"{path} is a symlink; saved commands must live in the repo itself")
        if not path.exists():
            if not make:
                return None
            path.mkdir()
        elif not path.is_dir():
            raise RunError(f"{path} is not a directory")
    return path


def saved_path(root: Path, name: str) -> Path | None:
    """The saved command NAME's script, or None when there is none."""
    directory = _commands_dir(root)
    if directory is None:
        return None
    path = directory / f"{check_name(name)}.sh"
    if path.is_symlink():
        raise RunError(f"{path} is a symlink; a saved command must be a file in the repo")
    if not path.exists():
        return None
    if not path.is_file():
        raise RunError(f"{path} is not a file")
    return path


def saved_names(root: Path) -> list[str]:
    directory = _commands_dir(root)
    if directory is None:
        return []
    return sorted(p.stem for p in directory.glob("*.sh")
                  if NAME.fullmatch(p.stem) and p.is_file() and not p.is_symlink())


def toolchain_bins(root: Path) -> tuple[list[str], list[str]]:
    """(bin directories to put first on PATH, pinned toolchains with none synced here).

    Only links `qq sync` made into this machine's store count, so a toolchain directory committed
    to the repo never lands on PATH.
    """
    try:
        pinned = list(load(root / MANIFEST).get("toolchains", {}))
    except ManifestError as e:
        raise RunError(str(e)) from None
    synced = root / TOOLCHAINS
    store_root = store.store_dir().resolve()
    bins, missing = [], []
    for name in pinned:
        link = synced / name
        if not link.is_symlink() or not link.resolve().is_relative_to(store_root):
            missing.append(name)
            continue
        if (link / "bin").is_dir():
            bins.append(str(link.resolve() / "bin"))
    return bins, missing


def environment(root: Path) -> dict[str, str]:
    bins, missing = toolchain_bins(root)
    if missing:
        print(f"qq: not synced here: {', '.join(missing)}; using PATH for them. Run qq sync"
              " (toolchains are published for Linux only so far)", file=sys.stderr)
    env = dict(os.environ)
    env["PATH"] = os.pathsep.join([*bins, env.get("PATH", os.defpath)])
    return env


def execute(argv: list[str], root: Path, env: dict[str, str]) -> int:
    try:
        proc = subprocess.Popen(argv, cwd=root, env=env)
    except FileNotFoundError:
        print(f"qq: {argv[0]}: command not found", file=sys.stderr)
        return 127
    except PermissionError:
        print(f"qq: {argv[0]}: permission denied", file=sys.stderr)
        return 126
    while True:
        try:
            code = proc.wait()
            break
        except KeyboardInterrupt:
            continue   # the terminal sent the command the same signal; its exit code decides
    return 128 - code if code < 0 else code


def save(root: Path, name: str, words: list[str], force: bool) -> Path:
    check_name(name)
    if not words:
        raise RunError(f"nothing to save: qq run --save {name} COMMAND")
    # One argument is a command line, kept as typed; several are quoted so each stays one word.
    line = words[0] if len(words) == 1 else shlex.join(words)
    if not line.strip():
        raise RunError(f"nothing to save: qq run --save {name} COMMAND")
    directory = _commands_dir(root, make=True)
    path = directory / f"{name}.sh"
    if path.is_symlink() or (path.exists() and not force):
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


def _summary(path: Path) -> str:
    for line in path.read_text(errors="replace").splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            return line.strip()
    return ""


def run_command(args: argparse.Namespace) -> int:
    root = repo_root(Path.cwd())
    if root is None:
        print(f"qq: no {MANIFEST} here or above {Path.cwd()}; run qq run inside a repo", file=sys.stderr)
        return 2
    words = args.words[1:] if args.words[:1] == ["--"] else args.words
    try:
        if args.list:
            for name in saved_names(root):
                print(f"{name}\t{_summary(root / COMMANDS / f'{name}.sh')}")
            return 0
        if args.show is not None:
            path = saved_path(root, args.show)
            if path is None:
                raise RunError(f"no saved command {args.show!r} (qq run --list shows them)")
            sys.stdout.write(path.read_text(errors="replace"))
            return 0
        if args.save is not None:
            path = save(root, args.save, words, args.force)
            print(f"qq: saved {path.relative_to(root)}; commit it to share it, run it with qq run {args.save}")
            return 0
        if not words:
            raise RunError('nothing to run: qq run "COMMAND", qq run NAME, or qq run --list')
        path = saved_path(root, words[0]) if NAME.fullmatch(words[0]) else None
        if path is not None:
            print(f"qq: running saved command {words[0]} ({path.relative_to(root)})", file=sys.stderr)
            argv = [SHELL, str(path), *words[1:]]
        elif len(words) == 1:
            argv = [SHELL, "-c", words[0]]
        else:
            argv = words
        return execute(argv, root, environment(root))
    except (RunError, OSError) as e:
        print(f"qq: {e}", file=sys.stderr)
        return 2


def register(sub) -> None:
    p = sub.add_parser("run", help="run a command, or a saved one, with the repo's pinned toolchains",
                       description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = p.add_mutually_exclusive_group()
    group.add_argument("--save", metavar="NAME", help="save COMMAND as infra/commands/NAME.sh")
    group.add_argument("--list", action="store_true", help="list the saved commands")
    group.add_argument("--show", metavar="NAME", help="print the saved command NAME")
    p.add_argument("--force", action="store_true", help="with --save, replace a saved command")
    p.add_argument("words", nargs=argparse.REMAINDER, metavar="COMMAND",
                   help="a command line, a program and its arguments, or a saved command's name")
    p.set_defaults(run=run_command)
