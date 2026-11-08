"""Exact paste evidence, restart-stable deadlines and safe independent progress."""

from pathlib import Path

from dataclasses import replace
from datetime import timedelta
import json
from types import SimpleNamespace

import pytest

from quarterdeck import pending, post, watch
from quarterdeck.door import Door, Delivery
from quarterdeck.harness.codex import CodexHarness
from quarterdeck.harness.base import Screen
from quarterdeck.service import Service
from quarterdeck.ship import Pool, SeatSpec, Session, Ship
from quarterdeck.tmux import Pane


@pytest.fixture
def setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    pool = Pool("test", "codex")
    spec = SeatSpec("worker", "bridge", "worker", "codex", ("test",), cwd=tmp_path)
    ship = Ship(tmp_path, "example", "operator", tmp_path, {"test": pool},
                {"bridge": Session("bridge", "Test")}, {"worker": spec})
    live = {"pane": Pane("%1", "Test", "worker", 123, 80, 24, False, False, None), "box": "", "submitted": 0, "pasted": 0}

    def capture(*a, **k):
        return "• Working (1s • esc to interrupt)\n› " + live["box"].replace("\n", "\n  ") + "\n\n  test low · /work\n"

    def paste(*args):
        live["pasted"] += 1
        live["box"] = args[1].rstrip("\n")

    def keys(*args):
        assert args[1:] == ("Enter",), "no clear, interrupt, or duplicate submit allowed"
        live["submitted"] += 1
    tmux = SimpleNamespace(pane=lambda p: live["pane"], capture=capture, paste=paste, keys=keys)
    svc = Service(ship, tmux)
    seat = svc.seat("worker")
    seat.folder.mkdir(parents=True)
    launch = {"pane": "%1", "pid": 123, "at": (post.now() - timedelta(minutes=1)).isoformat(),
              "pool": "test", "argv": ["codex"], "harness": "codex", "resume": None, "version": "synthetic"}
    (seat.folder / "launch.json").write_text(json.dumps(launch))
    record = seat.home / "sessions" / "synthetic.jsonl"
    record.parent.mkdir(parents=True)
    record.write_text(json.dumps({"type": "session_meta", "timestamp": launch["at"], "payload": {"id": "synthetic"}}) + "\n")
    (seat.folder / "record").write_text(str(record) + "\n")
    from quarterdeck.record_owner import register
    register(seat.folder, record, "codex")
    original = Door.confirm
    monkeypatch.setattr(Door, "confirm", lambda self, text: original(self, text, seconds=0))
    yield svc, seat, live, record
    svc.ledger.close()


def send(svc, seat, body="a precise  payload\nsecond line"):
    message = svc.store.new("order", "desk", seat.name, body)
    svc.deliver(seat, [message])
    return message, pending.active(seat)[-1]


