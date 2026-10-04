"""Fetch pinned toolchains and dependencies into a content-addressed store, checked by digest.

A pin (quirq-repo/1) is a source and a digest. The source's scheme picks the fetcher:

    https://..., file://...        an artifact; its bytes must hash to the sha256 digest
    oci://REGISTRY/REPO[@MANIFEST] an artifact layer in an OCI registry (the toolchains repo
                                   publishes here); the blob named by the sha256 digest
    any git URL + git:<commit>     a source tree at that commit

Each pin is fetched once per machine into $QQ_HOME/store/<algo>-<hex>, unpacked when it is a
tarball, and never changed after.

TODO(expert): use qqsync.pins (find_pin, fetch, verify_checkout; V0-SYN-03) for the https, file
and git paths once depot and recipes move their qqsync pin past it together; pip refuses two
different pins of one package. Keep only the OCI fetch and unpacking here.
TODO(expert): make store entries read only, so nothing installed into a synced toolchain
changes the copy other repos share. A half-fetched entry is never visible: it is built in a
scratch directory and renamed into place.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from qqdepot.pin import qq_home

OCI_ACCEPT = ", ".join([
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
])


class FetchError(Exception):
    """A pin cannot be fetched or does not match its digest."""


@dataclass(frozen=True)
class Artifact:
    """One thing to fetch: a manifest pin resolved for this platform."""

    name: str
    source: str
    digest: str

    @property
    def algo(self) -> str:
        return self.digest.partition(":")[0]

    @property
    def value(self) -> str:
        return self.digest.partition(":")[2]


def host_platform() -> str:
    """<os>-<arch> as manifests spell it, for example linux-x86_64 or macos-arm64."""
    system = {"darwin": "macos"}.get(platform.system().lower(), platform.system().lower())
    machine = {"amd64": "x86_64", "aarch64": "arm64"}.get(platform.machine().lower(), platform.machine().lower())
    return f"{system}-{machine}"


def resolve(name: str, pin: dict, where: str) -> Artifact:
    """The artifact a pin names for this machine's platform."""
    if "platforms" in pin:
        here = host_platform()
        if here not in pin["platforms"]:
            raise FetchError(f"{where} {name!r} has no build for {here} (it has: {', '.join(pin['platforms'])})")
        pin = pin["platforms"][here]
    return Artifact(name, pin["source"], pin["digest"])


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
    scheme = urllib.parse.urlparse(artifact.source).scheme
    if scheme == "oci":
        _download_oci(artifact, path)
    elif scheme in ("https", "http", "file"):
        _download(artifact.source, path, {})
    else:
        raise FetchError(f"{artifact.name}: cannot fetch {artifact.source!r} with digest {artifact.digest}:"
                         " use https://, file://, oci:// or a git digest")
    actual = f"sha256:{_sha256(path)}"
    if actual != artifact.digest:
        raise FetchError(f"{artifact.name}: {artifact.source} has digest {actual}, but the manifest pins"
                         f" {artifact.digest}")
    return path


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _download(url: str, path: Path, headers: dict[str, str]) -> None:
    request = urllib.request.Request(url, headers=headers)
    try:
        with _OPENER.open(request, timeout=120) as response, path.open("wb") as out:
            shutil.copyfileobj(response, out)
    except (OSError, ValueError) as e:
        raise FetchError(f"cannot download {url}: {e}") from None


def _oci_parts(source: str) -> tuple[str, str, str | None]:
    """registry, repository and the optional manifest digest of oci://REGISTRY/REPO[@DIGEST]."""
    rest = source.removeprefix("oci://")
    rest, _, manifest = rest.partition("@")
    registry, _, repository = rest.partition("/")
    if not registry or not repository:
        raise FetchError(f"{source!r} is not oci://REGISTRY/REPOSITORY[@sha256:...]")
    return registry, repository, manifest or None


