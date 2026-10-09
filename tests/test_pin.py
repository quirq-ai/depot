import hashlib
import subprocess
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
    assert pin.requirement(pin.Pin("1.0"), tmp_path) == "git+https://github.com/quirq-ai/qq@v1.0"
    monkeypatch.setenv("QQ_DEPOT_URL", "file:///mirror/depot")
    assert pin.requirement(pin.Pin("1.0"), tmp_path) == "git+file:///mirror/depot@v1.0"


def _repo(tmp_path):
    def git(*args):
        return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
                              check=True, capture_output=True, text=True).stdout.strip()
    repo = tmp_path / "depot"
    repo.mkdir()
    git("init", "-q", "-b", "main")
    git("commit", "-q", "--allow-empty", "-m", "merged")
    merged = git("rev-parse", "HEAD")
    git("commit", "-q", "--allow-empty", "-m", "a fork's commit")
    fork = git("rev-parse", "HEAD")
    git("update-ref", "refs/pull/1/head", fork)   # served by id, on no branch or tag
    git("reset", "-q", "--hard", merged)
    git("commit", "-q", "--allow-empty", "-m", "pushed by a writer")
    pushed = git("rev-parse", "HEAD")
    git("branch", "feature", pushed)              # a branch anyone with write access can move
    git("reset", "-q", "--hard", merged)
    git("commit", "-q", "--allow-empty", "-m", "released")
    released = git("rev-parse", "HEAD")
    git("tag", "v1.0", released)
    git("reset", "-q", "--hard", merged)
    return repo, merged, fork, pushed, released


def test_a_commit_pin_must_be_on_main_or_a_release_tag(tmp_path):
    repo, merged, fork, pushed, released = _repo(tmp_path)
    for ok in (merged, released):
        assert pin.requirement(pin.Pin("1.0", repo.as_uri(), f"git:{ok}"), tmp_path) == f"git+{repo.as_uri()}@{ok}"
    for bad in (fork, pushed):
        with pytest.raises(pin.PinError, match="neither main nor a v\\* tag"):
            pin.requirement(pin.Pin("1.0", repo.as_uri(), f"git:{bad}"), tmp_path)
    assert not pin.is_self(pin.Pin(__version__, repo.as_uri(), f"git:{merged}"))


def test_an_empty_depot_url_means_the_default(monkeypatch, tmp_path):
    monkeypatch.setenv("QQ_DEPOT_URL", "")
    assert pin.requirement(pin.Pin("1.0"), tmp_path) == "git+https://github.com/quirq-ai/qq@v1.0"


@pytest.mark.parametrize("source, ok", [
    ("https://github.com/quirq-ai/depot", False),
    ("https://github.com/quirq-ai/depot.git", False),
    ("https://github.com/quirq-ai/depot/releases/download/v1/qq.tar.gz", False),
    ("https://github.com/quirq-ai/depot-evil", False),
    ("https://github.com/someone/depot", False),
    ("https://example.invalid/depot", False),
    ("https://github.com/quirq-ai/depot/../../evil/depot", False),
    ("https://github.com/quirq-ai/qq/%2e%2e/evil", False),
    ("https://github.com/quirq-ai/qq@evil.invalid/x", False),
    ("https://user@github.com/quirq-ai/qq", False),
    ("https://github.com/quirq-ai/qq?x=/evil", False),
    ("github.com/quirq-ai/qq", False),
    ("https://github.com/quirq-ai/qq", True),
    ("https://github.com/quirq-ai/qq.git", True),
    ("https://github.com/quirq-ai/qq/releases/download/v1/qq.tar.gz", True),
    ("https://github.com/quirq-ai/qq-evil", False),
    ("https://github.com/quirq-ai/qqx", False),
    ("https://github.com/someone/qq", False),
    ("https://github.com/quirq-ai/qq/../../evil/qq", False),
])
def test_only_the_depot_is_trusted_by_default(monkeypatch, source, ok):
    monkeypatch.delenv("QQ_DEPOT_URL", raising=False)
    monkeypatch.delenv("QQ_TRUSTED_SOURCES", raising=False)
    assert pin.trusted(source) is ok


def test_an_untrusted_source_is_refused_before_anything_installs(monkeypatch, tmp_path):
    monkeypatch.delenv("QQ_TRUSTED_SOURCES", raising=False)
    monkeypatch.setattr(pin, "_run", lambda *a, **k: pytest.fail("must not install"))
    evil = pin.Pin("1.0", "https://example.invalid/depot", "git:" + "a" * 40, Path("infra/repo.toml"))
    with pytest.raises(pin.PinError, match="QQ_TRUSTED_SOURCES=https://example.invalid/depot"):
        pin.ensure(evil)
    monkeypatch.setenv("QQ_TRUSTED_SOURCES", "https://other.invalid https://example.invalid/depot")
    pin.check(evil)
    with pytest.raises(pin.PinError, match="needs a digest"):
        pin.check(pin.Pin("1.0", "https://github.com/quirq-ai/qq", None))


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


def test_installs_take_pypi_only_by_hash_and_the_rest_with_no_index(tmp_path):
    lock, rest, check = pin.pip_install(tmp_path / "python", "git+https://github.com/quirq-ai/depot@" + "a" * 40)
    assert "--require-hashes" in lock and "--no-deps" in lock and str(pin.PYPI_LOCK) in lock
    assert "--no-index" in rest and "--no-build-isolation" in rest
    assert check[-1] == "check"
    names = [line.split("==")[0] for line in pin.PYPI_LOCK.read_text().splitlines() if "==" in line]
    assert "setuptools" in names and all("--hash=sha256:" in pin.PYPI_LOCK.read_text() for _ in names)


def test_the_bootstrap_uses_the_same_lock():
    src = (Path(pin.__file__).parent / "bootstrap.py").read_text()
    assert '"locks" / "pypi.txt"' in src and "--require-hashes" in src and "--no-index" in src
