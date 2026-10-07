"""qq: the quirq infra command line.

Run inside a repo, qq runs the version that repo pins in infra/repo.toml.
Other qq repos add subcommands through the `qq.commands` entry point group.
"""
from __future__ import annotations

import argparse
import sys
from importlib.metadata import entry_points

from qqdepot import __version__
from qqdepot.commands import build, change, run, sync
from qqdepot.pin import PinError, dispatch

# An entry point in this group names a function register(subparsers) that adds one
# subcommand whose parser sets `run`, a function of the parsed args returning the exit code.
COMMANDS_GROUP = "qq.commands"
BUILTIN = (sync, build, change, run)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="qq", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version", version=f"qq {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    for module in BUILTIN:
        module.register(sub)
    for ep in sorted(entry_points(group=COMMANDS_GROUP), key=lambda ep: ep.name):
        try:
            ep.load()(sub)
        except Exception as e:  # one broken plugin must not take down every command
            print(f"qq: skipping subcommand {ep.name!r}: {e}", file=sys.stderr)
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    try:
        dispatch(argv)
    except PinError as e:
        print(f"qq: {e}", file=sys.stderr)
        return run.QQ_FAILED if argv[:1] == ["run"] else 2   # qq run's own failures are 125
    if argv[:1] == ["run"]:   # parsed on its own: its usage errors must not exit 2 like a command's
        return run.main(argv[1:])
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "run", None) is None:
        parser.print_help()
        return 0
    return args.run(args)


if __name__ == "__main__":
    sys.exit(main())