class _HttpsOnlyRedirects(urllib.request.HTTPRedirectHandler):
    """Registries redirect blobs to storage hosts; follow only https there."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl).scheme.lower() != "https":
            raise urllib.error.URLError(f"refusing a redirect to {newurl}; only https is followed")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_HttpsOnlyRedirects)


def _oci_get(url: str, accept: str | None, token: list[str], path: Path | None = None) -> bytes:
    """GET from a registry, answering one bearer-token challenge anonymously (public packages).

    With `path`, stream the body there and return b"".
    """
    for attempt in range(2):
        request = urllib.request.Request(url, headers={"Accept": accept} if accept else {})
        if token:
            # Unredirected: the token is for the registry, never for the storage host a blob
            # redirects to (which carries its own signed URL).
            request.add_unredirected_header("Authorization", f"Bearer {token[0]}")
        try:
            with _OPENER.open(request, timeout=120) as r:
                if path is None:
                    return r.read()
                with path.open("wb") as out:
                    shutil.copyfileobj(r, out, 1 << 20)
                return b""
        except urllib.error.HTTPError as e:
            challenge = e.headers.get("WWW-Authenticate", "")
            if e.code != 401 or attempt or not challenge.lower().startswith("bearer "):
                hint = " (is the package public?)" if e.code in (401, 403) else ""
                raise FetchError(f"cannot fetch {url}: HTTP {e.code}{hint}") from None
            token[:] = [_anonymous_token(challenge)]
        except (OSError, ValueError) as e:
            raise FetchError(f"cannot fetch {url}: {e}") from None
    raise AssertionError("unreachable")


def _anonymous_token(challenge: str) -> str:
    fields = {k: v for k, v in re.findall(r'(\w+)="([^"]*)"', challenge)}
    if "realm" not in fields:
        raise FetchError(f"registry asked for a token without a realm: {challenge!r}")
    query = urllib.parse.urlencode({k: fields[k] for k in ("service", "scope") if k in fields})
    try:
        sep = "&" if "?" in fields["realm"] else "?"
        with _OPENER.open(f"{fields['realm']}{sep}{query}", timeout=60) as r:
            body = json.load(r)
    except (OSError, ValueError) as e:
        raise FetchError(f"cannot get a registry token from {fields['realm']}: {e}") from None
    token = body.get("token") or body.get("access_token")
    if not token:
        raise FetchError(f"registry token endpoint {fields['realm']} returned no token")
    return token


def _download_oci(artifact: Artifact, path: Path) -> None:
    registry, repository, manifest_digest = _oci_parts(artifact.source)
    # Plain http is allowed only for a registry on this machine (tests, a local mirror).
    local = registry.split(":")[0] in ("127.0.0.1", "localhost", "[::1]")
    scheme = "http" if local and os.environ.get("QQ_OCI_SCHEME") == "http" else "https"
    base = f"{scheme}://{registry}/v2/{repository}"
    token: list[str] = []
    if manifest_digest:
        # The source names a manifest: the pinned layer must be in it, so a pin cannot pair one
        # artifact's manifest with another's bytes.
        raw = _oci_get(f"{base}/manifests/{manifest_digest}", OCI_ACCEPT, token)
        if f"sha256:{hashlib.sha256(raw).hexdigest()}" != manifest_digest:
            raise FetchError(f"{artifact.name}: manifest {manifest_digest} does not match its digest")
        layers = [layer.get("digest") for layer in json.loads(raw).get("layers", [])]
        if artifact.digest not in layers:
            raise FetchError(f"{artifact.name}: manifest {manifest_digest} has no layer {artifact.digest}")
    _oci_get(f"{base}/blobs/{artifact.digest}", None, token, path)


def _unpack(blob: Path, into: Path, artifact: Artifact) -> None:
    if tarfile.is_tarfile(blob):
        try:
            with tarfile.open(blob) as archive:
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
    head = subprocess.run(["git", "-C", str(into), "rev-parse", "HEAD"], capture_output=True, text=True,
                          env=env).stdout.strip()
    if head != artifact.value:
        raise FetchError(f"{artifact.name}: {artifact.source} gave commit {head}, not {artifact.value}")
