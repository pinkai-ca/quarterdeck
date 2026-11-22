"""qd ls reads only the selected ship's running sessions on a private tmux server."""

import json
from datetime import datetime, timezone
from pathlib import Path
from subprocess import CompletedProcess

import pytest

from quarterdeck import cli, ship, standing, tmux as transport

from .waiting import attached_client


@pytest.fixture(autouse=True)
def unseated(monkeypatch):
    for name in ("QD_SHIP", "QD_SEAT", "QD_TMUX_SOCKET", cli.HANDED):
        monkeypatch.delenv(name, raising=False)


def read(capsys, *args, status=0):
    code = cli.main(list(args))
    out, err = capsys.readouterr()
    assert code == status, err or out
    if status == 0:
        assert not err
    return out, err


def test_lists_running_declared_sessions_with_creation_and_attachment(drydock, tmux, monkeypatch, capsys):
    monkeypatch.setenv("QD_TMUX_SOCKET", tmux.socket)
    declaration = drydock / "ship.toml"
    text = ship.edit(declaration.read_text(), "sessions.crew", "title", "Bridge")
    declaration.write_text(ship.edit(text, "seats.worker-1", "up", False))
    bridge = tmux.new_session("Bridge")
    office = tmux.new_session("Office")
    tmux.new_session("Bridge Annex")

    def created(session):
        timestamp = int(tmux.run("display-message", "-p", "-t", session.id, "#{session_created}"))
        return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()

    bridge_created, office_created = created(bridge), created(office)

    with attached_client(tmux.attach_command("Bridge"), lambda: tmux.clients_attached(["Bridge"])):
        text, _ = read(capsys, "--ship", str(drydock), "ls")
        assert text.splitlines() == [
            f"bridge (Bridge) · 2 declared seats · created {bridge_created} · attached",
            f"office (Office) · 1 declared seat · created {office_created} · detached",
        ]
        result, _ = read(capsys, "--ship", str(drydock), "ls", "--json")
        assert json.loads(result) == {
            "ship": "drydock",
            "server_running": True,
            "seat_count_basis": "declared",
            "sessions": [
                {"name": "bridge", "title": "Bridge", "seat_count": 2, "created_at": bridge_created, "attached": True},
                {"name": "office", "title": "Office", "seat_count": 1, "created_at": office_created, "attached": False},
            ],
        }
    assert not (drydock / "runtime").exists()


def test_lists_the_default_ship_and_an_explicit_selection(drydock, tmux, monkeypatch, capsys):
    monkeypatch.setenv("QD_TMUX_SOCKET", tmux.socket)
    monkeypatch.setattr(standing, "PROJECT_DIR", drydock.parents[1])
    default = drydock.with_name("endeavour")
    default.mkdir()
    text = (drydock / "ship.toml").read_text()
    text = ship.edit(text, "ship", "name", "Endeavour")
    (default / "ship.toml").write_text(ship.edit(text, "sessions.bridge", "title", "Endeavour Bridge"))
    (drydock.parent / ship.MARK).write_text("endeavour\n")
    tmux.new_session("Bridge")
    tmux.new_session("Endeavour Bridge")

    out, _ = read(capsys, "ls")
    assert out.startswith("bridge (Endeavour Bridge) ")
    assert out.rstrip().endswith("(default ship: endeavour)")
    out, _ = read(capsys, "--json", "ls")
    assert json.loads(out)["default_ship"] == "endeavour"
    out, _ = read(capsys, "--ship", str(drydock), "--json", "ls")
    result = json.loads(out)
    assert "default_ship" not in result
    assert result["ship"] == "drydock"
    assert [row["title"] for row in result["sessions"]] == ["Bridge"]
    monkeypatch.setenv("QD_SHIP", str(drydock))
    out, _ = read(capsys, "ls", "--json")
    assert json.loads(out) == result


def test_legacy_listing_requires_an_exact_declared_title_and_its_ship(drydock, tmux, monkeypatch, capsys):
    monkeypatch.setattr(transport, "_default_sockets", lambda: [Path(tmux.socket)])
    tmux.new_session("Bridge", env={"QD_SHIP": str(drydock)})
    tmux.new_session("Bridge Annex", env={"QD_SHIP": str(drydock)})
    tmux.new_session("Crew", env={"QD_SHIP": str(drydock.with_name("other"))})
    tmux.new_session("Office")

    out, _ = read(capsys, "--ship", str(drydock), "ls", "--json")
    assert [row["title"] for row in json.loads(out)["sessions"]] == ["Bridge"]


def test_an_absent_server_has_an_empty_reading_and_is_never_started(drydock, tmux, monkeypatch, capsys):
    monkeypatch.setenv("QD_TMUX_SOCKET", tmux.socket)
    out, _ = read(capsys, "--ship", str(drydock), "ls")
    assert out == "drydock: no tmux server is running\n"
    out, _ = read(capsys, "--ship", str(drydock), "ls", "--json")
    assert json.loads(out) == {
        "ship": "drydock", "server_running": False, "seat_count_basis": "declared", "sessions": [],
    }
    assert not Path(tmux.socket).exists()
    assert not (drydock / "runtime").exists()


def test_a_running_server_without_matching_sessions_is_distinct(drydock, tmux, monkeypatch, capsys):
    monkeypatch.setenv("QD_TMUX_SOCKET", tmux.socket)
    tmux.new_session("Unrelated")
    out, _ = read(capsys, "--ship", str(drydock), "ls")
    assert out == "drydock: no declared sessions are running\n"
    tmux.kill_session("Unrelated")
    out, _ = read(capsys, "--ship", str(drydock), "ls", "--json")
    result = json.loads(out)
    assert result["server_running"] is True and result["sessions"] == []


def test_a_legacy_probe_error_is_distinct_from_no_server(drydock, tmux, monkeypatch, capsys):
    tmux.new_session("Bridge", env={"QD_SHIP": str(drydock)})
    monkeypatch.setattr(transport, "_default_sockets", lambda: [Path(tmux.socket)])
    run = transport.subprocess.run

    def fail_listing(argv, *args, **kwargs):
        if argv[:6] == ["tmux", "-u", "-S", tmux.socket, "-N", "list-sessions"]:
            return CompletedProcess(argv, 1, "", f"error connecting to {tmux.socket} (Permission denied)")
        return run(argv, *args, **kwargs)

    monkeypatch.setattr(transport.subprocess, "run", fail_listing)
    out, err = read(capsys, "--ship", str(drydock), "ls", "--json", status=2)
    assert out == ""
    assert err.startswith("qd: tmux list-sessions: error connecting")
    assert "Permission denied" in err


@pytest.mark.parametrize("args, expected", [
    (("--help",), "list running tmux sessions of the selected ship"),
    (("ls", "--help"), "--json"),
])
def test_help_describes_the_command_without_a_ship(capsys, args, expected):
    with pytest.raises(SystemExit) as exit_:
        cli.main(list(args))
    assert exit_.value.code == 0
    assert expected in capsys.readouterr().out
