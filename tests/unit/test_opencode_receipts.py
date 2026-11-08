"""Root queue acceptance stays distinct from consumption through reconciliation and restart."""

from datetime import timedelta
import json
from types import SimpleNamespace

import pytest

from quarterdeck import input_receipt, pending, post, record_owner
from quarterdeck.harness import opencode
from quarterdeck.harness.base import Input
from quarterdeck.seat import Launched, Seat
from quarterdeck.service import Service
from quarterdeck.ship import Pool, SeatSpec, Session, Ship
from quarterdeck.tmux import Pane
from .test_opencode_record import AT, MS, database, session, user, assistant, part


def no_keys(*args, **kwargs):
    pytest.fail("reconciliation must never paste or submit")


@pytest.mark.parametrize("journaled", [False, True])
def test_queued_root_input_survives_reconciliation_and_restart_until_consumed(tmp_path, monkeypatch, journaled):
    pool = Pool("local", "synthetic-endpoint", endpoint="http://synthetic.invalid/v1")
    spec = SeatSpec("worker", "crew", "worker", "opencode", ("local",), cwd=tmp_path)
    ship = Ship(tmp_path / "ship", "synthetic", "operator", tmp_path, {"local": pool},
                {"crew": Session("crew", "Synthetic")}, {"worker": spec})
    tmux = SimpleNamespace(pane=lambda _: Pane("%1", "Synthetic", "worker", 123, 80, 24, False, False, None),
                           keys=no_keys, paste=no_keys)
    seat = Seat(ship, spec, tmux)
    record = seat.home / "data-001" / "opencode" / "opencode.db"
    con = database(record)
    launch = Launched("%1", 123, AT.isoformat(), "local", "root", ["opencode"], "synthetic", harness="opencode")
    launch.write(seat.folder / "launch.json")
    (seat.folder / "record").write_text(str(record))
    record_owner.register(seat.folder, record, "opencode")
    clock = [AT + timedelta(milliseconds=30)]
    monkeypatch.setattr(post, "now", lambda: clock[0])
    svc = Service(ship, tmux)
    message = svc.store.new("answer", "ship", seat.name, "exact  queued payload\nsecond line")
    payload = post.typed([message])
    session(con)
    user(con, "first", "root", 10, payload)  # identical older input must not confirm this attempt
    assistant(con, "first-step", "first", "root", 20, finish="tool-calls")
    part(con, "task", "first-step", "root", 21, {"type": "tool", "tool": "task",
         "state": {"status": "running", "time": {"start": MS+21}}})
    con.commit()
    def attempt_on(service):
        attempt = service.attempts.open(message, seat.name, "%1", 123, launch.at)
        if journaled:
            proof = pending.Proof.begin(seat, [attempt], payload)
            proof.save(phase="submitted", shown=payload)
            proof.finish("queued")
        service.attempts.close(attempt, "queued")
        return attempt
    attempt = attempt_on(svc)
    user(con, "queued", "root", 40, payload)
    # A later answered child containing the same payload is not a root receipt.
    session(con, "child", 45, "root")
    user(con, "child-input", "child", 50, payload)
    assistant(con, "child-answer", "child-input", "child", 60)
    con.commit()
    try:
        def still_queued(service):
            state = seat.harness.state(record)
            assert state.queued == (payload,)
            assert input_receipt.confirmed(seat.harness, record, payload, attempt.receipt_since) == "queued"
            for _ in range(2):
                service.reconcile()
                assert service.attempts.latest()[message.id] == "queued"
                assert seat._queued_unread(service.store, service.attempts) == [message]
            assert len(service.attempts.all()) == 1
        still_queued(svc)
        assert [(t.index, t.tokens.total) for t in seat.harness.turns(record)] == [(2, 5)], "child usage stays accounted"
        svc.ledger.close()
        svc = Service(ship, tmux)
        still_queued(svc)
        # Completion of the old turn is not a receipt for the queued input.
        con.execute("UPDATE part SET data=? WHERE id='task'", (json.dumps({"type": "tool", "tool": "task",
                    "state": {"status": "completed", "time": {"start": MS+21, "end": MS+65}}}),))
        assistant(con, "first-final", "first", "root", 70)
        con.commit()
        still_queued(svc)
        # A step addressed to this exact root message proves consumption, before the turn ends.
        assistant(con, "queued-step", "queued", "root", 80, completed=False, finish="tool-calls")
        con.commit()
        state = seat.harness.state(record)
        assert state.queued == () and state.turn_open and state.last_input == payload
        assert seat.harness.inputs(record)[1] == Input(payload, AT+timedelta(milliseconds=40), "consumed", AT+timedelta(milliseconds=80))
        for _ in range(2):
            svc.reconcile()
            assert svc.attempts.latest()[message.id] == "delivered"
            assert seat._queued_unread(svc.store, svc.attempts) == []
        assert len(svc.attempts.all()) == 1
        assert input_receipt.confirmed(seat.harness, record, payload, AT+timedelta(milliseconds=45)) is None
        # A new identical queued input cannot borrow the older consumption receipt.
        clock[0] = AT + timedelta(milliseconds=85)
        later = attempt_on(svc)
        user(con, "queued-again", "root", 90, payload)
        con.commit()
        svc.reconcile()
        assert input_receipt.confirmed(seat.harness, record, payload, later.receipt_since) == "queued"
        assert svc.attempts.latest()[message.id] == "queued"
        assert seat._queued_unread(svc.store, svc.attempts) == [message]
        assert len(svc.attempts.all()) == 2
    finally:
        con.close()
        svc.ledger.close()
