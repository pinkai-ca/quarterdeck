"""The qd command: proof lines, refusals, JSON."""

from __future__ import annotations

from pathlib import Path

from quarterdeck import cli
from quarterdeck.cli import parse_value
from quarterdeck.ship import load


def run(capsys, *argv: str) -> tuple[int, str, str]:
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def test_set_writes_the_ship_file_and_says_what_applies_it(example_ship: Path, capsys, monkeypatch):
    monkeypatch.setenv("QD_TMUX_SOCKET", "qd-test-nobody-here")
    code, out, err = run(capsys, "--ship", str(example_ship), "set", "captain", "model=fake-2", "pool=fake")
    assert code == 0 and err == ""
    lines = out.splitlines()
    assert lines[0].startswith('ship.toml: seats.captain.model = "fake-2" · written')
    assert lines[1] == "  model written; captain is down, so qd up captain applies it"
    assert lines[3] == "  pool written; qd restart captain applies it"
    assert load(example_ship).seat("captain").model == "fake-2"


def test_set_takes_the_ship_from_the_environment_and_refuses_with_a_reason(example_ship: Path, capsys, monkeypatch):
    monkeypatch.setenv("QD_SHIP", str(example_ship))
    code, out, err = run(capsys, "set", "nobody", "model=x")
    assert code == 2 and "no seat 'nobody'" in err and "declared: captain, desk, worker-1" in err
    code, out, err = run(capsys, "set", "captain")
    assert code == 2 and "KEY=VALUE" in err
    monkeypatch.delenv("QD_SHIP")
    code, out, err = run(capsys, "set", "captain", "model=x")
    assert code == 2 and "--ship" in err


def test_brief_and_json_output(example_ship: Path, capsys):
    code, out, _ = run(capsys, "--ship", str(example_ship), "--json", "brief", "desk", "--sources")
    assert code == 0 and '"proof"' in out and "laws" in out
    code, out, _ = run(capsys, "--ship", str(example_ship), "brief", "desk")
    assert out.startswith("# Standing orders for Maeve")


def test_parse_value():
    assert parse_value("true") is True and parse_value("false") is False
    assert parse_value("42") == 42 and parse_value("-1") == -1
    assert parse_value("a, b") == ["a", "b"] and parse_value("claude-opus-5-5") == "claude-opus-5-5"
