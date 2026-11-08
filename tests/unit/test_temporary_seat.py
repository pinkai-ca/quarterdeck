"""Temporary check and doctor work owns only its transport, orders, home and session."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from quarterdeck import check, doctor, pending, post, seat as seatmod
from quarterdeck.drill import Check
from quarterdeck.harness.fake import FakeHarness
from quarterdeck.service import Service
from quarterdeck.ship import SeatSpec, Session, ShipError
from .test_check_lifetime import synthetic_ship
from .test_removed_delivery import launch


class Sessions:
    def __init__(self):
        self.names = {"pre-existing": "$0"}
        self.killed = []

    def has_session(self, name):
        return name in self.names

    def new_session(self, name, **kwargs):
        assert name not in self.names
        self.names[name] = "$1"
        return "$1"

    def kill_session_id(self, ident):
        self.killed.append(ident)
        self.names = {name: value for name, value in self.names.items() if value != ident}

    def status_line(self, *args):
        pass

    def pane(self, ident):
        return SimpleNamespace(id=ident, pid=111, dead=False)


@pytest.mark.parametrize("caller", ["check", "doctor"])
@pytest.mark.parametrize("outcome", ["success", "failed", "body", "down", "kill", "report", "remove", "retire"])
def test_both_callers_retire_transport_and_finish_cleanup_after_exceptions(tmp_path, monkeypatch, caller, outcome):
    ship, tmux = synthetic_ship(tmp_path), Sessions()
    svc = Service(ship, tmux)
    state = {}
    monkeypatch.setattr(FakeHarness, "version", lambda self: "synthetic")
    monkeypatch.setattr(doctor.shutil, "which", lambda *args, **kwargs: "synthetic-program")
    real_new = post.Store.new
    real_abandon = pending.abandon
    real_remove = seatmod.shutil.rmtree

    def body(seat, orders):
        state["seat"] = seat
        assert seat.spec.cwd == (ship.workspace if caller == "check" else ship.folder)
        assert seat.spec.persona == (None if caller == "check" else "McCoy")
        assert seat.spec.pools == ("fake",) and seat.spec.harness == "fake"
        with pending.transient_lock(ship.runtime, seat.name) as available:
            assert not available
        launch(seat)
        if caller == "check":
            orders.append(svc.store.new("order", "ship", seat.name, "owned task"))
        own = orders[0]
        state["own"] = own
        foreign = svc.store.new("order", "desk", seat.name, "not opened by this operation")
        state["foreign"] = foreign
        attempt = svc.attempts.open(own, seat.name, "%1", 111, seat.launched().at)
        proof = pending.Proof.begin(seat, attempt, post.typed([own]))
        proof.save(phase="submitted", shown=proof.data["payload"])
        state.update(attempt=attempt, proof=proof)
        svc.reconcile()
        assert svc.attempts.latest()[own.id] is None
        if outcome == "success":
            proof.finish("delivered")
            svc.attempts.close(attempt, "delivered", "synthetic receipt")
            svc.store.new("report", seat.name, own.sender, "done", re=own.id)
        if outcome == "body":
            raise RuntimeError("body")
        return [Check("synthetic task", outcome == "success")]

    def probe(checked, seat, orders):
        return body(seat, orders)

    def up(seat, **kwargs):
        orders = [m for m in svc.store.for_seat(seat.name) if m.subject == "qd doctor"]
        assert len(orders) == 1 and orders[0].short in seat.first_message()[0]
        body(seat, orders)
        return "synthetic up"

    def down(seat):
        with pending.transient_lock(ship.runtime, seat.name) as available:
            assert not available
        expected = "delivered" if outcome == "success" else (None if outcome == "retire" else "abandoned")
        assert svc.attempts.latest()[state["own"].id] == expected
        if outcome == "down":
            raise RuntimeError("down")

    def kill(ident):
        Sessions.kill_session_id(tmux, ident)
        if outcome == "kill":
            raise RuntimeError("kill")

    def new(store, kind, *args, **kwargs):
        if kind == "report" and outcome == "report":
            raise RuntimeError("report")
        return real_new(store, kind, *args, **kwargs)

    def remove(path):
        if outcome == "remove":
            raise RuntimeError("remove")
        return real_remove(path)

    def abandon(*args):
        if outcome == "retire":
            raise RuntimeError("retire")
        return real_abandon(*args)

    monkeypatch.setattr(check, "probe", probe)
    monkeypatch.setattr(seatmod.Seat, "up", up)
    monkeypatch.setattr(seatmod.Seat, "down", down)
    monkeypatch.setattr(tmux, "kill_session_id", kill)
    monkeypatch.setattr(post.Store, "new", new)
    monkeypatch.setattr(seatmod.shutil, "rmtree", remove)
    monkeypatch.setattr(pending, "abandon", abandon)
    monkeypatch.setattr(doctor, "wait_for", lambda predicate, seconds: predicate())
    try:
        def run():
            return check.check(ship, "fake", tmux) if caller == "check" else doctor.fix(ship, [Check("synthetic", False)], tmux)
        if outcome in ("success", "failed"):
            result = run()
            assert all(item.passed for item in result) == (outcome == "success")
        else:
            with pytest.raises(RuntimeError, match=outcome):
                run()
        seat = state["seat"]
        assert tmux.names == {"pre-existing": "$0"} and tmux.killed == ["$1"]
        assert seat.folder.exists() == (outcome in ("remove", "retire"))
        assert svc.store.is_open(state["foreign"]), "unrelated orders are never closed by teardown"
        assert svc.store.is_open(state["own"]) == (outcome == "report")
        assert state["proof"].path.exists() and len(svc.attempts.all()) == 1
        with pending.transient_lock(ship.runtime, seat.name) as available:
            assert available
        monkeypatch.setattr(pending, "abandon", real_abandon)
        svc.reconcile()
        assert svc.attempts.latest()[state["own"].id] == ("delivered" if outcome == "success" else "abandoned")
    finally:
        svc.ledger.close()


@pytest.mark.parametrize("existing", ["seat", "session", "tmux", "folder", "message", "attempt"])
def test_temporary_names_never_claim_existing_resources(tmp_path, existing):
    ship, tmux = synthetic_ship(tmp_path), Sessions()
    spec = SeatSpec("temporary", "temporary-session", "worker", "fake", ("fake",))
    folder = ship.runtime / "seats" / spec.name
    if existing == "seat":
        ship.seats[spec.name] = spec
    elif existing == "session":
        ship.sessions[spec.session] = Session(spec.session, "unrelated")
    elif existing == "tmux":
        tmux.names[spec.session] = "$2"
    elif existing == "folder":
        folder.mkdir(parents=True)
        (folder / "marker").write_text("keep")
    else:
        msg = post.Store(ship.runtime).new("order", "desk", spec.name, "keep")
        if existing == "attempt":
            post.Attempts(ship.runtime).open(msg, spec.name, "%2", 222, None)
    before = dict(tmux.names)
    with pytest.raises(ShipError, match="already exists"):
        with seatmod.temporary_seat(ship, tmux, spec, [], "synthetic check"):
            pytest.fail("a pre-existing resource was claimed")
    assert tmux.names == before and not tmux.killed
    if existing == "folder":
        assert (folder / "marker").read_text() == "keep"
