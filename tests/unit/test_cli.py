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
    code, out, err = run(capsys, "--ship", str(example_ship), "set", "captain", "model=fake-2", "pool=fake", "remote_control=true")
    assert code == 0 and err == ""
    lines = out.splitlines()
    assert lines[0].startswith('ship.toml: seats.captain.model = "fake-2" · written')
    assert lines[1] == "  model written; captain is down, so qd up captain applies it"
    assert lines[3] == "  pool written; qd restart captain applies it"
    assert lines[4].startswith("ship.toml: seats.captain.remote_control = true")
    assert lines[5] == "  remote_control written; qd restart captain applies it"
    captain = load(example_ship).seat("captain")
    assert captain.model == "fake-2" and captain.remote_control


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


def test_inside_a_seat_the_seat_is_known_and_outside_the_operator_has_an_inbox(example_ship: Path, capsys, monkeypatch):
    monkeypatch.setenv("QD_SHIP", str(example_ship))
    monkeypatch.setenv("QD_SEAT", "worker-1")
    code, out, _ = run(capsys, "brief")
    assert code == 0 and out.startswith("# Standing orders for worker-1: worker-1, worker")
    monkeypatch.delenv("QD_SEAT")
    code, out, err = run(capsys, "brief")
    assert code == 2 and "QD_SEAT is not set" in err
    code, out, _ = run(capsys, "inbox")
    assert code == 0 and out.startswith("# Inbox: operator"), "outside a seat, the operator's inbox"
    code, out, _ = run(capsys, "inbox", "Operator")
    assert code == 0 and out.startswith("# Inbox: operator"), "case does not matter for the operator"


def test_set_routes_a_programs_own_key_to_the_settings_table(enterprise_ship: Path, capsys, monkeypatch):
    monkeypatch.setenv("QD_TMUX_SOCKET", "qd-test-nobody-here")
    code, out, err = run(capsys, "--ship", str(enterprise_ship), "set", "worker-1", "model_auto_compact_token_limit=100000",
                         "settings.model_reasoning_summary=concise", "effort=low")
    assert code == 0, err
    lines = out.splitlines()
    assert lines[0].startswith("ship.toml: seats.worker-1.settings.model_auto_compact_token_limit = 100000")
    assert lines[1] == "  model_auto_compact_token_limit written under [seats.worker-1.settings]; qd restart worker-1 passes it to the program"
    assert lines[2].startswith('ship.toml: seats.worker-1.settings.model_reasoning_summary = "concise"')
    assert lines[4].startswith('ship.toml: seats.worker-1.effort = "low"')
    seat = load(enterprise_ship).seat("worker-1")
    assert seat.settings == {"model_auto_compact_token_limit": 100000, "model_reasoning_summary": "concise"} and seat.effort == "low"


def test_an_ack_needs_no_body_and_wakes_nobody(example_ship: Path, capsys, monkeypatch):
    from quarterdeck import post

    monkeypatch.setenv("QD_TMUX_SOCKET", "qd-test-nobody-here")
    monkeypatch.setenv("QD_SHIP", str(example_ship))
    code, out, _ = run(capsys, "post", "order", "--to", "worker-1", "build it")
    short = out.split(" ")[0]
    code, out, err = run(capsys, "post", "ack", "--from", "worker-1", "--re", short)
    assert code == 0, err
    assert out.startswith("ack-") and out.strip().endswith(f"claims {short} for the ledger; an ack wakes nobody")
    store, attempts = post.Store(example_ship / "runtime"), post.Attempts(example_ship / "runtime")
    ack = store.all()[-1]
    assert ack.kind == "ack" and ack.body == "claimed" and ack.to == "operator"
    assert attempts.undelivered(store, "operator") == [], "an ack is never delivered"  # law: open-until-closed
    assert (example_ship / "runtime" / ".git").is_dir(), "qd post made the fence before writing"  # law: nothing-leaks


def test_parse_value():
    assert parse_value("true") is True and parse_value("false") is False
    assert parse_value("42") == 42 and parse_value("-1") == -1
    assert parse_value("a, b") == ["a", "b"] and parse_value("claude-opus-5-5") == "claude-opus-5-5"
