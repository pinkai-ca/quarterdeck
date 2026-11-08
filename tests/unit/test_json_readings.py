"""The CLI's readings expose source fields, never columns cut out of human output."""

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from quarterdeck import cli, doctor, post, standing, watch
from quarterdeck.drill import Check
from quarterdeck.harness.base import Join, Screen, State
from quarterdeck.seat import Seat
from quarterdeck.tmux import Pane


@pytest.fixture
def invoke(drydock, capsys, monkeypatch):
    monkeypatch.delenv("QD_SHIP", raising=False)
    monkeypatch.delenv("QD_SEAT", raising=False)
    monkeypatch.setenv("QD_TMUX_SOCKET", "qd-json-no-server")

    def run(*args, json_output=True):
        code = cli.main(["--ship", str(drydock), *(["--json"] if json_output else []), *args])
        captured = capsys.readouterr()
        assert code == 0 and captured.err == ""
        return json.loads(captured.out) if json_output else captured.out

    return run


def test_status_and_line_expose_seats_orders_and_facts(invoke, drydock):
    store = post.Store(drydock / "runtime")
    order = store.new("order", "desk", "captain", "synthetic instruction", subject="a subject")
    data = invoke("status")
    assert [s["name"] for s in data["seats"]] == ["captain", "desk", "worker-1"]
    captain = data["seats"][0]
    assert captain["activity"] == {"state": "off", "detail": "down"}
    assert captain["door"]["open"] is False and captain["today"] == {"known": 0, "unmeasured": 0}
    assert captain["model"] is None and captain["declared_model"] == "fake-1"
    assert captain["version"] is None and captain["harness"] == "fake"
    assert captain["order"] == order.id
    assert data["open_orders"][0]["id"] == order.id
    assert data["open_orders"][0]["body"] == order.body
    assert datetime.fromisoformat(data["open_orders"][0]["at"]) == order.at
    assert data["facts"]["waiting"] == 1 and data["facts"]["service"] == "down"
    line = invoke("status", "--line")
    assert line["waiting"] == 1 and line["blind"] == 0
    assert isinstance(line["queued"], int) and isinstance(line["needs_human"], list)
    assert invoke("watch")["seats"] == data["seats"]
    text = invoke("status", json_output=False)
    assert text.startswith("service down; qd up restarts it\nseat ")
    assert f"  {order.short} desk → captain · " in text and text.endswith(" · a subject\n")
    assert invoke("status", "--line", json_output=False).startswith("SERVICE DOWN · 1 waiting, oldest 0m")


def test_watch_keeps_null_state_and_full_screen_fields(invoke, drydock, monkeypatch):
    at = datetime(2031, 1, 2, tzinfo=timezone.utc)
    path = drydock / "runtime" / "synthetic-record"
    state = State(turn_open=None, turns=None, last_input="a long input\n" * 20,
                  last_input_at=at, model="synthetic-model", queued=("queued input",), consumed=("earlier",))
    screen = Screen("synthetic screen", None, working=True, unknown="unrecognized layout", queued=1,
                    shows={"model": "screen-model"})
    monkeypatch.setattr(Seat, "pane", lambda s: Pane("%1", "synthetic", s.name, 1, 80, 24, False, False, None))
    monkeypatch.setattr(Seat, "record", lambda s: path)
    monkeypatch.setattr(Seat, "join", lambda s: Join(path))
    monkeypatch.setattr(watch.Door, "screen", lambda d: screen)
    reader = cli.harnesses.get("fake")
    monkeypatch.setattr(type(reader), "state", lambda self, record: state)
    data = invoke("watch", "captain")
    assert data["state"]["turn_open"] is None and data["state"]["turns"] is None
    assert data["state"]["last_input"] == state.last_input
    assert data["state"]["last_input_at"] == at.isoformat()
    assert data["state"]["queued"] == ["queued input"]
    assert data["state"]["consumed"] == ["earlier"]
    assert data["screen"]["box"] is None and data["screen"]["working"] is True
    assert data["screen"]["queued"] == 1 and data["screen"]["unknown"] == "unrecognized layout"
    assert data["seat"]["model"] == "synthetic-model"
    assert "unknown turns" in invoke("watch", "captain", json_output=False)
    def broken(self, record):
        raise OSError("synthetic unreadable")
    monkeypatch.setattr(type(reader), "state", broken)
    data = invoke("watch", "captain")
    assert data["state"] is None and data["record_error"] == "synthetic unreadable"
    assert data["seat"]["activity"]["state"] == "unknown"


