"""The qq bootstrap, run by bin/qq straight from a depot checkout, before anything is installed.

Standard library only. It builds a launcher environment from the checkout's committed
tree, once per checkout and commit, and runs its qq. The launcher then runs the version
the current repo pins (see pin.py).

Each launcher is built in place under a lock with a completion marker, so concurrent
first runs build it once and nobody uses or deletes a half-built one. It is built from
an export of the commit, not from the checkout itself, so the checkout stays untouched
(it may be read only) and no build leftovers leak into the launcher.
"""
from __future__ import annotations

import fcntl
import hashlib
import io
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

_SKIP = shutil.ignore_patterns(".git", "build", "*.egg-info", "__pycache__")


def qq_home() -> Path:
    if home := os.environ.get("QQ_HOME"):
        return Path(home)
    cache = os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache"
    return Path(cache) / "qq"


def head(depot: Path) -> str | None:
    try:
        out = subprocess.run(["git", "-C", str(depot), "rev-parse", "HEAD"],
                             check=True, capture_output=True, text=True)
    except (OSError, subprocess.CalledProcessError):
        return None
    return out.stdout.strip()


def export(depot: Path, commit: str | None, into: Path) -> Path:
    """A copy of the depot tree to install from: the commit if git knows it, else the files."""
    src = into / "depot"
    if commit:
        tar = subprocess.run(["git", "-C", str(depot), "archive", "--format=tar", commit],
                             check=True, capture_output=True).stdout
        with tarfile.open(fileobj=io.BytesIO(tar)) as archive:
            archive.extractall(src, filter="data")
    else:
        shutil.copytree(depot, src, ignore=_SKIP)
    return src


def ensure_launcher(depot: Path) -> Path:
    commit = head(depot)
    # TODO(expert): without git the key cannot see edits; a content hash would.
    key = hashlib.sha256(f"{depot}\0{commit or 'no-git'}".encode()).hexdigest()[:16]
    launchers = qq_home() / "launchers"
    target = launchers / key
    qq = target / "bin" / "qq"
    done = target / ".qq-installed"
    if done.is_file():
        return qq
    launchers.mkdir(parents=True, exist_ok=True)
    with (launchers / f".{key}.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if done.is_file():
            return qq
        print(f"qq: setting up the launcher for {depot} in {target}", file=sys.stderr)
        shutil.rmtree(target, ignore_errors=True)
        try:
            with tempfile.TemporaryDirectory(dir=launchers, prefix=f".{key}.") as scratch:
                src = export(depot, commit, Path(scratch))
                subprocess.run([sys.executable, "-m", "venv", str(target)], check=True)
                # As pin.pip_install: PyPI only by hash from the lock, the rest with no index.
                pip = [str(target / "bin" / "python"), "-m", "pip", "--disable-pip-version-check", "--quiet"]
                lock = src / "src" / "qqdepot" / "locks" / "pypi.txt"
                for cmd in ([*pip, "install", "--require-hashes", "--no-deps", "-r", str(lock)],
                            [*pip, "install", "--no-index", "--no-build-isolation", str(src)],
                            [*pip, "check"]):
                    subprocess.run(cmd, check=True, capture_output=True)
        except BaseException:
            shutil.rmtree(target, ignore_errors=True)
            raise
        done.write_text(f"{depot} {commit or 'no-git'}\n")
    # TODO(expert): remove launchers of commits no checkout is at any more.
    return qq


def main(argv: list[str]) -> int:
    depot = Path(argv[0])
    try:
        qq = ensure_launcher(depot)
    except (OSError, subprocess.CalledProcessError) as e:
        detail = getattr(e, "stderr", None)
        detail = detail.decode(errors="replace") if isinstance(detail, bytes) else detail or ""
        print(f"qq: cannot set up the launcher from {depot}: {e}\n{detail.strip()}".rstrip(), file=sys.stderr)
        return 1
    os.execv(qq, [str(qq), *argv[1:]])
    return 1  # not reached


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
