"""The door's next pass after a paste whose journal did not survive.

A power cut can lose the journal after the paste. When the attempt file remains,
the record decides: the paste is delivered and not typed again, or the attempt
is a blind hold. A faithful cut loses every ordinary replacement since the last
law-record write and keeps that write: the attempt opened before typing stays,
the record still holds the paste, and the door does not type again.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

from quarterdeck import durable, post, seat as seatmod, service, ship as shipmod
from quarterdeck.door import Door

from .waiting import wait_for


def ship_with(drydock, extra: str) -> shipmod.Ship:
    file = drydock / "ship.toml"
    file.write_text(file.read_text() + extra)
    return shipmod.load(drydock)


def world(drydock, tmux):
    ship = ship_with(drydock, '\n[seats.captain.settings]\nstep_seconds = "0.05"\n')
    seat = seatmod.Seat(ship, ship.seat("captain"), tmux)
    seat.up()
    wait_for(lambda: Door(seat).reading().open)
    pasted = {"n": 0}
    original = tmux.paste

    def counting(pane, text, *args, **kwargs):
        pasted["n"] += 1
        return original(pane, text, *args, **kwargs)

    tmux.paste = counting
    return service.Service(ship, tmux), seat, pasted


def settle(seat, body: str) -> None:
    wait_for(lambda: seat.record() and body in seat.record().read_text())
    wait_for(lambda: (screen := Door(seat).screen()) is not None and screen.box == "" and not screen.working)


def deliver(svc, seat, body: str) -> post.Message:
    message = post.Store(seat.ship.runtime).new("order", "desk", seat.name, body)
    svc.deliver(seat, [message])
    settle(seat, body)
    return message


def drop_journals(seat) -> None:
    folder = seat.ship.runtime / "post" / "pending" / seat.name
    for path in folder.glob("*.json"):
        path.unlink()


def unfinish(path) -> None:
    data = json.loads(path.read_text())
    data["result"] = None
    data["detail"] = ""
    data["at"] = (post.now() - timedelta(seconds=30)).isoformat()
    path.write_text(json.dumps(data, indent=2) + "\n")


def attempt_of(seat, message):
    found = post.Attempts(seat.ship.runtime).for_message(message.id)
    assert len(found) == 1
    return found[0]


def passes(svc) -> None:
    svc.reconcile()
    svc.looks()
    svc.deliveries()


class Writers:
    """Which writer put each file, in order, with the bytes that were there before."""

    def __init__(self, monkeypatch):
        self.events: list[tuple[str, Path, bytes | None]] = []
        real_replace = durable.replace_file
        real_law = durable._replace_law_record

        def replace(path, text, *, encoding=None):
            target = Path(path)
            prior = target.read_bytes() if target.is_file() else None
            self.events.append(("replace", target, prior))
            return real_replace(path, text, encoding=encoding)

        def law(path, text, *, encoding=None):
            target = Path(path)
            prior = target.read_bytes() if target.is_file() else None
            self.events.append(("law", target, prior))
            return real_law(path, text, encoding=encoding)

        monkeypatch.setattr(durable, "replace_file", replace)
        monkeypatch.setattr(durable, "_replace_law_record", law)

    def cut(self) -> dict[Path, bytes | None]:
        """Lose ordinary replacements since the last law-record write; that write stays."""
        last = max((index for index, event in enumerate(self.events) if event[0] == "law"), default=None)
        assert last is not None, "no law-record write"
        restore: dict[Path, bytes | None] = {}
        for kind, path, prior in self.events[last + 1:]:
            if kind == "replace" and path not in restore:
                restore[path] = prior
        for path, prior in restore.items():
            if prior is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(prior)
        return restore


def test_unjournaled_attempt_is_delivered_from_the_record(drydock, tmux):
    svc, seat, pasted = world(drydock, tmux)
    try:
        message = deliver(svc, seat, "token-record-holds")
        drop_journals(seat)
        unfinish(attempt_of(seat, message).path)
        before = pasted["n"]
        passes(svc)
        judged = attempt_of(seat, message)
        assert pasted["n"] == before
        assert judged.result == "delivered"
        assert judged.detail == "found in the record on reconciliation"
        passes(svc)
        assert pasted["n"] == before
        assert attempt_of(seat, message).result == "delivered"
    finally:
        svc.ledger.close()
        seat.down()


def test_unjournaled_attempt_without_the_paste_is_a_blind_hold(drydock, tmux):
    svc, seat, pasted = world(drydock, tmux)
    try:
        deliver(svc, seat, "warmup-turn")
        saved = seat.record().read_bytes()
        message = deliver(svc, seat, "token-record-lacks")
        seat.record().write_bytes(saved)
        drop_journals(seat)
        unfinish(attempt_of(seat, message).path)
        state = seat.harness.state(seat.record())
        assert state.turn_open is False and state.pending_tools == ()
        assert Door(seat).screen().box == ""
        before = pasted["n"]
        passes(svc)
        judged = attempt_of(seat, message)
        assert pasted["n"] == before
        assert judged.result == "blind"
        assert judged.detail == "the service died mid-delivery and the record does not show it; qd post resend"
        passes(svc)
        assert pasted["n"] == before
        assert attempt_of(seat, message).result == "blind"
    finally:
        svc.ledger.close()
        seat.down()


def test_power_cut_keeps_the_law_attempt_and_does_not_type_again(drydock, tmux, monkeypatch):
    svc, seat, pasted = world(drydock, tmux)
    try:
        writers = Writers(monkeypatch)
        message = deliver(svc, seat, "token-power-cut")
        lost = writers.cut()
        pending = seat.ship.runtime / "post" / "pending" / seat.name
        assert any(path.parent == pending and path.suffix == ".json" for path in lost)
        assert not any(pending.glob("*.json"))
        attempt = attempt_of(seat, message)
        assert json.loads(attempt.path.read_text())["result"] is None
        assert "token-power-cut" in seat.record().read_text()
        # The attempt's time stays as the law writer left it. Close the window so
        # reconciliation reads the record on this pass.
        monkeypatch.setattr(service, "CONFIRM_SECONDS", 0)
        before = pasted["n"]
        passes(svc)
        judged = attempt_of(seat, message)
        assert pasted["n"] == before
        assert judged.result == "delivered"
        assert judged.detail == "found in the record on reconciliation"
        passes(svc)
        assert pasted["n"] == before
        assert attempt_of(seat, message).result == "delivered"
    finally:
        svc.ledger.close()
        seat.down()
