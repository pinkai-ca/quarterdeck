"""The qd command: proof lines, refusals, JSON."""

from __future__ import annotations

from pathlib import Path

from quarterdeck import cli
from quarterdeck.cli import parse_value


def run(capsys, *argv: str) -> tuple[int, str, str]:
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def test_set_writes_the_ship_file_and_prints_a_proof_line(enterprise_ship: Path, capsys):
    code, out, err = run(capsys, "--ship", str(enterprise_ship), "set", "pike", "model=claude-fable-5-1", "effort=high")
    assert code == 0 and err == ""
    lines = out.splitlines()
    assert lines[0].startswith('ship.toml: seats.pike.model = "claude-fable-5-1" · committed ')
    assert lines[1].startswith('ship.toml: seats.pike.effort = "high" · committed ')


def test_set_takes_the_ship_from_the_environment_and_refuses_with_a_reason(enterprise_ship: Path, capsys, monkeypatch):
    monkeypatch.setenv("QD_SHIP", str(enterprise_ship))
    code, out, err = run(capsys, "set", "nobody", "model=x")
    assert code == 2 and "no seat 'nobody'" in err and "declared: pike, scott, desk, worker-1" in err
    code, out, err = run(capsys, "set", "pike")
    assert code == 2 and "KEY=VALUE" in err
    monkeypatch.delenv("QD_SHIP")
    code, out, err = run(capsys, "set", "pike", "model=x")
    assert code == 2 and "--ship" in err


def test_json_output(enterprise_ship: Path, capsys):
    code, out, _ = run(capsys, "--ship", str(enterprise_ship), "--json", "set", "pike", "pool=claude-primary,claude-secondary")
    assert code == 0 and '"ship.toml: seats.pike.pool = [\\"claude-primary\\", \\"claude-secondary\\"]' in out


def test_parse_value():
    assert parse_value("true") is True and parse_value("false") is False
    assert parse_value("42") == 42 and parse_value("-1") == -1
    assert parse_value("a, b") == ["a", "b"] and parse_value("claude-opus-5-5") == "claude-opus-5-5"
