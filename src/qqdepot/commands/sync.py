"""qq sync and qq fetch: get a repo's pinned toolchains and dependencies.

    qq sync           in a repo: fetch every pin in infra/repo.toml and link it under .qq/
    qq fetch URL [DIR]  clone a repo, then qq sync in it

Toolchains land at <repo>/.qq/toolchains/<name> and dependencies at <repo>/.qq/deps/<name>,
each a link into the shared store (see store.py), so `qq build` and `qq test` find them.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from qqsync.errors import ManifestError
from qqsync.manifest import load

from qqdepot import store
from qqdepot.commands.build import TOOLCHAINS, repo_root
from qqdepot.pin import MANIFEST

DEPS = Path(".qq/deps")
RECORD = Path(".qq/sync.json")
SECTIONS = (("toolchains", TOOLCHAINS), ("deps", DEPS))


def _link(target: Path, link: Path) -> None:
    """Point `link` at `target`, replacing what was there in one step."""
    link.parent.mkdir(parents=True, exist_ok=True)
    if link.exists() and not link.is_symlink():
        raise store.FetchError(f"{link} is not a link qq made; move it away and run qq sync again")
    tmp = link.with_name(f".{link.name}.{os.getpid()}.tmp")
    tmp.unlink(missing_ok=True)
    tmp.symlink_to(target, target_is_directory=True)
    os.replace(tmp, link)


def sync(root: Path) -> list[dict]:
    """Fetch and link every pin of the repo at `root`. Returns what was synced."""
    manifest = load(root / MANIFEST)
    synced = []
    for section, base in SECTIONS:
        pins = manifest.get(section, {})
        for name in pins:
            artifact = store.resolve(manifest, section, name)
            entry = store.ensure(artifact)
            _link(entry, root / base / name)
            synced.append({"section": section, "name": name, "digest": artifact.digest, "path": str(entry)})
            print(f"{base / name}: {artifact.digest[:19]}…")
        # A pin dropped from the manifest must not linger and be built with.
        if (root / base).is_dir():
            for stale in (root / base).iterdir():
                if stale.name not in pins and not stale.name.startswith(".") and stale.is_symlink():
                    stale.unlink()
    (root / RECORD).parent.mkdir(parents=True, exist_ok=True)
    (root / RECORD).write_text(json.dumps(synced, indent=2) + "\n")
    return synced


def run_sync(args: argparse.Namespace) -> int:
    root = repo_root(Path.cwd())
    if root is None:
        print(f"qq: no {MANIFEST} here or above {Path.cwd()}; run qq sync inside a repo", file=sys.stderr)
        return 2
    try:
        sync(root)
    except (ManifestError, store.FetchError, OSError) as e:
        print(f"qq: {e}", file=sys.stderr)
        return 1
    return 0


def run_fetch(args: argparse.Namespace) -> int:
    directory = Path(args.directory or Path(args.url.rstrip("/")).name.removesuffix(".git"))
    if directory.exists():
        print(f"qq: {directory} already exists; run qq sync inside it instead", file=sys.stderr)
        return 1
    try:
        subprocess.run(["git", "clone", "--quiet", args.url, str(directory)], check=True)
    except (OSError, subprocess.CalledProcessError) as e:
        print(f"qq: cannot clone {args.url}: {e}", file=sys.stderr)
        return 1
    if not (directory / MANIFEST).is_file():
        print(f"qq: cloned {args.url}, which has no {MANIFEST}; nothing to sync", file=sys.stderr)
        return 1
    try:
        sync(directory.resolve())
    except (ManifestError, store.FetchError, OSError) as e:
        print(f"qq: {e}\nqq: cloned into {directory}; fix the problem and run qq sync there", file=sys.stderr)
        return 1
    return 0


def register(sub) -> None:
    s = sub.add_parser("sync", help="fetch the repo's pinned toolchains and dependencies",
                       description="Fetch every pin in infra/repo.toml, check it against its digest,"
                                   " and link it under <repo>/.qq.")
    s.set_defaults(run=run_sync)
    f = sub.add_parser("fetch", help="clone a repo, then qq sync in it")
    f.add_argument("url", help="the repo to clone")
    f.add_argument("directory", nargs="?", help="where to clone it (default: its name)")
    f.set_defaults(run=run_fetch)
