"""qq create: give this repo a qq command of its own.

    qq create NAME COMMAND...   make NAME a qq command; then `qq NAME [ARG ...]` runs it

The command is saved as infra/commands/NAME.sh, to commit with the repo, so teammates and agents
get it too. It runs from the repo root with the repo's pinned toolchains first on PATH, like
`qq run`. One argument is a command line, kept as typed; several are quoted so each stays one word,
and the arguments given to `qq NAME` are passed on after them. A name qq already answers to (sync,
fetch, build, land, ...) is refused, so a repo can never take over one of qq's own commands.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from qqdepot.commands import run
from qqdepot.commands.build import repo_root
from qqdepot.pin import MANIFEST


def run_create(args: argparse.Namespace) -> int:
    root = repo_root(Path.cwd())
    if root is None:
        print(f"qq: no {MANIFEST} here or above {Path.cwd()}; run qq create inside a repo", file=sys.stderr)
        return run.QQ_FAILED
    words = args.words[1:] if args.words[:1] == ["--"] else args.words
    try:
        path = run.save(root, args.name, words, args.force)
    except (run.RunError, OSError) as e:
        print(f"qq: {e}", file=sys.stderr)
        return run.QQ_FAILED
    print(f"qq: created qq {args.name} ({path.relative_to(root)}); commit it to share it")
    return 0


def register(sub) -> None:
    p = sub.add_parser("create", help="give this repo a qq command of its own (qq create NAME COMMAND)",
                       description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
                       allow_abbrev=False)
    p.add_argument("--force", action="store_true", help="replace a command this repo already has")
    p.add_argument("name", metavar="NAME", help="the new command: qq NAME runs it")
    p.add_argument("words", nargs=argparse.REMAINDER, metavar="COMMAND",
                   help="a command line in one argument, or a program and its arguments")
    p.set_defaults(run=run_create)
