"""Synthetic word wraps before flags, preserving observed 318-column geometry."""

from pathlib import Path

import json
from datetime import timedelta
from types import SimpleNamespace

import pytest

from quarterdeck import pending, post, record_owner
from quarterdeck.door import Door
from quarterdeck.harness import get
from quarterdeck.service import Service
from quarterdeck.ship import Pool, SeatSpec, Session, Ship
from quarterdeck.tmux import Pane

from .test_padded_box import capture, door_for


def example(name):
    """Only geometry is carried over: every character below is synthetic."""
    first = "alpha " * 50 + "before the flag"
    assert len(first) == 315
    length = 126 if name == "codex" else 180
    prefix = "-x 8 -yz; " if name == "codex" else "-x 8. "
    tail = (prefix + "synthetic continuation words " * 8)[:length].rstrip()
    tail += "z" * (length - len(tail))
    header = "H" * (47 if name == "codex" else 32)
    footer = "F" * (30 if name == "codex" else 68)
    payload = "\n".join([header, first + " " + tail, footer]) + "\n"
    rows = "\n".join([header, first, tail, footer])
    return payload, rows


@pytest.mark.parametrize("name", ["codex", "pi"])
@pytest.mark.parametrize("padding", [0, "full"])
def test_flag_word_wrap_passes_through_screen_and_door(name, padding, monkeypatch: pytest.MonkeyPatch) -> None:
    payload, rows = example(name)
    door = door_for(name, capture(name, rows, padding, 318), monkeypatch, width=318)
    assert door.harness.box_columns(318) == (315 if name == "codex" else 317)
    assert door.harness.wrap_slack == 0  # pi now uses its editor's word-wrap rule, not a one-column heuristic
    screen = door.screen()
    assert door.matches(door.harness.box(payload), screen)  # law: one-door
    assert not door.reading().open, "matching our transport is not permission to overwrite an existing draft"
    for edited in ("foreign" + payload, payload.replace("alpha alpha", "alpha  alpha", 1),
                   payload.replace("before", "bef0re"), payload.rstrip("\n")[:-1]):
        assert not door.matches(door.harness.box(edited), screen)


@pytest.mark.parametrize("name", ["codex", "pi"])
@pytest.mark.parametrize("flag", ["-x", "--option", "---", "-x-y"])
def test_short_word_breaks_and_unknown_padding_are_not_accepted(name, flag, monkeypatch: pytest.MonkeyPatch) -> None:
    payload = "short " + flag + " argument\n  keep this indentation"
    door = door_for(name, capture(name, "short\n" + flag + " argument\n  keep this indentation", "full", 318),
                    monkeypatch, width=318)
    assert not door.matches(door.harness.box(payload), door.screen()), "the next token fits; this is a hard newline"
    payload, rows = example(name)
    door = door_for(name, capture(name, rows + " " * 640, 0, 318), monkeypatch, width=318)
    assert not door.matches(door.harness.box(payload), door.screen())
    door = door_for(name, capture(name, "short\n keep this indentation", "full", 318), monkeypatch, width=318)
    assert not door.matches(door.harness.box("short\n  keep this indentation"), door.screen())


