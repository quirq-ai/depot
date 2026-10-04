"""Version pinning: run the qq version the current repo pins in infra/repo.toml.

The pin is the manifest's `[qq]` table (schema quirq-repo/1, read only through qqsync):

    [qq]
    version = "0.1.0"                  # installs the depot tag v0.1.0
    source = "https://..."             # optional: where to get exactly these bytes
    digest = "git:<commit>"            # or sha256:<archive digest>

Each pinned version gets its own environment under $QQ_HOME/versions, made once and
reused. Before first use the installed qq must report the pinned version, so a
tag or archive that holds a different version is an error, not a silent mismatch.
"""
from __future__ import annotations

import fcntl
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from qqsync.errors import ManifestError
from qqsync.manifest import load

from qqdepot import __version__

MANIFEST = Path("infra/repo.toml")
# Set in the environment of a pinned qq, so it runs instead of dispatching again.
PINNED_ENV = "QQ_PINNED"
DEFAULT_DEPOT_URL = "https://github.com/quirq-ai/depot"


class PinError(Exception):
    """The pinned qq cannot be found, installed or verified."""


@dataclass(frozen=True)
class Pin:
    version: str
    source: str | None = None
    digest: str | None = None
    manifest: Path | None = None

    @property
    def key(self) -> str:
        """The directory name of this pin's environment under $QQ_HOME/versions."""
        if not self.digest:
            return self.version
        algo, _, value = self.digest.partition(":")
        return f"{self.version}-{algo}-{value[:16]}"


def qq_home() -> Path:
    if home := os.environ.get("QQ_HOME"):
        return Path(home).resolve()   # links into the store must not depend on the cwd
    cache = os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache"
    return Path(cache) / "qq"


def find_manifest(start: Path) -> Path | None:
    """The nearest infra/repo.toml at or above `start`."""
    for directory in (start, *start.parents):
        candidate = directory / MANIFEST
        if candidate.is_file():
            return candidate
    return None


def read_pin(start: Path) -> Pin | None:
    """The pin of the repo containing `start`, or None outside a repo or without a [qq] table."""
    manifest = find_manifest(start)
    if manifest is None:
        return None
    try:
        data = load(manifest)
    except ManifestError as e:
        raise PinError(f"{e}\nFix the manifest, or set {PINNED_ENV}=1 to run qq {__version__} as is.") from None
    table = data.get("qq")
    if not table:
        return None
    return Pin(table["version"], table.get("source"), table.get("digest"), manifest)


def is_self(pin: Pin) -> bool:
    """True when this process already is the pinned qq."""
    return pin.source is None and pin.version == __version__


def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, check=True, text=True, capture_output=True, **kwargs)
    except subprocess.CalledProcessError as e:
        detail = (e.stderr or e.stdout or "").strip().splitlines()[-5:]
        raise PinError(f"command failed: {' '.join(cmd)}\n" + "\n".join(detail)) from None


def _fetch_archive(pin: Pin, into: Path) -> Path:
    """Download `pin.source` and check it against the sha256 digest."""
    name = Path(urllib.parse.urlparse(pin.source).path).name or "qqdepot.tar.gz"
    path = into / name
    try:
        with urllib.request.urlopen(pin.source, timeout=60) as response, path.open("wb") as out:
            shutil.copyfileobj(response, out)
    except (OSError, ValueError) as e:
        raise PinError(f"cannot download {pin.source}: {e}") from None
    actual = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != pin.digest:
        raise PinError(f"{pin.source} has digest {actual}, but {pin.manifest} pins {pin.digest}")
    return path


def requirement(pin: Pin, scratch: Path) -> str:
    """What pip installs for this pin."""
    if pin.source is None:
        base = os.environ.get("QQ_DEPOT_URL", DEFAULT_DEPOT_URL)
        return f"git+{base}@v{pin.version}"
    algo, _, value = pin.digest.partition(":")
    if algo == "git":
        return f"git+{pin.source}@{value}"
    return str(_fetch_archive(pin, scratch))


def installed_version(qq: Path) -> str:
    env = {**os.environ, PINNED_ENV: "1"}
    env.pop("PYTHONPATH", None)
    out = _run([str(qq), "--version"], env=env).stdout.strip()
    return out.removeprefix("qq ")


def ensure(pin: Pin) -> Path:
    """The qq executable for `pin`, installing it into $QQ_HOME/versions first if needed."""
    versions = qq_home() / "versions"
    target = versions / pin.key
    qq = target / "bin" / "qq"
    done = target / ".qq-installed"  # written last: a half-made environment is never used
    if done.is_file():
        return qq
    versions.mkdir(parents=True, exist_ok=True)
    # Environments cannot move once made, so build in place under a lock that keeps
    # concurrent qq runs from building the same version twice.
    with (versions / f".{pin.key}.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if done.is_file():
            return qq
        print(f"qq: installing qq {pin.version} pinned by {pin.manifest}", file=sys.stderr)
        shutil.rmtree(target, ignore_errors=True)
        try:
            with tempfile.TemporaryDirectory(dir=versions, prefix=f".{pin.key}.") as scratch:
                _run([sys.executable, "-m", "venv", str(target)])
                _run([str(target / "bin" / "python"), "-m", "pip", "install", "--quiet",
                      "--disable-pip-version-check", requirement(pin, Path(scratch))])
            got = installed_version(qq)
            if got != pin.version:
                raise PinError(f"{pin.manifest} pins qq {pin.version}, but what it names installs qq {got}")
        except BaseException:
            shutil.rmtree(target, ignore_errors=True)
            raise
        done.write_text(f"{pin.version} {pin.source or ''} {pin.digest or ''}\n")
    return qq


def dispatch(argv: list[str]) -> None:
    """Replace this process with the pinned qq, unless this already is it."""
    if os.environ.pop(PINNED_ENV, None) == "1":
        # Only this process skips the pin; a qq it runs, maybe in another repo, pins again.
        return
    pin = read_pin(Path.cwd())
    if pin is None or is_self(pin):
        return
    qq = ensure(pin)
    env = {**os.environ, PINNED_ENV: "1"}
    env.pop("PYTHONPATH", None)  # the pinned qq runs its own code, never code shadowing it
    os.execve(qq, [str(qq), *argv], env)
