"""qd set-aside: one named, provably stuck delivery journal moved aside unchanged, with a proof line (law: one-door).

A synthetic Codex seat on a throwaway runtime under tmp_path and a stand-in tmux. A real delivery through the door
leaves the journal: pasted and submitted, its confirmation never comes, so it rests at 'submitted' with its attempt
closed 'pending'. No program runs.
"""

import hashlib
import json
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from quarterdeck import acts, lifecycle, pending, post
from quarterdeck.door import Door
from quarterdeck.service import Service
from quarterdeck.ship import Pool, SeatSpec, Session, Ship
from quarterdeck.tmux import Pane


@pytest.fixture
def setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    pool = Pool("test", "codex")
    seats = {"worker": SeatSpec("worker", "bridge", "worker", "codex", ("test",), cwd=tmp_path),
             "watcher": SeatSpec("watcher", "bridge", "systems", "codex", ("test",), cwd=tmp_path),
             "builder": SeatSpec("builder", "bridge", "engineer", "codex", ("test",), cwd=tmp_path),
             "person": SeatSpec("person", "office", "desk", "codex", ("test",), cwd=tmp_path)}
    ship = Ship(tmp_path, "example", "operator", tmp_path, {"test": pool},
                {"bridge": Session("bridge", "Test"), "office": Session("office", "Office", human=True)}, seats)
    live = {"pane": Pane("%1", "Test", "worker", 123, 80, 24, False, False, None), "box": ""}

    def capture(*a, **k):
        return "• Working (1s • esc to interrupt)\n› " + live["box"].replace("\n", "\n  ") + "\n\n  test low · /work\n"

    def paste(*args):
        live["box"] = args[1].rstrip("\n")

    tmux = SimpleNamespace(pane=lambda p: live["pane"], capture=capture, paste=paste, keys=lambda *a: None)
    svc = Service(ship, tmux)
    seat = svc.seat("worker")
    seat.folder.mkdir(parents=True)
    seat.trusted.mkdir(parents=True, exist_ok=True)
    launch = {"pane": "%1", "pid": 123, "at": (post.now() - timedelta(minutes=1)).isoformat(),
              "pool": "test", "argv": ["codex"], "harness": "codex", "resume": None, "version": "synthetic"}
    (seat.trusted / "launch.json").write_text(json.dumps(launch))
    original = Door.confirm
    monkeypatch.setattr(Door, "confirm", lambda self, text: original(self, text, seconds=0))
    yield svc, seat, live
    svc.ledger.close()


def stuck(svc, seat):
    """A delivery the door typed and could not confirm: its journal rests, its attempt closed."""
    message = svc.store.new("order", "builder", seat.name, "a synthetic order")
    svc.deliver(seat, [message])
    proof = pending.active(seat)[-1]
    assert proof.data["phase"] == "submitted"
    assert all(a.result is not None for a in svc.attempts.all() if a.seat == seat.name)
    return message, proof


def aside(svc, seat, name, caller="watcher"):
    return svc.answer(dict(kind="set-aside", seat=seat.name, journal=name, caller=caller))


def acts_of(seat):
    path = acts.path(seat)
    return [json.loads(row) for row in path.read_text().splitlines()] if path.exists() else []


def archive(svc):
    folder = svc.ship.runtime / "evidence" / acts.ASIDE
    return sorted(p for p in folder.rglob("*") if p.is_file()) if folder.exists() else []


def test_a_stuck_journal_is_moved_unchanged_with_its_proof_line_and_act_record(setup):
    svc, seat, live = setup
    message, proof = stuck(svc, seat)
    before = proof.path.read_bytes()
    line = aside(svc, seat, proof.path.name)
    [moved] = archive(svc)
    assert moved.read_bytes() == before, "moved byte for byte"
    assert not proof.path.exists() and moved.name.endswith(proof.path.name)
    digest = hashlib.sha256(before).hexdigest()
    assert "set aside: set aside post/pending/worker/" in line
    assert f"({len(before)} bytes, sha256 {digest}) to evidence/{acts.ASIDE}/worker/" in line
    assert "rests at 'submitted'" in line and f"message {message.id}" in line
    assert "never typed again by the door" in line
    first, second = acts_of(seat)
    assert first["verb"] == "set-aside" and first["caller"] == "watcher" and first["journal"] == proof.path.name
    assert first["sha256"] == digest and first["size"] == len(before) and first["message"] == message.id
    assert second["act"] == first["act"] and second["outcome"] == "set aside"
    assert acts.describe(acts.last(seat)).startswith(f"last act: set aside {proof.path.name!r} by watcher")
    # The door is free of it, and the message is held by its attempt: the service never types it again.
    assert pending.journals(seat) == ([], [])
    assert message.id not in [m.id for m in svc.attempts.undelivered(svc.store, seat.name)]
    log = (svc.ship.runtime / "log" / "service.jsonl").read_text()
    assert '"event": "set-aside"' in log and digest in log


