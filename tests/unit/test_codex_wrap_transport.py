"""The saved slash-wrap shape verifies one paste; only an exact fresh record input confirms it."""

from datetime import timedelta
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from quarterdeck import pending, post, record_owner
from quarterdeck.door import Door
from quarterdeck.service import Service
from quarterdeck.ship import Pool, SeatSpec, Session, Ship
from quarterdeck.tmux import Pane
from tests.contract.test_codex_breaks import FIXTURE
from tests.contract.test_padded_box import capture


def input_row(path, text, at):
    with path.open("a") as stream:
        stream.write(json.dumps({"type": "response_item", "timestamp": at.isoformat(), "payload": {
            "type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}})+"\n")


@pytest.fixture
def transport(tmp_path: Path, monkeypatch):
    spec = SeatSpec("worker", "crew", "worker", "codex", ("synthetic",), cwd=tmp_path)
    ship = Ship(tmp_path, "synthetic", "operator", tmp_path, {"synthetic": Pool("synthetic", "codex")},
                {"crew": Session("crew", "Synthetic")}, {"worker": spec})
    pane = Pane("%1", "Synthetic", "worker", 123, 478, 113, False, False, None)
    live = dict(box="", pastes=[], keys=[], receipt="exact", damage=None)

    def paste(_pane, text):
        proof = pending.active(seat)[0]
        assert proof.data["phase"] == "pasting" and proof.data["payload"] == text
        live["pastes"].append(text)
        head, body, tail = text.rstrip("\n").split("\n")
        assert len(body) == 795 and body[470] == "/"
        live["box"] = "\n".join([head, body[:471], body[471:], tail])
        if live["damage"] == "partial":
            live["box"] = live["box"][:-1]
        elif live["damage"] == "edited":
            live["box"] = live["box"].replace("x/", "y/", 1)
        elif live["damage"] == "spaces":
            live["box"] = live["box"].replace("x x", "x  x", 1)

    def keys(_pane, *keys):
        proof = pending.active(seat)[0]
        assert proof.data["phase"] == "submitting" and proof.data["shown"]
        assert keys == ("Enter",), "no clear, interrupt or duplicate submission"
        live["keys"].append(keys)
        text = live["pastes"][-1]
        if live["receipt"] == "edited":
            text = text.replace("/", "/ ", 1)
        at = post.now() - timedelta(days=1) if live["receipt"] == "stale" else post.now()
        input_row(record, text, at)
        live["box"] = ""

    tmux = SimpleNamespace(pane=lambda _: pane, paste=paste, keys=keys,
                           capture=lambda *a, **k: capture("codex", live["box"], "full", pane.width))
    svc = Service(ship, tmux)
    seat = svc.seat("worker")
    seat.home.mkdir(parents=True)
    launch = dict(pane=pane.id, pid=pane.pid, at=(post.now()-timedelta(minutes=1)).isoformat(), pool="synthetic",
                  argv=["codex"], harness="codex", resume=None, version="synthetic")
    (seat.folder/"launch.json").write_text(json.dumps(launch))
    record = seat.home/"synthetic.jsonl"
    record.write_text("")
    (seat.folder/"record").write_text(str(record))
    record_owner.register(seat.folder, record, "codex")
    confirm, shows = Door.confirm, Door.box_shows
    monkeypatch.setattr(Door, "confirm", lambda self, text: confirm(self, text, seconds=0))
    monkeypatch.setattr(Door, "box_shows", lambda self, box: shows(self, box, seconds=0))
    body = json.loads(FIXTURE.read_text())["payload"].splitlines()[1]
    message = svc.store.new("answer", "desk", seat.name, body)
    yield svc, seat, live, record, message
    svc.ledger.close()


@pytest.mark.parametrize("receipt", ["exact", "stale", "edited"])
def test_wrap_verifies_once_and_only_a_fresh_exact_receipt_delivers(transport, receipt):
    svc, seat, live, record, message = transport
    live["receipt"] = receipt
    svc.deliver(seat, [message])
    assert live["pastes"] == [post.typed([message])] and live["keys"] == [("Enter",)]
    result = "delivered" if receipt == "exact" else "pending"
    assert svc.attempts.latest()[message.id] == result
    restarted = Service(svc.ship, svc.tmux)
    try:
        for _ in range(2):
            restarted.reconcile()
            restarted.deliveries()
        assert svc.attempts.latest()[message.id] == result
        if receipt != "exact":
            input_row(record, post.typed([message]), post.now())
            restarted.reconcile()
        assert svc.attempts.latest()[message.id] == "delivered"
        assert len(svc.attempts.for_message(message.id)) == len(live["pastes"]) == len(live["keys"]) == 1
    finally:
        restarted.ledger.close()


@pytest.mark.parametrize("damage", ["partial", "edited", "spaces", "foreign"])
def test_changed_or_foreign_text_is_never_submitted_cleared_or_resent(transport, damage):
    svc, seat, live, record, message = transport
    live["damage"] = damage
    if damage == "foreign":
        live["box"] = "a person's draft"
    svc.deliver(seat, [message])
    shown = live["box"]
    svc.reconcile()
    svc.deliveries()
    assert shown and live["box"] == shown and not live["keys"]
    assert len(live["pastes"]) == (0 if damage == "foreign" else 1)
    if damage != "foreign":
        assert svc.attempts.latest()[message.id] == "blind"
        assert pending.active(seat)[0].data["phase"] == "pasting"
    else:
        assert not svc.attempts.for_message(message.id)
