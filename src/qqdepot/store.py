"""Fetch pinned toolchains and dependencies into a content-addressed store, checked by digest.

A pin (quirq-repo/1) is a source and a digest. The source's scheme picks the fetcher:

    https://..., file://...        an artifact; its bytes must hash to the sha256 digest
    oci://REGISTRY/REPO[@MANIFEST] an artifact layer in an OCI registry (the toolchains repo
                                   publishes here); the blob named by the sha256 digest
    any git URL + git:<commit>     a source tree at that commit

Each pin is fetched once per machine into $QQ_HOME/store/<algo>-<hex>, unpacked when it is a
tarball, and never changed after.

Pins are resolved, fetched (https, file, oci) and verified (every scheme, git checkouts included)
by qqsync.pins, so depot only adds the git checkout and unpacking.
TODO(expert): make store entries read only, so nothing installed into a synced toolchain
changes the copy other repos share. A half-fetched entry is never visible: it is built in a
scratch directory and renamed into place.
"""
from __future__ import annotations

import os
import posixpath
import shutil
import subprocess
import tarfile
import tempfile
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

from qqsync import pins

from qqdepot.pin import qq_home


class FetchError(Exception):
    """A pin cannot be fetched or does not match its digest."""


@dataclass(frozen=True)
class Artifact:
    """One thing to fetch: a manifest pin, resolved for this platform by qqsync."""

    pin: pins.Pin

    @classmethod
    def of(cls, name: str, source: str, digest: str, section: str = "deps") -> Artifact:
        return cls(pins.Pin(section, name, None, source, digest))

    name = property(lambda self: self.pin.label)
    source = property(lambda self: self.pin.source)
    digest = property(lambda self: self.pin.digest)
    algo = property(lambda self: self.pin.algorithm)
    value = property(lambda self: self.pin.digest.partition(":")[2])


def resolve(manifest: dict, section: str, name: str) -> Artifact:
    """The artifact `section.name` pins for this machine's platform (qqsync.pins.find_pin)."""
    try:
        return Artifact(pins.find_pin(manifest, section, name))
    except pins.PinError as e:
        raise FetchError(str(e)) from None


def store_dir() -> Path:
    return qq_home() / "store"


def ensure(artifact: Artifact) -> Path:
    """The store entry for `artifact`, fetching it first if this machine does not have it."""
    store = store_dir()
    entry = store / f"{artifact.algo}-{artifact.value}"
    if entry.is_dir():
        return entry
    store.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=store, prefix=f".{artifact.algo}-{artifact.value[:16]}.") as scratch:
        scratch = Path(scratch)
        staged = scratch / "entry"
        if artifact.algo == "git":
            _fetch_git(artifact, staged)
        else:
            blob = _fetch_blob(artifact, scratch / "blob")
            _unpack(blob, staged, artifact)
        try:
            staged.rename(entry)
        except OSError:
            if not entry.is_dir():  # another qq finished the same entry first: theirs is identical
                raise
    return entry


def _fetch_blob(artifact: Artifact, path: Path) -> Path:
    """Download the artifact's bytes to `path` with qqsync.pins.fetch, which checks them against
    the pin (https://, file:// and oci:// registry layers)."""
    try:
        pins.fetch(artifact.pin, path)
    except pins.PinError as e:
        scheme = urllib.parse.urlparse(artifact.source).scheme
        hint = "" if scheme in ("https", "file", "oci") else "; a git source needs digest = \"git:<commit>\""
        raise FetchError(f"{e}{hint}") from None
    return path


def _check_members(archive: tarfile.TarFile, artifact: Artifact) -> None:
    """Containment that does not rest on the CPython build: the "data" filter had bypasses until
    3.11.13, 3.12.11 and 3.13.4 (symlink chains), and qq runs on older system Pythons too.

    Every member is a plain relative path; no member sits under a symlink member, so a link
    cannot move where later members land; a link's target, taken from the link's own directory,
    stays inside the archive; and a hard link names a regular file of the archive.
    """
    def inside(path: str) -> bool:
        norm = posixpath.normpath(path)
        return not (norm == ".." or norm.startswith("../") or posixpath.isabs(norm))

    links, files = set(), set()
    for m in archive.getmembers():
        if posixpath.isabs(m.name) or ".." in m.name.split("/") or not inside(m.name):
            raise FetchError(f"{artifact.name}: archive member {m.name!r} leaves the archive")
        name = posixpath.normpath(m.name)
        parts = name.split("/")
        if any("/".join(parts[:i]) in links for i in range(1, len(parts))):
            raise FetchError(f"{artifact.name}: archive member {m.name!r} sits under a symlink")
        if m.issym():
            if not inside(posixpath.join(posixpath.dirname(name), m.linkname)):
                raise FetchError(f"{artifact.name}: symlink {m.name!r} points outside the archive")
            links.add(name)
        elif m.islnk():
            if posixpath.normpath(m.linkname) not in files:
                raise FetchError(f"{artifact.name}: hard link {m.name!r} does not name a file before it")
        elif m.isfile():
            files.add(name)


def _unpack(blob: Path, into: Path, artifact: Artifact) -> None:
    if tarfile.is_tarfile(blob):
        try:
            with tarfile.open(blob) as archive:
                _check_members(archive, artifact)
                # "data" refuses absolute paths, links that leave `into` and device files, and keeps
                # executable bits and in-tree symlinks, which is all a toolchain needs.
                archive.extractall(into, filter="data")
        except (tarfile.TarError, OSError) as e:
            raise FetchError(f"{artifact.name}: cannot unpack {artifact.source}: {e}") from None
    else:
        into.mkdir()
        name = Path(urllib.parse.urlparse(artifact.source).path).name or artifact.name
        shutil.copy2(blob, into / name)


def _fetch_git(artifact: Artifact, into: Path) -> None:
    if artifact.source.startswith("-"):
        raise FetchError(f"{artifact.name}: {artifact.source!r} is not a repository URL")
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}  # GIT_DIR would override -C

    def git(*args: str) -> None:
        try:
            subprocess.run(["git", "-C", str(into), *args], check=True, capture_output=True, text=True, env=env)
        except subprocess.CalledProcessError as e:
            raise FetchError(f"{artifact.name}: git {' '.join(args)} failed: {e.stderr.strip()}") from None
        except OSError as e:
            raise FetchError(f"{artifact.name}: cannot run git: {e}") from None

    into.mkdir()
    git("init", "-q", f"--object-format={'sha256' if len(artifact.value) == 64 else 'sha1'}")
    git("fetch", "-q", "--depth=1", "--end-of-options", artifact.source, artifact.value)
    git("checkout", "-q", "--detach", "FETCH_HEAD")
    try:
        pins.verify_checkout(artifact.pin, into)
    except pins.PinError as e:
        raise FetchError(str(e)) from None