def input_row(name, record, text, stamp):
    if name == "codex":
        row = {"type": "response_item", "timestamp": stamp.isoformat(), "payload": {
            "type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}}
    else:
        row = {"type": "message", "timestamp": stamp.isoformat(), "message": {"role": "user", "content": text}}
    with record.open("a") as stream:
        stream.write(json.dumps(row) + "\n")


@pytest.fixture(params=["codex", "pi"])
def transport(request, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    name = request.param
    harness = get(name)
    spec = SeatSpec("worker", "crew", "worker", name, ("synthetic",), cwd=tmp_path)
    ship = Ship(tmp_path, "synthetic", "operator", tmp_path, {"synthetic": Pool("synthetic", name)},
                {"crew": Session("crew", "Synthetic")}, {"worker": spec})
    pane = Pane("%1", "Synthetic", "worker", 123, 318, 71, False, False, None)
    live = {"box": "", "pastes": [], "keys": [], "receipt": "exact", "layout": "wrapped"}

    def paste(_pane, text):
        live["pastes"].append(text)
        rows = text.rstrip("\n").split("\n")
        if live["layout"] == "wrapped":
            body = rows[1]
            assert len(body) > 315 and body[315] == " "
            rows[1:2] = [body[:315], body[316:]]
        elif live["layout"] == "folded":
            rows = [f"[Pasted Content {len(text)} chars]" if name == "codex" else f"[paste #1 {len(text)} chars]"]
        live["box"] = "\n".join(rows)
        if live.get("damage") == "edited":
            live["box"] = live["box"].replace("before", "bef0re")
        elif live.get("damage") == "partial":
            live["box"] = live["box"][:-1]
        elif live.get("damage") == "spaces":
            live["box"] = live["box"].replace("alpha alpha", "alpha  alpha", 1)

    def keys(_pane, *keys):
        assert keys == ("Enter",), "no clear, interrupt, or extra keys"
        live["keys"].append(keys)
        text = live["pastes"][-1]
        if live["receipt"] == "edited":
            text = text.replace("flag -x", "flag-x")  # same possible visual wrap, different actual input
        stamp = post.now() - timedelta(days=1) if live["receipt"] == "stale" else post.now()
        input_row(name, record, text, stamp)
        live["box"] = ""
    tmux = SimpleNamespace(pane=lambda _: pane, paste=paste, keys=keys,
                           capture=lambda *a, **k: capture(name, live["box"], "full", 318))
    svc = Service(ship, tmux)
    seat = svc.seat("worker")
    seat.home.mkdir(parents=True)
    launch = {"pane": pane.id, "pid": pane.pid, "at": (post.now() - timedelta(minutes=1)).isoformat(),
              "pool": "synthetic", "argv": [harness.program], "harness": name, "resume": None, "version": "synthetic"}
    (seat.folder / "launch.json").write_text(json.dumps(launch))
    record = seat.home / "sessions" / "synthetic.jsonl"
    record.parent.mkdir()
    record.write_text("")
    (seat.folder / "record").write_text(str(record) + "\n")
    record_owner.register(seat.folder, record, name)
    confirm = Door.confirm
    monkeypatch.setattr(Door, "confirm", lambda self, text: confirm(self, text, seconds=0))
    box_shows = Door.box_shows
    monkeypatch.setattr(Door, "box_shows", lambda self, box: box_shows(self, box, seconds=0))
    yield svc, seat, live, record
    svc.ledger.close()


@pytest.mark.parametrize("receipt", ["exact", "stale", "edited"])
def test_wrapped_transport_submits_once_and_only_fresh_exact_input_delivers(transport, receipt) -> None:
    svc, seat, live, record = transport
    payload, _ = example(seat.harness.name)
    body = payload.splitlines()[1]
    live["receipt"] = receipt
    message = svc.store.new("question", "desk", seat.name, body)
    svc.deliver(seat, [message])
    assert live["pastes"] == [post.typed([message])] and live["keys"] == [("Enter",)]
    result = "delivered" if receipt == "exact" else "pending"
    assert svc.attempts.latest()[message.id] == result
    proof = pending.active(seat, include_terminal=True)[0]
    assert proof.data["phase"] == ("delivered" if receipt == "exact" else "submitted")
    svc.reconcile()
    svc.deliveries()
    assert len(live["pastes"]) == len(live["keys"]) == len(svc.attempts.for_message(message.id)) == 1
    if receipt != "exact":
        input_row(seat.harness.name, record, post.typed([message]), post.now())
        svc.reconcile()
        assert svc.attempts.latest()[message.id] == "delivered"
        assert len(live["pastes"]) == len(live["keys"]) == 1


@pytest.mark.parametrize("layout", ["one-line", "folded"])
def test_one_line_and_folded_transports_still_confirm(transport, layout) -> None:
    svc, seat, live, _ = transport
    live["layout"] = layout
    body = "short line" if layout == "one-line" else "long words " * 200
    if layout == "one-line":
        assert Door(seat).deliver(body).result == "delivered"
        expected = body
    else:
        message = svc.store.new("question", "desk", seat.name, body)
        svc.deliver(seat, [message])
        assert svc.attempts.latest()[message.id] == "delivered"
        expected = post.typed([message])
    assert live["pastes"] == [expected] and live["keys"] == [("Enter",)]


@pytest.mark.parametrize("damage", ["edited", "partial", "spaces"])
def test_changed_display_is_not_submitted_cleared_or_automatically_resent(transport, damage) -> None:
    svc, seat, live, _ = transport
    live["damage"] = damage
    message = svc.store.new("question", "desk", seat.name, example(seat.harness.name)[0].splitlines()[1])
    svc.deliver(seat, [message])
    assert svc.attempts.latest()[message.id] == "blind"
    assert live["pastes"] == [post.typed([message])] and live["keys"] == []
    assert pending.active(seat)[0].data["phase"] == "pasting"
    svc.reconcile()
    svc.deliveries()
    assert live["pastes"] == [post.typed([message])] and live["keys"] == []
    assert live["box"], "uncertain text is left untouched"


def test_foreign_box_obstructs_without_any_keys(transport) -> None:
    svc, seat, live, _ = transport
    live["box"] = "foreign text"
    message = svc.store.new("question", "desk", seat.name, "another input")
    svc.deliver(seat, [message])
    assert live["box"] == "foreign text" and not live["pastes"] and not live["keys"]
    assert svc.attempts.for_message(message.id) == []
