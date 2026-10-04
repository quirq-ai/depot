import argparse

import pytest

from qqdepot import __version__, cli


def test_version(capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["--version"])
    assert e.value.code == 0
    assert capsys.readouterr().out.strip() == f"qq {__version__}"


def test_no_command_prints_help(capsys):
    assert cli.main([]) == 0
    assert "usage: qq" in capsys.readouterr().out


def test_plugin_subcommand(monkeypatch, capsys):
    def register(sub):
        p = sub.add_parser("hello", help="say hello")
        p.set_defaults(run=lambda args: print("hello from a plugin") or 7)

    class EntryPoint:
        name = "hello"

        def load(self):
            return register

    monkeypatch.setattr(cli, "entry_points", lambda group: [EntryPoint()] if group == cli.COMMANDS_GROUP else [])
    assert cli.main(["hello"]) == 7
    assert "hello from a plugin" in capsys.readouterr().out


def test_pin_error_exits_2(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("QQ_PINNED")
    (tmp_path / "infra").mkdir()
    (tmp_path / "infra" / "repo.toml").write_text('schema = "quirq-repo/1"\n')  # no targets
    monkeypatch.chdir(tmp_path)
    assert cli.main(["--version"]) == 2
    assert "QQ_PINNED=1" in capsys.readouterr().err


def test_broken_plugin_is_skipped(monkeypatch, capsys):
    class Broken:
        name = "broken"

        def load(self):
            raise ImportError("no module named nowhere")

    monkeypatch.setattr(cli, "entry_points", lambda group: [Broken()])
    assert cli.main([]) == 0
    assert "skipping subcommand 'broken'" in capsys.readouterr().err