def test_empty_watch_has_absence_reasons(invoke):
    data = invoke("watch", "captain")
    assert data["record"] is None and data["state"] is None and data["screen"] is None
    assert data["record_absent"] == "not up" and data["record_error"] is None
    assert "record absent: not up" in invoke("watch", "captain", json_output=False)


def test_controls_are_definitions_and_laws_have_real_locations(invoke):
    data = invoke("controls", "captain")
    assert data["seat"] == "captain"
    assert data["relaunch"] == {"pool": "restart", "harness": "fresh", "cwd": "restart", "persona": "restart"}
    model = next(c for c in data["controls"] if c["name"] == "model")
    assert model["launch"] == "--model <value>" and model["session"] == "/model {value}"
    assert model["confirmed_by"]
    assert invoke("controls", "captain", json_output=False).startswith("control    at launch")
    data = invoke("laws")
    assert data["strays"] == []
    for law in data["laws"]:
        assert law["part"] and law["text"] and law["carriers"]
        for carrier in law["carriers"]:
            line = (standing.PROJECT_DIR / carrier["path"]).read_text().splitlines()[carrier["line"] - 1]
            assert f"law: {law['id']}" in line
    assert invoke("laws", json_output=False).splitlines() == standing.laws_table()


def test_brief_sources_and_doctor_checks_are_data(invoke, monkeypatch):
    for options in ((), ("--sources",)):
        data = invoke("brief", "desk", *options)
        assert data["text"].startswith("# Standing orders for Maeve")
        assert data["words"] == len(data["text"].split()) and data["target_words"] == 3000
        assert {s["name"] for s in data["sources"]} >= {"laws", "your seat"}
        assert next(s for s in data["sources"] if s["name"] == "your seat")["path"] is None
    assert invoke("brief", "desk", json_output=False) == data["text"]
    monkeypatch.setattr(doctor, "checks", lambda ship: [Check("synthetic", True, "read without calls")])
    assert invoke("doctor") == {"checks": [{"name": "synthetic", "passed": True, "detail": "read without calls"}]}
    assert invoke("doctor", json_output=False) == "PASS  synthetic · read without calls\n"


def test_no_reading_creates_a_ledger(invoke, drydock):  # law: record-first
    for command in (("status",), ("status", "--line"), ("watch", "captain"), ("ledger",),
                    ("ledger", "--tools"), ("ledger", "--by", "seat"), ("inbox",)):
        invoke(*command)
    assert not (drydock / "runtime" / "ledger.sqlite").exists()


def test_json_does_not_accept_an_opaque_object_as_a_string(capsys):
    from quarterdeck.output import Reading, emit

    with pytest.raises(TypeError):
        emit(Reading({"unknown": object()}, lambda data: "text"), json_output=True)
    assert capsys.readouterr().out == "", "serialization completes before output begins"


def test_model_and_version_are_observations_not_table_labels():
    from quarterdeck.door import Reading
    from quarterdeck.ledger import Count
    from quarterdeck.output import value
    from quarterdeck.watch import Activity, SeatReading

    row = SeatReading("seat", Activity("idle"), Reading(True), None, Count(), "pool", "observed",
                      "fake", "declared", "synthetic-version")
    assert row.model_text == "observed (ship file: declared)"
    assert row.harness_text == "fake synthetic-version"
    data = value(row)
    assert data["model"] == "observed" and data["declared_model"] == "declared"
    assert data["harness"] == "fake" and data["version"] == "synthetic-version"
    assert "model_text" not in data and "harness_text" not in data
