"""V0-DEP-02: qq sync and qq fetch get a repo's pinned toolchains and dependencies.

Done-when shape: a fresh clone is buildable after `qq sync` alone. Here the repo pins a
toolchain its test needs, so `qq test` fails before `qq sync` and passes after it, with nothing
installed by hand. Sources are local (file://, a local git repo, a local OCI registry) so the
test needs no network.
"""
import hashlib
import io
import json
import subprocess
import tarfile
import textwrap
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from qqrecipes import loader

from qqdepot import cli, store


@pytest.fixture(autouse=True)
def parity_adapters(monkeypatch):
    monkeypatch.setattr(loader, "PACKAGE", "parity_adapters")


def sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def tool_tarball(message: str = "hello from the toolchain") -> bytes:
    """A toolchain: bin/hello, an executable that prints `message`."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        script = f"#!/bin/sh\necho '{message}'\n".encode()
        info = tarfile.TarInfo("bin/hello")
        info.size, info.mode = len(script), 0o755
        tar.addfile(info, io.BytesIO(script))
    return buf.getvalue()


def git(cwd, *args) -> str:
    return subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
                          check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def dep_repo(tmp_path) -> tuple[Path, str]:
    repo = tmp_path / "dep"
    repo.mkdir()
    (repo / "data.txt").write_text("dep data\n")
    git(repo, "init", "-q")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "dep")
    git(repo, "config", "uploadpack.allowReachableSHA1InWant", "true")
    return repo, git(repo, "rev-parse", "HEAD")


def product(tmp_path: Path, pins: str) -> Path:
    repo = tmp_path / "product"
    (repo / "infra").mkdir(parents=True)
    (repo / "infra" / "repo.toml").write_text('schema = "quirq-repo/1"\n' + textwrap.dedent(pins) + textwrap.dedent("""
        [[targets]]
        name = "greet"
        kind = "uses-tool"
        """))
    git(repo, "init", "-q")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "product")
    return repo


def test_fresh_clone_is_buildable_after_sync(tmp_path, monkeypatch, dep_repo):
    archive = tmp_path / "hello.tar.gz"
    archive.write_bytes(tool_tarball())
    dep, commit = dep_repo
    origin = product(tmp_path, f"""
        [toolchains.hello]
        version = "1.0"
        source = "{archive.as_uri()}"
        digest = "{sha256(archive.read_bytes())}"
        [deps.data]
        source = "{dep.as_uri()}"
        digest = "git:{commit}"
        """)

    monkeypatch.chdir(tmp_path)
    assert cli.main(["fetch", origin.as_uri(), "clone"]) == 0
    clone = tmp_path / "clone"
    assert (clone / ".qq" / "toolchains" / "hello" / "bin" / "hello").is_file()
    assert (clone / ".qq" / "deps" / "data" / "data.txt").read_text() == "dep data\n"
    record = json.loads((clone / ".qq" / "sync.json").read_text())
    assert {r["name"] for r in record} == {"hello", "data"}

    monkeypatch.chdir(clone)
    assert cli.main(["test"]) == 0
    log = (clone / ".qq" / "out" / "logs" / "greet.test.hello.log").read_text()
    assert "hello from the toolchain" in log


def test_test_fails_without_sync(tmp_path, monkeypatch):
    archive = tmp_path / "hello.tar.gz"
    archive.write_bytes(tool_tarball())
    repo = product(tmp_path, f"""
        [toolchains.hello]
        source = "{archive.as_uri()}"
        digest = "{sha256(archive.read_bytes())}"
        """)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")  # no `hello` on PATH: only qq sync provides it
    assert cli.main(["test"]) == 1
    assert cli.main(["sync"]) == 0
    assert cli.main(["test"]) == 0


def test_digest_mismatch_is_an_error(tmp_path, monkeypatch, capsys):
    archive = tmp_path / "hello.tar.gz"
    archive.write_bytes(tool_tarball())
    repo = product(tmp_path, f"""
        [toolchains.hello]
        source = "{archive.as_uri()}"
        digest = "sha256:{'0' * 64}"
        """)
    monkeypatch.chdir(repo)
    assert cli.main(["sync"]) == 1
    assert "but the manifest pins" in capsys.readouterr().err
    assert not (repo / ".qq" / "toolchains" / "hello").exists()
    assert not any(store.store_dir().glob("sha256-*"))


def test_store_is_shared_and_stale_links_go(tmp_path, monkeypatch):
    archive = tmp_path / "hello.tar.gz"
    archive.write_bytes(tool_tarball())
    pin = f'[toolchains.hello]\nsource = "{archive.as_uri()}"\ndigest = "{sha256(archive.read_bytes())}"\n'
    repo = product(tmp_path, pin)
    monkeypatch.chdir(repo)
    assert cli.main(["sync"]) == 0
    archive.unlink()  # the store has it now; a second sync must not fetch again
    assert cli.main(["sync"]) == 0

    manifest = repo / "infra" / "repo.toml"
    manifest.write_text(manifest.read_text().replace(pin, ""))
    assert cli.main(["sync"]) == 0
    assert not (repo / ".qq" / "toolchains" / "hello").exists()


def test_platform_pins(monkeypatch):
    pin = {"platforms": {"linux-x86_64": {"source": "https://example.invalid/a", "digest": "sha256:" + "a" * 64}}}
    monkeypatch.setattr(store, "host_platform", lambda: "linux-x86_64")
    assert store.resolve("t", pin, "[toolchains]").source == "https://example.invalid/a"
    monkeypatch.setattr(store, "host_platform", lambda: "macos-arm64")
    with pytest.raises(store.FetchError, match="no build for macos-arm64"):
        store.resolve("t", pin, "[toolchains]")


def test_unknown_scheme(tmp_path):
    with pytest.raises(store.FetchError, match="cannot fetch"):
        store.ensure(store.Artifact("t", "ftp://example.invalid/x", "sha256:" + "a" * 64))


class Registry(BaseHTTPRequestHandler):
    """A minimal OCI registry that wants an anonymous bearer token, as ghcr.io does."""

    blobs: dict[str, bytes] = {}
    manifests: dict[str, bytes] = {}

    def log_message(self, *args):
        pass

    def do_GET(self):
        host = self.headers["Host"]
        if self.path.startswith("/token"):
            return self._send(200, json.dumps({"token": "anon"}).encode())
        if self.headers.get("Authorization") != "Bearer anon":
            self.send_response(401)
            self.send_header("WWW-Authenticate",
                             f'Bearer realm="http://{host}/token",service="test",scope="repository:tc/hello:pull"')
            self.end_headers()
            return
        kind, _, digest = self.path.removeprefix("/v2/tc/hello/").partition("/")
        found = {"blobs": self.blobs, "manifests": self.manifests}.get(kind, {}).get(digest)
        self._send(200, found) if found is not None else self._send(404, b"")

    def _send(self, code, body):
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def registry(monkeypatch):
    server = ThreadingHTTPServer(("127.0.0.1", 0), Registry)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("QQ_OCI_SCHEME", "http")
    yield f"127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def test_oci_toolchain(tmp_path, monkeypatch, registry):
    layer = tool_tarball("hello from the registry")
    other = tool_tarball("some other artifact")
    manifest = json.dumps({"schemaVersion": 2, "layers": [{"digest": sha256(layer)}]}).encode()
    Registry.blobs = {sha256(layer): layer, sha256(other): other}
    Registry.manifests = {sha256(manifest): manifest}
    repo = product(tmp_path, f"""
        [toolchains.hello]
        source = "oci://{registry}/tc/hello@{sha256(manifest)}"
        digest = "{sha256(layer)}"
        """)
    monkeypatch.chdir(repo)
    assert cli.main(["sync"]) == 0
    assert cli.main(["test"]) == 0
    log = (repo / ".qq" / "out" / "logs" / "greet.test.hello.log").read_text()
    assert "hello from the registry" in log

    # A layer that is not in the named manifest is refused, even though the blob exists.
    bad = store.Artifact("hello", f"oci://{registry}/tc/hello@{sha256(manifest)}", sha256(other))
    with pytest.raises(store.FetchError, match="has no layer"):
        store.ensure(bad)
