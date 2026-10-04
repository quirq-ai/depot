import hashlib
from pathlib import Path

import pytest

from qqdepot import __version__, pin

TARGETS = '\n[[targets]]\nname = "t"\nkind = "k"\n'


def write_manifest(root: Path, qq_table: str = "") -> Path:
    (root / "infra").mkdir(parents=True, exist_ok=True)
    path = root / "infra" / "repo.toml"
    path.write_text('schema = "quirq-repo/1"\n' + qq_table + TARGETS)
    return path


def test_find_manifest_walks_up(tmp_path):
    manifest = write_manifest(tmp_path)
    deep = tmp_path / "a" / "b"
    deep.mkdir(parents=True)
    assert pin.find_manifest(deep) == manifest


def test_no_manifest_no_pin(tmp_path):
    assert pin.read_pin(tmp_path) is None


def test_manifest_without_qq_table(tmp_path):
    write_manifest(tmp_path)
    assert pin.read_pin(tmp_path) is None


def test_reads_pin(tmp_path):
    manifest = write_manifest(tmp_path, '[qq]\nversion = "1.2.3"\n')
    assert pin.read_pin(tmp_path) == pin.Pin("1.2.3", manifest=manifest)


def test_invalid_manifest_is_an_error(tmp_path):
    write_manifest(tmp_path, '[qq]\nversion = "1 2"\n')
    with pytest.raises(pin.PinError, match="QQ_PINNED=1"):
        pin.read_pin(tmp_path)


def test_is_self():
    assert pin.is_self(pin.Pin(__version__))
    assert not pin.is_self(pin.Pin("0.0.0-other"))
    assert not pin.is_self(pin.Pin(__version__, "https://example.invalid/x", "git:" + "a" * 40))


def test_key():
    assert pin.Pin("1.0").key == "1.0"
    assert pin.Pin("1.0", "s", "git:" + "ab" * 20).key == "1.0-git-abababababababab"


def test_requirement_by_version(monkeypatch, tmp_path):
    monkeypatch.delenv("QQ_DEPOT_URL", raising=False)
    assert pin.requirement(pin.Pin("1.0"), tmp_path) == "git+https://github.com/quirq-ai/depot@v1.0"
    monkeypatch.setenv("QQ_DEPOT_URL", "file:///mirror/depot")
    assert pin.requirement(pin.Pin("1.0"), tmp_path) == "git+file:///mirror/depot@v1.0"


def test_requirement_by_commit(tmp_path):
    sha = "c" * 40
    got = pin.requirement(pin.Pin("1.0", "https://example.invalid/depot", f"git:{sha}"), tmp_path)
    assert got == f"git+https://example.invalid/depot@{sha}"


def test_archive_digest_checked(tmp_path):
    archive = tmp_path / "qqdepot-1.0.tar.gz"
    archive.write_bytes(b"bytes")
    good = "sha256:" + hashlib.sha256(b"bytes").hexdigest()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    fetched = pin.requirement(pin.Pin("1.0", archive.as_uri(), good), scratch)
    assert Path(fetched).read_bytes() == b"bytes"
    with pytest.raises(pin.PinError, match="has digest"):
        pin.requirement(pin.Pin("1.0", archive.as_uri(), "sha256:" + "0" * 64), scratch)


def test_dispatch_skipped_when_pinned(monkeypatch, tmp_path):
    write_manifest(tmp_path, '[qq]\nversion = "0.0.0-other"\n')
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(pin, "ensure", lambda p: pytest.fail("must not install"))
    pin.dispatch([])  # QQ_PINNED=1 from conftest
    assert "QQ_PINNED" not in pin.os.environ  # a qq this one runs pins again


def test_dispatch_execs_pinned(monkeypatch, tmp_path):
    monkeypatch.delenv("QQ_PINNED")
    write_manifest(tmp_path, '[qq]\nversion = "0.0.0-other"\n')
    monkeypatch.chdir(tmp_path)
    fake = tmp_path / "bin" / "qq"
    monkeypatch.setattr(pin, "ensure", lambda p: fake)
    calls = []
    monkeypatch.setattr(pin.os, "execve", lambda *a: calls.append(a))
    pin.dispatch(["status"])
    (path, args, env), = calls
    assert path == fake and args == [str(fake), "status"] and env["QQ_PINNED"] == "1"


def test_ensure_rejects_wrong_version(monkeypatch, tmp_path):
    monkeypatch.setattr(pin, "_run", lambda cmd, **kw: cmd[1:3] == ["-m", "venv"] and Path(cmd[-1]).mkdir(parents=True))
    monkeypatch.setattr(pin, "installed_version", lambda qq: "9.9.9")
    with pytest.raises(pin.PinError, match="installs qq 9.9.9"):
        pin.ensure(pin.Pin("1.0", manifest=Path("infra/repo.toml")))
    assert not (pin.qq_home() / "versions" / "1.0").exists()


def test_source_without_scheme_is_a_pin_error(tmp_path):
    with pytest.raises(pin.PinError, match="cannot download"):
        pin.requirement(pin.Pin("1.0", "relative/x.tar.gz", "sha256:" + "0" * 64), tmp_path)
