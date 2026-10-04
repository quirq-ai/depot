import pytest


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    """Unit tests run the qq under test, never a pinned one, with a private QQ_HOME."""
    monkeypatch.setenv("QQ_PINNED", "1")
    monkeypatch.setenv("QQ_HOME", str(tmp_path / "qq-home"))