def test_an_unreadable_journal_is_set_aside_when_no_delivery_is_open(setup):
    svc, seat, live = setup
    folder = svc.ship.runtime / "post" / "pending" / seat.name
    folder.mkdir(parents=True)
    broken = folder / "unreadable.json"
    broken.write_bytes(b"{not a journal")
    line = aside(svc, seat, broken.name)
    [moved] = archive(svc)
    assert moved.read_bytes() == b"{not a journal" and not broken.exists()
    assert "could not be read" in line and "its message could not be identified" in line


def test_a_journal_with_a_live_delivery_is_refused(setup):
    svc, seat, live = setup
    message, proof = stuck(svc, seat)
    other = svc.store.new("order", "builder", seat.name, "another synthetic order")
    svc.attempts.open(other, seat.name, "%1", 123, None)  # a delivery the door has open
    before = proof.path.read_bytes()
    line = aside(svc, seat, proof.path.name)
    assert "is still open" in line and "a live delivery" in line and "nothing is moved" in line
    assert proof.path.read_bytes() == before and archive(svc) == [] and acts_of(seat) == []


@pytest.mark.parametrize("held", ["admission", "lifecycle"])
def test_an_active_door_is_refused(setup, held):
    svc, seat, live = setup
    message, proof = stuck(svc, seat)
    hold = (post.admission(svc.ship.runtime, seat.name) if held == "admission"
            else lifecycle.claim(svc.ship.runtime, seat.name))
    with hold:
        line = aside(svc, seat, proof.path.name)
    assert "door is active" in line and "nothing is moved" in line
    assert proof.path.exists() and archive(svc) == [] and acts_of(seat) == []


def test_a_terminal_journal_holds_nothing_and_is_refused(setup):
    svc, seat, live = setup
    message, proof = stuck(svc, seat)
    proof.save(phase="delivered")
    line = aside(svc, seat, proof.path.name)
    assert "which holds nothing: it is not stuck" in line and proof.path.exists() and archive(svc) == []


def test_a_human_session_and_a_seat_that_is_not_systems_are_refused(setup):
    svc, seat, live = setup
    message, proof = stuck(svc, seat)
    for caller in ("builder", "person", None, "stranger"):
        line = aside(svc, seat, proof.path.name, caller=caller)
        assert line.startswith("refused:") and proof.path.exists(), line
    person = svc.seat("person")
    line = svc.answer(dict(kind="set-aside", seat="person", journal="any.json", caller="watcher"))
    assert "sits in a human's session" in line
    assert archive(svc) == [] and acts_of(seat) == [] and acts_of(person) == []


@pytest.mark.parametrize("name", ["..", ".", "../worker.json", "a/b.json", "/etc/passwd", ".hidden.json",
                                  "journal.txt", "", "linked.json"])
def test_a_name_outside_the_pending_folder_or_a_link_is_refused(setup, name):
    svc, seat, live = setup
    message, proof = stuck(svc, seat)
    (proof.path.parent / "linked.json").symlink_to(proof.path)
    before = proof.path.read_bytes()
    line = aside(svc, seat, name)
    assert line.startswith("refused:") and "nothing is moved" in line, line
    assert proof.path.read_bytes() == before and (proof.path.parent / "linked.json").is_symlink()
    assert archive(svc) == [] and acts_of(seat) == []


def test_a_name_that_is_no_journal_of_the_seat_is_refused(setup):
    svc, seat, live = setup
    line = aside(svc, seat, "missing.json")
    assert "has no pending journal 'missing.json'" in line and archive(svc) == []


def test_a_message_never_typed_is_named_as_one_the_door_may_still_deliver(setup):
    svc, seat, live = setup
    message, proof = stuck(svc, seat)
    [name] = proof.data["attempts"]
    record = post.attempt_path(svc.ship.runtime, name)
    data = json.loads(record.read_text())
    record.write_text(json.dumps(dict(data, result="withheld", detail="synthetic: never typed")))
    line = aside(svc, seat, proof.path.name)
    assert f"message {message.id}, whose attempt reads withheld: it was never typed, so the door may deliver it" in line
