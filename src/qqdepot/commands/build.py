"""qq build and qq test: run the same adapter actions as CI, here.

Both hand the repo's manifest to quirq-ai/recipes, the same planner and runner CI uses, so a
local run and a CI run of one commit execute the same actions and leave the same JUnit XML
under <repo>/.qq/out. qq adds only the repo root and the toolchains `qq sync` unpacked.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from qqrecipes import cli as recipes

from qqdepot.pin import MANIFEST, find_manifest

# `qq sync` (V0-DEP-02) unpacks each pinned toolchain to <repo>/.qq/toolchains/<name>.
TOOLCHAINS = Path(".qq/toolchains")


def repo_root(start: Path) -> Path | None:
    manifest = find_manifest(start)
    return None if manifest is None else manifest.parent.parent


def toolchain_args(root: Path) -> list[str]:
    """--toolchain NAME=ROOT for every toolchain `qq sync` unpacked; others come from PATH."""
    synced = root / TOOLCHAINS
    if not synced.is_dir():
        return []
    return [f"--toolchain={d.name}={d}" for d in sorted(synced.iterdir()) if d.is_dir()]


def recipes_argv(goal: str, args: argparse.Namespace, root: Path) -> list[str]:
    argv = ["execute", goal, "--repo", str(root)]
    argv += [f"--target={t}" for t in args.targets]
    argv += toolchain_args(root)
    argv += [f"--toolchain={t}" for t in args.toolchain]
    if args.out:
        argv += ["--out", args.out]
    if args.keep_going:
        argv.append("--keep-going")
    return argv


def run(args: argparse.Namespace) -> int:
    root = repo_root(Path.cwd())
    if root is None:
        print(f"qq: no {MANIFEST} here or above {Path.cwd()}; run qq {args.goal} inside a repo", file=sys.stderr)
        return 2
    # TODO(expert): with the executor interface (V0-RBE-01), offer acknowledge-then-push here
    # too: return a run ID at once and push the verdict, as `qq try` does.
    return recipes.main(recipes_argv(args.goal, args, root))


def register(sub) -> None:
    for goal, help_text in (("build", "build targets here, as CI does"),
                            ("test", "build and test targets here, as CI does")):
        p = sub.add_parser(goal, help=help_text,
                           description=f"{help_text}. Results land in <repo>/.qq/out (JUnit XML,"
                                       " logs and results.json).")
        p.add_argument("targets", nargs="*", metavar="TARGET", help="limit to these targets (default all)")
        p.add_argument("--toolchain", action="append", default=[], metavar="NAME=ROOT",
                       help="use the toolchain unpacked at ROOT (overrides what qq sync unpacked)")
        p.add_argument("--out", help="results directory (default <repo>/.qq/out)")
        p.add_argument("--keep-going", action="store_true", help="run every action even after a failure")
        p.set_defaults(run=run, goal=goal)