def input_row(record, text, stamp):
    with record.open("a") as stream:
        stream.write(json.dumps({"type": "response_item", "timestamp": stamp.isoformat(),
                                "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}}) + "\n")


def test_owned_pending_restart_original_deadline_and_record_only_confirmation(setup, monkeypatch: pytest.MonkeyPatch) -> None:
    svc, seat, live, record = setup
    message, proof = send(svc, seat)
    assert svc.attempts.latest()[message.id] == "pending"
    assert live["pasted"] == live["submitted"] == 1
    assert Door(seat).reading().waiting and "waiting to be taken" in str(Door(seat).reading())
    assert not svc.attempts.undelivered(svc.store, seat.name)
    start = post.now()
    original_at = proof.data["at"]
    restarted = Service(svc.ship, svc.tmux)
    try:
        for _ in range(3):
            restarted.reconcile()
            restarted.deliveries()
        assert pending.active(seat)[0].data["at"] == original_at
        assert live["pasted"] == live["submitted"] == 1  # law: one-door
        f = watch.facts(svc.ship, svc.tmux, walls=[], login=[])
        assert not f.needs_human and "waiting to be taken" in f.line()
        # A missing box is not confirmation. It still holds the next paste.
        live["box"] = ""
        restarted.reconcile()
        assert svc.attempts.latest()[message.id] == "pending"
        assert "waiting for record confirmation" in str(Door(seat).reading())
        # A stale identical input and a whitespace-edited fresh input prove nothing.
        input_row(record, proof.data["payload"], start - timedelta(days=1))
        input_row(record, proof.data["payload"].replace("precise  ", "precise "), post.now())
        restarted.reconcile()
        assert svc.attempts.latest()[message.id] == "pending"
        deadline = post.datetime.fromisoformat(original_at) + timedelta(seconds=600)
        monkeypatch.setattr(pending.post, "now", lambda: deadline - timedelta(microseconds=1))
        assert Door(seat).reading().waiting
        monkeypatch.setattr(pending.post, "now", lambda: deadline)
        assert Door(seat).reading().clears == "needs a human"
        reason = Door(seat).reading().reason
        assert message.short in reason and "exceeded 600s" in reason
        assert reason.endswith("inspect with qd watch worker") and "qd post resend" not in reason
        assert watch.facts(svc.ship, svc.tmux, walls=[], login=[]).needs_human == ["worker"]
        input_row(record, proof.data["payload"].strip(), deadline)
        restarted.reconcile()
        assert svc.attempts.latest()[message.id] == "delivered"
        assert not pending.active(seat) and Door(seat).reading().open
        assert live["pasted"] == live["submitted"] == 1
    finally:
        restarted.ledger.close()


@pytest.mark.parametrize("change", ["foreign", "edited", "partial", "pane", "pid", "launch"])
def test_pending_ownership_rejects_changed_text_and_identity(setup, change) -> None:
    svc, seat, live, record = setup
    message, proof = send(svc, seat)
    if change == "foreign":
        live["box"] = "a person typing"
    elif change == "edited":
        live["box"] += " edit"
    elif change == "partial":
        live["box"] = live["box"][:-1]
    elif change in ("pane", "pid"):
        live["pane"] = replace(live["pane"], **({"id": "%2"} if change == "pane" else {"pid": 456}))
    else:
        path = seat.folder / "launch.json"
        data = json.loads(path.read_text())
        data["at"] = post.now().isoformat()
        path.write_text(json.dumps(data))
    reading = Door(seat).reading()
    assert not reading.waiting and reading.clears == "needs a human"
    assert message.short in reading.reason and reading.reason.endswith("inspect with qd watch worker")
    assert "qd post resend" not in reading.reason and "qd fresh" not in reading.reason
    assert svc.deliver(seat, [svc.store.new("order", "desk", "worker", "later")])
    assert live["pasted"] == live["submitted"] == 1
    assert proof.data["phase"] == "submitted"


def test_due_failed_item_is_isolated_and_later_item_progresses(setup) -> None:
    svc, seat, live, record = setup
    bad = svc.store.new("order", "desk", "worker", "bad \x1b terminal control")
    svc.deliver(seat, [bad])
    assert svc.attempts.latest()[bad.id] == "withheld" and live["pasted"] == 0
    good = svc.store.new("order", "desk", "worker", "later valid item")
    # Both become due together. The failed item never enters the good payload.
    svc.withheld.clear()
    svc.deliveries()
    proof = pending.active(seat)[0]
    assert good.short in proof.data["payload"] and bad.short not in proof.data["payload"]
    assert live["pasted"] == live["submitted"] == 1
    input_row(record, proof.data["payload"], post.now())
    svc.reconcile()
    live["box"] = ""
    svc.deliveries()
    assert svc.attempts.latest()[good.id] == "delivered"
    assert svc.attempts.latest()[bad.id] == "withheld"
    assert live["pasted"] == live["submitted"] == 1
    assert watch.facts(svc.ship, svc.tmux, walls=[], login=[]).waiting == 1


def test_uncertain_paste_is_not_cleared_or_retried(setup, monkeypatch: pytest.MonkeyPatch) -> None:
    svc, seat, live, record = setup
    monkeypatch.setattr(Door, "box_shows", lambda *a: Screen("layout unknown", "part"))
    message, proof = send(svc, seat)
    assert svc.attempts.latest()[message.id] == "blind"
    assert live["pasted"] == 1 and live["submitted"] == 0
    Service.reconcile(svc)
    svc.deliveries()
    assert Door(seat).reading().clears == "needs a human"
    assert live["pasted"] == 1 and live["submitted"] == 0


def test_exact_text_without_our_transport_journal_is_still_foreign(setup) -> None:
    svc, seat, live, record = setup
    message = svc.store.new("order", "desk", "worker", "same words")
    live["box"] = post.typed([message]).rstrip("\n")
    assert Door(seat).reading().clears == "needs a human"
    svc.deliveries()
    assert live["pasted"] == live["submitted"] == 0  # law: one-door
    assert not pending.active(seat)


@pytest.mark.parametrize("phase", ["prepared", "pasting", "submitting", "submitted"])
def test_crash_at_each_durable_boundary_never_repeats_paste_or_submit(setup, phase) -> None:
    svc, seat, live, record = setup
    message = svc.store.new("order", "desk", "worker", "crash boundary")
    launch = seat.launched()
    attempt = svc.attempts.open(message, seat.name, launch.pane, launch.pid, launch.at)
    proof = pending.Proof.begin(seat, attempt, post.typed([message]))
    proof.save(phase=phase, shown=post.typed([message]).rstrip("\n"))
    live["box"] = proof.data["shown"]
    other = Service(svc.ship, svc.tmux)
    try:
        other.reconcile()
        other.deliveries()
        assert svc.attempts.latest()[message.id] == ("pending" if phase == "submitted" else "blind")
        assert not svc.attempts.undelivered(svc.store, seat.name)
        assert live["pasted"] == live["submitted"] == 0
        if phase == "submitted":
            assert Door(seat).reading().waiting
        else:
            assert Door(seat).reading().clears == "needs a human"
        # Late exact record evidence still resolves a blind attempt.
        input_row(record, proof.data["payload"], post.now())
        other.reconcile()
        assert svc.attempts.latest()[message.id] == "delivered"
    finally:
        other.ledger.close()


def test_unsafe_foreign_door_holds_every_item_without_attempt_or_clear(setup) -> None:
    svc, seat, live, record = setup
    a = svc.store.new("order", "desk", "worker", "earlier")
    b = svc.store.new("order", "desk", "worker", "later")
    live["box"] = "unknown draft"
    svc.deliver(seat, [a, b])
    assert live["box"] == "unknown draft" and live["pasted"] == live["submitted"] == 0
    assert not svc.attempts.all()
    assert len(svc.attempts.undelivered(svc.store, seat.name)) == 2


def test_original_first_attempt_time_survives_prior_withheld_and_restart(setup) -> None:
    svc, seat, live, record = setup
    msg = svc.store.new("order", "desk", "worker", "later usable")
    old = svc.attempts.open(msg, seat.name, "%1", 123, seat.launched().at)
    original = (post.now() - timedelta(seconds=500)).isoformat()
    svc.attempts.close(replace(old, at=original), "withheld", "temporary pre-paste refusal")
    svc.deliver(seat, [msg])
    assert pending.active(seat)[0].data["at"] == original


def test_record_error_retains_pending_uncertainty(setup) -> None:
    svc, seat, live, record = setup
    msg, proof = send(svc, seat)
    record.write_bytes(b"\xff SQLite mismatch")
    svc.reconcile()
    assert svc.attempts.latest()[msg.id] == "pending"
    record.unlink()
    svc.reconcile()
    assert svc.attempts.latest()[msg.id] == "pending"
    assert live["pasted"] == live["submitted"] == 1


def test_confirmation_verdict_survives_crash_before_attempt_close(setup) -> None:
    svc, seat, live, record = setup
    msg = svc.store.new("order", "desk", "worker", "already recorded")
    a = svc.attempts.open(msg, seat.name, "%1", 123, seat.launched().at)
    proof = pending.Proof.begin(seat, a, post.typed([msg]))
    proof.save(phase="delivered")  # crash after durable verdict, before attempts.close
    record.unlink()  # persisted confirmation is sufficient even if source later disappears
    svc.reconcile()
    assert svc.attempts.latest()[msg.id] == "delivered"
    assert live["pasted"] == live["submitted"] == 0


def test_unreadable_pending_journal_holds_without_touching_box(setup) -> None:
    svc, seat, live, record = setup
    _, proof = send(svc, seat)
    proof.path.write_text("{ broken")
    reading = Door(seat).reading()
    assert reading.clears == "needs a human" and "journal unreadable" in reading.reason
    assert reading.reason.endswith("inspect with qd watch worker")
    svc.deliveries()
    assert live["pasted"] == live["submitted"] == 1


@pytest.mark.parametrize("filename", [None, "receipt.json", "receipt-1.json", "receipt-other-1.json", "ord-invalid-abcd-1.json"])
def test_unrecognized_attempt_names_hold_the_door_without_inventing_message_ids(setup, filename) -> None:
    svc, seat, live, record = setup
    message, proof = send(svc, seat)
    attempt = svc.attempts.for_message(message.id)[0]
    original = attempt.path.read_bytes()
    path = attempt.path
    if filename is not None:
        path = attempt.path.with_name(filename)
        attempt.path.rename(path)
        proof.save(attempts=[filename])
    # Only the name changed: the actual attempt and journal still read and validate.
    assert svc.attempts.all() == [replace(attempt, path=path)]
    pending.validate(proof.data, seat.name)
    assert pending.active(seat)[0].data == proof.data
    live["box"] = "edited"
    reading = Door(seat).reading()
    assert not reading.open and not reading.waiting and reading.clears == "needs a human"
    assert reading.reason.endswith("inspect with qd watch worker")
    assert "qd post resend" not in reading.reason
    if filename is None:
        assert message.short in reading.reason and "message ID unknown" not in reading.reason
    else:
        assert f"attempt {filename!r}" in reading.reason and "message ID unknown" in reading.reason
        assert message.short not in reading.reason
    svc.reconcile()
    svc.deliveries()
    assert path.read_bytes() == original and proof.path.is_file()
    assert svc.attempts.latest()[message.id] == "pending"
    assert live["pasted"] == live["submitted"] == 1  # law: one-door


def test_explicit_resend_requires_empty_box_and_never_claims_prior_delivery(setup) -> None:
    svc, seat, live, record = setup
    message, proof = send(svc, seat)
    svc.deliver(seat, [message], resend=True)
    assert live["pasted"] == live["submitted"] == 1
    live["box"] = "foreign edit"
    svc.deliver(seat, [message], resend=True)
    assert live["pasted"] == live["submitted"] == 1
    live["box"] = ""
    svc.deliver(seat, [message], resend=True)
    assert live["pasted"] == live["submitted"] == 2
    assert [a.result for a in svc.attempts.for_message(message.id)] == ["blind", "pending"]
    svc.deliveries()
    assert live["pasted"] == live["submitted"] == 2


def test_new_launch_empty_door_progresses_but_old_uncertainty_remains_visible(setup) -> None:
    svc, seat, live, record = setup
    old, proof = send(svc, seat)
    data = json.loads((seat.folder / "launch.json").read_text())
    data["at"] = post.now().isoformat()
    (seat.folder / "launch.json").write_text(json.dumps(data))
    live["box"] = ""
    assert Door(seat).reading().open
    facts = watch.facts(svc.ship, svc.tmux, walls=[], login=[])
    assert any("prior launch" in reason for reason in facts.pending)
    assert "pending" in facts.line() and "details: qd status" in facts.line()
    assert old.short in pending.detached(seat)[0]
    assert pending.detached(seat)[0].endswith("inspect with qd watch worker")
    assert svc.attempts.latest()[old.id] == "pending"
    later = svc.store.new("order", "desk", "worker", "new launch order")
    svc.deliver(seat, [later])
    assert live["pasted"] == live["submitted"] == 2
    assert svc.attempts.latest()[old.id] == "pending"
    # Lifecycle has a distinct receipt for explicitly carried context. It never
    # changes the earlier unconfirmed attempt into a delivered one.
    carried = svc.attempts.open(old, seat.name, "%1", 123, data["at"])
    svc.attempts.close(carried, "launch", "carried as the first message of the launch")
    svc.reconcile()
    assert svc.attempts.for_message(old.id)[0].result == "pending"
    assert svc.attempts.latest()[old.id] == "launch"
    assert json.loads(proof.path.read_text())["phase"] == "carried"
    assert not pending.detached(seat)


def test_padded_capture_never_confirms_a_visually_hidden_record_difference(setup, monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.contract.test_padded_box import capture

    svc, seat, live, record = setup
    monkeypatch.setattr(svc.tmux, "capture", lambda *a, **k: capture("codex", live["box"], "full"))
    assert Door(seat).reading().open
    message, proof = send(svc, seat, "first  line\nsecond line")
    assert svc.attempts.latest()[message.id] == "pending"
    assert proof.data["shown"].endswith(" ")
    assert live["pasted"] == live["submitted"] == 1
    # The extra spaces occur before a hard newline, not at the trimmed outer
    # message boundary; they must not become an exact record receipt.
    input_row(record, proof.data["payload"].replace("first  line\n", "first  line  \n"), post.now())
    svc.reconcile()
    svc.deliveries()
    assert svc.attempts.latest()[message.id] == "pending"
    assert live["pasted"] == live["submitted"] == 1  # law: one-door
    input_row(record, proof.data["payload"], post.now())
    svc.reconcile()
    assert svc.attempts.latest()[message.id] == "delivered"
    assert live["pasted"] == live["submitted"] == 1


@pytest.mark.parametrize("count", [1, 2])
def test_saved_journal_lists_remain_readable_by_the_preceding_code_and_reconcile_once(setup, count):
    import subprocess
    from types import ModuleType

    svc, seat, live, record = setup
    messages = [svc.store.new("order", "desk", seat.name, f"historical transport item {i}") for i in range(count)]
    launch = seat.launched()
    attempts = [svc.attempts.open(m, seat.name, launch.pane, launch.pid, launch.at) for m in messages]
    payload = post.typed(messages)
    proof = pending.Proof.begin(seat, attempts[0], payload)
    # Multi-attempt lists are an existing disk format, even though new transports take one message.
    proof.save(attempts=[a.path.name for a in attempts], phase="submitted", shown=payload)
    source = subprocess.check_output(
        ["git", "show", "8d89929a9fa828d0052bb0a0f0829a0da001a58e:src/quarterdeck/pending.py"],
        cwd=Path(__file__).parents[2], text=True)
    previous = ModuleType("quarterdeck.previous_pending")
    previous.__package__ = "quarterdeck"
    exec(compile(source, "previous_pending.py", "exec"), previous.__dict__)
    found, errors = previous.journals(seat)
    assert not errors and len(found) == 1 and found[0].data == proof.data
    assert found[0].confirmed(seat) is None
    svc.reconcile()
    assert [a.result for a in svc.attempts.all()] == ["pending"] * count
    input_row(record, payload, post.now())
    assert found[0].confirmed(seat) == "delivered"
    restarted = Service(svc.ship, svc.tmux)
    try:
        for _ in range(2):
            restarted.reconcile()
            restarted.deliveries()
        assert [a.result for a in svc.attempts.all()] == ["delivered"] * count
        assert len(svc.attempts.all()) == count
        assert json.loads(proof.path.read_text())["phase"] == "delivered"
        assert live["pasted"] == live["submitted"] == 0
    finally:
        restarted.ledger.close()
